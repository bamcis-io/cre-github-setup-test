"""GitHub's REST API, as the builder (their `gh` login) and as their GitHub App.

  gh = GitHub()                          # the gh CLI's login; nothing else is needed
  r = gh.call("GET", "/repos/o/r")       # -> Reply(kind, status, data, message)
  r = every(gh, "/repos/o/r/actions/variables", key="variables")
  gh.set_secret("o/r", "NAME", value)    # encrypted by gh; the value goes on stdin, never argv
  app = App(slug, app_id, key_file)      # calls signed as the App (a JWT from its private key)

kind: ok (2xx); missing (404); refused (403, 422: the login or the repo's plan may not do this, so a
re-run can't help); creds (401: gh's login is gone or expired); failed (anything else, gh or curl
failing included). Same kinds as gitlab_api.
"""
import base64
import dataclasses
import json
import os
import time

import midway
import proc

API = os.environ.get("CRE_GITHUB_API", "https://api.github.com")
WEB = os.environ.get("CRE_GITHUB_WEB", "https://github.com")
PAGE = 100
CALL_TIMEOUT = 60
LOGIN = "gh auth login -s repo,workflow"


@dataclasses.dataclass(frozen=True)
class Reply:
    kind: str           # ok | missing | refused | creds | failed
    status: int = 0
    data: object = None
    message: str = ""

    @property
    def ok(self):
        return self.kind == "ok"


def classify(status, data, what, headers=None):
    if 200 <= status < 300:
        return Reply("ok", status, data)
    said = data.get("message", data) if isinstance(data, dict) else data
    errors = data.get("errors") if isinstance(data, dict) else None
    message = f"{what}: {status} {said}" + (f" {errors}" if errors else "")
    if status == 404:
        return Reply("missing", 404, data, message)
    if status == 401:
        return Reply("creds", 401, data, message + f" (gh's login expired or was revoked: {LOGIN})")
    if status == 403 and "x-github-sso" in (headers or {}):
        # An organization's SAML single sign-on hasn't authorized gh's token: a credential step.
        return Reply("creds", 403, data, message + " (authorize gh's token for the organization's SAML sign-on: "
                                                   "gh auth refresh -h github.com)")
    if status == 403 and "rate limit" in str(said).lower():
        return Reply("failed", 403, data, message + " (GitHub's rate limit: a re-run later continues)")
    if status in (403, 422):
        return Reply("refused", status, data, message)
    return Reply("failed", status, data, message)


def split_response(text):
    """(status, data, headers) from `gh api --include` or `curl -i` output: the last status line,
    its headers (names lowercased), a blank line, the body."""
    status, rest, headers = 0, text.replace("\r\n", "\n"), {}
    while rest.startswith("HTTP/"):
        head, _, rest = rest.partition("\n\n")
        first, _, fields = head.partition("\n")
        parts = first.split()
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in fields.splitlines()) if k.strip()}
    try:
        data = json.loads(rest) if rest.strip() else None
    except ValueError:
        data = rest.strip()[:300]
    return status, data, headers


class GitHub:
    """Calls as the builder, through `gh api` (gh holds the login; no token passes through here)."""

    def __init__(self, runner=proc.run):
        self._run = runner

    def call(self, method, path, body=None):
        cmd = ["gh", "api", "--include", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        if body is not None:
            cmd += ["--input", "-"]
        r = self._run(cmd, CALL_TIMEOUT, input=json.dumps(body) if body is not None else None)
        status, data, headers = split_response(r.out)
        if not status:
            text = (r.err or r.out).strip()
            if "auth login" in text:
                return Reply("creds", 0, message=f"{method} {path}: gh isn't signed in. Run: {LOGIN}")
            return Reply("failed", 0, message=f"{method} {path}: {text.splitlines()[-1] if text else f'gh exited {r.code}'}")
        return classify(status, data, f"{method} {path}", headers)

    def set_secret(self, repo, name, value, env=None):
        """An Actions secret (the repo's, or an environment's): gh encrypts it with the repo's key; the
        value reaches gh on stdin."""
        r = self._run(["gh", "secret", "set", name, "-R", repo, *(["--env", env] if env else [])], CALL_TIMEOUT, input=value)
        if r.ok:
            return Reply("ok")
        text = (r.err or r.out).strip()
        kind = "creds" if "auth login" in text or "HTTP 401" in text else "refused" if "HTTP 403" in text else "failed"
        return Reply(kind, message=f"Setting secret {name} on {repo}: {text.splitlines()[-1] if text else r.code}")


def every(gh, path, key=None):
    """Every page of a GET list, as one Reply. `key`: the list's name in an object answer
    ({"total_count": n, "variables": [...]})."""
    items, page, sep = [], 1, "&" if "?" in path else "?"
    while True:
        r = gh.call("GET", f"{path}{sep}per_page={PAGE}&page={page}")
        if not r.ok:
            return r
        got = (r.data or {}).get(key, []) if key else (r.data or [])
        items += got
        if len(got) < PAGE:
            return Reply("ok", r.status, items)
        page += 1


class App:
    """Calls to GitHub as the App itself: a JWT signed with its private key (openssl, RS256). The
    JWT goes to curl through a pipe, never argv."""

    def __init__(self, slug, app_id, key_file):
        self.slug, self.id, self.key_file = slug, str(app_id), key_file

    def jwt(self):
        """GitHub accepts at most 10 minutes: issued 60 s in the past, valid 5 minutes, so a clock
        a minute off either way works."""
        def b64(b):
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        now = int(time.time())
        head = b64(b'{"alg":"RS256","typ":"JWT"}')
        body = b64(json.dumps({"iat": now - 60, "exp": now + 300, "iss": self.id}, separators=(",", ":")).encode())
        r = proc.run_bytes(["openssl", "dgst", "-sha256", "-sign", self.key_file], CALL_TIMEOUT, f"{head}.{body}".encode())
        if not r.ok or not r.out:
            return ""
        return f"{head}.{body}.{b64(r.out)}"

    def call(self, method, path, body=None, token=None):
        """As the App (its JWT), or as one of its installations (`token`). Token and body go to curl
        through pipes."""
        auth = token or self.jwt()
        if not auth:
            return Reply("refused", message=f"{self.key_file} can't sign: it isn't an RSA private key. Use the .pem GitHub "
                                            f"generated for {self.slug}.")
        feeds = {"hdr": f"Authorization: Bearer {auth}\n"}
        args = ["-sS", "-i", "-X", method, "-H", "@<<hdr>>", "-H", "Accept: application/vnd.github+json"]
        if body is not None:
            feeds["body"] = json.dumps(body)
            args += ["-H", "Content-Type: application/json", "--data-binary", "@<<body>>"]
        code, out, err, _ = midway.curl(args + [f"{API}{path}"], feeds=feeds)
        if code != 0:
            return Reply("failed", 0, message=f"{method} {path} as {self.slug}: {midway.last_line(err, code)}")
        status, data, headers = split_response(out)
        r = classify(status, data, f"{method} {path} as {self.slug}", headers)
        if r.status == 401:   # as the App, a 401 is its key, not the builder's login
            return Reply("refused", 401, r.data, f"GitHub refused a token signed with {self.key_file} for {self.slug} (ID {self.id}): "
                                                 f"the key isn't this App's, or this machine's clock is off. Make a new key at "
                                                 f"{WEB}/settings/apps/{self.slug} → Private keys, saved as {self.key_file}.")
        return r

    def installation_token(self, installation_id):
        """An hour-long token for one installation: to act as the App (its approval), never as the builder."""
        r = self.call("POST", f"/app/installations/{installation_id}/access_tokens")
        return Reply(r.kind, r.status, (r.data or {}).get("token"), r.message) if r.ok else r
