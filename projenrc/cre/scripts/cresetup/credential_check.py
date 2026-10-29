#!/usr/bin/env python3
"""Every credential a setup run needs, checked before it starts: the first step of the chain.

  credential_check.py --provider gitlab --profile <aws profile> [--dns-profile <aws profile>] --token-file <file>
  credential_check.py --provider github --profile <aws profile> [--dns-profile <aws profile>]

AWS: each profile answers `sts get-caller-identity`. GitLab: the Midway session (its cookie has more
than MIN_MIDWAY minutes left, so a run with an MR wait can't lapse halfway), the token (active, the
api scope, not expiring today) and ssh to gitlab.aws.dev. GitHub: the gh CLI is signed in with the
repo and workflow scopes (git pushes through gh, so that is all). All are checked, in parallel, and every problem is named at once with how to refresh
it, so one refresh and one re-run is enough.

Exit: 0 all valid; 11 one or more credentials to refresh; 20 a check couldn't run (no network, a
missing tool). Last stdout line `RESULT code=<n>`; before it ACCOUNT_ID (and DNS_ACCOUNT_ID).
"""
import argparse
import concurrent.futures
import datetime
import os
import re
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import midway  # noqa: E402
import proc  # noqa: E402
from aws import Aws  # noqa: E402
from gitlab_api import HOST, MWINIT, GitLab, read_token  # noqa: E402
from result import CREDS, FAILED, OK, REFUSED  # noqa: E402

MIN_MIDWAY = 60            # minutes of Midway session a run may need (its longest wait is a 30-minute MR)
SSH_TIMEOUT = 20
MIDWAY_HOST = "midway-auth.amazon.com"


class CredentialProblem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---- each check returns ({output: value}, a line to show) or raises CredentialProblem ---------------------------------

def check_aws_profile(profile, key):
    def check():
        r = Aws(profile).call("sts", "get-caller-identity")
        if r.kind == "creds":
            raise CredentialProblem(CREDS, f"AWS profile {profile}: {r.message}. Refresh its credentials (isengard <account> --role "
                                 f"Admin --store {profile}, or aws sso login --profile {profile}).")
        if not r.ok:
            raise CredentialProblem(FAILED, f"AWS profile {profile}: {r.message}")
        return {key: r.data["Account"]}, f"AWS profile {profile}: {r.data['Arn']}"
    return check


def check_midway_session(now=None):
    """The Midway `session` cookie's minutes left, from ~/.midway/cookie (Netscape format)."""
    text = midway.read_cookie()
    expiry = None
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) >= 7 and f[0].removeprefix("#HttpOnly_").lstrip(".") == MIDWAY_HOST and f[5] == "session":
            expiry = int(f[4])
    if expiry is None:
        raise CredentialProblem(CREDS, f"No Midway session ({midway.cookie_path()}). Run: {MWINIT}")
    left = (expiry - (now or time.time())) / 60
    if left < MIN_MIDWAY:
        raise CredentialProblem(CREDS, f"The Midway session ends in {max(left, 0):.0f} min, under the {MIN_MIDWAY} a run may need. "
                             f"Run: {MWINIT}")
    return {}, f"Midway: {left / 60:.1f} h left"


def check_gitlab_token(token_file, today=None):
    def check():
        token, problem = read_token(token_file)
        if problem:
            raise CredentialProblem(CREDS, problem)
        r = GitLab(token).call("GET", "/personal_access_tokens/self")
        if r.kind == "creds":
            raise CredentialProblem(CREDS, f"GitLab token ({token_file}): {r.message}")
        if not r.ok:
            raise CredentialProblem(FAILED, f"GitLab token ({token_file}): {r.message}")
        t = r.data
        if not t.get("active", True) or t.get("revoked") or "api" not in t.get("scopes", []):
            raise CredentialProblem(CREDS, f"The GitLab token in {token_file} is revoked, expired or lacks the api scope: make one "
                                 f"with the api scope at https://{HOST}/-/user_settings/personal_access_tokens.")
        exp = t.get("expires_at")
        if exp and datetime.date.fromisoformat(exp) <= (today or datetime.date.today()):
            raise CredentialProblem(CREDS, f"The GitLab token in {token_file} expires {exp}: make a new one.")
        return {}, f"GitLab token: {t.get('name', '?')}, expires {exp or 'never'}"
    return check


def check_ssh(host, ok_text, how):
    def check():
        r = proc.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-T", host], timeout=SSH_TIMEOUT)
        text = (r.out + r.err).strip()
        if ok_text in text:
            return {}, f"ssh {host}: ok"
        if "Permission denied" in text or "Host key verification failed" in text:
            raise CredentialProblem(CREDS, f"ssh {host} refused ({text.splitlines()[-1]}). {how}")
        raise CredentialProblem(FAILED, f"ssh {host}: {text.splitlines()[-1] if text else f'exit {r.code}'}")
    return check


GH_SCOPES = ("repo", "workflow")   # create and push the repo, and its .github/workflows


def check_gh_login():
    """gh signed in to github.com with the scopes setup uses. git pushes through gh too (its
    credential helper), so this is GitHub's only credential."""
    r = proc.run(["gh", "api", "-i", "user"], timeout=30)
    if r.ok:
        head, _, body = r.out.partition("\r\n\r\n") if "\r\n\r\n" in r.out else r.out.partition("\n\n")
        scopes = next((ln.split(":", 1)[1] for ln in head.splitlines() if ln.lower().startswith("x-oauth-scopes:")), None)
        have = {x.strip() for x in scopes.split(",")} if scopes is not None else None
        missing = [x for x in GH_SCOPES if have is not None and x not in have]
        if missing:
            raise CredentialProblem(CREDS, f"gh's token lacks the {', '.join(missing)} scope(s). Run: gh auth refresh -s {','.join(missing)}")
        login = re.search(r'"login"\s*:\s*"([^"]+)"', body)
        return {}, f"gh: signed in as {login.group(1) if login else '?'}"
    text = (r.err or r.out).strip()
    if r.code == 127:
        raise CredentialProblem(FAILED, "The gh CLI isn't installed (https://cli.github.com).")
    if "auth login" in text or "401" in text or "Bad credentials" in text:
        raise CredentialProblem(CREDS, f"gh isn't signed in to github.com ({text.splitlines()[-1]}). Run: gh auth login")
    raise CredentialProblem(FAILED, f"gh api user: {text.splitlines()[-1] if text else f'exit {r.code}'}")


def credential_checks(a):
    out = [check_aws_profile(a.profile, "ACCOUNT_ID")]
    if a.dns_profile:
        out.append(check_aws_profile(a.dns_profile, "DNS_ACCOUNT_ID"))
    if a.provider == "gitlab":
        out += [check_midway_session, check_gitlab_token(a.token_file),
                check_ssh(f"git@ssh.{HOST}", "Welcome to GitLab", f"Run: {MWINIT}")]
    else:
        out.append(check_gh_login)
    return out


def check_credentials(a, say):
    """{output: value} when every check passed; else raises Problem with every failure named."""
    outputs, problems = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for f in [pool.submit(c) for c in credential_checks(a)]:
            try:
                values, line = f.result()
                outputs.update(values)
                say(f"✓  {line}")
            except CredentialProblem as p:
                problems.append(p)
                say(f"❌ {p}")
    if problems:
        code = CREDS if any(p.code == CREDS for p in problems) else FAILED
        raise CredentialProblem(code, f"{len(problems)} credential check(s) failed: refresh as above, then re-run setup.")
    return outputs


def parse(argv):
    ap = argparse.ArgumentParser(prog="credential_check.py")
    ap.add_argument("--provider", required=True, choices=("gitlab", "github"))
    ap.add_argument("--profile", required=True, help="the app account's AWS profile")
    ap.add_argument("--dns-profile", help="the DNS account's AWS profile, when the zones are there")
    ap.add_argument("--token-file", help="GitLab: the token file")
    a = ap.parse_args(argv)
    if a.provider == "gitlab" and not a.token_file:
        ap.error("--provider gitlab needs --token-file")
    if a.provider == "github" and a.token_file:
        ap.error("--token-file is GitLab's")
    return a


def main(argv=None):
    t0 = time.time()

    def say(msg):
        print(f"[{time.strftime('%H:%M:%S')} +{time.time() - t0:.1f}s] {msg}", file=sys.stderr, flush=True)

    def done(code):
        print(f"RESULT code={code}")
        return code

    try:
        a = parse(argv)
    except SystemExit as e:
        if e.code in (0, None):
            raise
        return done(REFUSED)
    try:
        outputs = check_credentials(a, say)
    except CredentialProblem as p:
        say(f"❌ {p}")
        return done(p.code)
    except Exception as e:  # noqa: BLE001: a bug is still 20, never a bare traceback
        say(f"❌ credential_check.py failed: {type(e).__name__}: {e}")
        return done(FAILED)
    for k, v in outputs.items():
        print(f"{k}={v}")
    say("✅ every credential is valid")
    return done(OK)


if __name__ == "__main__":
    sys.exit(main())
