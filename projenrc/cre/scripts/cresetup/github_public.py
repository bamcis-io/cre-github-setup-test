#!/usr/bin/env python3
"""A CRE project's private GitHub repo made public, end to end: `npx projen setup:github-public`.

  github_public.py --repo <owner>/<name> --project <name> --app-slug <slug> [--partition aws]
                   --profile <p> [--dns-profile <p>] [--allow-production <account>]... [--dry-run]

The task passes --repo, --project, --app-slug and --partition from .projenrc.ts. Run in the project,
on main with nothing uncommitted: the switch is committed on its own.

1. Credentials: AWS (and the DNS account's) and gh, before anything changes (credential_check.py).
2. The repo made public (github.py publish): refused (10) when any commit on any branch, tag or PR
   head holds something shaped like a credential. Then the `AWS` environment (deploys from main
   only), GitHub auto-merge, main protected. --dry-run stops before this, after the scan, and
   changes nothing.
3. The trust widened to the environment's subject as well as main's (account.py --github-environment
   AWS --github-also-trust-main; with --dns-profile the DNS account's stack too), so main's old
   workflow, which declares no environment, keeps deploying while the switch merges.
4. The deploy roles' ARNs set as secrets of the environment (github.py variables --environment AWS);
   their repo-level copies are deleted. Any other repo-level variable or secret is named, with what
   to do: one the workflows read is moved with setup:repo (a secret's value can't be read back, so
   it isn't moved here); one nothing reads is a leftover to delete.
5. `github.visibility: 'public'` in .projenrc.ts, the regenerated workflows (the deploy jobs declare
   the environment) pushed on cre/github-public-<time> through the git tasks' push, and the PR merged by
   the App once its checks pass (github.py merge). Then main is checked out and caught up.
6. Once no deploy run of main but the switch's own is queued or running (each runs the old
   workflow), the trust narrowed to the environment's subject alone.

Re-run after any exit: steps 1-4 change only what differs (the trust stays wide until step 6 has
recorded its narrowing). Step 5 records its branch in
.git/cre-github-public.json before the push and the pushed sha after it: a re-run on that branch
carries the push on (push.py stopped after committing), and one with the sha merges that commit
instead of pushing again.

Exit: 0 done; 10 refused (nothing changed by this step: a credential in the history, not on main,
uncommitted changes, a PR a person must look at); 11 credentials; 20 a call failed (a re-run
continues). Last stdout line `RESULT code=<n>`; before it PR_URL and MERGED_SHA.
"""
import argparse
import json
import os
import re
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import account  # noqa: E402
import credential_check  # noqa: E402
import github  # noqa: E402
import proc  # noqa: E402
from aws import Aws  # noqa: E402
from github_api import GitHub, every  # noqa: E402
from gitlocal import git  # noqa: E402
from result import CREDS, FAILED, OK, REFUSED, Failed, must  # noqa: E402
from setup_args import SingleValue  # noqa: E402

BRANCH = "cre/github-public"
TITLE = "ci: deploy from the AWS environment now the repo is public"
STATE = "cre-github-public.json"
PUSH = os.path.join(os.path.dirname(HERE), "git", "push.py")
PUSH_TIMEOUT = 3600   # push builds first
DEPLOY_WAIT, DEPLOY_POLL = 3600, 15   # main's earlier deploys, before the trust narrows
# What setup itself puts in the repo: the App's (every projen workflow uses them) stay repo-level.
APP_SECRETS = ("PROJEN_APP_ID", "PROJEN_APP_PRIVATE_KEY")
ROLE_SECRETS = {"RUNNER_ROLE_ARN": "AWS_CREDS_TARGET_ROLE", "CFN_EXECUTION_ROLE_ARN": "CFN_EXECUTION_ROLE_ARN",
                "DNS_ROLE_ARN": "DNS_ROLE_ARN", "DNS_CFN_EXECUTION_ROLE_ARN": "DNS_CFN_EXECUTION_ROLE_ARN"}
VISIBILITY = re.compile(r"(github:\s*\{[^}]*?)visibility:\s*(['\"])(private|public|internal)\2")
GITHUB_OPTIONS = re.compile(r"github:\s*\{")


def state_path(d):
    return os.path.join(d, ".git", STATE)


def load(d):
    try:
        with open(state_path(d)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(d, state):
    tmp = state_path(d) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_path(d))


def made_public(text):
    """.projenrc.ts with github.visibility 'public' (None when there is no `github: {` to put it in)."""
    if VISIBILITY.search(text):
        return VISIBILITY.sub(lambda m: f"{m.group(1)}visibility: {m.group(2)}public{m.group(2)}", text, count=1)
    if GITHUB_OPTIONS.search(text):
        return GITHUB_OPTIONS.sub("github: { visibility: 'public',", text, count=1)
    return None


def preflight(d, state):
    """Before anything changes. A recorded push carries on from wherever the clone is."""
    rc = os.path.join(d, ".projenrc.ts")
    if not os.path.isfile(rc):
        raise Failed(REFUSED, f"{d} has no .projenrc.ts: run it in the project (npx projen setup:github-public).")
    if state.get("sha"):
        return
    with open(rc) as f:
        if made_public(f.read()) is None:
            raise Failed(REFUSED, ".projenrc.ts has no `github: { ... }` options to set visibility in.")
    branch = git(d, "branch", "--show-current").out.strip()
    dirty = git(d, "status", "--porcelain").out.splitlines()
    if branch == state.get("branch") and not dirty:
        return   # an earlier push stopped after it committed on its branch (push.py's 40): it resumes there
    if branch != "main" or dirty:
        raise Failed(REFUSED, "The switch is committed on its own, so start on main with nothing uncommitted (now: "
                              f"{branch or 'a detached HEAD'}{'; changes in ' + ', '.join(x[3:] for x in dirty[:5]) if dirty else ''}).")


def trust(a, gh, say, also_main):
    """The runner role (and the DNS account's role) trusting the environment's subject, and with
    also_main main's too: while the switch merges, main's old workflow must still deploy."""
    acct = account.parse(["--project", a.project, "--provider", "github", "--partition", a.partition,
                          "--github-repo", a.repo, "--github-environment", github.DEPLOY_ENVIRONMENT, "--profile", a.profile]
                         + (["--github-also-trust-main"] if also_main else [])
                         + (["--dns-profile", a.dns_profile] if a.dns_profile else [])
                         + [x for acct_id in a.allow_production for x in ("--allow-production", acct_id)])
    acct.github_subject = github.oidc_subject(gh, a.repo)
    return account.setup(acct, Aws(a.profile), say, dns_aws=Aws(a.dns_profile) if a.dns_profile else None)


def read_by_workflows(d):
    """The names the generated workflows read (`vars.X`, `secrets.X`)."""
    names, wf = set(), os.path.join(d, ".github", "workflows")
    for f in sorted(os.listdir(wf)) if os.path.isdir(wf) else []:
        with open(os.path.join(wf, f)) as fh:
            names |= set(re.findall(r"\b(?:vars|secrets)\.([A-Za-z_][A-Za-z0-9_]*)", fh.read()))
    return names


def environment_secrets(a, gh, roles, say, d):
    v = github.parse(["variables", "--repo", a.repo, "--environment", github.DEPLOY_ENVIRONMENT])
    v.secret = [(ROLE_SECRETS[k], roles[k]) for k in ROLE_SECRETS if roles.get(k)]
    github.variables(v, gh, say)
    ours = {name for name, _ in v.secret} | set(APP_SECRETS)
    read = read_by_workflows(d)
    for kind in ("variables", "secrets"):
        for x in must(every(gh, f"/repos/{a.repo}/actions/{kind}", key=kind), f"Listing the repo's {kind}"):
            name = x["name"]
            if name in ours:
                continue
            if name in read:
                flag = "--secret" if kind == "secrets" else "--var"
                say(f"⚠  repo {kind[:-1]} {name} is still repo-level, so every workflow can read it, not only the AWS "
                    f"environment's jobs. Move it: npx projen setup:repo {flag} {name}=<value> (a secret's value can't be "
                    "read back, so give it again).")
            else:
                say(f"⚠  repo {kind[:-1]} {name}: no workflow reads it (a leftover). Delete it: "
                    f"gh api -X DELETE repos/{a.repo}/actions/{kind}/{name}")


def earlier_deploys_done(gh, repo, merged, say, sleep=time.sleep, clock=time.monotonic, wait=DEPLOY_WAIT):
    """Wait until no deploy run of main but the switch's own is queued, waiting or running: each of
    those runs main's old workflow, which declares no environment, so it needs main's subject."""
    deadline = clock() + wait
    while True:
        runs = must(every(gh, f"/repos/{repo}/actions/workflows/deploy.yml/runs?branch=main&per_page=100", key="workflow_runs"),
                    "Listing main's deploy runs")
        busy = [r for r in runs if r.get("status") != "completed" and r.get("head_sha") != merged]
        if not busy:
            return
        if clock() >= deadline:
            raise Failed(FAILED, f"main's deploy of {', '.join(r['head_sha'][:9] for r in busy)} is still running after {wait}s; "
                                 "the trust stays wide (main's subject too) until it ends. Re-run once it has.")
        say(f"…  waiting for main's deploy of {', '.join(r['head_sha'][:9] for r in busy)} (main's old workflow) to finish")
        sleep(DEPLOY_POLL)


def push_switch(d, branch, say):
    """push.py on `branch`: from main it makes the branch; on it (an earlier push stopped after it
    committed) it carries on."""
    rc = os.path.join(d, ".projenrc.ts")
    with open(rc) as f:
        text = f.read()
    with open(rc, "w") as f:
        f.write(made_public(text))
    say(f"→  github.visibility: 'public' in .projenrc.ts; push it on {branch} (the push builds first: a few minutes)")
    on_it = git(d, "branch", "--show-current").out.strip() == branch
    # Nothing else was uncommitted (preflight): "." is this change and what projen regenerates from it.
    r = proc.run([sys.executable, PUSH, "--message", TITLE, *([] if on_it else ["--branch", branch]), "--title", TITLE,
                  "--files", "."], PUSH_TIMEOUT, cwd=d)
    sys.stderr.write(r.err[-4000:])
    last = (r.out.strip().splitlines() or [""])[-1]
    if not r.ok:
        code = {10: REFUSED, 11: CREDS, 30: REFUSED}.get(r.code, FAILED)
        raise Failed(code, f"Pushing the switch failed ({last}). Fix what it says, then re-run.")
    m = re.search(r"\bsha=([0-9a-f]{40})\b", last)
    branch = re.search(r"\bbranch=(\S+)", last)
    if not m or not branch:
        raise Failed(FAILED, f"The push's RESULT line has no branch and sha: {last}")
    return branch.group(1), m.group(1)


def run(a, say, gh=None, out=lambda k, v: None, sleep=time.sleep, clock=time.monotonic):
    d = os.path.abspath(a.dir)
    gh = gh or GitHub()
    state = load(d)
    preflight(d, state)
    credential_check.check_credentials(argparse.Namespace(provider="github", profile=a.profile, dns_profile=a.dns_profile,
                                                          token_file=None), say)
    if a.dry_run:
        hits = github.credentials_in_history(d, a.repo)
        if hits:
            raise Failed(REFUSED, "Commits that look like they hold a credential would become readable by anyone:\n"
                         + "\n".join("     " + h for h in hits[:20]))
        say(f"ℹ  (dry run) the history scan found no known credential format. A real run makes {a.repo} public: its code, "
            "history, issues, PRs and workflow logs become readable by anyone (secrets stay secret), then moves the deploy "
            "trust and secrets to the AWS environment and merges the regenerated workflows. Nothing changed.")
        return {}
    github.publish(github.parse(["publish", "--repo", a.repo, "--dir", d]), gh, say)
    # Wide (main's subject too) until the narrowing below has run: never narrowed before the switch merged.
    roles = trust(a, gh, say, also_main=not state.get("narrowed"))
    environment_secrets(a, gh, roles, say, d)
    if not state.get("sha"):
        # Recorded before the push: a push that stops after committing leaves the clone on it.
        state = {"branch": state.get("branch") or f"{BRANCH}-{time.strftime('%Y%m%d-%H%M%S')}"}
        save(d, state)
        branch, sha = push_switch(d, state["branch"], say)
        state.update(branch=branch, sha=sha, pushed=time.strftime("%Y-%m-%dT%H:%M:%S"))
        save(d, state)
    if not state.get("merged"):
        m = github.parse(["merge", "--repo", a.repo, "--app-slug", a.app_slug, "--branch", state["branch"],
                          "--sha", state["sha"], "--title", TITLE])
        state["merged"] = github.merge(m, gh, say, out=out).get("MERGED_SHA", "")
        save(d, state)
        git(d, "switch", "-q", "main", what="Switching back to main")
        git(d, "pull", "-q", "--ff-only", "origin", "main", what="Catching main up with the merge")
    if not state.get("narrowed"):
        earlier_deploys_done(gh, a.repo, state["merged"], say, sleep=sleep, clock=clock)
        say("→  narrow the trust to the AWS environment's jobs (main's old workflow has finished)")
        trust(a, gh, say, also_main=False)
        state["narrowed"] = True
        save(d, state)
    say(f"✓  {a.repo} is public; its deploys run in the AWS environment, from main only")
    return {"MERGED_SHA": state["merged"]}


def parse(argv):
    ap = argparse.ArgumentParser(prog="github_public.py")
    ap.add_argument("--repo", required=True, action=SingleValue, help="<owner>/<name>")
    ap.add_argument("--project", required=True, action=SingleValue)
    ap.add_argument("--app-slug", required=True, action=SingleValue, help="the builder's GitHub App")
    ap.add_argument("--partition", default="aws", action=SingleValue)
    ap.add_argument("--profile", required=True, action=SingleValue, help="the deploy account's AWS profile")
    ap.add_argument("--dns-profile", default="", action=SingleValue,
                    help="the hosted zone account's profile, when the project has one: its trust moves too")
    ap.add_argument("--allow-production", action="append", default=[], metavar="ACCOUNT")
    ap.add_argument("--dir", default=".", action=SingleValue, help="the project's clone (default: here)")
    ap.add_argument("--dry-run", action="store_true", help="scan the history and say what becomes readable; change nothing")
    return ap.parse_args(argv)


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
        outputs = run(a, say, out=lambda k, v: print(f"{k}={v}", flush=True))
    except (Failed, credential_check.CredentialProblem) as e:
        say(f"❌ {e}")
        return done(e.code)
    except Exception as e:  # noqa: BLE001: a bug is still 20, never a bare traceback
        say(f"❌ github_public.py failed: {type(e).__name__}: {e}")
        return done(FAILED)
    for k, v in outputs.items():
        print(f"{k}={v}")
    say("✅ done")
    return done(OK)


if __name__ == "__main__":
    sys.exit(main())
