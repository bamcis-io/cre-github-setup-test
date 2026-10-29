#!/usr/bin/env python3
"""npx projen update-template [--from <path-to-cre-demo-template>] [--allow-branch]

Refresh this demo's copy of the CRE project-type package (projenrc/cre/) and its installed construct
library (cre-constructs/, the `@cre/constructs` dependency; new and changed constructs arrive,
removed ones go) from a cre-demo-template clone, then re-synth and build. The package is never
edited inside a demo; a change goes to the template via an MR, and every demo picks it up with this.

Template location, first match wins: --from <path>, $CRE_TEMPLATE_DIR, the clone recorded on this
machine in .cre/template.json (`templatePath`, written by attach-template.py; older projects:
projenrc/cre/SOURCE.json), or the sibling directory under the recorded `templateDirName` (the
layout the skill links rely on). When none exists, it stops and says how to point it at one; the
path used is written back.

Before copying, it pulls the clone (`git pull --ff-only` from origin's default branch, recorded as
`templateBranch`) when the clone is on that branch with no uncommitted changes to tracked files. A
clone on another branch (template work in progress) is refused: its unmerged changes would land in
this project. --allow-branch uses it as it is. A clone with local changes outside the package is
used as it is, with a warning. The pull also updates the skills every project links from the clone.
Then the clone's own (just pulled) copy of this script does the rest, so the newest logic runs.

It prints the template's CHANGELOG entries added since the version this project had, and refreshes
AGENTS.md and CLAUDE.md from the skeleton when the project hasn't edited them (an edited one is left
alone, the template's new version written to .cre/template/<file> to compare).

Exit 0 updated (or already current) | 10 refused, nothing changed | 20 synth/build failed after
copying (the diff is left to inspect; the restore command is printed).
"""
import os
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import crelib as c  # noqa: E402

DEST = os.path.join("projenrc", "cre")
CONSTRUCTS = "cre-constructs"


warn, info = c.warn, c.say   # crelib's printers (review 10c G5); info lines carry their own mark


git = c.git


def jget(path, key):
    return c.json_str(c.read_json(path), key)


def find_template(step, frm):
    # crelib.project_template_dir is the one lookup (review 10c G6); this adds --from and the messages.
    recorded, src = c.recorded_template(".")
    remote, name = c.json_str(src, "templateRemote"), c.json_str(src, "templateDirName")
    # A directory name, never a path: it comes from a committed file, and `../<name>` is where a
    # script is then run from (review 10b C23).
    if "/" in name or name in ("..", "."):
        step.die(c.REFUSED, f"projenrc/cre/SOURCE.json's templateDirName ('{name}') isn't a plain directory name. "
                 "Pass --from <path to your template clone>.")
    if not frm:
        frm = c.project_template_dir(".") or os.path.join("..", name or "cre-demo-template")
    if not c.is_template_clone(frm):
        step.die(c.REFUSED, f"No cre-demo-template clone at '{recorded or frm}' or '{frm}' (looked for packages/cre-projen/src). "
                 "Point it at your clone once: npx projen update-template --from <path-to-cre-demo-template>"
                 f"{f' (no clone? git clone {remote})' if remote else ''}. The path is recorded for next time.")
    return os.path.realpath(frm), remote


def pull(step, frm, remote, allow_branch):
    if git("-C", frm, "status", "--porcelain", "--", "packages/cre-projen", "constructs/src").stdout.strip():
        step.die(c.REFUSED, f"The template at {frm} has uncommitted changes under packages/cre-projen or constructs/src. "
                 "Commit or stash them there first.")
    for d in (DEST, CONSTRUCTS):
        # SOURCE.json is this tool's own record (rewritten on every run), never a hand edit.
        if git("status", "--porcelain", "--", d, f":(exclude){DEST}/SOURCE.json").stdout.strip():
            step.die(c.REFUSED, f"{d} has uncommitted changes. It is installed from the template and never edited here; discard "
                     f"them (git restore --source=HEAD -- {d}) or move the change to the template.")
    origin = git("-C", frm, "remote", "get-url", "origin").stdout.strip()
    if remote and origin and origin != remote:
        warn(f"{frm}'s origin is {origin}, not {remote} (the template this project was made from). Updating from it anyway.")
    if os.environ.get("CRE_UPDATE_REEXEC"):
        return
    tb = jget(os.path.join(DEST, "SOURCE.json"), "templateBranch")
    if not tb:
        h = git("-C", frm, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").stdout.strip()
        tb = h[len("origin/"):] if h.startswith("origin/") else h
    tb = tb or "main"
    on = git("-C", frm, "branch", "--show-current").stdout.strip()
    if not origin:
        warn("The template clone has no origin; updating from it as it is.")
    elif on != tb:
        if not allow_branch:
            step.die(c.REFUSED, f"The template clone ({frm}) is on branch '{on or 'detached HEAD'}', not {tb}: its unmerged changes "
                     f"would come into this project. Switch it back (git -C {frm} switch {tb}), or pass --allow-branch to test "
                     "that branch here.")
        warn(f"The template clone is on branch '{on or 'detached HEAD'}', not {tb} (--allow-branch): not pulling; updating from "
             "it as it is.")
    elif git("-C", frm, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        warn("The template clone has uncommitted changes: not pulling. Updating from it as it is.")
    else:
        r = git("-C", frm, "pull", "-q", "--ff-only", "origin", tb)
        if r.returncode == 0:
            short = git("-C", frm, "rev-parse", "--short", "HEAD").stdout.strip()
            info(f"Pulled the template clone ({frm}, {tb}): now at {short}.")
        else:
            warn(f"Could not pull the template clone ({c.last_line(r.stderr or r.stdout)}); updating from it as it is.")


def run_helper(step, pkg, script, *args):
    r = c.stream([sys.executable, os.path.join(pkg, "scripts", "template", script), *args])
    if r != 0:
        step.die(c.FAILED, {"attach-template.py": "Recording the template and linking its skills failed.",
                            "sync-constructs.py": "Installing the construct library failed."}[script])


def refresh_agents(frm, old_sha, root):
    """AGENTS.md / CLAUDE.md: the skeleton's, refreshed where the project kept them as scaffolded."""
    name = jget(os.path.join(root, "package.json"), "name")   # projen writes it from .projenrc.ts's name
    fill = lambda t: t.replace("__DEMO_NAME__", name)  # noqa: E731
    for f in ("AGENTS.md", "CLAUDE.md"):
        src = os.path.join(frm, "skeleton", "_shared", f)
        if not os.path.isfile(src):
            continue
        with open(src) as fh:
            new = fill(fh.read())
        cur_path = os.path.join(root, f)
        cur = open(cur_path).read() if os.path.isfile(cur_path) else None
        if cur == new:
            continue
        if cur is None and git("-C", root, "log", "-1", "--format=%h", "--diff-filter=D", "--", f).stdout.strip():
            # Deleted on purpose (a commit removed it): not brought back on every update (review 10b C24).
            continue
        old = None
        if old_sha:
            r = git("-C", frm, "show", f"{old_sha}:skeleton/_shared/{f}")
            old = fill(r.stdout) if r.returncode == 0 else None
        if cur is None or (old is not None and cur == old):
            with open(cur_path, "w") as fh:
                fh.write(new)
            info(f"Updated {f} from the template (it was as scaffolded).")
        else:
            side = os.path.join(root, ".cre", "template", f)
            os.makedirs(os.path.dirname(side), exist_ok=True)
            with open(side, "w") as fh:
                fh.write(new)
            warn(f"{f} was edited in this project, so it is left as is. The template's version changed: compare with "
                 f"diff {f} .cre/template/{f}   and merge what you want.")


def porcelain_paths(untracked):
    """Changed (untracked=False) or untracked paths, exactly as on disk. -z: git doesn't C-quote a
    name with a backslash, a quote or a non-ASCII byte, which the restore command must name as is
    (review 10c E5). A rename's entry carries its source as the next NUL-separated field."""
    fields = git("status", "--porcelain", "-z", "--untracked-files=all").stdout.split("\0")
    paths, i = set(), 0
    while i < len(fields):
        ln = fields[i]
        i += 1
        if len(ln) < 4:
            continue
        if ln[0] in "RC" and i < len(fields):   # "R  new\0old": both sides belong to the change
            if not untracked:
                paths.add(fields[i])
            i += 1
        if ln.startswith("??") == untracked:
            paths.add(ln[3:])
    return sorted(paths)


def changelog(frm, old_sha, new_sha, old_ver, new_ver):
    if not old_sha or git("-C", frm, "cat-file", "-e", f"{old_sha}^{{commit}}").returncode != 0:
        return
    diff = git("-C", frm, "diff", "--unified=0", old_sha, new_sha, "--", "CHANGELOG.md").stdout.splitlines()
    notes = [ln[1:] for ln in diff if ln.startswith("+") and not ln.startswith("+++")]
    if not notes:
        return
    info(f"\nWhat changed in the template (CHANGELOG.md, {old_ver} → {new_ver}):")
    for ln in notes[:80]:
        info(f"   {ln}")
    if len(notes) > 80:
        info(f"   … (more: git -C {frm} diff {old_sha[:9]} {new_sha[:9]} -- CHANGELOG.md)")


def main(step, argv):
    p = c.Parser(step, "update-template")
    p.add_argument("--from", dest="frm", default="",
                   help="the cre-demo-template clone to update from (default: $CRE_TEMPLATE_DIR, the recorded clone, else the sibling directory)")
    p.add_argument("--allow-branch", action="store_true",
                   help="use a clone that is on a branch other than main, as it is, without pulling")
    a = p.parse_args(argv)
    allow_branch = a.allow_branch or bool(os.environ.get("CRE_TEMPLATE_ALLOW_BRANCH"))
    r = git("rev-parse", "--show-toplevel")
    if r.returncode != 0:
        step.die(c.REFUSED, "Run inside the demo's git repository.")
    root = r.stdout.strip()
    os.chdir(root)
    if not os.path.isfile(os.path.join(DEST, "SOURCE.json")):
        step.die(c.REFUSED, f"{DEST}/SOURCE.json not found; this demo was not scaffolded with the CRE project-type package.")
    frm, remote = find_template(step, a.frm or os.environ.get("CRE_TEMPLATE_DIR", ""))
    pkg = os.path.join(frm, "packages", "cre-projen")
    pull(step, frm, remote, allow_branch)

    # Run the clone's own (just pulled) copy, not this project's vendored one: the newest logic does
    # the copy, the breadcrumb and the skill links. The second run skips the pull.
    me = os.path.join(pkg, "scripts", "template", "update-template.py")
    if not os.environ.get("CRE_UPDATE_REEXEC") and os.path.isfile(me):
        env = {**os.environ, "CRE_UPDATE_REEXEC": "1", "CRE_TEMPLATE_ALLOW_BRANCH": "1" if allow_branch else ""}
        c.exec_replace([sys.executable, me, "--from", frm], env)

    old_sha = jget(os.path.join(DEST, "SOURCE.json"), "templateSha")
    old_ver = jget(os.path.join(DEST, "SOURCE.json"), "packageVersion")
    new_sha = git("-C", frm, "rev-parse", "HEAD").stdout.strip()
    new_ver = jget(os.path.join(pkg, "package.json"), "version")
    if old_sha == new_sha:
        # Same template commit, but the clone may have moved or the skill links gone stale.
        run_helper(step, pkg, "attach-template.py", frm, root)
        c.ok(f"Already at template {new_sha[:9]} (package {new_ver}).")
        return

    # What was already changed before the update: the restore command puts back only what the
    # update itself changed (review 10b C21).
    before_tracked, before_new = set(porcelain_paths(False)), set(porcelain_paths(True))
    for d in ("src", "scripts", "steering", "setup-cdk"):   # scaffold.py PACKAGE_DIRS
        try:
            c.mirror(os.path.join(pkg, d), os.path.join(DEST, d), skip_ignored=True)
            if d == "scripts":
                c.drop_template_only(os.path.join(DEST, d))
        except OSError as e:
            step.die(c.FAILED, f"Copying the package's {d}/ failed: {e}")
    run_helper(step, pkg, "sync-constructs.py", frm, CONSTRUCTS)
    try:
        refresh_agents(frm, old_sha, root)
    except OSError as e:
        step.die(c.FAILED, f"Refreshing AGENTS.md / CLAUDE.md failed: {e}")
    run_helper(step, pkg, "attach-template.py", frm, root)

    info(f"Copied package {old_ver} ({old_sha[:9]}) → {new_ver} ({new_sha[:9]}). Re-synthesising and building...")
    log = os.path.join(git("rev-parse", "--git-common-dir").stdout.strip(), "cre-update-template.log")
    open(log, "w").close()
    if c.stream(["npx", "projen"], log=log) != 0 or c.stream(["npx", "projen", "build"], log=log) != 0:
        with open(log, errors="replace") as f:
            info("".join(f.readlines()[-30:]).rstrip("\n"))
        tracked = [x for x in porcelain_paths(False) if x not in before_tracked]
        added = [x for x in porcelain_paths(True) if x not in before_new]
        parts = []
        if tracked:
            parts.append("git restore --source=HEAD --staged --worktree -- " + " ".join(shlex.quote(x) for x in tracked))
        if added:
            parts.append("git clean -fdq -- " + " ".join(shlex.quote(x) for x in added))
        parts.append("npx projen")
        step.die(c.FAILED, f"Synth or build failed on the new version (log: {log}). Inspect with git diff, or go back with:\n"
                 f"   {' && '.join(parts)}")
    subprocess.run(["git", "--no-pager", "diff", "--stat", "--", "."], stdout=sys.stderr)
    changelog(frm, old_sha, new_sha, old_ver, new_ver)
    c.ok(f"Updated to template {new_sha[:9]}. Review the diff, then push it with npx projen push.")


if __name__ == "__main__":
    c.cli("update-template", main)
