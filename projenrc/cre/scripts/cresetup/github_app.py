#!/usr/bin/env python3
"""The builder's GitHub App, `aws-cre-automation-<alias>`: made once, checked on every run.

  github_app.py [--alias <amazon alias>] [--key-dir <dir>] [--wait <seconds>]

One App per builder, used by projen's workflows in each of their repos (dependency upgrades,
self-mutation, approving and merging PRs). Its private key lives at <key-dir>/<slug>.pem (default
~/.cre, mode 600), never in a repo; github.py setup copies it into each repo's secrets.

The App exists: it must be the builder's (owner = their gh login), the key must be one of its keys
(GitHub accepts a JWT it signed), and it must have Contents, Pull requests and Workflows write.

It doesn't exist: GitHub's App manifest flow, one browser click by GitHub's design (no API makes an
App). A page on 127.0.0.1 posts the manifest to GitHub, the builder clicks "Create GitHub App",
GitHub sends the browser back with a one-time code, and setup exchanges it for the App's ID and
key. The page opens in the browser and setup waits for the click (--wait, default 600 s; 0 opens
nothing, prints the page's address and stops with 10). The manifest: public (installable on organizations; useless without
the key), no webhook, those three permissions, and the Setup URL github.py serves while it waits
for an install (127.0.0.1:47391, "Redirect on update").

Exit: 0 ready; 10 refused (someone else's App by that name, no or wrong key, missing permissions,
not created yet); 11 gh isn't signed in; 20 a call failed. Last stdout line `RESULT code=<n>`;
before it APP_SLUG, APP_ID, APP_KEY_FILE.
"""
import argparse
import getpass
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import midway  # noqa: E402
from github import NEEDED, SETUP_PORT, open_browser  # noqa: E402
from github_api import API, WEB, App, GitHub, classify, split_response  # noqa: E402
from result import FAILED, OK, REFUSED, Failed, must  # noqa: E402

PREFIX = "aws-cre-automation-"
CREATE_WAIT = 600   # seconds for the builder's Create click


def manifest(slug, login, port):
    return {"name": slug, "url": f"{WEB}/{login}", "redirect_url": f"http://127.0.0.1:{port}/callback",
            "setup_url": f"http://127.0.0.1:{SETUP_PORT}/installed", "setup_on_update": True, "public": True,
            "hook_attributes": {"url": f"{WEB}/{login}", "active": False},
            "default_permissions": {p: "write" for p in NEEDED}, "default_events": []}


def exchange(code):
    """The App GitHub made for a manifest code: {id, slug, owner, pem}. The pem stays in memory."""
    # The code is in the URL, and whoever exchanges it first gets the App's key: the URL reaches curl
    # in a config on a pipe, never its argv.
    url = f"{API}/app-manifests/{urllib.parse.quote(code, safe='')}/conversions"
    rc, out, err, _ = midway.curl(["-sS", "-i", "-X", "POST", "-H", "Accept: application/vnd.github+json", "-K", "<<cfg>>"],
                                  feeds={"cfg": f'url = "{url}"\n'})
    if rc != 0:
        raise Failed(FAILED, f"Exchanging the App's code: {midway.last_line(err, rc)}")
    status, data, headers = split_response(out)
    r = classify(status, data, "Exchanging the App's code", headers)
    if r.status == 404:
        raise Failed(REFUSED, f"{r.message} (a code is single-use and lasts an hour): re-run to make the App again.")
    data = must(r, "Exchanging the App's code")
    if not all(k in data for k in ("id", "slug", "pem")):
        raise Failed(FAILED, f"GitHub's code exchange returned no App ({str(data)[:200]})")
    return data


class ManifestPage:
    """127.0.0.1: `/` posts the manifest to GitHub; `/callback` takes GitHub's code (the state must match)."""

    def __init__(self, slug, login):
        self.state, self.code, self.done = secrets.token_urlsafe(16), None, threading.Event()
        page = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, body):
                b = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):   # noqa: N802
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/":
                    m = html.escape(json.dumps(manifest(slug, login, page.port)))
                    self.reply(f"""<!doctype html><title>Create {html.escape(slug)}</title>
<form id="f" method="post" action="{WEB}/settings/apps/new?state={page.state}"><input type="hidden" name="manifest" value="{m}">
<p>Opening GitHub to create the App <b>{html.escape(slug)}</b>...</p><button>Continue</button></form>
<script>document.getElementById('f').submit()</script>""")
                elif u.path == "/callback" and q.get("state", [""])[0] == page.state and q.get("code"):
                    page.code = q["code"][0]
                    self.reply("<p>Got it: setup carries on by itself. You can close this tab.</p>")
                    page.done.set()
                else:
                    self.reply("<p>Not this run's page: close this tab and re-run setup.</p>")

            def log_message(self, *a):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def write_key(path, pem):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pem)


def create(slug, login, key_dir, wait, say, browser=open_browser, page_factory=ManifestPage, exchange_code=exchange):
    """(slug, id, key file) of the App made. GitHub names it what the builder left in the name field."""
    page = page_factory(slug, login)
    try:
        wait = wait if wait is not None else CREATE_WAIT
        opened = wait != 0 and browser(page.url)
        say(f"⚠  ACTION NEEDED in the browser{' (opened)' if opened else ''}: {page.url} goes to GitHub; check the name "
            f"{slug} and click Create GitHub App." + (f" Waiting up to {wait}s." if wait else ""))
        if not wait or not page.done.wait(wait):
            raise Failed(REFUSED, f"GitHub App {slug} isn't created yet: open {page.url} and click Create GitHub App, with "
                                  "this run waiting (--wait 600).")
    finally:
        page.close()
    app = exchange_code(page.code)
    key = os.path.join(key_dir, f"{slug}.pem")   # under the name setup looks for, whatever GitHub named it
    write_key(key, app["pem"])
    if app["slug"] != slug:
        # The builder edited the name: a re-run, finding no App by the expected name, would make another.
        raise Failed(REFUSED, f"GitHub made the App as {app['slug']}, not {slug}. Rename it to {slug} at "
                              f"{WEB}/settings/apps/{app['slug']} (its key is already at {key}), then re-run.")
    say(f"✓  created GitHub App {app['slug']} (ID {app['id']}); its key is {key}")
    return app["slug"], str(app["id"]), key


def check(gh, slug, login, key, found, make_app=App):
    """An existing App: the builder's, with this key, and the permissions projen's workflows use."""
    owner = (found.get("owner") or {}).get("login", "")
    if owner.lower() != login.lower():
        raise Failed(REFUSED, f"GitHub App {slug} belongs to {owner}, not you ({login}): pick another --alias.")
    settings = f"{WEB}/settings/apps/{slug}"
    if not os.path.isfile(key):
        raise Failed(REFUSED, f"GitHub App {slug} exists, but its key isn't at {key}. Make one: {settings} → Private keys → "
                              f"Generate a private key, then save the download as {key} (chmod 600 {key}).")
    lacks = [p for p in NEEDED if (found.get("permissions") or {}).get(p) != "write"]
    if lacks:
        raise Failed(REFUSED, f"GitHub App {slug} lacks write on {', '.join(lacks)}: set them at {settings}/permissions, "
                              "then accept them on each installation.")
    me = make_app(slug, found["id"], key).call("GET", "/app")
    if not me.ok:
        raise Failed(REFUSED if me.kind in ("refused", "missing") else FAILED, me.message)
    if str(me.data.get("id")) != str(found["id"]):
        raise Failed(REFUSED, f"{key} signs for App ID {me.data.get('id')}, not {slug} ({found['id']}): use {slug}'s key.")
    return str(found["id"])


def app(a, gh, say, make_app=App, **create_kw):
    """create_kw: create()'s browser, page_factory and exchange_code (tests)."""
    slug = PREFIX + a.alias
    key_dir = os.path.abspath(os.path.expanduser(a.key_dir))
    key = os.path.join(key_dir, f"{slug}.pem")
    login = must(gh.call("GET", "/user"), "Reading your GitHub login")["login"]
    found = gh.call("GET", f"/apps/{slug}")
    if found.ok:
        app_id = check(gh, slug, login, key, found.data, make_app)
        say(f"ℹ  GitHub App {slug} (ID {app_id}) is yours, with its key and permissions")
    elif found.kind == "missing":
        slug, app_id, key = create(slug, login, key_dir, a.wait, say, **create_kw)
    else:
        must(found, f"Reading GitHub App {slug}")
    return {"APP_SLUG": slug, "APP_ID": app_id, "APP_KEY_FILE": key}


def parse(argv):
    ap = argparse.ArgumentParser(prog="github_app.py")
    ap.add_argument("--alias", default=getpass.getuser(), help="your Amazon alias (default: whoami)")
    ap.add_argument("--key-dir", default=os.environ.get("CRE_KEY_DIR") or "~/.cre", help="where the App's key lives")
    ap.add_argument("--wait", type=int, help="seconds to wait for the Create click (default 600; 0: no browser, no wait)")
    a = ap.parse_args(argv)
    # GitHub caps an App's name at 34 characters: the prefix's 19 leave 15 for the alias.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,14}", a.alias):
        ap.error(f"--alias {a.alias!r}: up to 15 lowercase letters, digits and hyphens")
    return a


def main(argv=None, github_factory=GitHub):
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
        outputs = app(a, github_factory(), say)
    except Failed as e:
        say(f"❌ {e}")
        return done(e.code)
    except Exception as e:  # noqa: BLE001: a bug is still 20, never a bare traceback
        say(f"❌ github_app.py failed: {type(e).__name__}: {e}")
        return done(FAILED)
    for k, v in outputs.items():
        print(f"{k}={v}")
    say("✅ done")
    return done(OK)


if __name__ == "__main__":
    sys.exit(main())
