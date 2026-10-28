#!/usr/bin/env python3
"""Create or adopt the GitHub repository for a CRE demo, using the builder's saved `gh` login.

  repo_github.py prepare  --repo <owner>/<name> [--secret KEY=VALUE]... [--var KEY=VALUE]...
                          [--app-slug <slug> [--app-id <id>] [--app-key-file <key.pem>]]
                          [--wait-install <seconds>]
  repo_github.py finalize --repo <owner>/<name> --dir <demo-dir> [--message <msg>]
  repo_github.py publish  [--dry-run]   (npx projen setup:github-public) a private repo made public

In a project (npx projen setup:repo), --repo and --app-slug default to cre-setup.json and --dir to
its directory.

prepare: checks `gh auth status` (exit 11 → `gh auth login`), adopts the repo if it exists or
  creates it private (an existing public or internal repo is adopted with a warning), and sets the
  Actions secrets (values piped to gh on stdin, never on its command line). With --app-slug: the
  builder's GitHub App (github_app.py creates it as aws-cre-automation-<alias>; its ID is looked up
  from the slug, its key defaults to ~/.cre/<slug>.pem), projen's only credential (gotcha 3). Its
  key is checked with GitHub (a JWT `GET /app`), PROJEN_APP_ID and PROJEN_APP_PRIVATE_KEY are
  stored, then its installation is checked:
    - installed on the repo with Contents, Pull requests and Workflows write: done;
    - not installed: installing is a browser step (GitHub has no API a CLI login may use; adding
      the repo to an existing installation is tried once and is refused in practice). GitHub's
      page opens when there is a GUI browser; after Save, GitHub redirects to the App's Setup URL,
      a page served here that says it's done once the install is confirmed as the App. The page
      opens when there is a GUI browser, its URL is printed as APP_INSTALL_URL, and the run ends 13
      with NEEDS=APP_INSTALL. --wait-install N first re-checks (as the App) every 10 s for up to N
      seconds (default: 600 when the page opened in a browser on this machine, 0 when it didn't).
    - installed with fewer permissions: NEEDS=APP_PERMISSIONS (accept them on the install page).
finalize: sets origin, commits and pushes main as the first commit, into an empty repo only (one with
  any commit is adopted by new_project.py: adopt.py, through a PR; finalize refuses it), then enables auto-merge and squash merges,
  deletes branches on merge, lets Actions approve PRs, and protects main (the `build` check and one
  approval). A step it can't do prints NEEDS=<what> and the manual fix (exit 13): no rights to
  protect main (BRANCH_PROTECTION; a free plan's private repo, which GitHub can't protect, is only
  reported); no App secrets yet (PROJEN_CREDENTIALS).
"""
import json
import os
import re
import sys
import time

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402

NAME = "repo-github"
PROTECTION = {"required_status_checks": {"strict": False, "contexts": ["build"]}, "enforce_admins": True,
              "required_pull_request_reviews": {"required_approving_review_count": 1}, "restrictions": None,
              "allow_force_pushes": False, "allow_deletions": False}


def parse(step, argv):
    p = c.Parser(step, "repo_github.py")
    p.add_argument("mode", choices=["prepare", "finalize", "publish"],
                   help="prepare: create or adopt the repo, set secrets and the App; finalize: push main and protect it; publish: make the repo public")
    p.add_argument("--repo", default="", help="<owner>/<name> (default: cre-setup.json)")
    c.repo_flags(p, "repo")
    p.add_argument("--secret", action="append", default=[],
                   help="prepare: an Actions secret KEY=VALUE (repeatable); VALUE @<file> reads a file; piped to gh on stdin")
    # Plain values (bucket prefix, domain, bindle) as repository variables: a secret is masked in
    # every log line, which hid the bucket names (review N10).
    p.add_argument("--var", action="append", default=[],
                   help="prepare: an Actions repository variable KEY=VALUE (repeatable), for values that needn't be masked")
    p.add_argument("--app-slug", dest="app_slug", default="",
                   help="prepare: the builder's GitHub App slug, stored as projen's credential (default: cre-setup.json)")
    p.add_argument("--app-id", dest="app_id", default="", help="prepare: the App's ID (default: looked up from --app-slug)")
    p.add_argument("--app-key-file", dest="app_key_file", default="",
                   help="prepare: the App's private key (default: ~/.cre/<slug>.pem)")
    p.add_argument("--wait-install", dest="wait_install", type=int, default=None,
                   help="prepare: seconds to wait for the App install click (default 600 when the page opened in a browser here, else 0)")
    c.common_flags(p)
    a = p.parse_args(argv)
    step.dry_run = a.dry_run
    c.take_repo_decisions(step, a)
    sf = c.SetupFile(step)
    sf.default(a, "repo", "githubRepo")
    if a.mode in ("prepare", "finalize"):
        # finalize: the App approves the scaffold's PR when main is protected (an adopted repo).
        sf.default(a, "app_slug", "appSlug")
    if not a.dir and sf.dir:
        a.dir = sf.dir
    sf.report()
    a.setup_file = sf
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", a.repo or ""):
        step.die(c.REFUSED, "--repo must be <owner>/<name>.")
    a.owner = a.repo.split("/")[0]
    if a.wait_install is not None and a.wait_install < 0:
        step.die(c.REFUSED, "--wait-install takes a number of seconds.")
    if a.mode != "prepare" and (a.var or a.secret):
        # Only prepare sets them; the other modes took them and ignored them (review 10c F10).
        step.die(c.REFUSED, "--var and --secret are for setup:repo prepare; this mode doesn't set them.")
    if a.mode != "prepare" and (a.no_create or a.recreate):
        step.die(c.REFUSED, "--no-create and --recreate are for prepare (the only mode that creates the repo).")
    if a.no_create and a.recreate:
        step.die(c.REFUSED, "--no-create and --recreate contradict each other.")
    # After this clone's first push, a missing repo was deleted (or renamed) since: prepare refuses
    # to re-create it unless --recreate says so, in a project's own setup:repo as in new_project.py
    # (review 10f C1: 10e C5 covered only new_project's call).
    if a.mode == "prepare" and not a.recreate and c.pushed_before(a.dir):
        a.no_create = True
    if a.mode == "publish":
        # publish takes --repo, --dir and --dry-run; it took the rest and ignored them (10c F9).
        unused = [f for f, v in (("--message", a.message != c.SCAFFOLD_MESSAGE), ("--app-slug", a.app_slug),
                                 ("--app-id", a.app_id), ("--app-key-file", a.app_key_file),
                                 ("--wait-install", a.wait_install is not None)) if v]
        if unused:
            step.die(c.REFUSED, f"{' '.join(unused)}: setup:github-public doesn't use them (it takes --repo, --dir, --dry-run).")
    a.secrets = [c.parse_secret(step, s) for s in a.secret]
    a.vars = [c.parse_secret(step, v) for v in a.var]
    return a


def set_variable(step, repo, name, value):
    """An Actions repository variable (shown in logs), on stdin."""
    if step.dry_run:
        step.plan(f"(dry run) set Actions variable {name}")
        return
    step.plan(f"set Actions variable {name}")
    r = c.gh("variable", "set", name, "-R", repo, input=value)
    if r.returncode != 0:
        c.gh_fail(step, c.FAILED, f"gh variable set {name}", r.stderr or r.stdout, repo.split("/")[0])


def set_secret(step, repo, name, value):
    """On stdin, so the value never reaches argv."""
    if step.dry_run:
        step.plan(f"(dry run) set Actions secret {name}")
        return
    step.plan(f"set Actions secret {name}")
    r = c.gh("secret", "set", name, "-R", repo, input=value)
    if r.returncode != 0:
        c.gh_fail(step, c.FAILED, f"gh secret set {name}", r.stderr or r.stdout, repo.split("/")[0])


def git_url(repo):
    """origin's URL in the protocol gh is configured for (default https)."""
    proto = c.gh("config", "get", "git_protocol", "-h", "github.com").stdout.strip()
    return f"git@github.com:{repo}.git" if proto == "ssh" else f"https://github.com/{repo}.git"


def visibility(step, a):
    """PRIVATE / PUBLIC / INTERNAL, or "" when the repo doesn't exist. A renamed repo is refused:
    GitHub answers for the old name with the new repo, and the runner trust would name the old
    subject, so the first deploy gets no credentials (review 10c C10)."""
    r = c.gh("repo", "view", a.repo, "--json", "visibility,nameWithOwner", "--jq", '[.visibility, .nameWithOwner] | @tsv')
    if r.returncode == 0:
        vis, _, name = r.stdout.strip().partition("\t")
        if name and name.lower() != a.repo.lower():
            step.die(c.REFUSED, f"GitHub repo {a.repo} is now {name} (renamed or moved). Setup names the repo in the AWS trust, so it "
                     f"won't adopt it under the old name: rename it back to {a.repo}, or start a new project for {name}.")
        return vis
    text = r.stderr + r.stdout
    if "Could not resolve to a Repository" in text or "HTTP 404" in text:
        return ""
    c.gh_fail(step, c.FAILED, f"Reading {a.repo}", text, a.owner)


def install_page(owner, installation_id):
    """The installation's settings page (an organization's, or the user's own)."""
    if c.gh("api", f"orgs/{owner}").returncode == 0:
        return f"https://github.com/organizations/{owner}/settings/installations/{installation_id}"
    return f"https://github.com/settings/installations/{installation_id}"


class Install:
    """The App's installation as it covers this repo: state installed | suspended | owner | none."""

    def __init__(self, app, repo, owner):
        self.app, self.repo, self.owner = app, repo, owner
        self.state, self.id, self.missing = "none", "", []

    def check(self):
        self.state, self.id, self.missing = "none", "", []
        status, body = self.app.get(f"/repos/{self.repo}/installation")
        if status == "200":
            # A suspended installation still answers 200, but GitHub mints no tokens for it, so
            # projen's upgrade, self-mutation and auto-merge fail later in Actions (review 10e C6).
            self.state = "suspended" if body.get("suspended_at") else "installed"
            self.id = str(body.get("id", ""))
            self.missing = c.missing_permissions(body.get("permissions"))
            return self
        if status != "404":
            self.app.fail(status, f"/repos/{self.repo}/installation", body)
        for path in (f"/orgs/{self.owner}/installation", f"/users/{self.owner}/installation"):
            status, body = self.app.get(path)
            if status == "200":
                self.state, self.id = "owner", str(body.get("id", ""))
                break
        return self


class SetupReturn:
    """The page GitHub sends the browser to after the install or the repo-access change (the App's
    Setup URL, 127.0.0.1 on a fixed port). It says it's done once the install is confirmed as the
    App (the redirect itself proves nothing: anyone can open the URL). Until then it refreshes.
    url is "" when the port is taken (then the step only polls)."""

    def __init__(self, slug, repo):
        import html
        import socketserver
        import threading
        from http.server import BaseHTTPRequestHandler
        self.status, self.returned = "waiting", False
        me = self

        def page(done):
            if done:
                head, msg, color = "", (f"✅ {html.escape(slug)} is installed on {html.escape(repo)}. Setup goes on by itself "
                                        "(scaffold, build, first push). You can close this tab."), "green"
            else:
                head, msg, color = ('<meta http-equiv="refresh" content="2">',
                                    f"⏳ Checking that {html.escape(slug)} now covers {html.escape(repo)}…", "#555")
            return (f'<!doctype html><meta charset="utf-8">{head}<title>CRE setup</title>'
                    f'<body style="font:18px system-ui;max-width:40em;margin:4em auto;color:{color}"><p>{msg}</p>').encode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if not self.path.startswith("/installed"):
                    self.send_response(404)
                    self.end_headers()
                    return
                me.returned = True
                # The step checks as the App every second once the browser is back.
                deadline = time.time() + float(os.environ.get("CRE_SETUP_RETURN_WAIT", "8"))
                while me.status != "installed" and time.time() < deadline:
                    time.sleep(0.2)
                data = page(me.status == "installed")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

        try:
            # A subclass: setting allow_reuse_address on ThreadingTCPServer itself changed every
            # server in the process (review 10b C26).
            class Server(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True
            self.srv = Server(("127.0.0.1", c.APP_SETUP_PORT), H)
        except OSError:
            self.srv, self.url = None, ""
            return
        self.srv.daemon_threads = True
        self.url = c.APP_SETUP_URL
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def finish(self, installed):
        """Let a page that's open see the outcome, then stop."""
        if not self.srv:
            return
        self.status = "installed" if installed else "gave-up"
        if self.returned:
            time.sleep(float(os.environ.get("CRE_SETUP_RETURN_LINGER", "3")))
        self.srv.shutdown()
        self.srv.server_close()


def app_id_from_slug(step, slug):
    """The ID from the slug (public Apps answer without credentials). gh prints an error body on
    stdout on a 404, so only a number counts."""
    r = c.gh("api", f"apps/{slug}", "--jq", ".id")
    v = r.stdout.strip()
    if r.returncode != 0 or not v.isdigit():
        step.die(c.REFUSED, f"No GitHub App '{slug}' found (gh api apps/{slug}). Create it first: github_app.py "
                 "(npx projen setup:github-app in a project; new_project.py runs it).")
    return v


def load_app(step, a):
    """(App, its id, its key file) from --app-slug / --app-id / --app-key-file (or cre-setup.json),
    its key checked against GitHub."""
    if not a.app_slug:
        step.die(c.REFUSED, "--app-slug is required with --app-id / --app-key-file.")
    if a.app_id and not a.app_id.isdigit():
        step.die(c.REFUSED, "--app-id must be the App's numeric ID (its settings page, 'App ID').")
    app_id = a.app_id or app_id_from_slug(step, a.app_slug)
    key = a.app_key_file or os.path.join(os.environ.get("CRE_KEY_DIR") or os.path.expanduser("~/.cre"), f"{a.app_slug}.pem")
    if not os.path.isfile(key) or not os.access(key, os.R_OK) or os.path.getsize(key) == 0:
        step.die(c.REFUSED, f"No readable private key at {key}. Run github_app.py (npx projen setup:github-app in a project; it says "
                 "how to make one), or pass --app-key-file.")
    app = c.App(step, a.app_slug, app_id, key)
    app.check_key()
    return app, app_id, key


def adopt_main(step, argv):
    """new_project.py's review step for an adoption (adopt.py; not a setup:repo mode): push
    ADOPT_BRANCH with a lease, find or open its PR, approve it as the builder's App (GitHub refuses
    an author's own approval; a GitHub project always has the App), and squash-merge it once every
    check passed on its head commit (polled until then, or CRE_ADOPT_MR_WAIT seconds, default 2400). Outputs
    ADOPT_PUSHED, ADOPT_REVIEW (the PR's URL) and, once merged, ADOPT_MERGED (origin/main's sha).
    A failed check is 20; the wait running out is NEEDS=ADOPT_PR (13)."""
    p = c.Parser(step, "repo_github.py adopt")
    p.add_argument("--repo", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--app-slug", dest="app_slug", required=True)
    p.add_argument("--pushed", default="", help="what setup pushed to the branch last")
    p.add_argument("--pr", default="", help="the PR's URL, once opened")
    a = p.parse_args(argv)
    a.owner, a.app_id, a.app_key_file = a.repo.split("/")[0], "", ""
    c.require_tools(step, "git", "gh")
    c.require_gh(step)
    url = a.pr
    if url:
        state = c.gh("pr", "view", url, "--repo", a.repo, "--json", "state", "--jq", ".state").stdout.strip()
        if state == "MERGED":
            return adopt_merged(step, a)
        if state != "OPEN":
            step.die(c.REFUSED, f"{url} is {state.lower() or 'unreadable'}, not merged. Reopen it on GitHub, then re-run.")
    step.out("ADOPT_PUSHED", c.push_adopt_branch(step, a.dir, a.pushed))
    if not url:
        # The repo's own branch, not a fork's of the same name.
        url = c.gh("pr", "list", "--repo", a.repo, "--head", c.ADOPT_BRANCH, "--state", "open", "--json", "url,isCrossRepository",
                   "--jq", "[.[] | select(.isCrossRepository | not)][0].url // empty").stdout.strip()
    if not url.startswith("https://"):
        r = c.gh("pr", "create", "--repo", a.repo, "--base", "main", "--head", c.ADOPT_BRANCH, "--title", a.title,
                 "--body", "The CRE template adopted into this repo (new_project.py).")
        url = r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else ""
        if not url:
            c.gh_fail(step, c.FAILED, "Opening the scaffold's PR", r.stderr + r.stdout, a.owner)
    step.out("ADOPT_REVIEW", url)
    step.ok(f"{url} holds the scaffold.")
    app = load_app(step, a)[0]
    inst = Install(app, a.repo, a.owner).check()
    if inst.state != "installed":
        step.die(c.REFUSED, f"{a.app_slug} isn't installed on {a.repo}, so it can't approve the scaffold's PR ({url}). Install it "
                 "(setup:repo prepare says how), then re-run.")
    r = c.run(["gh", "pr", "review", url, "--repo", a.repo, "--approve", "--body", "Approved by the CRE setup App."],
              env={"GH_TOKEN": app.installation_token(inst.id)})
    if r.returncode != 0:
        step.die(c.FAILED, f"{a.app_slug} couldn't approve {url}: {c.last_line(r.stderr or r.stdout)}")
    step.ok(f"Approved it as {a.app_slug}.")
    # Squash, the template's merge method (finalize sets the same later): an adopted repo may allow
    # only some methods.
    r = c.gh("api", "-X", "PATCH", f"repos/{a.repo}", "-F", "allow_squash_merge=true")
    if r.returncode != 0:
        c.gh_fail(step, c.FAILED, f"Allowing squash merges on {a.repo}", r.stderr + r.stdout, a.owner)
    wait = int(os.environ.get("CRE_ADOPT_MR_WAIT") or 2400)
    poll = float(os.environ.get("CRE_ADOPT_MR_POLL") or 20)   # seconds between polls (the harness shortens it)
    deadline = time.time() + wait
    # Merged once every check on the head commit passed, never before: main isn't protected until
    # finalize, so GitHub would merge it with its checks still running (live check of review 10h, L1).
    # --match-head-commit: a commit pushed since the checks passed is refused, not merged unbuilt.
    last = "its checks haven't passed"
    while True:
        r = c.gh("pr", "view", url, "--repo", a.repo, "--json", "headRefOid,statusCheckRollup", "--jq",
                 '[.headRefOid, ([.statusCheckRollup[] | (.conclusion // .state)] | map(if . == "SUCCESS" or . == "NEUTRAL" or . == '
                 '"SKIPPED" then "ok" elif . == "FAILURE" or . == "ERROR" or . == "CANCELLED" or . == "TIMED_OUT" or . == '
                 '"ACTION_REQUIRED" or . == "STARTUP_FAILURE" then "failed" else "waiting" end) | join(","))] | join(" ")')
        head, _, checks = r.stdout.strip().partition(" ")
        states = [x for x in checks.split(",") if x]
        failed = states.count("failed")
        if failed:
            step.die(c.FAILED, f"{url} won't merge: {failed} of its checks failed. Nothing reached main. Fix the cause with a commit "
                     f"on {c.ADOPT_BRANCH} in {a.dir}, then re-run: setup pushes the branch and merges the PR.")
        if r.returncode == 0 and head and states and all(x == "ok" for x in states):
            m = c.gh("pr", "merge", url, "--repo", a.repo, "--squash", "--delete-branch", "--match-head-commit", head)
            if m.returncode == 0:
                step.ok(f"Merged {url} into main (its checks passed on {head[:9]}).")
                return adopt_merged(step, a)
            last = c.last_line(m.stderr or m.stdout)
        else:
            last = "its checks haven't passed" if states else "no checks have started"
        if time.time() >= deadline:
            break
        step.info(f"Waiting for {url}'s checks to pass ({last}).")
        time.sleep(min(poll, max(0, deadline - time.time())))
    step.warn(f"{url} can't merge yet, after {wait}s ({last}). A person approves or merges it, then "
              "re-run: setup goes on from the merged main.")
    step.need("ADOPT_PR")


def adopt_merged(step, a):
    if c.git("fetch", "-q", "origin", cwd=a.dir).returncode != 0:
        step.die(c.FAILED, "The PR merged, but fetching origin failed. Re-run.")
    step.out("ADOPT_MERGED", c.git("rev-parse", "origin/main", cwd=a.dir).stdout.strip())


def setup_app(step, a):
    """projen's own credential (gotcha 3): its workflows push (self-mutation), open PRs (dependency
    upgrades) and enable auto-merge with it. The workflow's GITHUB_TOKEN can't: what it pushes,
    opens or merges starts no workflow. The builder's GitHub App is the only credential: never a
    personal access token, never the builder's gh token."""
    app, app_id, key = load_app(step, a)
    # Secrets first: they don't depend on the installation, and the scaffold can go on meanwhile.
    set_secret(step, a.repo, "PROJEN_APP_ID", app_id)
    with open(key) as f:
        set_secret(step, a.repo, "PROJEN_APP_PRIVATE_KEY", f.read())
    a.setup_file.set("appSlug", a.app_slug)
    step.out("APP_SLUG", a.app_slug)

    inst = Install(app, a.repo, a.owner).check()
    if inst.state == "owner" and not step.dry_run:
        # Installed on the owner, not on this repo. Adding the repo through the API with the gh login
        # is refused even for an org owner (verified Oct 2026: 403 "You do not have permission to
        # modify this app"); tried once, in case GitHub allows it later.
        r = c.gh("api", f"repos/{a.repo}", "--jq", ".id")
        repo_id = r.stdout.strip()
        if r.returncode == 0 and repo_id.isdigit() and \
                c.gh("api", "-X", "PUT", f"/user/installations/{inst.id}/repositories/{repo_id}").returncode == 0:
            added_to = inst.id
            if inst.check().state == "installed":
                step.ok(f"Added {a.repo} to GitHub App {a.app_slug}'s installation {added_to} on {a.owner}.")
    if inst.state == "suspended":
        page = install_page(a.owner, inst.id)
        step.out("APP_INSTALL_URL", page)
        step.warn(f"GitHub App {a.app_slug}'s installation on {a.owner} is SUSPENDED, so it can't act on {a.repo} (projen's upgrade, "
                  f"self-mutation and auto-merge would fail in Actions). Unsuspend it at {page} (an owner of {a.owner} if it isn't "
                  "yours), then re-run.")
        step.need("APP_INSTALL")
        return
    if inst.state != "installed":
        if inst.state == "owner":
            page = install_page(a.owner, inst.id)
            how = (f"Repository access → Only select repositories → add {a.repo} → Save. (If you can't save there, {a.owner} "
                   "lets only owners change App access: ask one.)")
        else:
            page = f"{c.github_web()}/apps/{a.app_slug}/installations/new"
            how = f"choose {a.owner} → Only select repositories → {a.repo} → Install."
        step.out("APP_INSTALL_URL", page)
        if step.dry_run:
            step.plan(f"(dry run) GitHub App {a.app_slug} isn't installed on {a.repo}: that would be a browser step ({page})")
        else:
            # GitHub's own page, opened directly. After Save GitHub sends the browser back to the
            # App's Setup URL, served here, which says it's done (Apps made by github_app.py have it).
            back = SetupReturn(a.app_slug, a.repo)
            browser = c.open_url(page)
            opened = " (opened in your browser)" if browser else ""
            step.warn(f"ACTION NEEDED in the browser{opened}: install GitHub App {a.app_slug} on {a.repo} (GitHub has no API for "
                      f"it). On {page} → {how} GitHub then shows a page saying you can close the tab.")
            # By default wait only when the page opened here (someone is there to click it).
            wait = a.wait_install if a.wait_install is not None else (600 if browser else 0)
            if wait > 0:
                poll = float(os.environ.get("CRE_APP_INSTALL_POLL", "3"))
                step.info(f"Waiting up to {wait}s for the install (it carries on by itself within seconds of Save)...")
                deadline, last = time.time() + wait, time.time()
                while time.time() < deadline:
                    # Every second once GitHub sent the browser back, so its page turns green at once.
                    step_wait, t0 = (1 if back.returned else poll), time.time()
                    while time.time() - t0 < step_wait and not back.returned:
                        time.sleep(0.2)
                    # Once the browser is back the inner loop doesn't sleep: without this, the checks
                    # ran back to back against GitHub (review 10b C4).
                    time.sleep(max(0.0, step_wait - (time.time() - t0)))
                    if inst.check().state == "installed":
                        back.status = "installed"
                        break
                    if time.time() - last >= 30:
                        last = time.time()
                        step.info(f"Still waiting for the install of {a.app_slug} on {a.repo} ({int(deadline - time.time())}s left): {page}")
            back.finish(inst.state == "installed")
        if inst.state != "installed":
            step.info("Re-run this command once it's installed (--wait-install 90 waits for it). Everything else is set.")
            step.need("APP_INSTALL")
    if inst.state == "installed":
        step.ok(f"GitHub App {a.app_slug} is installed on {a.repo}.")
        if inst.missing:
            step.warn(f"Its installation lacks write access to: {' '.join(inst.missing)} (projen's workflows need Contents, Pull requests "
                      "and Workflows: write): the App was made by hand, or its new permissions weren't accepted. Give the App those "
                      f"permissions (github_app.py checks and says how), then accept the new permissions on the installation page, "
                      f"{install_page(a.owner, inst.id)}, and re-run.")
            step.need("APP_PERMISSIONS")


def history(step, a):
    """(empty | trivial | history, last author, summary) of an existing repo, read through the API.
    summary: its files, shown with the adopt decision."""
    r = c.gh("api", f"repos/{a.repo}/commits?per_page=2", "--jq", '[length, (.[0].commit.author.name // ""), (.[0].sha // "")] | @tsv')
    text = r.stdout + r.stderr
    if r.returncode != 0:
        if "HTTP 409" in text or "is empty" in text:
            return "empty", "", ""
        c.gh_fail(step, c.FAILED, f"Reading {a.repo}'s history", text, a.owner)
    n, who, sha = (r.stdout.rstrip("\n").split("\t") + ["", "", ""])[:3]
    if n == "0":
        return "empty", "", ""
    t = c.gh("api", f"repos/{a.repo}/git/trees/{sha}?recursive=1", "--jq", '.tree[] | select(.type == "blob") | .path')
    files = t.stdout.split() if t.returncode == 0 else []
    if n == "1" and t.returncode == 0 and all(c.TRIVIAL_FILE.fullmatch(f) for f in files):
        return "trivial", who, ""
    return "history", who, c.history_summary(n != "1", who, files)


def prepare(step, a):
    vis = visibility(step, a)
    if vis:
        kind, who, summary = history(step, a)
        c.refuse_foreign(step, f"GitHub repo {a.repo}", kind, who, a.dir, a.repo, summary)
        if step.discovery and kind != "empty" and step.outputs.get("REPO_STATE") != "own":
            # A repo setup didn't make: its own variables (one setup made has only setup's).
            got = [c.gh(what, "list", "--repo", a.repo) for what in ("variable", "secret")]
            ok = all(g.returncode == 0 for g in got)
            c.ci_variables_check(step, f"GitHub repo {a.repo}", a.repo,
                                 [ln.split()[0] for g in got for ln in g.stdout.splitlines() if ln.strip()] if ok else [],
                                 "" if ok else c.last_line(next(g for g in got if g.returncode).stderr))
    if not vis:
        step.check("adoptExistingRepo", a.repo, "not-needed", f"GitHub repo {a.repo} doesn't exist: setup creates it")
    if step.discovery:
        step.ok(f"Discovery for {a.repo}: {len(step.found)} decision(s); nothing was changed.")
        return
    if vis == "PRIVATE":
        step.ok(f"GitHub repo {a.repo} exists (private); adopting it and completing its setup.")
    elif vis == "INTERNAL":
        step.warn(f"GitHub repo {a.repo} exists and is INTERNAL; adopting it. Every member of the enterprise can read its code, "
                  "cre-setup.json (account ID, role ARNs) and workflow logs. Make it private unless that is intended.")
    elif vis:
        # Made by hand, possibly public on purpose: a free plan protects main only on public repos.
        step.warn(f"GitHub repo {a.repo} exists and is NOT private; adopting it. Its code, cre-setup.json (account ID, role ARNs) "
                  "and workflow logs are public. Make it private unless that is intended.")
    elif getattr(a, "no_create", False):
        # Pushed by an earlier run and gone now: a retirement as likely as an accident (review 10e C5).
        step.die(c.REFUSED, f"GitHub repo {a.repo} doesn't exist, but an earlier run of these answers pushed to it, so it was "
                 "deleted (or renamed) since. If that was on purpose (the project is retired), stop here. To re-create it from "
                 "this clone's main and set it up again: in the project, npx projen setup:repo prepare --recreate, then setup:repo finalize (a new repo id: then npx projen setup:aws, for the runner role's OIDC trust); "
                 "from the template, new_project.py --recreate.")
    elif step.dry_run:
        step.plan(f"(dry run) create private repo {a.repo}")
    else:
        step.plan(f"create private repo {a.repo}")
        r = c.gh("repo", "create", a.repo, "--private")
        if r.returncode != 0:
            out = r.stderr + r.stdout
            if "SAML" in out or "HTTP 401" in out:
                c.gh_fail(step, c.FAILED, f"Creating {a.repo}", out, a.owner)
            if re.search(r"HTTP 403|not have permission|correct permissions|Resource not accessible|is not a member", out):
                step.die(c.REFUSED, f"You may not create repositories in {a.owner} ({c.last_line(out)}). Ask an owner of {a.owner} to create "
                         f"{a.repo} (private, empty), then re-run: an existing repo is adopted.")
            step.die(c.FAILED, f"gh repo create {a.repo} failed: {c.last_line(out)}")
    # How PRs will merge (cre-setup.json githubVisibility → the project's merge mode).
    step.out("REPO_VISIBILITY", (vis or "PRIVATE").lower())
    # In a project: recorded where its merge mode is read from (a project made before the field
    # existed gets it here).
    if not step.dry_run:
        a.setup_file.set("githubVisibility", (vis or "PRIVATE").lower())
    for key, value in a.secrets:
        set_secret(step, a.repo, key, value)
    for key, value in a.vars:
        set_variable(step, a.repo, key, value)
    if (a.secrets or a.vars) and not step.dry_run:
        # Every GitHub secret is masked in the logs; variables aren't.
        secrets = [k for k, _ in a.secrets]
        c.record_ci_variables(a.setup_file, secrets + [k for k, _ in a.vars], secrets)
    if a.app_slug or a.app_id or a.app_key_file:
        setup_app(step, a)
    else:
        step.info("No --app-slug: PROJEN_APP_ID / PROJEN_APP_PRIVATE_KEY not changed (finalize checks they're set).")
    step.out("REPO_SSH_URL", f"git@github.com:{a.repo}.git")
    step.out("REPO_GIT_URL", git_url(a.repo))
    step.out("REPO_WEB_URL", f"https://github.com/{a.repo}")


def finalize(step, a):
    c.require_repo_dir(step, a.dir)
    if not visibility(step, a):
        step.die(c.REFUSED, f"GitHub repo {a.repo} doesn't exist; run 'repo_github.py prepare' first.")
    # The protocol the builder's gh login uses for git (`gh auth login` asks): SSH needs a key on
    # their GitHub account, HTTPS a credential helper. Either way, use what already works for them.
    if not c.scaffold_pushed(step, a.dir):
        c.set_origin(step, a.dir, git_url(a.repo))
        c.require_main(step, a.dir)
        c.bootstrap_push(step, a.dir, a.message)
    if step.dry_run:
        return
    r = c.gh("api", "-X", "PATCH", f"repos/{a.repo}", "-F", "allow_auto_merge=true", "-F", "allow_squash_merge=true",
             "-F", "delete_branch_on_merge=true")
    if r.returncode != 0:
        c.gh_fail(step, c.FAILED, f"Updating {a.repo} merge settings", r.stderr + r.stdout, a.owner)
    # GitHub accepts allow_auto_merge on a free private repo and leaves it off (seen live): say
    # what it is. A private CRE project doesn't use it (the build workflow merges).
    am = c.gh("api", f"repos/{a.repo}", "--jq", ".allow_auto_merge").stdout.strip()
    step.ok("Squash merges enabled; branches deleted on merge; " + ("auto-merge enabled." if am == "true" else
            "auto-merge not available (a free plan's private repo: the build workflow merges PRs instead)."))
    # projen's auto-approve approves with the workflow token, which GitHub allows only when the repo
    # lets Actions approve pull requests.
    r = c.gh("api", "-X", "PUT", f"repos/{a.repo}/actions/permissions/workflow", "-f", "default_workflow_permissions=read",
             "-F", "can_approve_pull_request_reviews=true")
    if r.returncode == 0:
        step.ok("GitHub Actions may approve pull requests (projen's auto-approve).")
    else:
        step.warn(f"Could not let GitHub Actions approve pull requests on {a.repo} (an organization policy, or no admin rights). "
                  "auto-approve can't approve until it is allowed: Settings > Actions > General > Allow GitHub Actions to create and "
                  "approve pull requests.")
        step.need("ACTIONS_CAN_APPROVE")
    # Branch protection, GitLab's push = no one: every change reaches main through a PR that the
    # build passed and someone (auto-approve, for a trusted author) approved; the merge deploys.
    r = c.gh("api", "-X", "PUT", f"repos/{a.repo}/branches/main/protection", "--input", "-", input=json.dumps(PROTECTION))
    out = r.stderr + r.stdout
    if r.returncode == 0:
        step.ok("Protected main: changes only through a PR with a passing build check and an approval.")
    elif "Upgrade to GitHub Pro" in out:
        # GitHub Free protects branches only on public repos. A private CRE project doesn't rely on
        # it: its build workflow merges each PR only after the PR's build passes (merge mode
        # "workflow"). What protection would add is refusing direct pushes to main.
        step.info(f"GitHub can't protect main on {a.repo} (a free plan protects only public repos). PRs still merge only after "
                  "their build passes: the build workflow merges them. Direct pushes to main aren't blocked. When the repo goes "
                  "public: npx projen setup:github-public (it protects main and switches to GitHub auto-merge).")
    else:
        # Setup may lack the rights to change it, while an admin protected it already: check.
        g = c.gh("api", f"repos/{a.repo}/branches/main/protection", "--jq", ".required_status_checks.contexts[]")
        if g.returncode == 0 and "build" in g.stdout.split():
            step.ok(f"main is already protected with the build check (setup couldn't change it: {c.last_line(r.stderr)}).")
        else:
            step.warn(f"Could not protect main on {a.repo} ({c.last_line(r.stderr)}), and it isn't protected with the build check. Ask "
                      "a repo admin to protect main: Settings > Branches > require a pull request with one approval and the 'build' "
                      "status check.")
            step.need("BRANCH_PROTECTION")
    # projen's workflows need the App's secrets (prepare --app-slug sets them).
    names = [ln.split()[0] for ln in c.gh("secret", "list", "-R", a.repo).stdout.splitlines() if ln.strip()]
    if "PROJEN_APP_ID" in names and "PROJEN_APP_PRIVATE_KEY" in names:
        step.ok("PROJEN_APP_ID and PROJEN_APP_PRIVATE_KEY are set.")
    else:
        step.warn(f"projen's workflows have no credential: PROJEN_APP_ID / PROJEN_APP_PRIVATE_KEY aren't set on {a.repo}, so upgrade "
                  "PRs, self-mutation and auto-merge fail. Run github_app.py once (npx projen setup:github-app; it makes "
                  f"aws-cre-automation-<alias>), then: repo_github.py prepare --repo {a.repo} --app-slug aws-cre-automation-<alias>")
        step.need("PROJEN_CREDENTIALS")
    step.out("REPO_WEB_URL", f"https://github.com/{a.repo}")


# Credential formats looked for in the whole history before a repo goes public (the git tasks'
# list, cregit.py SECRET_CONTENT): once public, every commit ever pushed is readable.
HISTORY_SECRETS = c.SECRET_CONTENT_PATTERN


PUBLISH_BRANCH = "chore/public-repo-workflows"
# The git tasks (push.py, watch.py) beside setup/. CRE_GIT_SCRIPTS replaces them (the offline harness).
GIT_DIR = os.environ.get("CRE_GIT_SCRIPTS") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "git")


def publish(step, a):
    """A private repo made public, end to end, so the next PR already merges the public way:
      1. preflight: in the project, on main, nothing uncommitted (the switch is committed on its
         own), and no credential anywhere in the history;
      2. what becomes readable, then the repo made public: running publish is the decision, so it
         doesn't ask (--dry-run shows what it would do);
      3. main protected (the build check, one approval), githubVisibility=public recorded;
      4. `npx projen` regenerates the workflows (auto-queue replaces the build workflow's merge job),
         and the git tasks' push opens the PR for it (GitHub auto-merge, behind the new protection);
      5. it waits for that PR to merge and fast-forwards main, so the new workflows are on main
         (auto-approve and auto-queue run main's copy) before any PR with application code.
    Re-running carries on from wherever it stopped."""
    if not a.dir or not os.path.isfile(os.path.join(a.dir, "cre-setup.json")):
        step.die(c.REFUSED, "Run it in the project (npx projen setup:github-public): it records the change in cre-setup.json.")
    vis = visibility(step, a)
    if not vis:
        step.die(c.REFUSED, f"GitHub repo {a.repo} doesn't exist.")
    sf = a.setup_file
    git = lambda *x: c.run(["git", "-C", a.dir, *x])  # noqa: E731
    branch = git("branch", "--show-current").stdout.strip()
    dirty = [ln for ln in git("status", "--porcelain").stdout.splitlines() if ln[3:] != "cre-setup.json"]
    # A re-run after the workflows PR was opened resumes on its branch.
    resuming = branch.startswith(PUBLISH_BRANCH) and vis != "PRIVATE"
    if (branch != "main" and not resuming) or dirty:
        step.die(c.REFUSED, "Switching to public commits the regenerated workflows on their own, so start on main with nothing "
                 f"uncommitted (now: {branch or 'detached HEAD'}{', changes in ' + ', '.join(ln[3:] for ln in dirty[:5]) if dirty else ''}). "
                 "Push or stash your work, git switch main, then re-run.")
    if vis != "PRIVATE":
        step.ok(f"{a.repo} is already {vis.lower()}.")
    else:
        # Everything that becomes public, not only the local refs: every branch on origin (a
        # teammate's included) and every PR's head, which GitHub keeps for closed PRs too.
        f = git("fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*", "+refs/pull/*/head:refs/cre-scan/pull/*")
        if f.returncode != 0:
            step.die(c.FAILED, f"Couldn't fetch origin's branches and PR heads to scan them: {c.last_line(f.stderr)}")
        r = git("log", "--all", "-G", HISTORY_SECRETS, "--format=%h %s")
        for ref in git("for-each-ref", "--format=%(refname)", "refs/cre-scan/").stdout.split():
            git("update-ref", "-d", ref)
        if r.returncode != 0:
            step.die(c.FAILED, f"Couldn't scan the history: {c.last_line(r.stderr)}")
        hits = [ln for ln in r.stdout.splitlines() if re.match(r"^[0-9a-f]{7,} ", ln)]
        if hits:
            step.die(c.REFUSED, "The history has commits that look like they hold a credential; public, they'd be readable by anyone:\n"
                     + "\n".join("     " + h for h in hits[:20]) + "\n   Revoke those credentials and rewrite that history first (or keep the "
                     "repo private).")
        step.info(f"Making {a.repo} PUBLIC makes readable by anyone: all its code and history, cre-setup.json (AWS account "
                  f"{sf.data.get('accountId', '?')}, role ARNs), the workflow logs, issues and PRs. Secrets stay secret. The history "
                  "scan found no known credential format. It then protects main and pushes and merges the regenerated workflows.")
        if step.dry_run:
            step.info(f"(dry run) would make {a.repo} public.")
            return
        r = c.gh("repo", "edit", a.repo, "--visibility", "public", "--accept-visibility-change-consequences")
        if r.returncode != 0:
            c.gh_fail(step, c.FAILED, f"Making {a.repo} public", r.stderr + r.stdout, a.owner)
        step.ok(f"{a.repo} is public.")
    if step.dry_run:
        return
    r = c.gh("api", "-X", "PUT", f"repos/{a.repo}/branches/main/protection", "--input", "-", input=json.dumps(PROTECTION))
    if r.returncode != 0:
        step.die(c.FAILED, f"Could not protect main ({c.last_line(r.stderr + r.stdout)}). The repo is public; until main is protected, "
                 "GitHub auto-merge wouldn't wait for the build, so nothing was switched. Fix it (admin rights?), then re-run.")
    step.ok("Protected main: changes only through a PR with a passing build check and an approval.")
    # GitHub keeps allow_auto_merge off on a free private repo even when it was asked for (setup
    # asked at finalize), and doesn't turn it on when the repo goes public: turn it on, and check.
    c.gh("api", "-X", "PATCH", f"repos/{a.repo}", "-F", "allow_auto_merge=true")
    if c.gh("api", f"repos/{a.repo}", "--jq", ".allow_auto_merge").stdout.strip() != "true":
        step.die(c.FAILED, f"GitHub didn't enable auto-merge on {a.repo} (Settings > General > Allow auto-merge), so a PR could never "
                 "merge the public way. Enable it, then re-run.")
    step.ok("Auto-merge enabled.")
    sf.set("githubVisibility", "public")

    # Where an earlier run left the workflows PR (review 10b C12): merged while we were away (back
    # to main, caught up), or open while this runs from main (back onto its branch, to watch it).
    head = branch if branch.startswith(PUBLISH_BRANCH) else PUBLISH_BRANCH
    r = c.gh("pr", "list", "-R", a.repo, "--head", head, "--state", "all", "--limit", "1", "--json", "state",
             "--jq", '.[0].state // "none"')
    if r.returncode != 0:
        c.gh_fail(step, c.FAILED, f"Looking up the {PUBLISH_BRANCH} PR", r.stderr + r.stdout, a.owner)
    pr_state = r.stdout.strip() or "none"
    if pr_state == "MERGED" and branch == head:
        step.info(f"The {head} PR has merged; back to main.")
        git("switch", "-q", "main")
        if git("fetch", "-q", "origin", "main").returncode == 0:
            git("merge", "-q", "--ff-only", "origin/main")
        branch, resuming = "main", False
    elif pr_state == "OPEN" and branch != head:
        step.info(f"The {PUBLISH_BRANCH} PR is open; carrying on from its branch.")
        if git("fetch", "-q", "origin", f"{PUBLISH_BRANCH}:refs/remotes/origin/{PUBLISH_BRANCH}").returncode != 0 or \
                git("switch", "-q", PUBLISH_BRANCH).returncode != 0:
            step.die(c.FAILED, f"Could not switch to {PUBLISH_BRANCH} (the open PR's branch). Switch to it yourself, then re-run.")
        branch, resuming = PUBLISH_BRANCH, True
    sf.set("githubVisibility", "public")   # again after a switch: the other branch's copy may predate it
    # A merged PR's branch name is taken: a later change goes on a dated one.
    push_branch = PUBLISH_BRANCH if pr_state != "MERGED" else f"{PUBLISH_BRANCH}-{time.strftime('%Y%m%d-%H%M%S')}"

    # The regenerated workflows, committed and merged through the git tasks.
    env = {"CRE_GIT_PROVIDER": "github"}
    step.plan("npx projen (regenerate the workflows for GitHub auto-merge)")
    r = c.run(["npx", "projen"], cwd=a.dir)
    if r.returncode != 0:
        step.die(c.FAILED, f"npx projen failed: {c.last_line(r.stderr + r.stdout)}. The repo is public and main protected; fix it, then re-run.")
    if not resuming and not git("status", "--porcelain").stdout.strip():
        step.ok("The workflows on main are already the public repo's.")
        return
    step.plan(f"push the regenerated workflows as a PR ({push_branch}) and merge it")
    r = c.run(["python3", os.path.join(GIT_DIR, "push.py"), "--message", "ci: merge through GitHub auto-merge now the repo is public",
               "--branch", push_branch, "--files", "cre-setup.json"], cwd=a.dir, env=env)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        step.die(c.FAILED, f"Pushing the regenerated workflows failed ({c.last_line(r.stdout)}). Fix what it says, then re-run.")
    branch = git("branch", "--show-current").stdout.strip()
    timeout = os.environ.get("CRE_PUBLISH_TIMEOUT", "2700")
    step.plan(f"wait for the PR from {branch} to merge (its build, then auto-merge; up to {timeout} s)")
    r = c.run(["python3", os.path.join(GIT_DIR, "watch.py"), "--branch", branch, "--until", "merged", "--timeout", timeout],
              cwd=a.dir, env=env)
    sys.stderr.write(r.stderr[-3000:])
    if r.returncode != 0:
        step.die(c.FAILED, f"The workflows PR didn't merge ({c.last_line(r.stdout)}). See it with npx projen watch --branch {branch}; "
                 "once it merges, nothing else is needed.")
    # Back to main, caught up with the merge (the PR's branch is deleted on merge).
    git("switch", "-q", "main")
    if git("fetch", "-q", "origin", "main").returncode == 0:
        git("merge", "-q", "--ff-only", "origin/main")
    step.ok(f"{a.repo} is public, main is protected, and the auto-merge workflows are on main: the next PR merges through "
            "GitHub auto-merge once its build passes and it is approved.")


def main(step, argv):
    a = parse(step, argv)
    c.require_tools(step, "gh", "git", "curl", "openssl")
    c.require_gh(step)
    {"prepare": prepare, "finalize": finalize, "publish": publish}[a.mode](step, a)


if __name__ == "__main__":
    c.cli(NAME, main)
