#!/usr/bin/env python3
"""Create or adopt the GitLab project for a CRE demo, using Midway credentials.

  repo_gitlab.py prepare  --namespace <group/path> --project <name> [--var KEY=VALUE[:protected][:masked]]...
                          [--bindle <owning team bindle id>]   (default: the aws-cre bindle)
  repo_gitlab.py finalize --namespace <group/path> --project <name> --dir <demo-dir> [--message <msg>]

In a project (npx projen setup:repo), --namespace, --project and --dir default to cre-setup.json
and its directory.

prepare (before the scaffold exists):
  - Checks the Midway cookie and SSH certificate (exit 11 with the refresh command).
  - Gets an `api` token for the REST calls (sent with the Midway cookie): one minted for a day over
    the Midway-signed SSH session and revoked on exit, when GitLab allows it (it refuses Midway
    certificate users today); otherwise the recorded token file (the project's .cre/gitlab-token,
    the template's record), and $GITLAB_TOKEN only when none is recorded. Exit 11 says how to
    create one.
  - Adopts the project if it already exists (made by hand in the UI), otherwise creates it private,
    without a README, through the GitLab Management Portal API with an owning team Bindle
    (gitlab.aws.dev refuses GitLab's own create-project API).
  - Upserts the CI/CD variables (`:protected` marks one Protected: it reaches only protected-branch
    pipelines). That alone doesn't keep the runner role from other branches, since any branch's CI
    file can name the role ARN itself: the runner role's trust (aws_account.py) also requires the
    vendor's `GitLab:Project:ProtectedBranch = Yes` tag.
  Prints REPO_SSH_URL, REPO_WEB_URL, PROJECT_ID.

finalize (after the scaffold is green, in its directory):
  - Sets origin to the SSH URL, commits the scaffold if it has no commit yet, and pushes main as the
    first commit, into an empty project only: one with any commit is adopted by new_project.py
    (adopt.py, through an MR), and finalize refuses it (10).
  - Then protects main (push: no one, merge: developers), requires squash, and deletes source
    branches on merge. Protection is applied after the first push so it can't reject it.
  - The weekly dependency upgrade: a project access token `cre-upgrade` (write_repository,
    Developer, 364 days; rotated by a finalize run once it has under 30 days left) stored as the
    masked, Protected variable CRE_UPGRADE_TOKEN, and a pipeline schedule (Mondays 06:00 UTC, main,
    CRE_UPGRADE=1) that runs the pipeline's `upgrade:main` job. When GitLab refuses project access
    tokens (a group setting, or a Developer on an adopted project): neither, and NEEDS=UPGRADE_TOKEN (13).
"""
import os
import re
import time
import sys

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402

NAME = "repo-gitlab"
DEFAULT_BINDLE = c.DEFAULT_BINDLE
PROTECT = {"name": "main", "push_access_level": 0, "merge_access_level": 30}


def parse(step, argv):
    p = c.Parser(step, "repo_gitlab.py")
    p.add_argument("mode", choices=["prepare", "finalize"],
                   help="prepare: create or adopt the project and set CI/CD variables; finalize: push main and protect it")
    p.add_argument("--namespace", default="", help="GitLab group path, e.g. aws-cre/resilience-demos (default: cre-setup.json)")
    p.add_argument("--project", default="", help="GitLab project name (default: cre-setup.json)")
    c.repo_flags(p, "project")
    p.add_argument("--var", action="append", default=[],
                   help="prepare: a CI/CD variable KEY=VALUE[:protected][:masked] (repeatable); VALUE @<file> reads a file")
    p.add_argument("--bindle", default=os.environ.get("CRE_GITLAB_BINDLE", DEFAULT_BINDLE),
                   help="prepare: the owning team's Bindle id for a new project (default: $CRE_GITLAB_BINDLE, else the aws-cre bindle)")
    c.common_flags(p)
    a = p.parse_args(argv)
    step.dry_run = a.dry_run
    c.take_repo_decisions(step, a)
    sf = c.SetupFile(step)
    sf.default(a, "namespace", "namespace")
    sf.default(a, "project", "project")
    if not a.dir and sf.dir:
        a.dir = sf.dir
    sf.report()
    a.setup_file = sf
    if not a.namespace or not a.project:
        step.die(c.REFUSED, "--namespace and --project are required.")
    if a.mode != "prepare" and a.var:
        # Only prepare sets variables; finalize took --var and ignored it (review 10c F10).
        step.die(c.REFUSED, "--var is for setup:repo prepare; finalize doesn't set variables.")
    if a.mode != "prepare" and (a.no_create or a.recreate):
        step.die(c.REFUSED, "--no-create and --recreate are for prepare (the only mode that creates the project).")
    if a.no_create and a.recreate:
        step.die(c.REFUSED, "--no-create and --recreate contradict each other.")
    # After this clone's first push, a missing project was deleted (or renamed) since: prepare refuses
    # to re-create it unless --recreate says so, in a project's own setup:repo as in new_project.py
    # (review 10f C1: 10e C5 covered only new_project's call).
    if a.mode == "prepare" and not a.recreate and c.pushed_before(a.dir):
        a.no_create = True
    a.vars = [c.parse_var(step, v) for v in a.var]
    a.full = f"{a.namespace}/{a.project}"
    a.ssh_url = f"{c.GITLAB_SSH}:{a.full}.git"
    return a


def project_id(step, gl, full):
    """The project's id, or "" when it doesn't exist."""
    status, body = gl.call("GET", f"/projects/{c.urlenc(full)}")
    if status == "200":
        # GitLab answers an old path with the moved project: refuse rather than adopt it under the
        # path the runner trust would name (review 10c C10).
        now = body.get("path_with_namespace") or full
        if now.lower() != full.lower():
            step.die(c.REFUSED, f"GitLab project {full} is now {now} (renamed or moved). Setup names the project in the AWS trust, "
                     f"so it won't adopt it under the old path: move it back to {full}, or start a new project for {now}.")
        return str(body.get("id", ""))
    if status == "404":
        return ""
    if status in ("401", "403"):
        step.die(c.CREDENTIALS, f"GitLab rejected the API call ({status}). Refresh Midway ({c.MWINIT_HINT}); if that "
                 "doesn't fix it, the stored token is expired or lacks the 'api' scope. Re-run after fixing.")
    step.die(c.FAILED, f"GET /projects/{full} returned {status}: {body}")


# The environment every deploy job declares (gitlab-workflow.ts deployJob). A deploy variable scoped
# to it reaches only those jobs: the GitLab runner vends AWS credentials to every job that can see
# AWS_CREDS_TARGET_ROLE, and the build and the weekly `projen upgrade` (third-party code, freshly
# resolved) run on protected main too (review 10b D4).
DEPLOY_ENVIRONMENT = "AWS"


def set_variable(step, gl, pid, key, value, protected, masked=False, scope="*", description=None):
    """Create or update a project CI/CD variable in one environment scope. A copy of the same key in
    the other scope (from before scopes were set) is moved, never left visible to every job."""
    label = f"{key}{' (protected)' if protected else ''}{' (masked)' if masked else ''}{f' (environment {scope})' if scope != '*' else ''}"
    if step.dry_run:
        step.plan(f"(dry run) set CI/CD variable {label}")
        return
    # raw: GitLab expands `$NAME` inside a value otherwise, mangling a secret with a `$` in it
    # (review 10b D19). Nothing relies on expansion: the pipeline escapes its own literals.
    body = {"key": key, "value": value, "protected": protected, "masked": masked, "raw": True,
            "environment_scope": scope, "variable_type": "env_var", **({"description": description} if description else {})}
    at = lambda sc: f"/projects/{pid}/variables/{key}?filter%5Benvironment_scope%5D={c.urlenc(sc)}"  # noqa: E731
    status, _ = gl.call("GET", at(scope))
    if status != "200" and scope != "*":
        moved, _ = gl.call("GET", at("*"))
        if moved == "200":
            status, resp = gl.call("PUT", at("*"), body)
            scope_note = " (moved from all environments)"
        else:
            status, resp = gl.call("POST", f"/projects/{pid}/variables", body)
            scope_note = ""
    elif status == "200":
        status, resp = gl.call("PUT", at(scope), body)
        scope_note = ""
    else:
        status, resp = gl.call("POST", f"/projects/{pid}/variables", body)
        scope_note = ""
    if status not in ("200", "201"):
        # The response may echo the value: report only GitLab's message.
        msg = resp.get("message") if isinstance(resp, dict) else ""
        step.die(c.FAILED, f"Setting CI/CD variable {key} failed ({status}): {msg}")
    step.ok(f"CI/CD variable {label} set{scope_note}.")


def variable_scope(key, protected):
    """Protected deploy variables (role ARNs, masked secrets) go to the deploy environment only;
    plain values, which the build may read, to every job."""
    return DEPLOY_ENVIRONMENT if protected else "*"


# ---- the weekly dependency upgrade -----------------------------------------------------------
# The pipeline's `upgrade:main` job (gitlab-workflow.ts) runs in a scheduled pipeline and pushes the
# upgrade to a branch with push options that open an MR set to merge when its pipeline passes. A
# job's own CI_JOB_TOKEN can't push, so it uses a project access token: write_repository only, this
# project only, Developer (to push a branch and merge to main, which merge = Developers allows), a
# year at most (GitLab's limit). Rotated by any finalize run once it has less than ROTATE_DAYS left.

UPGRADE_TOKEN_NAME = "cre-upgrade"
UPGRADE_VARIABLE = "CRE_UPGRADE_TOKEN"
UPGRADE_SCHEDULE = "CRE weekly dependency upgrade"
UPGRADE_CRON = "0 6 * * 1"    # Mondays 06:00 UTC
TOKEN_DAYS, ROTATE_DAYS = 364, 30


def token_label(t):
    """What the variable's description records about the token it holds (never the token)."""
    return f"{UPGRADE_TOKEN_NAME} token id {t.get('id')}"


def upgrade_token_refused(step, status, body, doing):
    """GitLab refuses project access tokens here (400: a group setting disables them; 403: a
    Developer, not a Maintainer, on an adopted project). Nothing the builder can fix from setup, and
    the weekly upgrade is optional: NEEDS=UPGRADE_TOKEN (13), never a 20 on every run (review 10d C3)."""
    msg = body.get("message") if isinstance(body, dict) else body
    step.warn(f"GitLab refused {doing} ({status}: {msg}), so the weekly dependency upgrade is not set up; everything else is. "
              "Project access tokens need the Maintainer role on the project, and a group can disable them (the group's "
              "Settings > General > Permissions). Once a Maintainer or the group owner allows them, re-run.")
    step.need("UPGRADE_TOKEN")
    return False


def record_expiry(step, a, expires, pushed):
    """Record the upgrade token's expiry in the demo's cre-setup.json, which check:deploy-prereqs warns
    from 30 days ahead (10c C9). Before the first push it is written into the file that push commits;
    new_project.py's steps have no cre-setup.json of their own (CRE_SETUP_FILE=/dev/null), so no
    project it made had the key, and the warning never fired (review 10f C3). After the push the file
    is on main: say to commit the change through an MR."""
    path = os.path.join(a.dir, "cre-setup.json") if a.dir else ""
    if not expires or step.dry_run or not path or not os.path.isfile(path):
        return
    try:
        data = c.read_json(path)
    except Exception:
        return
    if not isinstance(data, dict) or data.get("upgradeTokenExpires") == expires:
        return
    data["upgradeTokenExpires"] = expires
    c.write_json(path, data, sort_keys=False)
    if pushed:
        step.warn(f"Recorded the new upgrade token's expiry ({expires}) in cre-setup.json. Commit it through an MR: "
                  "npx projen branch chore/upgrade-token && npx projen push")


def ensure_upgrade_token(step, gl, pid, a=None, pushed=True):
    """True when the upgrade's token is set (or a dry run); False after NEEDS=UPGRADE_TOKEN."""
    import datetime
    today = datetime.date.today()
    status, tokens = gl.call("GET", f"/projects/{pid}/access_tokens?state=active&per_page=100")
    if status in ("400", "403"):
        return upgrade_token_refused(step, status, tokens, "listing the project's access tokens")
    if status != "200" or not isinstance(tokens, list):
        step.die(c.FAILED, f"Listing project access tokens failed ({status}): {tokens}")
    ours = [t for t in tokens if t.get("name") == UPGRADE_TOKEN_NAME and t.get("active", True) and not t.get("revoked")]
    var_status, var = gl.call("GET", f"/projects/{pid}/variables/{UPGRADE_VARIABLE}")
    # The variable's description names the token it holds: a rotation cut off after the new token
    # was made but before it was stored left a fresh token next to the old value, which read as
    # "set" until the old one expired (review 10b C5).
    holds = (var or {}).get("description", "") if var_status == "200" and isinstance(var, dict) else ""

    def days_left(t):
        try:
            return (datetime.date.fromisoformat(t.get("expires_at") or "") - today).days
        except ValueError:
            return 0
    fresh = [t for t in ours if days_left(t) > ROTATE_DAYS]
    stored = [t for t in fresh if holds == token_label(t)]
    if stored and len(ours) == 1:
        step.ok(f"The upgrade's project access token is set ({days_left(stored[0])} days left).")
        step.out("UPGRADE_TOKEN_EXPIRES", stored[0].get("expires_at", ""))
        if a is not None:
            record_expiry(step, a, stored[0].get("expires_at", ""), pushed)
        return True
    why = ("none yet" if not ours else "expiring soon" if not fresh else f"the {UPGRADE_VARIABLE} variable is missing"
           if var_status != "200" else "the variable doesn't hold the current token")
    if step.dry_run:
        step.plan(f"(dry run) create project access token {UPGRADE_TOKEN_NAME} ({why}) and store it as {UPGRADE_VARIABLE}")
        return True
    step.plan(f"create project access token {UPGRADE_TOKEN_NAME} (write_repository, Developer, {TOKEN_DAYS} days; {why})")
    status, made = gl.call("POST", f"/projects/{pid}/access_tokens", {
        "name": UPGRADE_TOKEN_NAME, "scopes": ["write_repository"], "access_level": 30,
        "expires_at": (today + datetime.timedelta(days=TOKEN_DAYS)).isoformat()})
    if status in ("400", "403"):
        return upgrade_token_refused(step, status, made, "creating the project access token")
    if status != "201" or not isinstance(made, dict) or not made.get("token"):
        msg = made.get("message") if isinstance(made, dict) else made
        step.die(c.FAILED, f"Creating the project access token failed ({status}): {msg}. The weekly upgrade can't push without it.")
    set_variable(step, gl, pid, UPGRADE_VARIABLE, made["token"], protected=True, masked=True, description=token_label(made))
    # The new token is stored: the ones it replaces go.
    for t in ours:
        st, _ = gl.call("DELETE", f"/projects/{pid}/access_tokens/{t['id']}")
        if st not in ("200", "204", "404"):
            # Left active, it makes len(ours) > 1 on every later run, which minted one more token each
            # finalize (review 10c C9): say which one to revoke by hand.
            step.warn(f"Revoking the old {UPGRADE_TOKEN_NAME} token (id {t['id']}) failed ({st}). Revoke it in the project's "
                      "Settings → Access tokens; until then each finalize makes a new one.")
    step.ok(f"Upgrade token created (expires {made.get('expires_at')}); a finalize run within {ROTATE_DAYS} days of that rotates it.")
    step.out("UPGRADE_TOKEN_EXPIRES", made.get("expires_at", ""))
    if a is not None:
        record_expiry(step, a, made.get("expires_at", ""), pushed)
    return True


def ensure_upgrade_schedule(step, gl, pid):
    status, schedules = gl.call("GET", f"/projects/{pid}/pipeline_schedules?per_page=100")
    if status != "200" or not isinstance(schedules, list):
        step.die(c.FAILED, f"Listing pipeline schedules failed ({status}): {schedules}")
    if any(s.get("description") == UPGRADE_SCHEDULE for s in schedules):
        step.ok(f"Pipeline schedule '{UPGRADE_SCHEDULE}' exists.")
        return
    if step.dry_run:
        step.plan(f"(dry run) create pipeline schedule '{UPGRADE_SCHEDULE}' ({UPGRADE_CRON} UTC, main)")
        return
    step.plan(f"create pipeline schedule '{UPGRADE_SCHEDULE}' ({UPGRADE_CRON} UTC, main)")
    status, made = gl.call("POST", f"/projects/{pid}/pipeline_schedules", {
        "description": UPGRADE_SCHEDULE, "ref": "main", "cron": UPGRADE_CRON, "cron_timezone": "UTC", "active": True})
    if status != "201" or not isinstance(made, dict) or "id" not in made:
        step.die(c.FAILED, f"Creating the pipeline schedule failed ({status}): {made}")
    status, resp = gl.call("POST", f"/projects/{pid}/pipeline_schedules/{made['id']}/variables",
                           {"key": "CRE_UPGRADE", "value": "1", "variable_type": "env_var"})
    if status != "201":
        step.die(c.FAILED, f"Setting CRE_UPGRADE=1 on the pipeline schedule failed ({status}): {resp}")
    step.ok(f"Pipeline schedule '{UPGRADE_SCHEDULE}' created: the weekly upgrade runs Mondays 06:00 UTC.")
    step.out("UPGRADE_SCHEDULE_ID", made["id"])


def history(step, gl, pid):
    """(empty | trivial | history, last author, summary) of an existing project, read through the
    API. summary: its files, shown with the adopt decision."""
    status, d = gl.call("GET", f"/projects/{pid}/repository/commits?per_page=2")
    if status == "404" or (status == "200" and d == []):
        return "empty", "", ""
    if status != "200" or not isinstance(d, list):
        step.die(c.FAILED, f"Reading the project's history failed ({status}): {d}")
    who = d[0].get("author_name", "") if d else ""
    status, tree = gl.call("GET", f"/projects/{pid}/repository/tree?recursive=true&per_page=100")
    files = [t.get("path", "") for t in tree if t.get("type") == "blob"] if status == "200" and isinstance(tree, list) else []
    if len(d) == 1 and status == "200" and all(c.TRIVIAL_FILE.fullmatch(f) for f in files):
        return "trivial", who, ""
    return "history", who, c.history_summary(len(d) > 1, who, files)


def prepare(step, gl, a):
    pid = project_id(step, gl, a.full)
    if pid:
        kind, who, summary = history(step, gl, pid)
        c.refuse_foreign(step, f"GitLab project {a.full}", kind, who, a.dir, a.full, summary)
        if step.discovery and kind != "empty" and step.outputs.get("REPO_STATE") != "own":
            # A repo setup didn't make: its own variables (one setup made has only setup's).
            status, vs = gl.call("GET", f"/projects/{pid}/variables?per_page=100")
            ok = status == "200" and isinstance(vs, list)
            c.ci_variables_check(step, f"GitLab project {a.full}", a.full, [v.get("key", "") for v in vs] if ok else [],
                                 "" if ok else f"{status}: reading them needs the Maintainer role")
        if not step.discovery:
            step.ok(f"GitLab project {a.full} exists (id {pid}); adopting it.")
    if not pid:
        step.check("adoptExistingRepo", a.full, "not-needed", f"GitLab project {a.full} doesn't exist: setup creates it")
    if step.discovery:
        step.ok(f"Discovery for {a.full}: {len(step.found)} decision(s); nothing was changed.")
        return
    if not pid and getattr(a, "no_create", False):
        # Pushed by an earlier run and gone now: a retirement as likely as an accident (review 10e C5).
        step.die(c.REFUSED, f"GitLab project {a.full} doesn't exist, but an earlier run of these answers pushed to it, so it was "
                 "deleted (or moved) since. If that was on purpose (the project is retired), stop here. To re-create it from "
                 "this clone's main and set it up again: in the project, npx projen setup:repo prepare --recreate, then setup:repo finalize; "
                 "from the template, new_project.py --recreate.")
    elif not pid:
        status, ns = gl.call("GET", f"/namespaces/{c.urlenc(a.namespace)}")
        if status != "200" or not str(ns.get("id", "")).isdigit():
            step.die(c.REFUSED, f"GitLab namespace '{a.namespace}' not found or not visible to you ({status}) with {gl.token_source or 'the token'}.")
        # gitlab.aws.dev disables GitLab's own POST /projects ("use the Amazon custom API"): projects
        # are created through the Management Portal API, which records the owning team Bindle.
        if step.dry_run:
            step.plan(f"(dry run) create private project {a.full} (owning bindle {a.bindle})")
            pid = "dry-run"
        else:
            step.plan(f"create private project {a.full} (owning bindle {a.bindle})")
            status, raw = gl.manage("POST", "/api/public/gitlab/createproject", {
                "project_name": a.project, "project_path": a.project, "namespace_id": int(ns["id"]),
                "private": True, "init_with_readme": False, "parent_bindle_id": a.bindle})
            if status not in ("200", "201") and re.search(r"already (been )?taken|already exists", raw or "", re.I):
                # A project of that path the builder can't see (private, someone else's): a name
                # clash is a 10, as the recipe says, not a 20 to retry (review 10d C11).
                step.die(c.REFUSED, f"The name {a.project} is already taken in {a.namespace} by a project you can't see "
                         f"({status}: {raw[:300]}). Pick another name.")
            if status not in ("200", "201"):
                step.die(c.FAILED, f"Creating {a.full} via {c.MANAGE_API} failed ({status}): {raw[:500]}")
            # The portal's response shape isn't documented, so read the id back from GitLab.
            pid = project_id(step, gl, a.full)
            if not pid:
                step.die(c.FAILED, f"The portal reported success but GitLab has no project {a.full} yet: {raw[:500]}")
            step.ok(f"Created {a.full} (id {pid}), owned by bindle {a.bindle}.")
    for key, value, protected, masked in a.vars:
        set_variable(step, gl, pid, key, value, protected, masked, scope=variable_scope(key, protected))
    if a.vars and not step.dry_run:
        c.record_ci_variables(a.setup_file, [v[0] for v in a.vars], [v[0] for v in a.vars if v[3]])
        if a.setup_file.dir:
            c.record_deploy_env(a.setup_file.dir, {k: val for k, val, _, masked in a.vars if not masked})
    step.out("PROJECT_ID", pid)
    step.out("REPO_SSH_URL", a.ssh_url)
    step.out("REPO_WEB_URL", f"https://{c.GITLAB_HOST}/{a.full}")


def protect_main(step, gl, pid):
    status, body = gl.call("GET", f"/projects/{pid}/protected_branches/main")
    if status != "200":
        status, body = gl.call("POST", f"/projects/{pid}/protected_branches", PROTECT)
        if status == "201":
            step.ok("Protected main (push: no one, merge: developers).")
            return
        if status != "409":
            step.die(c.FAILED, f"Protecting main failed ({status}): {body}")
        # GitLab protects the default branch itself right after the first push, so the GET above can
        # race it: read the protection GitLab made and judge it below.
        status, body = gl.call("GET", f"/projects/{pid}/protected_branches/main")
        if status != "200":
            step.die(c.FAILED, f"Reading main's protection failed ({status}): {body}")
    levels = [str(x.get("access_level")) for x in body.get("push_access_levels", [])]
    if levels == ["0"]:
        step.ok("main is protected (push: no one).")
        return
    # GitLab's own protection after the first push lets Maintainers push. Every change reaches main
    # through an MR (it deploys when the MR merges), so replace it: push no one, merge developers.
    # Protected branches can't be edited in place on this tier: unprotect, reprotect.
    status, body = gl.call("DELETE", f"/projects/{pid}/protected_branches/main")
    if status not in ("204", "404"):
        step.die(c.FAILED, f"Unprotecting main to tighten it failed ({status}): {body}")
    unprotected = "main is UNPROTECTED (its protection was removed to tighten it): anyone with Developer can push to it, and " \
                  "its pipelines get no protected variables. Re-run finalize now."
    try:
        status, body = gl.call("POST", f"/projects/{pid}/protected_branches", PROTECT)
    except c.Exit:
        # A Midway lapse (lapse_fatal) ends the step inside the call: the window must still be named
        # (review 10e C4).
        step.warn(unprotected)
        raise
    if status != "201":
        step.die(c.FAILED, f"Protecting main failed ({status}): {body}. {unprotected}")
    step.ok(f"Protected main (push: no one, merge: developers), replacing GitLab's default (levels {','.join(levels)}).")


# GitLab's detailed_merge_status values that only a person clears: waiting won't merge the MR.
PERSON_BLOCKS = {"not_approved", "conflict", "need_rebase", "discussions_not_resolved", "draft_status", "requested_changes",
                 "merge_request_blocked", "jira_association_missing", "not_open", "external_status_checks"}


def adopt_main(step, argv):
    """new_project.py's review step for an adoption (adopt.py; not a setup:repo mode): push
    ADOPT_BRANCH with a lease, find or open its MR, and merge it once its pipeline passed on its head
    commit (polled until then, or CRE_ADOPT_MR_WAIT seconds, default 2400). Setup never approves its
    own MR (GitLab refuses an author's approval). Outputs ADOPT_PUSHED, ADOPT_REVIEW (the MR's iid) and, once it
    merged, ADOPT_MERGED (origin/main's sha). A failed pipeline is 20; a block only a person clears
    (an approval rule, a conflict), or the wait running out, is NEEDS=ADOPT_MR (13)."""
    p = c.Parser(step, "repo_gitlab.py adopt")
    p.add_argument("--namespace", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--pushed", default="", help="what setup pushed to the branch last")
    p.add_argument("--mr", default="", help="the MR's iid, once opened")
    a = p.parse_args(argv)
    c.require_tools(step, "git", "ssh", "curl")
    gl = c.GitLab(step, lapse_fatal=True)
    gl.require_midway()
    with gl:
        gl.get_token()
        adopt_review(step, gl, a)


def adopt_review(step, gl, a):
    full = f"{a.namespace}/{a.project}"
    pid = project_id(step, gl, full)
    if not pid:
        step.die(c.REFUSED, f"GitLab project {full} doesn't exist.")
    mr = None
    if a.mr:
        status, mr = gl.call("GET", f"/projects/{pid}/merge_requests/{a.mr}")
        if status != "200" or not isinstance(mr, dict):
            step.die(c.FAILED, f"Reading MR !{a.mr} failed ({status}): {mr}")
        if mr.get("state") == "merged":
            return merged(step, a, mr)
        if mr.get("state") != "opened":
            step.die(c.REFUSED, f"{mr.get('web_url') or f'MR !{a.mr}'} is {mr.get('state')}, not merged. Reopen it in GitLab, then re-run.")
    step.out("ADOPT_PUSHED", c.push_adopt_branch(step, a.dir, a.pushed))
    if not mr:
        status, found = gl.call("GET", f"/projects/{pid}/merge_requests?state=opened&source_branch={c.ADOPT_BRANCH}")
        # The project's own branch, not a fork's of the same name.
        mine = [m for m in found if str(m.get("source_project_id", pid)) == str(pid)] if status == "200" and isinstance(found, list) else []
        mr = mine[0] if mine else None
    if not mr:
        status, mr = gl.call("POST", f"/projects/{pid}/merge_requests", {"source_branch": c.ADOPT_BRANCH, "target_branch": "main",
                                                                         "title": a.title, "remove_source_branch": True})
        if status != "201":
            step.die(c.FAILED, f"Opening the scaffold's MR failed ({status}): {mr}")
    iid, url = mr["iid"], mr.get("web_url") or f"MR !{mr['iid']}"
    step.out("ADOPT_REVIEW", str(iid))
    step.ok(f"{url} holds the scaffold.")
    # Merged once its own pipeline passed on the commit setup pushed, asked on every poll, so one
    # unattended run ends with main holding a scaffold its pipeline built. Merge is never asked before
    # that: a project that doesn't require a green pipeline merges at once, while the pipeline runs
    # (live check of review 10h, L1). Arming "merge when the pipeline succeeds" doesn't take before
    # GitLab has made the pipeline, so it isn't relied on (the axolotl run waited on an MR never armed).
    wait = int(os.environ.get("CRE_ADOPT_MR_WAIT") or 2400)
    poll = float(os.environ.get("CRE_ADOPT_MR_POLL") or 20)   # seconds between polls (the harness shortens it)
    deadline = time.time() + wait
    put_status, put_body = "", "not asked: its pipeline hasn't passed"
    while True:
        status, m = gl.call("GET", f"/projects/{pid}/merge_requests/{iid}")
        m = m if status == "200" and isinstance(m, dict) else {}
        if m.get("state") == "merged":
            return merged(step, a, m)
        head = m.get("sha", "")
        p = m.get("head_pipeline") or {}
        # Only a pipeline of the MR's head commit counts: one of an earlier push proves nothing.
        pipe = p.get("status", "") if head and p.get("sha") == head else ""
        if pipe in ("failed", "canceled"):
            step.die(c.FAILED, f"{url} won't merge: its pipeline {pipe}. Nothing reached main. Fix the cause with a commit on "
                     f"{c.ADOPT_BRANCH} in {a.dir}, then re-run: setup pushes the branch and merges the MR.")
        why = m.get("detailed_merge_status", "")
        why = f"GitLab says {why}" if why in PERSON_BLOCKS else ""
        if pipe == "success" and not why:
            # That commit only: one pushed since its pipeline passed is refused (409), not merged unbuilt.
            put_status, put_body = gl.call("PUT", f"/projects/{pid}/merge_requests/{iid}/merge",
                                           {"should_remove_source_branch": True, "sha": head})
            if put_status == "200":
                step.ok(f"Merged {url} into main (its pipeline passed on {head[:9]}).")
                return merged(step, a, put_body)
            if put_status in ("401", "403"):
                why = f"you may not merge into main ({put_status})"
        if why or time.time() >= deadline:
            break
        step.info(f"Waiting for {url}'s pipeline to pass (pipeline {pipe or 'not started'}).")
        time.sleep(min(poll, max(0, deadline - time.time())))
    step.warn(f"{url} can't merge yet: {why or f'its pipeline has not passed after {wait}s'} (its last merge answer: "
              f"{put_status or '-'} {str(put_body)[:200]}). A person approves or merges it, then re-run: setup goes on from the "
              "merged main.")
    step.need("ADOPT_MR")


def merged(step, a, mr):
    if c.git("fetch", "-q", "origin", cwd=a.dir).returncode != 0:
        step.die(c.FAILED, "The MR merged, but fetching origin failed. Re-run.")
    step.out("ADOPT_MERGED", c.git("rev-parse", "origin/main", cwd=a.dir).stdout.strip())


def finalize(step, gl, a):
    c.require_repo_dir(step, a.dir)
    pid = project_id(step, gl, a.full)
    if not pid:
        step.die(c.REFUSED, f"GitLab project {a.full} doesn't exist; run 'repo_gitlab.py prepare' first.")
    token_ok = None
    if not c.scaffold_pushed(step, a.dir):
        # The token before the first push, so its expiry goes in with it (review 10f C3). Its
        # variable is Protected: until main is protected below, no pipeline gets it.
        if not step.dry_run:
            token_ok = ensure_upgrade_token(step, gl, pid, a, pushed=False)
        c.set_origin(step, a.dir, a.ssh_url)
        c.require_main(step, a.dir)
        c.bootstrap_push(step, a.dir, a.message)
    if step.dry_run:
        return
    protect_main(step, gl, pid)
    # Prevent outdated deployment jobs: an older pipeline's deploy job never runs after a newer one
    # (with the per-stack resource groups, stacks can't be left at an older commit).
    status, body = gl.call("PUT", f"/projects/{pid}", {"squash_option": "always", "remove_source_branch_after_merge": True,
                                                       "ci_forward_deployment_enabled": True})
    if status != "200":
        step.die(c.FAILED, f"Updating merge settings failed ({status}): {body}")
    step.ok("Squash required on merge; source branches deleted on merge; outdated deploy jobs are skipped.")
    # The weekly dependency upgrade. Its token's variable is Protected, so it reaches only main's
    # (scheduled) pipelines once main is protected above; on a first push it was made before the push.
    # No token, no schedule: a weekly pipeline that can only fail at its push (NEEDS=UPGRADE_TOKEN says why).
    if token_ok is None:
        token_ok = ensure_upgrade_token(step, gl, pid, a, pushed=True)
    if token_ok:
        ensure_upgrade_schedule(step, gl, pid)
    step.out("REPO_WEB_URL", f"https://{c.GITLAB_HOST}/{a.full}")


def main(step, argv):
    a = parse(step, argv)
    c.require_tools(step, "git", "ssh", "curl")
    gl = c.GitLab(step, lapse_fatal=True)
    gl.require_midway()
    with gl:
        gl.get_token()
        (prepare if a.mode == "prepare" else finalize)(step, gl, a)


if __name__ == "__main__":
    c.cli(NAME, main)
