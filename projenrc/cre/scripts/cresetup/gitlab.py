#!/usr/bin/env python3
"""A CRE project's GitLab project on gitlab.aws.dev: set up, and a branch merged into main.

  gitlab.py setup --project <name> --namespace <group> --token-file <file> [--bindle <id>]
                  [--var KEY=VALUE]... [--deploy-var KEY=VALUE]...
  gitlab.py merge --project <name> --namespace <group> --token-file <file>
                  --branch <b> --sha <commit> --title <t> [--wait <s>]

setup: the project created if missing (--no-create: refused, 10) and set up either way:

1. The project: created through the Management Portal (private, owned by the bindle) if missing.
   An existing project's repository is left as it is (its history is the adoption step's;
   REPO_STATE says which); its variables, protection, settings, token and schedule below are set
   as for a new one.
2. CI/CD variables: --var for every job; --deploy-var protected and scoped to the deploy jobs'
   environment (`AWS`), so only deploy jobs on main see them. Set only where they differ. A key
   this sets lives in that one scope: a copy of it in any other scope is deleted. `KEY=@<file>`
   reads the value from a file. A key that feeds a noEcho stack parameter (cre-stacks.json and
   cdk.out in the current directory say which) is masked; GitLab refuses to mask some values (10).
3. main protected (no one pushes; developers merge), squash on merge, source branches deleted on
   merge, outdated deploy jobs skipped.
4. The weekly dependency upgrade: a project access token in CRE_UPGRADE_TOKEN (rotated within 30
   days of expiry) and the pipeline schedule that runs it with CRE_UPGRADE=1. A token GitLab
   refuses (403, or 400: tokens turned off for the group, a lifetime over the instance's limit)
   stops the run with 10. The old setup carried on without it (NEEDS=UPGRADE_TOKEN); dropped by
   decision, since a project without the token silently stops getting upgrades.

merge: the branch's MR into main merged once its pipeline passed on --sha, the commit the caller
pushed, and that commit only. The MR is the one for --sha (merged: done; closed: 10), else the open
one, else a new one. Polled every 15 s for up to --wait seconds (1800); the wait running out is 20.
10 for what only a person clears: a failed, canceled, skipped or manual pipeline, no pipeline at
all, an approval or conflict, a commit someone else pushed to the branch, a merge GitLab refuses
you. MR_URL is printed as soon as the MR is known; MERGED_SHA at the end.

Exit: 0 done; 10 refused (by the arguments, or by GitLab for your role: a re-run can't help);
11 credentials (Midway, or the token is expired, revoked or lacks the api scope); 20 a call failed
(a re-run continues). Last stdout line `RESULT code=<n>`; before it PROJECT_ID, REPO_SSH_URL,
REPO_WEB_URL, REPO_STATE (empty | history) and UPGRADE_TOKEN_EXPIRES (setup); MR_URL and
MERGED_SHA (merge).
"""
import argparse
import datetime
import os
import re
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gitlab_api import HOST, SSH, GitLab, enc, every, read_token  # noqa: E402
from noecho_variables import noecho_variables  # noqa: E402
from result import CREDS, FAILED, OK, REFUSED, Failed, must  # noqa: E402
from setup_args import SingleValue, key_value  # noqa: E402

DEFAULT_BINDLE = "amzn1.bindle.resource.v4o66fxvd3zfcqvcmhxq"
DEPLOY_ENVIRONMENT = "AWS"           # the environment every deploy job declares (gitlab-workflow.ts)
PROTECT = {"push": 0, "merge": 30}   # no one; developers (and above)
SETTINGS = {"squash_option": "always", "remove_source_branch_after_merge": True, "ci_forward_deployment_enabled": True}
UPGRADE_TOKEN, UPGRADE_VARIABLE = "cre-upgrade", "CRE_UPGRADE_TOKEN"
TOKEN_DAYS, ROTATE_DAYS = 364, 30
SCHEDULE, CRON = "CRE weekly dependency upgrade", "0 6 * * 1"
MERGE_WAIT, MERGE_POLL = 1800, 15
NO_PIPELINE_GRACE = 300   # seconds after GitLab has prepared the MR with no pipeline for the commit: none is coming
# GitLab's detailed_merge_status values only a person clears (docs.gitlab.com/api/merge_requests, "Merge status").
PERSON_BLOCKS = {"not_approved", "conflict", "need_rebase", "discussions_not_resolved", "draft_status", "requested_changes",
                 "merge_request_blocked", "jira_association_missing", "status_checks_must_pass", "commits_status",
                 "title_regex", "locked_paths", "locked_lfs_files", "security_policy_violations"}
STOPPED_PIPELINE = {"failed", "canceled", "skipped", "manual"}   # a pipeline that won't pass without a person


# ---- the steps -----------------------------------------------------------------------------------

def existing(gl, full):
    """The project at `full`, or None. A moved one is refused."""
    r = gl.call("GET", f"/projects/{enc(full)}")
    if r.kind == "missing":
        return None
    p = must(r, f"Reading project {full}")
    if p["path_with_namespace"].lower() != full.lower():
        # GitLab answers an old path with the moved project; the AWS trust names the path.
        raise Failed(REFUSED, f"GitLab project {full} is now {p['path_with_namespace']}: use that path, or move it back.")
    return p


def project(gl, full, namespace, name, bindle, say, create=True):
    """(project, created). An existing project is used; a missing one is created (create=False: refused)."""
    p = existing(gl, full)
    if p:
        return p, False
    if not create:
        raise Failed(REFUSED, f"GitLab project {full} isn't there (deleted, renamed or moved?). Setup in a project never "
                              "creates it again: a retired project doesn't come back by a routine re-run.")
    ns = gl.call("GET", f"/namespaces/{enc(namespace)}")
    if ns.kind == "missing":
        raise Failed(REFUSED, f"GitLab group {namespace} isn't there or you can't see it: {ns.message}")
    must(ns, f"Reading GitLab group {namespace}")
    say(f"→  create project {full} (private, bindle {bindle})")
    made = gl.portal("/api/public/gitlab/createproject", {
        "project_name": name, "project_path": name, "namespace_id": int(ns.data["id"]), "private": True,
        "init_with_readme": False, "parent_bindle_id": bindle})
    if not made.ok and "taken" in str(made.data).lower():
        raise Failed(REFUSED, f"The name {name} is taken in {namespace} by a project you can't see: pick another name.")
    must(made, f"Creating project {full}")
    p = gl.call("GET", f"/projects/{enc(full)}")
    return must(p, f"Reading the new project {full} (the portal answered {str(made.data)[:300]})"), True


def variables(gl, pid, wanted, say):
    """wanted: [(key, value, protected, masked, scope)]. Set each where it is missing or differs, and
    delete the key's copies in other scopes. Returns the variables as listed before any change."""
    have = {(v["key"], v["environment_scope"]): v for v in must(every(gl, f"/projects/{pid}/variables"),
                                                               "Listing CI/CD variables")}
    for key, value, protected, masked, scope in wanted:
        body = {"key": key, "value": value, "protected": protected, "masked": masked, "environment_scope": scope}
        if masked:
            # Never expanded: a `$` in a secret stays as it is, and GitLab then masks any characters (its docs).
            body["raw"] = True
        v = have.get((key, scope))
        if not (v and all(v.get(k) == body[k] for k in ("value", "protected", "masked")) and (not masked or v.get("raw"))):
            say(f"→  {'update' if v else 'set'} CI/CD variable {key}" + (f" ({scope})" if scope != "*" else ""))
            r = (gl.call("PUT", f"/projects/{pid}/variables/{enc(key)}?filter%5Benvironment_scope%5D={enc(scope)}", body) if v
                 else gl.call("POST", f"/projects/{pid}/variables", body))
            if masked and r.status == 400:
                # A re-run can't help: the value itself is what GitLab won't mask.
                raise Failed(REFUSED, f"GitLab can't mask {key}'s value ({r.message}). A masked value must be one line with "
                                      "no spaces, at least 8 characters, and not the name of another CI/CD variable.")
            must(r, key)
        # After the write, so a job starting between the two still finds the key.
        for k, other in have:
            if k == key and other != scope:
                say(f"→  delete CI/CD variable {key} ({other}): it belongs in {scope} only")
                must(gl.call("DELETE", f"/projects/{pid}/variables/{enc(key)}?filter%5Benvironment_scope%5D={enc(other)}"),
                     f"Deleting {key} ({other})")
    return have


def protect_main(gl, pid, say):
    r = gl.call("GET", f"/projects/{pid}/protected_branches/main")
    if r.ok:
        levels = ({x["access_level"] for x in r.data.get("push_access_levels", [])},
                  {x["access_level"] for x in r.data.get("merge_access_levels", [])})
        if levels == ({PROTECT["push"]}, {PROTECT["merge"]}):
            return
        # GitLab's free tier can't change the levels in place: unprotect, then protect again.
        say("→  re-protect main (no one pushes, developers merge)")
        must(gl.call("DELETE", f"/projects/{pid}/protected_branches/main"), "Unprotecting main")
    elif r.kind != "missing":
        must(r, "Reading main's protection")
    else:
        say("→  protect main (no one pushes, developers merge)")
    must(gl.call("POST", f"/projects/{pid}/protected_branches",
                 {"name": "main", "push_access_level": PROTECT["push"], "merge_access_level": PROTECT["merge"]}),
         "Protecting main (it is unprotected until this succeeds: re-run)")


def settings(gl, p, say):
    if all(p.get(k) == v for k, v in SETTINGS.items()):
        return
    say("→  set merge settings (squash, delete source branches, skip outdated deploy jobs)")
    must(gl.call("PUT", f"/projects/{p['id']}", SETTINGS), "Setting the project's merge settings")


def upgrade_token(gl, pid, have, say, today=None):
    """The weekly upgrade's push token. The one CRE_UPGRADE_TOKEN names (its description holds the
    token's id) is kept while it has more than ROTATE_DAYS left; every other live one is revoked
    first, so a store that keeps failing leaves at most one unused token. `have`: the variables."""
    today = today or datetime.date.today()
    ours = [t for t in must(every(gl, f"/projects/{pid}/access_tokens?state=active"), "Listing project access tokens")
            if t.get("name") == UPGRADE_TOKEN and not t.get("revoked")]
    var = have.get((UPGRADE_VARIABLE, "*"))
    held = [t for t in ours if var and var.get("description") == label(t)]

    def revoke(tokens):
        for t in tokens:
            say(f"→  revoke the old {UPGRADE_TOKEN} token {t['id']}")
            must(gl.call("DELETE", f"/projects/{pid}/access_tokens/{t['id']}"), f"Revoking old token {t['id']}")

    revoke([t for t in ours if t not in held])
    for t in held:
        if t.get("expires_at") and (datetime.date.fromisoformat(t["expires_at"]) - today).days > ROTATE_DAYS:
            return t["expires_at"]
    say(f"→  create project access token {UPGRADE_TOKEN} for the weekly upgrade")
    r = gl.call("POST", f"/projects/{pid}/access_tokens", {
        "name": UPGRADE_TOKEN, "scopes": ["write_repository"], "access_level": 30,
        "expires_at": (today + datetime.timedelta(days=TOKEN_DAYS)).isoformat()})
    if r.status == 400:
        raise Failed(REFUSED, f"GitLab refused the weekly upgrade's access token ({r.message}): project access tokens "
                              "may be turned off for the group, or 364 days may be over the instance's limit.")
    made = must(r, "Creating the upgrade's access token")
    body = {"key": UPGRADE_VARIABLE, "value": made["token"], "protected": True, "masked": True, "environment_scope": "*",
            "description": label(made)}
    if var:
        must(gl.call("PUT", f"/projects/{pid}/variables/{UPGRADE_VARIABLE}?filter%5Benvironment_scope%5D=%2A", body),
             f"Storing {UPGRADE_VARIABLE}")
    else:
        must(gl.call("POST", f"/projects/{pid}/variables", body), f"Storing {UPGRADE_VARIABLE}")
    revoke(held)
    return made["expires_at"]


def label(token):
    return f"{UPGRADE_TOKEN} id {token['id']}"


def upgrade_schedule(gl, pid, say):
    found = [s for s in must(every(gl, f"/projects/{pid}/pipeline_schedules"), "Listing pipeline schedules")
             if s.get("description") == SCHEDULE]
    if found:
        s = must(gl.call("GET", f"/projects/{pid}/pipeline_schedules/{found[0]['id']}"), "Reading the upgrade schedule")
    else:
        say(f"→  create pipeline schedule '{SCHEDULE}' (Mondays 06:00 UTC, main)")
        s = must(gl.call("POST", f"/projects/{pid}/pipeline_schedules", {
            # The full ref: GitLab calls a bare "main" ambiguous while the branch doesn't exist yet.
            "description": SCHEDULE, "ref": "refs/heads/main", "cron": CRON, "cron_timezone": "UTC", "active": True}),
            "Creating the upgrade schedule")
    if not any(v.get("key") == "CRE_UPGRADE" for v in s.get("variables", [])):
        must(gl.call("POST", f"/projects/{pid}/pipeline_schedules/{s['id']}/variables",
                     {"key": "CRE_UPGRADE", "value": "1", "variable_type": "env_var"}), "Setting CRE_UPGRADE=1 on the schedule")


def merged_sha(m):
    return m.get("merge_commit_sha") or m.get("squash_commit_sha") or ""


def find_mr(gl, pid, a):
    """The MR from a.branch into main this run continues: the one for a.sha (merged, closed or open),
    else the open one, else None. A fork's branch of the same name is never it."""
    mrs = [m for m in must(every(gl, f"/projects/{pid}/merge_requests?state=all&source_branch={enc(a.branch)}"
                                     "&target_branch=main"), "Listing the branch's MRs")
           if m.get("source_project_id", pid) == pid]
    return next((m for m in mrs if m.get("sha") == a.sha), None) or next((m for m in mrs if m.get("state") == "opened"), None)


def merge(a, gl, say, out=lambda k, v: None, sleep=time.sleep, clock=time.monotonic):
    """`out(key, value)` is called with MR_URL as soon as it is known, so a stop still names the MR."""
    full = f"{a.namespace}/{a.project}"
    p = existing(gl, full)
    if not p:
        raise Failed(REFUSED, f"GitLab project {full} doesn't exist.")
    pid = p["id"]
    mr = find_mr(gl, pid, a)
    if not mr:
        say(f"→  open an MR from {a.branch} into main")
        mr = must(gl.call("POST", f"/projects/{pid}/merge_requests", {
            "source_branch": a.branch, "target_branch": "main", "title": a.title, "remove_source_branch": True}),
            f"Opening the MR for {a.branch}")
    iid, url = mr["iid"], mr.get("web_url") or f"{full}!{mr['iid']}"
    out("MR_URL", url)
    say(f"ℹ  {url}")
    deadline, last, refused, prepared = clock() + a.wait, "", "", None
    while True:
        m = must(gl.call("GET", f"/projects/{pid}/merge_requests/{iid}"), f"Reading {url}")
        if m.get("state") == "merged":
            return {"MERGED_SHA": merged_sha(m)}
        if m.get("state") != "opened":
            raise Failed(REFUSED, f"{url} is {m.get('state')}: reopen it, then re-run.")
        if m.get("sha") and m["sha"] != a.sha:
            # GitLab updates the MR's head from the push in the background: the branch itself says.
            head = must(gl.call("GET", f"/projects/{pid}/repository/branches/{enc(a.branch)}"), f"Reading {a.branch}")
            if (head.get("commit") or {}).get("id") != a.sha:
                raise Failed(REFUSED, f"{a.branch} is at {head.get('commit', {}).get('id', '?')[:9]}, not {a.sha[:9]}, the commit "
                                      "pushed: someone else pushed to it. Merge the MR yourself, or push again and re-run.")
        pipe = m.get("head_pipeline") or {}
        status = pipe.get("status", "") if pipe.get("sha") == a.sha else ""
        why = m.get("detailed_merge_status", "")
        if status in STOPPED_PIPELINE:
            raise Failed(REFUSED, f"{url}'s pipeline is {status} ({pipe.get('web_url', '')}): nothing reached main. "
                                  + ("Fix it with a commit on the branch, push, and re-run." if status == "failed" else
                                     "A person starts or re-runs it; once it passes, re-run."))
        if why in PERSON_BLOCKS:
            raise Failed(REFUSED, f"{url} needs a person: GitLab says {why}. Once it is cleared, re-run.")
        if not status and m.get("prepared_at"):
            prepared = prepared or clock()
            if clock() - prepared >= NO_PIPELINE_GRACE:
                raise Failed(REFUSED, f"{url} has no pipeline for {a.sha[:9]} {NO_PIPELINE_GRACE}s after GitLab prepared it: "
                                      "the project's CI didn't run for it. Start one, then re-run.")
        if status == "success":
            # That commit only: GitLab refuses (409) a merge whose sha isn't the branch's head.
            r = gl.call("PUT", f"/projects/{pid}/merge_requests/{iid}/merge", {"sha": a.sha, "should_remove_source_branch": True})
            if r.ok:
                say(f"✓  merged {url} (its pipeline passed on {a.sha[:9]})")
                return {"MERGED_SHA": merged_sha(r.data)}
            if r.status == 401:   # the token works (every read did): this user may not merge into main
                raise Failed(REFUSED, f"GitLab won't let you merge {url} into main ({r.message}).")
            if r.kind in ("creds", "refused"):
                must(r, f"Merging {url}")
            refused = r.message   # 405/406/409/422: GitLab hasn't caught up yet, or won't; asked again on the next poll
        now = f"pipeline {status or 'not started'}, {why or 'checking'}" + (f"; last merge answer: {refused}" if refused else "")
        if now != last:
            say(f"…  waiting for {url}: {now}")
            last = now
        if clock() >= deadline:
            raise Failed(FAILED, f"{url} didn't merge within {a.wait}s ({now}). A re-run waits again.")
        sleep(MERGE_POLL)


def setup(a, gl, say):
    # A noEcho parameter's value is masked, whichever switch set it. Read first: a cdk.out that can't
    # say is refused before anything changes.
    hidden = noecho_variables({k for k, _ in a.var + a.deploy_var})
    full = f"{a.namespace}/{a.project}"
    p, created = project(gl, full, a.namespace, a.project, a.bindle or DEFAULT_BINDLE, say, create=not a.no_create)
    pid = p["id"]
    state = "empty" if p.get("empty_repo") else "history"
    say(f"ℹ  project {full} (id {pid}, {'created' if created else 'existing'}, {state})")
    for k in sorted(hidden):
        say(f"ℹ  {k} feeds a noEcho parameter: stored masked")
    have = variables(gl, pid, [(k, v, False, k in hidden, "*") for k, v in a.var] +
                     [(k, v, True, k in hidden, DEPLOY_ENVIRONMENT) for k, v in a.deploy_var], say)
    protect_main(gl, pid, say)
    settings(gl, p, say)
    expires = upgrade_token(gl, pid, have, say)
    upgrade_schedule(gl, pid, say)
    return {"PROJECT_ID": pid, "REPO_SSH_URL": f"{SSH}:{full}.git", "REPO_WEB_URL": f"https://{HOST}/{full}",
            "REPO_STATE": state, "UPGRADE_TOKEN_EXPIRES": expires}


# ---- command line --------------------------------------------------------------------------------

def full_sha(s):
    if not re.fullmatch(r"[0-9a-f]{40}", s):
        raise argparse.ArgumentTypeError(f"{s!r}: a full commit sha (40 hex)")
    return s


def positive(s):
    if not s.isdigit() or int(s) < 1:
        raise argparse.ArgumentTypeError(f"{s!r}: a whole number of seconds, at least 1")
    return int(s)


def parse(argv):
    ap = argparse.ArgumentParser(prog="gitlab.py")
    sub = ap.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", required=True, action=SingleValue)
    common.add_argument("--namespace", required=True, action=SingleValue, help="GitLab group path")
    common.add_argument("--token-file", required=True, action=SingleValue)
    s = sub.add_parser("setup", parents=[common], help="create the project if missing, and set it up")
    s.add_argument("--bindle", help=f"the owning bindle for a new project (default {DEFAULT_BINDLE})")
    s.add_argument("--no-create", action="store_true", help="refuse (10) when the project is missing, instead of creating it "
                   "(npx projen setup:repo always passes it)")
    s.add_argument("--var", type=key_value, action="append", default=[], help="KEY=VALUE or KEY=@<file> for every job")
    s.add_argument("--deploy-var", type=key_value, action="append", default=[],
                   help="KEY=VALUE or KEY=@<file> for deploy jobs on main only (a noEcho parameter's: masked)")
    m = sub.add_parser("merge", parents=[common], help="merge a branch into main once its pipeline passed")
    m.add_argument("--branch", required=True)
    m.add_argument("--sha", required=True, type=full_sha, help="the commit pushed to the branch (40 hex): only it is merged")
    m.add_argument("--title", required=True, help="the MR's title, when one is opened")
    m.add_argument("--wait", type=positive, default=MERGE_WAIT, help="seconds to wait for the pipeline")
    a = ap.parse_args(argv)
    if a.mode == "setup":
        if a.no_create and a.bindle:
            s.error("--bindle names the owner of a project setup creates: --no-create never creates one")
        keys = [k for k, _ in a.var + a.deploy_var]
        for k in sorted({k for k in keys if keys.count(k) > 1 or k == UPGRADE_VARIABLE}):
            s.error(f"{k}: " + ("set by this script itself" if k == UPGRADE_VARIABLE else "given more than once"))
    return a


def main(argv=None, gitlab_factory=GitLab):
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
    token, problem = read_token(a.token_file)
    if problem:
        say(f"❌ {problem}")
        return done(CREDS)
    try:
        gl = gitlab_factory(token)
        outputs = setup(a, gl, say) if a.mode == "setup" else merge(a, gl, say, out=lambda k, v: print(f"{k}={v}", flush=True))
    except Failed as e:
        say(f"❌ {e}")
        return done(e.code)
    except Exception as e:  # noqa: BLE001: a bug is still 20, never a bare traceback
        say(f"❌ gitlab.py failed: {type(e).__name__}: {e}")
        return done(FAILED)
    for k, v in outputs.items():
        print(f"{k}={v}")
    say("✅ done")
    return done(OK)


if __name__ == "__main__":
    sys.exit(main())
