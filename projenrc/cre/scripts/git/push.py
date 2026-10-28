#!/usr/bin/env python3
"""npx projen push --message "<conventional commit>" [--title "<MR/PR title>"] [--branch <new>]
                  [--files "<paths>"] [--allow-secret-path <path>]...

preflight → (new branch when needed) → validate → stage → commit → sync with the branch's own
remote (merge, then validate again) → conflict check → re-check merged + auth → push → MR/PR open
and auto-merge armed.

Exit codes: 0 done | 10 refused, nothing changed | 20 validation/hook failed | 30 conflict |
            40 incomplete (committed, branched or pushed, then stopped): re-run resumes.
Last stdout line: RESULT code=<n> state=<STATE> branch=<b> sha=<sha> mr=<url|none>
"""
import os
import sys

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cregit as g  # noqa: E402
from cregit import CONFLICT, INCOMPLETE, REFUSED, Die  # noqa: E402


def bootstrap(t):
    cur = g.gout("branch", "--show-current")
    if cur != "main":
        raise Die(REFUSED, "BOOTSTRAP", f"origin is empty. The first push must come from a local 'main' (you are on '{cur or 'detached HEAD'}').")
    g.validate(t)
    g.stage(t)
    g.commit(t)
    if not g.head():
        raise Die(REFUSED, "BOOTSTRAP", "Nothing to push: main has no commits.")
    t.provider.auth_check()
    if g.git_streamed("push", "-u", "origin", "main") != 0:
        raise Die(REFUSED, "PUSH_FAILED", "git push -u origin main failed; the commit is kept locally. Re-run to retry.")
    g.record_pushed("main", g.head())
    t.state, t.branch = "BOOTSTRAPPED", "main"
    g.ok("Pushed the first commit directly to main (no MR/PR for the bootstrap commit).")


def refuse_if_rewritten(branch, seen):
    """Refuse, before anything is committed, when origin/<branch> was rewritten (a force-push that
    removed commits) and this clone still has what was removed: merging origin back in would put it
    back (review 10c B4). The baselines are cregit.rewritten_base's. Once HEAD no longer contains
    the baseline (the user rebased onto the rewrite, as the message says), there is nothing to put
    back and the push goes on: the guard can't block a branch forever (10d B2)."""
    present, rsha = g.remote_branch(branch)
    if not present:
        return
    if not g.succeeds("cat-file", "-e", f"{rsha}^{{commit}}") and \
            not g.succeeds("fetch", "-q", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"):
        raise Die(REFUSED, "ORIGIN_UNREACHABLE", f"Could not fetch origin/{branch}.")
    base = g.rewritten_base(branch, seen, rsha)
    if base:
        removed = g.rev_list(f"{rsha}..{base}")
        # A plain rebase drops merge commits, and a conflict resolution with them (review 10f
        # GS-4): keep them when the user's own commits include one.
        merges = "--rebase-merges " if g.gout("rev-list", "--merges", f"{base}..HEAD") else ""
        raise Die(REFUSED, "REMOTE_REWRITTEN", f"origin/{branch} was rewritten: {len(removed)} commit(s) you have "
                  f"({', '.join(g.short(c) for c in removed[:5])}) are no longer in its history, and pushing would put them back. "
                  f"Nothing was committed or pushed. Your own commits are git log --oneline {base[:9]}..HEAD. Look at what changed "
                  f"(git log --oneline {base[:9]}...origin/{branch}), agree with whoever force-pushed, then move your own commits "
                  f"onto it (git rebase {merges}--onto origin/{branch} {base[:9]}) and re-run.")


def body(t, argv):
    g.preamble(t, argv)
    a = t.args
    g.check_message_and_title(a)
    t.provider.auth_check()
    # What this clone had seen of its branch's remote before this run's fetch: the baseline for
    # "was it rewritten?" when this clone never pushed it (review 10d B6).
    cur = g.gout("branch", "--show-current")
    seen = t.seen = g.gout("rev-parse", "-q", "--verify", f"refs/remotes/origin/{cur}") if cur else ""
    g.fetch()
    t.default = g.default_branch()
    g.classify(t)
    # The refusals git:status reports too: one list, so the two never disagree (review 10g B8).
    g.refuse_push(t)
    if t.cls not in ("ON_DEFAULT", "MERGED", "CLOSED"):   # resolve_branch says it for those
        g.info(t.note)
    if t.cls == "BOOTSTRAP":
        return bootstrap(t)
    if t.cls == "NEW" and g.remote_branch(t.current)[0] and g.remote_branch(t.current)[1] == g.pushing_sha(t.current):
        # This clone's own interrupted first push: origin took the ref, the run stopped before it
        # recorded it. Resume as a pushed branch (review 10d B8).
        g.record_pushed(t.current, g.pushing_sha(t.current))
        g.git("branch", "-q", "--set-upstream-to", f"origin/{t.current}")
        g.info(f"origin/{t.current} is the commit an interrupted run of this clone was pushing; continuing it.")
        t.cls = "ACTIVE"
    if t.cls == "MERGED" and not t.carry and not g.is_dirty():
        # It merged (between runs, or while this one was starting) and nothing new is here: done,
        # not a reason to ask for a new branch.
        t.branch = t.current
        g.ok(f"{t.current} has already merged and there is nothing new to push.")
        t.provider.ensure_mr(t, push_output=None)
        t.state = "MERGED"
        return

    g.resolve_branch(t, a.branch)
    branch = t.branch
    refuse_if_rewritten(branch, seen if branch == cur else "")

    g.validate(t)
    g.stage(t)
    committed = g.commit(t)

    # ---- sync with the branch's own remote (a teammate's push, projen's self-mutation commit)
    present, rsha = g.remote_branch(branch)
    if present:
        if not g.succeeds("cat-file", "-e", f"{rsha}^{{commit}}") and \
                not g.succeeds("fetch", "-q", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"):
            raise Die(REFUSED, "ORIGIN_UNREACHABLE", f"Could not fetch origin/{branch}.")
        if not g.succeeds("merge-base", "--is-ancestor", rsha, "HEAD"):
            g.refuse_ignored_overwrite(rsha, f"origin/{branch}", "merging its new commits",
                                       "Nothing was pushed" + ("; the commit is kept locally." if committed else "."))
            g.info(f"origin/{branch} has commits you don't have; merging them in.")
            # A merge, not a rebase: a rebase linearises history and drops what a merge commit
            # changed itself (a conflict resolution), silently (review 10c B1). MRs/PRs squash, so
            # the extra merge commit never reaches main. Changes left out of --files are stashed
            # explicitly, not with --autostash (review 10b B1).
            stash, untracked = "", []
            pre_merge = g.head()
            # Nothing of ours that origin lacks: a fast-forward, not an empty merge commit pushed
            # as if it were work (review 10g B12).
            behind_only = g.succeeds("merge-base", "--is-ancestor", "HEAD", rsha)
            try:
                stash, untracked = g.stash_work(t, f"cre-git: merging origin/{branch} into {branch}")
                merged = g.succeeds("merge", "-q", "--no-edit", "--ff-only" if behind_only else "--no-ff", rsha)
            except BaseException:
                # Interrupted while stashing or mid-merge: leave the branch as it was, not half
                # merged (review 10c B3), and the uncommitted work back in the tree, not left in the
                # stash (10d B5). t.held names the stash even when the stop landed as it was made.
                # A merge that completed (the stop came in its post-merge hook) is whole: said as
                # done (review 10f B9).
                with g.signals_held():   # a second signal can't cut the rollback short (review 10g B3)
                    if g.succeeds("rev-parse", "-q", "--verify", "MERGE_HEAD"):
                        g.c.run(["git", "merge", "--abort"])
                    elif g.head() != pre_merge:
                        t.done.append(f"merged origin/{branch} ({g.short(g.head())})")
                    if t.held:
                        g.restore_work(t, t.held, g.stash_untracked(t.held))
                raise
            if not merged:
                g.git("merge", "--abort")
                kept = "" if not stash or g.restore_work(t, stash, untracked) else f" Your other uncommitted changes are in stash {stash}."
                raise Die(CONFLICT, "CONFLICT_WITH_OWN_REMOTE", f"Your commits conflict with new commits on origin/{branch}. The merge was "
                          "aborted and your branch is as it was" + (" (including the commit just made, not pushed)" if committed else "")
                          + f". Resolve with: git merge origin/{branch}, then re-run.{kept}")
            t.done.append(f"merged origin/{branch}")
            if stash and not g.restore_work(t, stash, untracked):
                # 40, not 30: the merge stays on the branch (review 10g B12).
                raise Die(INCOMPLETE, "UNCOMMITTED_CONFLICT_AFTER_MERGE", f"Merged origin/{branch}, but your uncommitted changes (outside "
                          f"--files) conflict with its new commits. Nothing was pushed. They are kept in the stash entry {stash}: "
                          f"git stash apply {stash}, resolve the conflicts, then re-run.")
            # The merged tree is new: build it before it is pushed.
            before = g.gout("status", "--porcelain")
            g.validate(t, why=f"the merged tree (with origin/{branch})")
            if g.gout("status", "--porcelain") != before:
                raise Die(INCOMPLETE, "REGENERATED_AFTER_MERGE", f"The build of the merged tree changed files (generated files differ "
                          f"after origin/{branch}'s commits). Nothing was pushed. Re-run the same command to commit and push them.")

    # ---- conflicts with the default branch block auto-merge forever
    conflicts = g.conflicts_with_default(t)
    if conflicts:
        raise Die(CONFLICT, "CONFLICT_WITH_DEFAULT", f"{branch} conflicts with origin/{t.default}, so its MR/PR could never merge. Not pushed"
                  + ("; the commit is kept locally" if committed else "") + ". Conflicting files:\n" + g.indent(conflicts)
                  + f"\n   Resolve with: git merge origin/{t.default}"
                  # A rebased branch that was pushed can't be pushed (the tasks never force), and the
                  # next push merges its old commits back with the same conflict (review 10g B11).
                  + ("" if g.was_pushed(branch) else " (or rebase: it was never pushed)") + ", then re-run.")

    # ---- anything to push?
    head_sha = g.head()
    if not present and not committed and not g.rev_list(f"refs/remotes/origin/{t.default}..HEAD"):
        raise Die(REFUSED, "NOTHING_TO_PUSH", f"Nothing to commit, and {branch} has no commits that origin/{t.default} lacks.")
    if present and rsha == head_sha:
        g.info(f"Nothing new: origin/{branch} already has {g.short(head_sha)}.")
        g.record_pushed(branch, head_sha)
        t.provider.ensure_mr(t, push_output=None)
        t.state = t.state_override or "UP_TO_DATE"
        return

    # ---- last checks right before the push. The MR may have auto-merged while this ran (build
    # time): pushing then would recreate a deleted branch and open a new MR of merged commits.
    # Fetched again: telling "deleted on merge" from "deleted unmerged" needs the current main.
    g.fetch()
    if g.was_pushed(branch) and g.branch_merged(t, branch, fresh=True)[0]:
        raise Die(REFUSED, "MERGED_DURING_RUN", f"{branch} merged while this was running ({t.merged_reason}). Nothing was pushed. Re-run with "
                  "--branch <new-name> and the new work moves to a new branch.")
    t.provider.auth_check()   # a Midway SSH certificate can expire during a long build

    # With a lease: the push succeeds only if origin's branch is still what this run saw (absent,
    # or rsha), so a merge (which deletes it) or a teammate's push in the meantime stops it instead
    # of recreating a merged branch (review 10b B14).
    g.record_pushing(branch, head_sha)
    output = t.provider.push(branch, "" if present else a.title, expect=rsha if present else "")
    g.record_pushed(branch, head_sha)
    t.done.append(f"pushed {branch}")
    t.provider.ensure_mr(t, push_output=output)
    t.state = "PUSHED"
    g.ok(f"Pushed {branch} → {t.default}; MR/PR open{f' ({t.mr})' if t.mr != 'none' else ''}.")


if __name__ == "__main__":
    g.run_task("push", body)
