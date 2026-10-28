#!/usr/bin/env python3
"""npx projen watch — follow one MR/PR from its checks through the merge to the deploy of exactly
its merge commit, and stop at the first thing that needs attention. Read-only: it never pushes,
merges, retries or starts anything.

  npx projen watch                      the MR/PR of the current branch (open, or the latest one)
  npx projen watch <number>             MR !<number> / PR #<number>
  npx projen watch --branch <branch>    the latest MR/PR from <branch> (e.g. cre/upgrade-main,
                                        github-actions/upgrade-main: the weekly upgrade)
  npx projen watch --sha <sha>          only the deploy of commit <sha> on main (a first push)
  [--until checks|merged|deployed]      where to stop (default deployed)
  [--timeout <s>]                       give up after this long (default 3600)
  [--interval <s>]                      poll every s seconds (default 15)
  [--start-timeout <s>]                 how long a pipeline may take to appear (default 300)

Stages, each keyed on IDs read from the provider, never guessed:
  1. checks    the pipeline/check runs of the MR/PR's current head commit;
  2. merged    the MR/PR merged, and the commit it put on main (its merge or squash commit);
  3. deployed  the pipeline (GitLab) / `deploy` workflow run (GitHub) for exactly that commit on main.

Prints `KEY=value` lines as it learns them (MR, HEAD_SHA, CHECKS_URL, MERGE_SHA, DEPLOY_URL) and a
progress line on stderr on every change. On a failed job it prints the job's name, URL and the
last lines of its log. The last stdout line is `RESULT code=<n> stage=<stage> mr=<n> sha=<sha>`.

Exit codes: 0 reached --until | 10 nothing to watch (no MR/PR, bad input) | 11 credentials |
20 the MR/PR was closed without merging | 30 a check or deploy job failed | 40 timed out, the
pipeline never started, or the MR/PR couldn't be read (the stage and message say which; re-run).
"""
import argparse
import contextlib
import json
import os
import re
import signal
import sys
import time
import traceback
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "setup"))
sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, HERE)
import crelib as c  # noqa: E402

# CHECKS_FAILED is 30 here (the documented watch contract), not crelib's FAILED (20): a separate
# name, so nothing imports the wrong one (review 10b F1).
DONE, CLOSED, CHECKS_FAILED, TIMEOUT = 0, 20, 30, 40
STAGES = ("checks", "merged", "deployed")
LOG_LINES = 40


class Stop(Exception):
    def __init__(self, code, stage, msg):
        super().__init__(msg)
        self.code, self.stage, self.msg = code, stage, msg


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def out(key, value):
    print(f"{key}={value}", flush=True)


def sh(cmd, input=None):
    """crelib.run: never raises (a missing gh or git is rc 127)."""
    return c.run(cmd, input=input)


_DEFAULT = []


def default_branch():
    """The branch merges land on, as the git tasks work it out (cregit.default_branch: one rule, not
    a shorter copy, review 10c G3). Asked once per run: watch polls."""
    if not _DEFAULT:
        import cregit
        _DEFAULT.append(cregit.default_branch() or "main")
    return _DEFAULT[0]


# ---- providers: the same small interface over GitHub (gh) and GitLab (REST) ---------------------
#   find(number, branch) -> MR dict or None     {number, state: open|merged|closed, head, url, merge_sha}
#   refresh(mr) -> MR dict
#   checks(sha, mr) -> Pipeline or None          {id, url, status: pending|running|success|failed|canceled, jobs}
#   deploy(sha) -> Pipeline or None
#   log_tail(job) -> str
# Jobs: [{id, name, status, url}]; status success|failed|skipped|canceled|running|pending|manual.

def norm_status(s):
    s = (s or "").lower()
    if s in ("success", "passed", "neutral"):
        return "success"
    if s in ("failure", "failed", "timed_out", "action_required", "startup_failure"):
        return "failed"
    if s in ("cancelled", "canceled"):
        return "canceled"
    if s in ("skipped",):
        return "skipped"
    if s in ("manual",):
        return "manual"
    if s in ("in_progress", "running"):
        return "running"
    return "pending"


def pipeline_status(jobs, own=None):
    """A pipeline's status from its jobs (or the provider's own status when it has one). jobs None:
    the jobs couldn't be read, so the status is unknown (pending), never "no jobs, so done".
    own "blocked": waiting on a manual job, never success."""
    if own in ("success", "failed", "canceled", "blocked", "running"):
        return own
    if jobs is None:
        return "pending"
    sts = [j["status"] for j in jobs]
    if not sts:
        return own or "pending"
    if "failed" in sts:
        return "failed" if all(s not in ("running", "pending") for s in sts) else "running"
    if any(s in ("running", "pending") for s in sts):
        return "running"
    if "canceled" in sts:
        return "canceled"
    return "success"


# The GitHub deploy workflow's name (deploy-workflow.ts DEPLOY_WORKFLOW; a test keeps them equal).
DEPLOY_WORKFLOW = "deploy"


class GitHub:
    name = "github"

    def __init__(self):
        # origin's repository, as the git tasks use (gh's own choice prefers an `upstream` remote).
        import cregit_providers
        self.repo = (cregit_providers.github_repo(sh(["git", "config", "--get", "remote.origin.url"]).stdout.strip())
                     or cregit_providers.github_repo(sh(["git", "remote", "get-url", "origin"]).stdout.strip()))
        if not self.repo:
            r = sh(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
            if r.returncode != 0:
                raise Stop(c.CREDENTIALS, "checks", f"gh can't read this repo ({c.last_line(r.stderr)}). Run: gh auth login")
            self.repo = r.stdout.strip()
        if sh(["gh", "auth", "status"]).returncode != 0:
            raise Stop(c.CREDENTIALS, "checks", "The GitHub CLI is not logged in. Run: gh auth login")

    def _api(self, path):
        r = sh(["gh", "api", path])
        self.error = ""
        if r.returncode != 0:
            self.error = f"gh api: {c.last_line(r.stderr) or f'exit {r.returncode}'}"
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            self.error = "gh api answered with something that isn't JSON"
            return None

    def _mr(self, d):
        state = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}.get(d.get("state"), "open")
        return {"number": d["number"], "state": state, "head": d.get("headRefOid", ""), "url": d.get("url", ""),
                "merge_sha": (d.get("mergeCommit") or {}).get("oid", "") if state == "merged" else "",
                "branch": d.get("headRefName", "")}

    FIELDS = "number,state,headRefOid,url,mergeCommit,headRefName"

    # gh's own words for "no such PR" (anything else failing is a read error, not "no PR").
    NOT_FOUND = re.compile(r"no pull requests? found|could not resolve to a pullrequest|not found", re.I)

    def _json(self, r, what):
        """gh's JSON answer, or None with self.error saying why it couldn't be read: a failed gh
        call read as "no PR" (10, nothing to watch) though the PR was there (review 10g B9)."""
        if r.returncode != 0:
            if not self.NOT_FOUND.search(r.stderr or ""):
                self.error = f"gh {what}: {c.last_line(r.stderr) or f'exit {r.returncode}'}"
            return None
        try:
            return json.loads(r.stdout or "null")
        except ValueError:
            self.error = f"gh {what} answered with something that isn't JSON"
            return None

    def find(self, number, branch):
        self.error = ""
        if number:
            d = self._json(sh(["gh", "pr", "view", str(number), "-R", self.repo, "--json", self.FIELDS]), "pr view")
            return self._mr(d) if isinstance(d, dict) else None
        # --head matches the name only: a fork's PR from a same-named branch isn't ours (review 10d B7).
        d = self._json(sh(["gh", "pr", "list", "-R", self.repo, "--head", branch, "--state", "all", "--limit", "30", "--json",
                           self.FIELDS + ",isCrossRepository"]), "pr list")
        items = [i for i in (d if isinstance(d, list) else []) if isinstance(i, dict) and not i.get("isCrossRepository")]
        return self._mr(items[0]) if items else None

    def refresh(self, mr):
        return self.find(mr["number"], None) or mr

    def _run_jobs(self, run_id):
        """The run's jobs, or None when they couldn't be read (unknown, never "no jobs")."""
        d = self._api(f"repos/{self.repo}/actions/runs/{run_id}/jobs?per_page=100")
        if not isinstance(d, dict):
            return None
        return [{"id": j["id"], "name": j["name"], "url": j.get("html_url", ""),
                 "status": norm_status(j.get("conclusion") if j.get("status") == "completed" else j.get("status"))}
                for j in d.get("jobs", [])]

    @staticmethod
    def run_status(r):
        """A workflow run's own status: running until it completes (GitHub creates a run's jobs as
        it reaches them, so its jobs so far can all be done while it isn't), then its conclusion."""
        if r.get("status") != "completed":
            return "running"
        return norm_status(r.get("conclusion"))

    def checks(self, sha, mr):
        """Every workflow run of the PR's head commit (build, title lint, auto-approve, ...), as one pipeline."""
        d = self._api(f"repos/{self.repo}/actions/runs?head_sha={sha}&per_page=100") or {}
        runs = [r for r in d.get("workflow_runs", []) if r.get("event") in ("pull_request", "pull_request_target")]
        if not runs:
            return None
        # One PR starts the same workflow once per event (opened, each label added): a workflow
        # counts once, as its successful run if one succeeded (the others are redundant, and may
        # fail only because that one already did the work), else its latest run.
        by_name = {}
        for r in sorted(runs, key=lambda x: x["id"]):
            prev = by_name.get(r["name"])
            if prev is None or norm_status(prev.get("conclusion")) != "success":
                by_name[r["name"]] = r
        runs = list(by_name.values())
        jobs, unknown = [], False
        for r in runs:
            rj = self._run_jobs(r["id"])
            if rj is None:
                unknown = True
                continue
            for j in rj:
                j["name"] = f"{r['name']} / {j['name']}"
                jobs.append(j)
        own = [self.run_status(r) for r in runs]
        if "running" in own or "pending" in own or unknown:
            status = "running"
        elif "failed" in own:
            status = "failed"
        elif "canceled" in own:
            status = "canceled"
        else:
            status = "success"   # success, skipped (a job's `if` was false) or neutral
        return {"id": ",".join(str(r["id"]) for r in runs), "url": f"{mr['url']}/checks", "jobs": jobs, "status": status}

    def deploy(self, sha):
        d = self._api(f"repos/{self.repo}/actions/runs?head_sha={sha}&event=push&per_page=100")
        if d is None:
            return None   # self.error says why
        runs = [r for r in d.get("workflow_runs", []) if r.get("name") == DEPLOY_WORKFLOW and r.get("head_branch") == default_branch()]
        if not runs:
            return None
        r = sorted(runs, key=lambda x: x["id"])[-1]
        if self.run_status(r) == "canceled":
            # The deploy workflow has one concurrency group: a run still waiting when a newer merge
            # arrives is cancelled, and the newer run deploys both. That isn't a failed deploy of
            # this commit: follow the later run that contains it (review 10d F3).
            later = self.superseding(sha, r["id"])
            if later:
                if getattr(self, "_superseded", None) != later["id"]:
                    self._superseded = later["id"]
                    say(f"deployed: run {r['id']} of {sha[:8]} was cancelled for a newer merge; following run {later['id']}, "
                        f"which deploys {later['head_sha'][:8]} and contains it")
                r = later
        jobs = self._run_jobs(r["id"])
        return {"id": str(r["id"]), "url": r.get("html_url", ""), "jobs": jobs or [], "status": pipeline_status(jobs, self.run_status(r))}

    def superseding(self, sha, after_id):
        """The earliest deploy run on main after `after_id` whose commit contains `sha` (None when none yet)."""
        d = self._api(f"repos/{self.repo}/actions/runs?branch={default_branch()}&event=push&per_page=50") or {}
        later = sorted((r for r in d.get("workflow_runs", []) if r.get("name") == DEPLOY_WORKFLOW and r.get("id", 0) > after_id
                        and r.get("head_branch") == default_branch() and r.get("head_sha")), key=lambda x: x["id"])
        if later:
            sh(["git", "fetch", "-q", "origin", default_branch()])
        for r in later:
            if r["head_sha"] == sha or sh(["git", "merge-base", "--is-ancestor", sha, r["head_sha"]]).returncode == 0:
                return r
        return None

    def log_tail(self, job):
        r = sh(["gh", "api", f"repos/{self.repo}/actions/jobs/{job['id']}/logs"])
        return tail(r.stdout)


class GitLab:
    name = "gitlab"

    def __init__(self, step):
        import cregit_providers
        url = sh(["git", "config", "--get", "remote.origin.url"]).stdout.strip()
        self.full = cregit_providers.gitlab_project(url) or cregit_providers.gitlab_project(sh(["git", "remote", "get-url", "origin"]).stdout.strip())
        if not self.full:
            raise Stop(c.REFUSED, "checks", f"origin ({url or 'none'}) isn't a {c.GITLAB_HOST} project.")
        self.enc = quote(self.full, safe="")
        self.web = f"https://{c.GITLAB_HOST}/{self.full}"
        # A timeout per call: a poll on a dead network fails and retries, not hangs (review 10d B13).
        self.gl = c.GitLab(step, timeout=30)
        try:
            self.gl.require_midway()
            self.gl.__enter__()
            # No SSH mint attempt: gitlab.aws.dev refuses it, after a connect timeout (review 10b F5).
            self.gl.get_token(mint=False)
        except c.Exit as e:
            raise Stop(e.code, "checks", "GitLab credentials (see above).")

    def close(self):
        self.gl.__exit__(None, None, None)

    def _get(self, path, missing_ok=False):
        """The answer of a 200, else None with self.error saying why (a 404 of something asked for
        by id, with missing_ok, is "not there", no error)."""
        status, body = self.gl.call("GET", f"/projects/{self.enc}{path}")
        if status in ("401", "403"):
            raise Stop(c.CREDENTIALS, "checks", f"GitLab refused the API call ({status}): refresh Midway ({c.MWINIT_HINT}) or the token.")
        # Why a read failed (a timeout, a 5xx), so a poll that can't read a pipeline isn't taken
        # for "no pipeline" (review 10e live check).
        self.error = "" if status == "200" or (missing_ok and status == "404") else \
            f"GitLab answered {status}" + (f": {body.get('message')}" if isinstance(body, dict) and body.get("message") else "")
        return body if status == "200" else None

    def _mr(self, d):
        return {"number": d["iid"], "state": {"opened": "open", "merged": "merged", "closed": "closed"}.get(d["state"], "open"),
                "head": d.get("sha", ""), "url": d.get("web_url", ""), "branch": d.get("source_branch", ""),
                # With a merge commit (the default), main's pipeline runs for it; fast-forward: the squash commit.
                "merge_sha": (d.get("merge_commit_sha") or d.get("squash_commit_sha") or "") if d["state"] == "merged" else ""}

    def find(self, number, branch):
        if number:
            d = self._get(f"/merge_requests/{number}", missing_ok=True)
            return self._mr(d) if isinstance(d, dict) and "iid" in d else None
        # The branch as is: GitLab matches source_branch=cre%2Fupgrade-main against nothing (verified).
        d = self._get(f"/merge_requests?source_branch={quote(branch, safe='/')}&state=all&order_by=created_at&sort=desc&per_page=20")
        # A fork's MR from a branch of the same name is listed too: ours come from this project, as
        # in cregit_providers.latest_mr (review 10f B11).
        mine = [m for m in (d if isinstance(d, list) else []) if isinstance(m, dict) and "iid" in m
                and m.get("source_project_id", m.get("project_id")) == m.get("project_id")]
        return self._mr(mine[0]) if mine else None

    def refresh(self, mr):
        return self.find(mr["number"], None) or mr

    def _pipeline(self, p):
        d = self._get(f"/pipelines/{p['id']}/jobs?per_page=100&include_retried=false")
        jobs = [{"id": j["id"], "name": j["name"], "url": j.get("web_url", ""), "status": norm_status(j.get("status"))}
                for j in d] if isinstance(d, list) else None
        raw = (p.get("status") or "").lower()
        # A pipeline waiting on a manual job (or a schedule) has not deployed, whatever its jobs say.
        own = "blocked" if raw in ("manual", "scheduled") else "skipped" if raw == "skipped" else norm_status(raw)
        return {"id": str(p["id"]), "url": p.get("web_url", ""), "jobs": jobs or [],
                "status": own if own in ("success", "failed", "canceled", "blocked", "skipped") else pipeline_status(jobs)}

    def checks(self, sha, mr):
        d = self._get(f"/merge_requests/{mr['number']}/pipelines")
        ps = [p for p in (d or []) if p.get("sha") == sha]
        return self._pipeline(sorted(ps, key=lambda x: x["id"])[-1]) if ps else None

    def deploy(self, sha):
        d = self._get(f"/pipelines?sha={sha}&ref={quote(default_branch(), safe='')}&per_page=20")
        ps = [p for p in (d or []) if p.get("source") != "schedule"]
        return self._pipeline(sorted(ps, key=lambda x: x["id"])[-1]) if ps else None

    def log_tail(self, job):
        _, text = self.gl.call("GET", f"/projects/{self.enc}/jobs/{job['id']}/trace", raw=True)
        return tail(text)


def tail(text):
    text = re.sub(r"\x1b\[[0-9;]*[mK]", "", text or "")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-LOG_LINES:])


# ---- the watch ---------------------------------------------------------------------------------

class Watch:
    def __init__(self, provider, args, clock=time.time, sleep=time.sleep):
        self.p, self.a, self.clock, self.sleep = provider, args, clock, sleep
        self.deadline = clock() + args.timeout
        self.last = {}

    def none_means(self, stage, seen, started, label):
        """A poll found no pipeline. When the read failed (the provider says why), or this pipeline
        was already seen, it is a read to retry until --timeout, not "never started": a watch that
        had seen MR !12's pipeline running stopped 300 s later with "No checks started" after its
        reads failed (review 10e, live). Otherwise the --start-timeout applies."""
        error = getattr(self.p, "error", "")
        if error or seen:
            self.report(stage, f"{stage}: can't read the {label} right now ({error or 'not in the answer'}); retrying until "
                        f"--timeout ({self.a.timeout}s)")
            return
        if self.clock() - started > self.a.start_timeout:
            raise Stop(TIMEOUT, stage, f"No {label} started within {self.a.start_timeout}s.")
        self.report(stage, f"{stage}: waiting for the {label} to start...")

    def wait(self, stage):
        if self.clock() >= self.deadline:
            raise Stop(TIMEOUT, stage, f"Timed out after {self.a.timeout}s at stage {stage}.")
        self.sleep(self.a.interval)

    def report(self, key, line):
        if self.last.get(key) != line:
            self.last[key] = line
            say(line)

    def fail_jobs(self, stage, pipe):
        bad = [j for j in pipe["jobs"] if j["status"] == "failed"]
        for j in bad:
            print(f"\n--- {j['name']} failed: {j['url']}\n{self.p.log_tail(j)}\n---", file=sys.stderr)
        names = ", ".join(j["name"] for j in bad) or pipe["status"]
        raise Stop(CHECKS_FAILED, stage, f"{stage}: {names} failed ({pipe['url']}).")

    def follow(self, stage, get, label):
        """Poll get() until its pipeline finishes; returns it. None for --start-timeout = it never started."""
        started, seen = self.clock(), False
        while True:
            pipe = get()
            if pipe is None:
                self.none_means(stage, seen, started, label)
                self.wait(stage)
                continue
            seen = True
            summary = ", ".join(f"{j['name']}={j['status']}" for j in pipe["jobs"]) or "no jobs yet"
            if pipe["status"] == "blocked":
                manual = ", ".join(j["name"] for j in pipe["jobs"] if j["status"] == "manual") or "a manual job"
                self.report(stage, f"{stage}: blocked, waiting on {manual} ({pipe['url']}); start it there, or it times out")
            else:
                self.report(stage, f"{stage}: {pipe['status']} ({summary})")
            if pipe["status"] == "success":
                return pipe
            if pipe["status"] == "skipped":
                raise Stop(CHECKS_FAILED, stage, f"{stage}: the pipeline was skipped, so nothing ran ({pipe['url']}).")
            if pipe["status"] in ("failed", "canceled"):
                self.fail_jobs(stage, pipe)
            self.wait(stage)

    def run(self):
        a = self.a
        if a.sha:
            out("MERGE_SHA", a.sha)
            self.sha = a.sha
            pipe = self.follow("deployed", lambda: self.p.deploy(a.sha), f"deploy of {a.sha[:8]} on main")
            out("DEPLOY_URL", pipe["url"])
            return DONE, "deployed", "-", a.sha
        mr = self.p.find(a.number, a.branch)
        what = f"#{a.number}" if a.number else f"from branch {a.branch}"
        if not mr and getattr(self.p, "error", ""):
            # A read that failed is not "no MR/PR" (10, nothing to watch): an agent told there is
            # nothing to watch stops following a deploy that is running (review 10g B9).
            raise Stop(TIMEOUT, "checks", f"Couldn't read the MR/PR {what} ({self.p.error}). Nothing is known about it yet: re-run.")
        if not mr:
            raise Stop(c.REFUSED, "checks", f"No MR/PR {what}.")
        out("MR", mr["url"])
        # What is known so far, for the RESULT line of a stop (review 10d B13): ci:rerun --sha needs it.
        self.number, self.sha = mr["number"], mr["head"]
        # 1. checks of the head commit (the head can move: a new push restarts them)
        started, seen = self.clock(), set()
        while True:
            mr = self.p.refresh(mr)
            out_once(self, "HEAD_SHA", mr["head"])
            if mr["state"] == "closed":
                raise Stop(CLOSED, "checks", f"{mr['url']} was closed without merging.")
            pipe = self.p.checks(mr["head"], mr)
            if pipe is None:
                if mr["state"] == "merged":
                    say("checks: none ran for the head commit (merged without them).")
                    break
                if getattr(self.p, "error", "") or mr["head"] in seen:
                    self.none_means("checks", True, started, f"pipeline of {mr['head'][:8]}")
                elif self.clock() - started > self.a.start_timeout:
                    raise Stop(TIMEOUT, "checks", f"No checks started for {mr['head'][:8]} within {self.a.start_timeout}s "
                               "(no workflow or pipeline runs for this MR/PR).")
                else:
                    self.report("checks", "checks: waiting for the pipeline to start...")
                self.wait("checks")
                continue
            seen.add(mr["head"])
            summary = ", ".join(f"{j['name']}={j['status']}" for j in pipe["jobs"]) or "no jobs yet"
            self.report("checks", f"checks: {pipe['status']} ({summary})")
            out_once(self, "CHECKS_URL", pipe["url"])
            if pipe["status"] == "success":
                break
            if mr["state"] == "merged":
                # Nothing required the checks (no branch protection), so the merge didn't wait; a
                # check failing now may only be the race (its branch deleted on merge). Go on to the deploy.
                say(f"checks: ⚠ merged before its checks passed ({summary}); nothing required them (branch protection).")
                break
            if pipe["status"] in ("failed", "canceled"):
                self.fail_jobs("checks", pipe)
            self.wait("checks")
        if a.until == "checks":
            return DONE, "checks", mr["number"], mr["head"]
        # 2. merged
        while mr["state"] != "merged":
            if mr["state"] == "closed":
                raise Stop(CLOSED, "merged", f"{mr['url']} was closed without merging.")
            self.report("merged", f"merged: waiting (state {mr['state']})...")
            self.wait("merged")
            mr = self.p.refresh(mr)
        for _ in range(10):
            if mr["merge_sha"]:
                break
            self.wait("merged")
            mr = self.p.refresh(mr)
        if not mr["merge_sha"]:
            raise Stop(TIMEOUT, "merged", f"{mr['url']} is merged, but the provider names no merge commit.")
        say(f"merged: {mr['url']} → {mr['merge_sha'][:8]} on {default_branch()}")
        out("MERGE_SHA", mr["merge_sha"])
        self.sha = mr["merge_sha"]
        if a.until == "merged":
            return DONE, "merged", mr["number"], mr["merge_sha"]
        # 3. the deploy of exactly that commit
        pipe = self.follow("deployed", lambda: self.p.deploy(mr["merge_sha"]), f"deploy of {mr['merge_sha'][:8]} on main")
        out("DEPLOY_URL", pipe["url"])
        return DONE, "deployed", mr["number"], mr["merge_sha"]


def out_once(w, key, value):
    if value and w.last.get(f"out:{key}") != value:
        w.last[f"out:{key}"] = value
        out(key, value)


def full_sha(sha):
    """The full commit SHA for --sha as typed: the providers match only 40 characters, so a short
    one never matched and timed out as "no deploy started" (review 10c B9). Fetches main first, as
    the commit may only be on origin."""
    sh(["git", "fetch", "-q", "origin"])
    r = sh(["git", "rev-parse", "-q", "--verify", f"{sha}^{{commit}}"])
    if r.returncode != 0 or not r.stdout.strip():
        raise Stop(c.REFUSED, "checks", f"No commit {sha} here or on origin. Give a commit of main (git log origin/main).")
    return r.stdout.strip()


class Parser(argparse.ArgumentParser):
    """A bad switch is a refusal with the task's RESULT line, not argparse's bare exit 2 (review 10d B13)."""

    def __init__(self, *args, result="stage=checks mr=- sha=-", **kw):
        super().__init__(*args, **kw)
        self.result = result

    def error(self, message):
        print(f"❌ {self.prog}: {message}", file=sys.stderr)
        print(f"RESULT code={c.REFUSED} {self.result}", flush=True)
        sys.exit(c.REFUSED)


def parse(argv):
    p = Parser(prog="npx projen watch", allow_abbrev=False)
    p.add_argument("number", nargs="?", type=int)
    p.add_argument("--branch", default="")
    p.add_argument("--sha", default="")
    p.add_argument("--until", choices=STAGES, default="deployed")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--interval", type=int, default=15)
    p.add_argument("--start-timeout", dest="start_timeout", type=int, default=300)
    a = p.parse_args(argv)
    if sum(bool(x) for x in (a.number, a.branch, a.sha)) > 1:
        p.error("give one of <number>, --branch, --sha")
    if not (a.number or a.branch or a.sha):
        a.branch = sh(["git", "branch", "--show-current"]).stdout.strip()
        # Named as it is: a detached HEAD, or a default branch not called main (review 10g B12).
        if not a.branch:
            p.error("on a detached HEAD: give <number>, --branch <branch> or --sha <sha>")
        if a.branch == default_branch():
            p.error(f"on {a.branch}: give <number>, --branch <branch> or --sha <sha>")
    return a


def on_stop_signal(signum, frame):
    """SIGTERM (an agent's command timeout) and SIGHUP (a closed terminal) stop the watch as Ctrl-C
    does, with its RESULT line: they ended it with none (review 10g B12)."""
    raise KeyboardInterrupt


@contextlib.contextmanager
def stop_signals_caught():
    """on_stop_signal for SIGTERM and SIGHUP while the task runs; the old handlers after."""
    old = {s: signal.signal(s, on_stop_signal) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def main(argv=None):
    with stop_signals_caught():
        return _main(argv)


def _main(argv):
    a = parse(sys.argv[1:] if argv is None else argv)
    provider = os.environ.get("CRE_GIT_PROVIDER", "")
    p = w = None
    code, stage, number, sha = c.REFUSED, "checks", "-", "-"
    try:
        if provider == "github":
            p = GitHub()
        elif provider == "gitlab":
            p = GitLab(c.Step("watch", echo=False))
        else:
            raise Stop(c.REFUSED, "checks", "CRE_GIT_PROVIDER is not set (run it as npx projen watch).")
        if a.sha:
            a.sha = full_sha(a.sha)
        w = Watch(p, a)
        code, stage, number, sha = w.run()
        say(f"✅ {stage}.")
    except Stop as e:
        print(f"❌ {e.msg}", file=sys.stderr)
        code, stage = e.code, e.stage
    except KeyboardInterrupt:
        # Not a timeout: stopped by hand. Same code (the watch didn't reach its stage), its own stage.
        print("❌ Interrupted.", file=sys.stderr)
        code, stage = TIMEOUT, "interrupted"
    except Exception:  # noqa: BLE001 - a bug must still end with the RESULT line (review 10g B12)
        traceback.print_exc()
        print("❌ Internal error (above). Nothing was changed (watch only reads): re-run.", file=sys.stderr)
        code, stage = TIMEOUT, "internal-error"
    finally:
        if isinstance(p, GitLab):
            p.close()
    if w is not None and code != c.OK:
        number, sha = getattr(w, "number", number) or number, getattr(w, "sha", sha) or sha
    print(f"RESULT code={code} stage={stage} mr={number} sha={sha}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
