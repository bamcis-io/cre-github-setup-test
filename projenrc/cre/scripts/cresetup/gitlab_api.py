"""The gitlab.aws.dev REST API and its Management Portal, behind Midway, with a token from a file.

  gl = GitLab(token_file)              # one Midway session for the whole run
  r = gl.call("GET", "/projects/...")  # -> Reply(kind, status, data, message)
  r = every(gl, "/projects/1/variables")   # every page of a list, as one Reply

kind: ok (2xx); missing (404); refused (403: the token's owner may not do this, so a re-run can't
help); creds (Midway lapsed, or the token is refused or lacks the api scope); failed (anything
else, curl failing included). A sign-in page instead of JSON is the Midway session lapsing, never
"not found".
"""
import dataclasses
import json
import os
from urllib.parse import quote

import midway

HOST = os.environ.get("CRE_GITLAB_HOST", "gitlab.aws.dev")
API = f"https://{HOST}/api/v4"
PORTAL = "https://manage.gitlab.aws.dev"
SSH = f"git@ssh.{HOST}"
MWINIT = "mwinit -f -k ~/.ssh/id_ecdsa.pub"
PAGE = 100           # GitLab's largest page


@dataclasses.dataclass(frozen=True)
class Reply:
    kind: str           # ok | missing | refused | creds | failed
    status: int = 0
    data: object = None
    message: str = ""

    @property
    def ok(self):
        return self.kind == "ok"


def enc(s):
    return quote(str(s), safe="")


def read_token(path):
    """(token, problem): the file's one word, or why it can't be used."""
    p = os.path.expanduser(path)
    try:
        with open(p) as f:
            words = f.read().split()
    except OSError as e:
        return "", f"the GitLab token file {path} can't be read ({e.strerror})"
    if len(words) != 1:
        return "", f"the GitLab token file {path} must hold exactly one token"
    return words[0], ""


def primed(session, host, where):
    """None when the session reached `host`, else the Reply that says why it didn't."""
    if not midway.read_cookie():
        return Reply("creds", message=f"no Midway session ({midway.cookie_path()}). Run: {MWINIT}")
    final, error = session.prime()
    if error:
        return Reply("failed", message=f"{where} can't be reached: {error}")
    if not final.startswith(host):
        return Reply("creds", message=f"Midway didn't let the request through to {where}. Run: {MWINIT}")
    return None


class GitLab:
    def __init__(self, token, timeout=midway.TIMEOUT):
        self.token = token
        self.session = midway.Session(f"{API}/version", timeout=timeout)
        self.ready = False

    def call(self, method, path, body=None):
        if not self.ready:
            stop = primed(self.session, f"https://{HOST}", HOST)
            if stop:
                return stop
            self.ready = True
        headers = [f"Authorization: Bearer {self.token}"] + (["Content-Type: application/json"] if body is not None else [])
        r = self.session.request(method, f"{API}{path}", headers=headers, body=None if body is None else json.dumps(body))
        return classify(r, f"{method} {path}")

    def portal(self, path, body):
        """A POST to the Management Portal (gitlab.aws.dev turns off GitLab's own project creation).
        Its own Midway session; no redirect is followed (one would be a sign-in)."""
        s = midway.Session(f"{PORTAL}/api/public/gitlab/groups", timeout=self.session.timeout)
        stop = primed(s, PORTAL, "the GitLab portal")
        if stop:
            return stop
        r = s.request("POST", f"{PORTAL}{path}", headers=["Content-Type: application/json"], body=json.dumps(body))
        return classify(r, f"POST {PORTAL}{path}")


def every(gl, path):
    """Every page of a GET list, as one Reply."""
    items, page, sep = [], 1, "&" if "?" in path else "?"
    while True:
        r = gl.call("GET", f"{path}{sep}per_page={PAGE}&page={page}")
        if not r.ok:
            return r
        items += r.data or []
        if len(r.data or []) < PAGE:
            return Reply("ok", r.status, items)
        page += 1


def classify(r, what):
    if r.status == 0:
        return Reply("failed", 0, message=f"{what}: {r.error}")
    if 300 <= r.status < 400 or (r.url and not r.url.startswith(("https://" + HOST, PORTAL))):
        # Only a GET follows redirects: a write that meets one is the sign-in redirect.
        return Reply("creds", r.status, message=f"Midway sent {what} to sign-in: the session lapsed. Run: {MWINIT}")
    try:
        data = json.loads(r.text) if r.text.strip() else None
    except ValueError:
        if 200 <= r.status < 300:
            return Reply("creds", r.status, message=f"{what} got a page, not JSON: the Midway session lapsed. Run: {MWINIT}")
        data = r.text[:300]
    if 200 <= r.status < 300:
        return Reply("ok", r.status, data)
    said = data.get("message") or data.get("error_description") or data.get("error") or data if isinstance(data, dict) else data
    message = f"{what}: {r.status} {said}"
    if r.status == 404:
        return Reply("missing", 404, data, message)
    if r.status == 401 or (r.status == 403 and isinstance(data, dict) and data.get("error") == "insufficient_scope"):
        return Reply("creds", r.status, data, message + " (the GitLab token is expired, revoked or lacks the api scope)")
    if r.status == 403:
        return Reply("refused", 403, data, message + " (GitLab refuses it for your role on this project; these settings need Maintainer)")
    return Reply("failed", r.status, data, message)
