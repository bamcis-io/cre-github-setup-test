#!/usr/bin/env python3
"""npx projen ci:rerun — re-run the deploy of a commit on main, without a new commit.

  npx projen ci:rerun                 retry the failed jobs of the deploy of origin/main's head
  npx projen ci:rerun --sha <sha>     ... the same, naming main's head (any other commit: exit 10)
  npx projen ci:rerun --all           run the whole deploy again (a redeploy of origin/main's head:
                                      e.g. after fixing a setting outside the repo)

Finds the deploy exactly as `watch` does (the GitLab pipeline / GitHub `deploy` run for that commit
on main) and asks the provider to retry it: GitLab retries the pipeline's failed jobs, or (--all)
starts a new main pipeline; GitHub re-runs the run's failed jobs (--failed), or all of them. Then
follow it with `npx projen watch --sha <sha>`. It never pushes or merges.

Only main's head is ever re-run: a retry of an older commit's deploy would put its templates back
over the newer ones (and its asset prefix may be pruned, its artifacts expired).

Exit codes: 0 started | 10 nothing to re-run (no deploy for that commit, still running, blocked on a
manual job, skipped, passed and --all wasn't given, or not main's head) | 11 credentials | 20 the
provider refused, or the deploy couldn't be read (re-run).
Last stdout line: RESULT code=<n> sha=<sha> url=<url|none>
"""
import argparse
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, HERE)
import watch as w  # noqa: E402
from cregit import gout as git  # noqa: E402  (stdout stripped, "" on failure; review 10c G10)

c = w.c


def rerun(p, sha, all_jobs, head="", target="main"):
    if head and sha != head:
        raise w.Stop(c.REFUSED, "deployed", f"{sha[:9]} isn't {target}'s head ({head[:9]}): re-running its deploy would put its older "
                     f"templates back over the newer ones. Re-run the head's deploy (no --sha), or push a fix.")
    pipe = p.deploy(sha)
    if not pipe and getattr(p, "error", ""):
        # A failed read is not "no deploy" (10): a deploy that is there would be left un-retried
        # on the word that nothing exists to retry (review 10g B9).
        raise w.Stop(c.FAILED, "deployed", f"Couldn't read the deploy of {sha[:9]} on {target} ({p.error}). Nothing was retried: re-run.")
    if not pipe:
        raise w.Stop(c.REFUSED, "deployed", f"No deploy pipeline/run for {sha[:9]} on {target}. A merge that changed nothing deployable, "
                     f"or a commit that never reached {target}.")
    status = pipe["status"]
    if status in ("pending", "running"):
        raise w.Stop(c.REFUSED, "deployed", f"The deploy of {sha[:9]} is still {status} ({pipe['url']}): follow it with npx projen watch --sha {sha}.")
    if status in ("blocked", "skipped"):
        # A retry re-runs only failed or canceled jobs: here it would report a retry that does nothing.
        raise w.Stop(c.REFUSED, "deployed", f"The deploy of {sha[:9]} is {status} ({pipe['url']}), not failed: "
                     + ("start its manual job there." if status == "blocked" else "nothing ran to retry; --all runs a new deploy."))
    if status == "success" and not all_jobs:
        raise w.Stop(c.REFUSED, "deployed", f"The deploy of {sha[:9]} passed ({pipe['url']}); nothing failed to retry. --all runs it again.")
    if p.name == "github":
        cmd = ["gh", "run", "rerun", pipe["id"], "-R", p.repo] + ([] if all_jobs else ["--failed"])
        r = w.sh(cmd)
        if r.returncode != 0:
            raise w.Stop(c.FAILED, "deployed", f"gh run rerun failed: {c.last_line(r.stderr)}")
        return pipe["url"]
    if all_jobs:
        status, d = p.gl.call("POST", f"/projects/{p.enc}/pipeline", {"ref": target})
    else:
        status, d = p.gl.call("POST", f"/projects/{p.enc}/pipelines/{pipe['id']}/retry")
    if status in ("401", "403"):
        raise w.Stop(c.CREDENTIALS, "deployed", f"GitLab refused ({status}): your role may not run main's pipeline, or the token/Midway lapsed.")
    if status not in ("200", "201"):
        raise w.Stop(c.FAILED, "deployed", f"GitLab refused the retry ({status}): {str(d)[:300]}")
    return (d or {}).get("web_url") or pipe["url"]


def main(argv=None):
    with w.stop_signals_caught():
        return _main(argv)


def _main(argv):
    ap = w.Parser(prog="ci:rerun", allow_abbrev=False, result="sha=none url=none")   # `--a` must not mean --all, a redeploy (review 10d B13)
    ap.add_argument("--sha", default="")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    provider = os.environ.get("CRE_GIT_PROVIDER", "")
    p, code, url, sha = None, c.OK, "none", a.sha
    try:
        target = w.default_branch()
        r = c.run(["git", "fetch", "-q", "origin", target])
        if r.returncode != 0:
            raise w.Stop(c.REFUSED, "deployed", f"git fetch origin {target} failed, so its head is unknown: {c.last_line(r.stderr)}")
        head = git("rev-parse", f"refs/remotes/origin/{target}")
        if not head:
            raise w.Stop(c.REFUSED, "deployed", f"No origin/{target} to re-run.")
        sha = (git("rev-parse", "--verify", f"{sha}^{{commit}}") or sha) if sha else head
        if provider == "github":
            p = w.GitHub()
        elif provider == "gitlab":
            p = w.GitLab(c.Step("ci", echo=False))
        else:
            raise w.Stop(c.REFUSED, "deployed", "CRE_GIT_PROVIDER is not set (run it as npx projen ci:rerun).")
        url = rerun(p, sha, a.all, head, target)
        w.say(f"✅ {'Re-running the deploy' if a.all else 'Retrying the failed deploy jobs'} of {sha[:9]}: {url}")
        w.say(f"   Follow it: npx projen watch --sha {sha}")
    except w.Stop as e:
        print(f"❌ {e.msg}", file=sys.stderr)
        code = e.code
    except KeyboardInterrupt:
        # With the RESULT line, as every other stop (review 10g B12). Whether the provider took a
        # retry asked for just before is unknown: watch says.
        print(f"❌ Interrupted. Whether a retry started is unknown: npx projen watch --sha {sha or '<sha>'} shows it.", file=sys.stderr)
        code = c.FAILED
    except Exception:  # noqa: BLE001 - a bug must still end with the RESULT line
        traceback.print_exc()
        print("❌ Internal error (above).", file=sys.stderr)
        code = c.FAILED
    finally:
        if isinstance(p, w.GitLab):
            p.close()
    print(f"RESULT code={code} sha={sha or 'none'} url={url}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
