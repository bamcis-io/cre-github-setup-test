#!/usr/bin/env python3
"""A CRE project's GitHub repo: set up, and protected once main exists.

  github.py setup     --repo <owner>/<name> --app-slug <slug> [--app-key-file <pem>] [--wait-install <s>]
  github.py variables --repo <owner>/<name> [--environment AWS] [--var KEY=VALUE]... [--secret KEY=VALUE]...
  github.py publish   --repo <owner>/<name> --dir <clone>
  github.py merge   --repo <owner>/<name> --app-slug <slug> [--app-key-file <pem>]
                    --branch <b> --sha <commit> --title <t> [--wait <s>]
  github.py protect --repo <owner>/<name>

setup:
1. The repo: created private if missing (in the builder's account or their organization; with
   --no-create, refused: 10). An existing one's history is left as it is (REPO_STATE says which;
   the adoption step's); one that is public or internal is set up with a warning. A repo GitHub answers under another name
   (renamed or moved) is refused: the AWS trust names the repo. OIDC_SUBJECT is its OIDC subject
   prefix, read from GitHub (a new repo's names the owner's and the repo's IDs, so the repo must
   exist before account.py, which trusts it); a custom subject template is refused.
2. The builder's GitHub App (made once per builder by github_app.py), the only credential projen's
   workflows have (upgrades, self-mutation, approving and merging PRs: what the workflow's own
   token pushes or opens starts no workflow, and it can't change workflow files): its key checked
   with GitHub, PROJEN_APP_ID and PROJEN_APP_PRIVATE_KEY set, and its installation on the repo
   checked for Contents, Pull requests and Workflows write. Installing is a browser step (GitHub
   has no API for it): the page opens, and setup waits for the install (--wait-install, default
   600 s; 0 opens nothing and doesn't wait), then stops with 10 and APP_INSTALL_URL if it isn't done.
3. Squash merges, branches deleted on merge (and GitHub auto-merge when the repo isn't private:
   a private one's build workflow merges PRs itself), and Actions allowed to approve PRs.
4. A public repo: the `AWS` environment the deploy jobs run in, deployable from main only
   (GITHUB_ENVIRONMENT=AWS; account.py --github-environment AWS then trusts its OIDC subject
   instead of main's, so no other workflow on main can deploy). GitHub's free plan has no
   environments for private repos: GITHUB_ENVIRONMENT is empty there.

merge: the branch's PR into main approved by the App (GitHub refuses an author's own approval) and
squash-merged once every check passed on --sha, the commit the caller pushed, and that commit only.
The PR is the one for --sha (merged: done; closed: 10), else the open one, else a new one. Polled
every 15 s for up to --wait seconds (1800); the wait running out is 20. 10 for what only a person
clears: a failed check, no check 300 s after the PR opened, a conflict, a commit someone else pushed
to the branch, a merge GitHub refuses. PR_URL is printed as soon as it is known; MERGED_SHA at the end.

variables: Actions variables (--var, shown in logs) and secrets (--secret, masked), after
account.py made the roles they name; with --environment, the environment's, so only its jobs get
them (and the repo-level copies of those names are deleted). `KEY=@<file>` reads the value from a
file. A --var that feeds a noEcho stack parameter (cre-stacks.json and cdk.out in the current
directory say which) is stored as a secret. A variable is written only where missing or different; a
secret's value can't be read back, so each is set every run. A name lives in one place: a secret
of a --var's name is deleted, and a variable of a --secret's name.

publish: a private repo made public, for `npx projen setup:github-public`. Refused (10) when any
commit on any branch or PR head holds something shaped like a credential (once public, every
commit is readable). Then: public, the `AWS` environment, auto-merge, main protected. Outputs
GITHUB_ENVIRONMENT=AWS. Its caller then runs account.py --github-environment AWS (the trust moves to
the environment), github.py variables --environment AWS, and sets github.visibility: 'public' in
.projenrc.ts (the deploy jobs then declare the environment) through a PR. Until that PR merges,
main's old workflow can't deploy: the trust no longer takes main's subject. Re-run: what is done
is skipped.

protect: main requires a PR with the `build` check and one approval, admins included. A free plan
can't protect a private repo's main: that is reported, not a failure (the build workflow merges
only PRs whose build passed). Run after main exists (the first push, or the adoption's merge).

Exit: 0 done; 10 refused (by the arguments, or GitHub: a re-run can't help until it changes);
11 credentials (gh's login); 20 a call failed (a re-run continues). Last stdout line
`RESULT code=<n>`; before it (setup) REPO_GIT_URL, REPO_WEB_URL, REPO_STATE (empty | history), OIDC_SUBJECT,
REPO_VISIBILITY (private | public | internal), GITHUB_ENVIRONMENT, APP_ID, and APP_INSTALL_URL when
it isn't installed; (publish) GITHUB_ENVIRONMENT;
(merge) PR_URL and MERGED_SHA; (protect) PROTECTED.
"""
import argparse
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from github_api import WEB, App, GitHub, every  # noqa: E402
from noecho_variables import noecho_variables  # noqa: E402
from result import FAILED, OK, REFUSED, Failed, must  # noqa: E402
from setup_args import SingleValue  # noqa: E402
from setup_args import key_value as any_key_value  # noqa: E402

NEEDED = ("contents", "pull_requests", "workflows")   # write, for projen's workflows
SETTINGS = {"allow_squash_merge": True, "delete_branch_on_merge": True}
PROTECTION = {"required_status_checks": {"strict": False, "contexts": ["build"]}, "enforce_admins": True,
              "required_pull_request_reviews": {"required_approving_review_count": 1}, "restrictions": None,
              "allow_force_pushes": False, "allow_deletions": False}
INSTALL_POLL = float(os.environ.get("CRE_APP_INSTALL_POLL", "3"))
INSTALL_WAIT = 600   # seconds for the builder's Install click
SETUP_PORT = 47391   # the App's Setup URL (github_app.py's manifest): GitHub sends the browser here after an install
UPGRADE_FOR_PROTECTION = "Upgrade to GitHub Pro"
MERGE_WAIT, MERGE_POLL, NO_CHECKS_GRACE = 1800, 15, 300
DEPLOY_ENVIRONMENT = "AWS"   # the deploy jobs' environment in a public repo (deploy-workflow.ts, gitlab.py's)
ENV_POLICY = {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
PASSED = {"success", "neutral", "skipped"}
STOPPED = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale", "error"}


def key_file_for(slug):
    return os.path.join(os.environ.get("CRE_KEY_DIR") or os.path.expanduser("~/.cre"), f"{slug}.pem")


# ---- setup ---------------------------------------------------------------------------------------

def repo(gh, name, say, create=True):
    """(repo, created). A missing one is created (create=False: refused)."""
    r = gh.call("GET", f"/repos/{name}")
    if r.ok:
        if r.data["full_name"].lower() != name.lower():
            raise Failed(REFUSED, f"GitHub repo {name} is now {r.data['full_name']} (renamed or moved): the AWS trust names the "
                                  f"repo, so use {r.data['full_name']}, or rename it back.")
        if r.data.get("visibility") != "private":
            say(f"⚠  {name} is {r.data.get('visibility')}: its code and workflow logs are readable beyond you. "
                "Make it private unless that is meant.")
        return r.data, False
    if r.kind != "missing":
        must(r, f"Reading {name}")
    if not create:
        raise Failed(REFUSED, f"GitHub repo {name} isn't there (deleted, or you can't see it). Setup in a project never "
                              "creates it again: a retired project doesn't come back by a routine re-run.")
    owner, short = name.split("/")
    login = must(gh.call("GET", "/user"), "Reading your GitHub login")["login"]
    path = "/user/repos" if owner.lower() == login.lower() else f"/orgs/{owner}/repos"
    say(f"→  create private repo {name}")
    made = must(gh.call("POST", path, {"name": short, "private": True, "auto_init": False}), f"Creating {name}")
    return made, True


def repo_state(gh, name):
    r = gh.call("GET", f"/repos/{name}/commits?per_page=1")
    if r.status == 409:   # "Git Repository is empty."
        return "empty"
    return "history" if must(r, f"Reading {name}'s history") else "empty"


def variables_and_secrets(gh, name, plain, hidden, say, env=None):
    """In the repo, or (env) in its environment; a name lives in one place only."""
    # The repo's are under actions/; an environment's aren't (GitHub's paths).
    where = f"/repos/{name}/environments/{env}" if env else f"/repos/{name}/actions"
    label = f"{env} environment " if env else "Actions "
    listed = lambda base, key, field: {x["name"]: x.get(field) for x in must(every(gh, f"{base}/{key}", key=key),  # noqa: E731
                                                                              f"Listing {key}")}
    have_vars = listed(where, "variables", "value")
    have_secrets = listed(where, "secrets", "name")
    for key, value in plain:
        if key not in have_vars:
            say(f"→  set {label}variable {key}")
            must(gh.call("POST", f"{where}/variables", {"name": key, "value": value}), f"Setting variable {key}")
        elif have_vars[key] != value:
            say(f"→  update {label}variable {key}")
            must(gh.call("PATCH", f"{where}/variables/{key}", {"name": key, "value": value}), f"Setting variable {key}")
        if key in have_secrets:
            say(f"→  delete {label}secret {key}: it is a variable")
            must(gh.call("DELETE", f"{where}/secrets/{key}"), f"Deleting secret {key}")
    for key, value in hidden:
        must(gh.set_secret(name, key, value, env=env), f"Setting secret {key}")
        if key in have_vars:
            say(f"→  delete {label}variable {key}: it is a secret")
            must(gh.call("DELETE", f"{where}/variables/{key}"), f"Deleting variable {key}")
    if hidden:
        say(f"✓  set {label}secret(s) {', '.join(k for k, _ in hidden)}")
    if env:
        # The repo-level copies of these names: every job could read them, which the environment is for not allowing.
        repo_vars, repo_secrets = listed(f"/repos/{name}/actions", "variables", "value"), listed(f"/repos/{name}/actions", "secrets", "name")
        for key, _ in plain + hidden:
            for kind, have in (("variables", repo_vars), ("secrets", repo_secrets)):
                if key in have:
                    say(f"→  delete the repo's {kind[:-1]} {key}: it is the {env} environment's now")
                    must(gh.call("DELETE", f"/repos/{name}/actions/{kind}/{key}"), f"Deleting the repo's {key}")


def environment(gh, name, say):
    """The deploy jobs' environment, deployable from main only."""
    env = gh.call("GET", f"/repos/{name}/environments/{DEPLOY_ENVIRONMENT}")
    policy = (env.data or {}).get("deployment_branch_policy") if env.ok else None
    if env.kind not in ("ok", "missing"):
        must(env, f"Reading the {DEPLOY_ENVIRONMENT} environment")
    if policy != ENV_POLICY["deployment_branch_policy"]:
        say(f"→  {'make' if env.kind == 'missing' else 'set'} the {DEPLOY_ENVIRONMENT} environment (deploys from main only)")
        must(gh.call("PUT", f"/repos/{name}/environments/{DEPLOY_ENVIRONMENT}", ENV_POLICY), f"Making the {DEPLOY_ENVIRONMENT} environment")
    base = f"/repos/{name}/environments/{DEPLOY_ENVIRONMENT}/deployment-branch-policies"
    rules = must(every(gh, base, key="branch_policies"), "Reading its deployment branches")
    if not any(r.get("name") == "main" and r.get("type", "branch") == "branch" for r in rules):
        must(gh.call("POST", base, {"name": "main", "type": "branch"}), "Letting main deploy to it")
    for r in rules:
        if not (r.get("name") == "main" and r.get("type", "branch") == "branch"):
            say(f"→  remove deployment rule {r.get('type', 'branch')} {r.get('name')}: only main deploys")
            must(gh.call("DELETE", f"{base}/{r['id']}"), f"Removing deployment rule {r.get('name')}")


class SetupPage:
    """The App's Setup URL while setup waits: GitHub sends the browser here after Install, and the
    page says to close the tab. 127.0.0.1 only."""

    def __init__(self):
        self.returned = False
        page = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):   # noqa: N802
                page.returned = True
                body = b"<html><body><h2>Installed.</h2><p>Setup carries on by itself: you can close this tab.</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", SETUP_PORT), Handler)
        except OSError:
            self.server = None   # the port is taken: the browser shows GitHub's redirect failing, nothing more
            return
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def open_browser(url):
    """True when the browser opened the page (setup runs on the builder's own machine)."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        return subprocess.run([opener, url], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_page(gh, app, owner):
    """Where the builder installs the App on this repo: the owner's installation (add the repo), or
    a new one."""
    org = gh.call("GET", f"/orgs/{owner}").ok
    r = app.call("GET", f"/{'orgs' if org else 'users'}/{owner}/installation")
    if r.ok:
        settings = f"{WEB}/organizations/{owner}/settings/installations/{r.data['id']}" if org else \
            f"{WEB}/settings/installations/{r.data['id']}"
        return settings, "Repository access → Only select repositories → add the repo → Save"
    return f"{WEB}/apps/{app.slug}/installations/new", f"choose {owner} → Only select repositories → the repo → Install"


def installation(app, name):
    """The App's installation on the repo with what it may not do, or None when it isn't installed."""
    r = app.call("GET", f"/repos/{name}/installation")
    if r.kind == "missing":
        return None
    inst = must(r, f"Reading {app.slug}'s installation on {name}")
    if inst.get("suspended_at"):
        raise Failed(REFUSED, f"{app.slug}'s installation on {name} is suspended: unsuspend it at "
                              f"{WEB}/settings/installations/{inst['id']}, then re-run.")
    lacks = [p for p in NEEDED if (inst.get("permissions") or {}).get(p) != "write"]
    return inst, lacks


def the_app(gh, a, make_app=App):
    """The builder's App, its key checked with GitHub."""
    key = a.app_key_file or key_file_for(a.app_slug)
    if not os.path.isfile(key):
        raise Failed(REFUSED, f"No private key for GitHub App {a.app_slug} at {key}: run github_app.py (it makes the App and "
                              "its key), or pass --app-key-file.")
    found = gh.call("GET", f"/apps/{a.app_slug}")
    if found.kind == "missing":
        raise Failed(REFUSED, f"No GitHub App {a.app_slug}: run github_app.py, which makes it.")
    app_id = str(must(found, f"Reading GitHub App {a.app_slug}")["id"])
    app = make_app(a.app_slug, app_id, key)
    me = app.call("GET", "/app")
    if not me.ok:
        raise Failed(REFUSED if me.kind in ("refused", "missing") else FAILED, me.message)
    if str(me.data.get("id")) != app_id:
        raise Failed(REFUSED, f"{key} signs for App ID {me.data.get('id')}, not {a.app_slug} ({app_id}): use {a.app_slug}'s key.")
    return app, key


def app_on_repo(gh, a, name, say, out, make_app=App, clock=time.monotonic, sleep=time.sleep, browser=open_browser):
    app, key = the_app(gh, a, make_app)
    app_id = app.id
    out("APP_ID", app_id)
    must(gh.set_secret(name, "PROJEN_APP_ID", app_id), "Setting secret PROJEN_APP_ID")
    with open(key) as f:
        must(gh.set_secret(name, "PROJEN_APP_PRIVATE_KEY", f.read()), "Setting secret PROJEN_APP_PRIVATE_KEY")
    found = installation(app, name)
    if found is None:
        url, how = install_page(gh, app, name.split("/")[0])
        out("APP_INSTALL_URL", url)
        page = SetupPage()
        # --wait-install 0 asks for no waiting, so nothing opens either: the URL is the output.
        wait = a.wait_install if a.wait_install is not None else INSTALL_WAIT
        opened = wait != 0 and browser(url)
        say(f"⚠  ACTION NEEDED in the browser{' (opened)' if opened else ''}: install GitHub App {a.app_slug} on {name}. "
            f"On {url}: {how}." + (f" Waiting up to {wait}s." if wait else ""))
        deadline = clock() + wait
        try:
            while found is None and clock() < deadline:
                sleep(INSTALL_POLL if not page.returned else 1)
                found = installation(app, name)
        finally:
            page.close()
        if found is None:
            raise Failed(REFUSED, f"GitHub App {a.app_slug} isn't installed on {name}: install it at {url}, then re-run.")
        say(f"✓  {a.app_slug} installed on {name}")
    inst, lacks = found
    if lacks:
        raise Failed(REFUSED, f"{a.app_slug}'s installation on {name} lacks write on {', '.join(lacks)}: accept the App's new "
                              f"permissions at {WEB}/settings/installations/{inst['id']}, then re-run.")


def settings(gh, r, name, say):
    want = dict(SETTINGS, **({"allow_auto_merge": True} if r.get("visibility") != "private" else {}))
    if any(r.get(k) != v for k, v in want.items()):
        say("→  set merge settings (squash, delete branches on merge" + (", auto-merge" if "allow_auto_merge" in want else "") + ")")
        must(gh.call("PATCH", f"/repos/{name}", want), f"Setting {name}'s merge settings")
    perms = must(gh.call("GET", f"/repos/{name}/actions/permissions/workflow"), "Reading Actions' workflow permissions")
    if not perms.get("can_approve_pull_request_reviews"):
        say("→  let Actions approve pull requests (projen's auto-approve)")
        r2 = gh.call("PUT", f"/repos/{name}/actions/permissions/workflow",
                     {"default_workflow_permissions": perms.get("default_workflow_permissions", "read"),
                      "can_approve_pull_request_reviews": True})
        if r2.kind == "refused" or r2.status == 409:   # 409: "prevented by the owning organization" (GitHub's docs)
            raise Failed(REFUSED, f"GitHub won't let Actions approve PRs on {name} ({r2.message}): an organization policy. An owner "
                                  "allows it: Settings → Actions → General → Allow GitHub Actions to create and approve pull requests.")
        must(r2, "Letting Actions approve pull requests")


def oidc_subject(gh, repo):
    """The repo's OIDC subject prefix, as GitHub reports it: with immutable subjects it names the owner's
    and the repo's IDs (repo:<owner>@<id>/<name>@<id>), so it can't be built from the repo's name."""
    oidc = must(gh.call("GET", f"/repos/{repo}/actions/oidc/customization/sub"), "Reading the repo's OIDC subject")
    if oidc.get("use_default") is False:
        raise Failed(REFUSED, f"{repo} has a custom OIDC subject template; the deploy role trusts only GitHub's default. "
                              f"Reset it: gh api -X PUT repos/{repo}/actions/oidc/customization/sub -F use_default=true")
    return oidc.get("sub_claim_prefix") or f"repo:{repo}"


def setup(a, gh, say, out=lambda k, v: None, **app_kw):
    """app_kw: app_on_repo's make_app, clock, sleep and browser (tests)."""
    the_app(gh, a, app_kw.get("make_app", App))   # refused before anything changes: no key, no App, a wrong key
    r, created = repo(gh, a.repo, say, create=not a.no_create)
    state = "empty" if created else repo_state(gh, a.repo)
    visibility = r.get("visibility") or ("private" if r.get("private") else "public")
    say(f"ℹ  repo {a.repo} ({'created' if created else 'existing'}, {visibility}, {state})")
    subject = oidc_subject(gh, a.repo)
    settings(gh, r, a.repo, say)
    if visibility == "public":
        environment(gh, a.repo, say)
    outputs = {"REPO_GIT_URL": f"{WEB}/{a.repo}.git", "REPO_WEB_URL": f"{WEB}/{a.repo}", "REPO_STATE": state,
               "REPO_VISIBILITY": visibility, "OIDC_SUBJECT": subject,
               "GITHUB_ENVIRONMENT": DEPLOY_ENVIRONMENT if visibility == "public" else ""}
    for k, v in outputs.items():
        out(k, v)
    app_on_repo(gh, a, a.repo, say, out, **app_kw)
    return {}


def variables(a, gh, say, out=lambda k, v: None):
    # A noEcho parameter's value is a secret, whichever switch set it.
    hidden = noecho_variables({k for k, _ in a.var})
    for k in sorted(hidden):
        say(f"ℹ  {k} feeds a noEcho parameter: stored as a secret")
    plain = [(k, v) for k, v in a.var if k not in hidden]
    secret = a.secret + [(k, v) for k, v in a.var if k in hidden]
    variables_and_secrets(gh, a.repo, plain, secret, say, env=a.environment)
    return {}


# ---- publish -------------------------------------------------------------------------------------

def credentials_in_history(clone, repo):
    """Commits on every branch, tag and PR head of `repo` itself (fetched from GitHub into the clone,
    whatever its origin) that hold something shaped like a credential (credential_pattern.py, the
    git tasks' too). Once public, every one of them is readable."""
    from credential_pattern import SECRET_CONTENT_PATTERN
    from gitlocal import git
    url = f"{WEB}/{repo}.git"
    git(clone, "fetch", "-q", url, "+refs/heads/*:refs/cre-scan/heads/*", "+refs/tags/*:refs/cre-scan/tags/*",
        "+refs/pull/*/head:refs/cre-scan/pull/*", what=f"Fetching every branch, tag and PR head of {repo} to scan")
    try:
        r = git(clone, "log", "--glob=refs/cre-scan/*", "-G", SECRET_CONTENT_PATTERN, "--format=%h %s",
                what="Scanning the history")
    finally:
        for ref in git(clone, "for-each-ref", "--format=%(refname)", "refs/cre-scan/").out.split():
            git(clone, "update-ref", "-d", ref)
    return [ln for ln in r.out.splitlines() if ln.strip()]


def publish(a, gh, say, out=lambda k, v: None):
    r = must(gh.call("GET", f"/repos/{a.repo}"), f"Reading {a.repo}")
    if r.get("visibility") != "public":
        hits = credentials_in_history(os.path.abspath(os.path.expanduser(a.dir)), a.repo)
        if hits:
            raise Failed(REFUSED, "Commits that look like they hold a credential would become readable by anyone:\n"
                         + "\n".join("     " + h for h in hits[:20]) + "\n   Revoke those credentials and rewrite that history "
                         "first, or keep the repo private.")
        say(f"→  make {a.repo} public: its code, history and workflow logs become readable by anyone; secrets stay secret")
        r = must(gh.call("PATCH", f"/repos/{a.repo}", {"visibility": "public"}), f"Making {a.repo} public")
    environment(gh, a.repo, say)
    settings(gh, r, a.repo, say)
    protect(a, gh, say)
    return {"GITHUB_ENVIRONMENT": DEPLOY_ENVIRONMENT}


# ---- merge ---------------------------------------------------------------------------------------

def find_pr(gh, a):
    """The PR from a.branch into main this run continues: the one for a.sha (merged, closed or
    open), else the open one, else None. A fork's branch of the same name is never it."""
    owner = a.repo.split("/")[0]
    prs = [p for p in must(every(gh, f"/repos/{a.repo}/pulls?state=all&head={owner}:{a.branch}&base=main"), "Listing the branch's PRs")
           if (p.get("head") or {}).get("repo", {}).get("full_name", "").lower() == a.repo.lower()]
    found = next((p for p in prs if p["head"]["sha"] == a.sha), None) or next((p for p in prs if p["state"] == "open"), None)
    if found:
        return found
    # A person merged it with a commit of their own on top (what a "someone else pushed" refusal asks):
    # done when that PR's head contains --sha, which GitHub's compare says.
    for p in (x for x in prs if x.get("merged_at")):
        c = gh.call("GET", f"/repos/{a.repo}/compare/{a.sha}...{p['head']['sha']}")
        if c.ok and c.data.get("status") in ("ahead", "identical"):
            return p
    return None


def checks_on(gh, repo, sha):
    """(state, what): state passed | stopped | waiting | none, over the commit's check runs and statuses."""
    runs = must(every(gh, f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs"), "Reading the commit's checks")
    statuses = must(gh.call("GET", f"/repos/{repo}/commits/{sha}/status?per_page=100"), "Reading the commit's statuses").get("statuses") or []
    results = [(r["name"], r.get("conclusion") if r.get("status") == "completed" else None) for r in runs] + \
        [(s["context"], {"success": "success", "failure": "failure", "error": "error"}.get(s["state"])) for s in statuses]
    if not results:
        return "none", "no checks yet"
    stopped = [n for n, c in results if c in STOPPED]
    if stopped:
        return "stopped", f"failed: {', '.join(stopped)}"
    waiting = [n for n, c in results if c not in PASSED]
    if waiting:
        return "waiting", f"running: {', '.join(waiting)}"
    return "passed", f"{len(results)} passed"


def approve(gh, app, a, pr, say):
    """The App's approval of this commit, once (GitHub refuses the author's own)."""
    bot = f"{app.slug}[bot]"
    reviews = must(every(gh, f"/repos/{a.repo}/pulls/{pr['number']}/reviews"), "Reading the PR's reviews")
    if any(r.get("state") == "APPROVED" and (r.get("user") or {}).get("login") == bot and r.get("commit_id") == a.sha for r in reviews):
        return
    found = installation(app, a.repo)
    if found is None:
        raise Failed(REFUSED, f"{app.slug} isn't installed on {a.repo}, so it can't approve the PR: run github.py setup, then re-run.")
    token = must(app.installation_token(found[0]["id"]), f"A token for {app.slug}'s installation")
    say(f"→  approve PR #{pr['number']} as {app.slug}")
    must(app.call("POST", f"/repos/{a.repo}/pulls/{pr['number']}/reviews",
                  {"event": "APPROVE", "commit_id": a.sha, "body": "Approved by the CRE setup App."}, token=token),
         f"Approving PR #{pr['number']} as {app.slug}")


def merge(a, gh, say, out=lambda k, v: None, make_app=App, sleep=time.sleep, clock=time.monotonic):
    pr = find_pr(gh, a)
    if pr and pr.get("merged_at"):
        out("PR_URL", pr["html_url"])
        return {"MERGED_SHA": pr.get("merge_commit_sha") or ""}
    if not pr:
        say(f"→  open a PR from {a.branch} into main")
        pr = must(gh.call("POST", f"/repos/{a.repo}/pulls", {"title": a.title, "head": a.branch, "base": "main",
                                                            "body": "The CRE template, adopted into this repo by setup."}),
                  f"Opening the PR for {a.branch}")
    n, url = pr["number"], pr["html_url"]
    out("PR_URL", url)
    say(f"ℹ  {url}")
    app, _ = the_app(gh, a, make_app)
    deadline, started, last, refused, approved = clock() + a.wait, clock(), "", "", False
    while True:
        p = must(gh.call("GET", f"/repos/{a.repo}/pulls/{n}"), f"Reading {url}")
        if p.get("merged_at"):
            return {"MERGED_SHA": p.get("merge_commit_sha") or ""}
        if p["state"] != "open":
            raise Failed(REFUSED, f"{url} is closed: reopen it, then re-run.")
        if p["head"]["sha"] != a.sha:
            head = must(gh.call("GET", f"/repos/{a.repo}/branches/{a.branch}"), f"Reading {a.branch}")["commit"]["sha"]
            if head != a.sha:
                raise Failed(REFUSED, f"{a.branch} is at {head[:9]}, not {a.sha[:9]}, the commit pushed: someone else pushed to "
                                      "it. Merge the PR yourself, or push again and re-run.")
        if p.get("mergeable_state") == "dirty":
            raise Failed(REFUSED, f"{url} conflicts with main: resolve it on the branch, push, then re-run.")
        if not approved:
            approve(gh, app, a, p, say)
            approved = True
        state, what = checks_on(gh, a.repo, a.sha)
        if state == "stopped":
            raise Failed(REFUSED, f"{url}: {what}. Nothing reached main. Fix it with a commit on the branch, push, and re-run.")
        if state == "none" and clock() - started >= NO_CHECKS_GRACE:
            raise Failed(REFUSED, f"{url} has no checks on {a.sha[:9]} {NO_CHECKS_GRACE}s after it opened: the build workflow "
                                  "didn't run for it. Start it, then re-run.")
        if state == "passed":
            r = gh.call("PUT", f"/repos/{a.repo}/pulls/{n}/merge", {"merge_method": "squash", "sha": a.sha})
            if r.ok and r.data.get("merged"):
                say(f"✓  merged {url} (its checks passed on {a.sha[:9]})")
                return {"MERGED_SHA": r.data.get("sha") or ""}
            if r.kind in ("creds",) or (r.kind == "refused" and r.status == 403):
                must(r, f"Merging {url}")
            if refused:
                # Refused again a poll later, with the checks passed and the App's approval in: only a
                # person clears that (a draft, a code-owner review, another required approval or check).
                raise Failed(REFUSED, f"GitHub won't merge {url} ({r.message}): a person clears what it needs, then re-run.")
            refused = r.message   # 405 right after the checks pass: GitHub may not have caught up; asked once more
        now = what + (f"; last merge answer: {refused}" if refused else "")
        if now != last:
            say(f"…  waiting for {url}: {now}")
            last = now
        if clock() >= deadline:
            raise Failed(FAILED, f"{url} didn't merge within {a.wait}s ({now}). A re-run waits again.")
        sleep(MERGE_POLL)


# ---- protect -------------------------------------------------------------------------------------

def protected_as_wanted(p):
    """Requires the build check and at least one approval, admins included, no force pushes or
    deletions. More (other checks, more approvals, code-owner review, push restrictions) is kept."""
    reviews = p.get("required_pull_request_reviews") or {}
    return ("build" in ((p.get("required_status_checks") or {}).get("contexts") or [])
            and (p.get("enforce_admins") or {}).get("enabled") is True
            and reviews.get("required_approving_review_count", 0) >= 1
            and not (p.get("allow_force_pushes") or {}).get("enabled") and not (p.get("allow_deletions") or {}).get("enabled"))


def protection_with_ours(p):
    """The PUT body: what main already requires, with ours added (GitHub's PUT replaces it all)."""
    have = p or {}
    checks = (have.get("required_status_checks") or {}).get("contexts") or []
    reviews = {k: v for k, v in (have.get("required_pull_request_reviews") or {}).items()
               if k in ("dismiss_stale_reviews", "require_code_owner_reviews", "require_last_push_approval",
                        "required_approving_review_count")}
    reviews["required_approving_review_count"] = max(reviews.get("required_approving_review_count", 0), 1)
    r = have.get("restrictions")
    return {**PROTECTION,
            "required_status_checks": {"strict": bool((have.get("required_status_checks") or {}).get("strict")),
                                       "contexts": sorted(set(checks) | {"build"})},
            "required_pull_request_reviews": reviews,
            "restrictions": None if not r else {"users": [u["login"] for u in r.get("users", [])],
                                                "teams": [t["slug"] for t in r.get("teams", [])],
                                                "apps": [x["slug"] for x in r.get("apps", [])]}}


def protect(a, gh, say, out=lambda k, v: None):
    r = gh.call("GET", f"/repos/{a.repo}/branches/main/protection")
    if r.ok and protected_as_wanted(r.data):
        return {"PROTECTED": "yes"}
    if UPGRADE_FOR_PROTECTION in r.message:
        say(f"ℹ  GitHub's free plan can't protect a private repo's main: PRs still merge only once their build passed (the build "
            "workflow merges them), but a direct push to main isn't blocked.")
        return {"PROTECTED": "no (free plan, private repo)"}
    if r.kind == "missing" and "Branch not found" in r.message:
        raise Failed(REFUSED, f"{a.repo} has no main yet: run protect after the first push.")
    say("→  protect main (a PR with the build check and one approval, admins included)")
    p = gh.call("PUT", f"/repos/{a.repo}/branches/main/protection", protection_with_ours(r.data if r.ok else None))
    if UPGRADE_FOR_PROTECTION in p.message:
        say("ℹ  GitHub's free plan can't protect a private repo's main: PRs still merge only once their build passed.")
        return {"PROTECTED": "no (free plan, private repo)"}
    must(p, "Protecting main")
    return {"PROTECTED": "yes"}


# ---- command line --------------------------------------------------------------------------------

def key_value(s):
    return any_key_value(s, refused_prefix="GITHUB_")   # GitHub reserves the GITHUB_ names


def full_sha(s):
    if not re.fullmatch(r"[0-9a-f]{40}", s):
        raise argparse.ArgumentTypeError(f"{s!r}: a full commit sha (40 hex)")
    return s


def positive(s):
    if not s.isdigit() or int(s) < 1:
        raise argparse.ArgumentTypeError(f"{s!r}: a whole number of seconds, at least 1")
    return int(s)


def parse(argv):
    ap = argparse.ArgumentParser(prog="github.py")
    sub = ap.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", required=True, action=SingleValue, help="<owner>/<name>")
    s = sub.add_parser("setup", parents=[common], help="create the repo if missing, and set it up")
    s.add_argument("--app-slug", required=True, action=SingleValue, help="the builder's GitHub App (aws-cre-automation-<alias>)")
    s.add_argument("--app-key-file", help="its private key (default ~/.cre/<slug>.pem)")
    s.add_argument("--wait-install", type=int, help="seconds to wait for the App's install (default 600; 0: no browser, no wait)")
    s.add_argument("--no-create", action="store_true", help="refuse (10) when the repo is missing, instead of creating it "
                   "(npx projen setup:repo always passes it)")
    v = sub.add_parser("variables", parents=[common], help="set Actions variables and secrets")
    v.add_argument("--environment", choices=(DEPLOY_ENVIRONMENT,), action=SingleValue,
                   help="in the deploy jobs' environment (a public repo)")
    v.add_argument("--var", type=key_value, action="append", default=[],
                   help="an Actions variable KEY=VALUE or KEY=@<file> (shown in logs; a noEcho parameter's: a secret)")
    v.add_argument("--secret", type=key_value, action="append", default=[], help="an Actions secret KEY=VALUE or KEY=@<file> (masked)")
    m = sub.add_parser("merge", parents=[common], help="merge a branch into main once its checks passed")
    m.add_argument("--app-slug", required=True)
    m.add_argument("--app-key-file")
    m.add_argument("--branch", required=True)
    m.add_argument("--sha", required=True, type=full_sha, help="the commit pushed to the branch (40 hex): only it is merged")
    m.add_argument("--title", required=True, help="the PR's title, when one is opened")
    m.add_argument("--wait", type=positive, default=MERGE_WAIT, help="seconds to wait for the checks")
    pb = sub.add_parser("publish", parents=[common], help="make a private repo public, with the AWS environment")
    pb.add_argument("--dir", required=True, help="the project's clone (its history is scanned first)")
    sub.add_parser("protect", parents=[common], help="protect main (after the first push or the adoption's merge)")
    a = ap.parse_args(argv)
    if len(a.repo.split("/")) != 2 or not all(a.repo.split("/")):
        ap.error(f"--repo {a.repo!r}: <owner>/<name>")
    if a.mode == "variables":
        keys = [k for k, _ in a.var + a.secret]
        for k in sorted({k for k in keys if keys.count(k) > 1 or k in ("PROJEN_APP_ID", "PROJEN_APP_PRIVATE_KEY")}):
            v.error(f"{k}: " + ("set by this script itself" if k.startswith("PROJEN_APP") else "given more than once"))
    return a


def main(argv=None, github_factory=GitHub):
    t0 = time.time()

    def say(msg):
        print(f"[{time.strftime('%H:%M:%S')} +{time.time() - t0:.1f}s] {msg}", file=sys.stderr, flush=True)

    def out(k, v):
        print(f"{k}={v}", flush=True)

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
        outputs = {"setup": setup, "variables": variables, "merge": merge, "publish": publish,
                   "protect": protect}[a.mode](a, github_factory(), say, out)
    except Failed as e:
        say(f"❌ {e}")
        return done(e.code)
    except Exception as e:  # noqa: BLE001: a bug is still 20, never a bare traceback
        say(f"❌ github.py failed: {type(e).__name__}: {e}")
        return done(FAILED)
    for k, v in outputs.items():
        out(k, v)
    say("✅ done")
    return done(OK)


if __name__ == "__main__":
    sys.exit(main())
