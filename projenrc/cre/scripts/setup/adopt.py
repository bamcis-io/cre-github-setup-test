"""Adopting a repo that already has commits into the template (new_project.py's scaffold, push and
restore phases).

One flow, every time the same, whatever the repo holds (a UI README, an earlier template's scaffold,
the builder's own work), in a clone of it: the answers' existingClone, or one setup makes beside the
template. Its steps, each recorded in the state file (`adopt`) before the next starts, so a re-run
does the first step not recorded and never guesses from branch names or tree compares:

  start     local main's commits that origin doesn't have kept on PRE_ADOPT_BRANCH; the clone's
            uncommitted work stashed (untracked files too); origin/main recorded as the base
  scaffold  ADOPT_BRANCH made from the base, the earlier template's untouched files and the build's
            own paths removed, the scaffold written (the build phase follows)
  commit    the built scaffold committed on ADOPT_BRANCH
  review    ADOPT_BRANCH pushed with a lease (never over a commit setup didn't push), its MR/PR
            opened, merged once its pipeline (checks) passed on the pushed commit (a person may
            have to approve)
  merged    local main moved to origin's
  restored  the stashed work back on RESTORE_BRANCH, made from the new main, file by file

Files git ignores are never touched: the stash doesn't take them and nothing cleans the tree. Adopting
replaces what the template owns; anything replaced or removed stays in the history before the merge.
"""
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402

PRE_ADOPT_BRANCH = "cre/pre-adopt-main"
RESTORE_BRANCH = "chore/restore-local-work"
STASH_MESSAGE = "cre-adopt: uncommitted work before adopting the template"
ATTEMPT_MESSAGE = "cre-adopt: the clone's tree before a repeated scaffold"
# The first commit of a project an earlier template scaffolded (recipe or new_project.py).
TEMPLATE_SCAFFOLD_COMMIT = re.compile(r"^chore: (initial scaffold|scaffold .+ from cre-demo-template)\b")
# What the project's build owns at the top level (build-tasks.ts CLEAN, and dist/): removed from an
# adopted tree, since every build deletes or rewrites them.
BUILD_OWNED = ("assets", "cdk.out", "dist")


def data(r):
    return r.state.data.setdefault("adopt", {})


def save(r):
    r.state.save()


def git(r, *args, what=None):
    res = c.git(*args, cwd=r.demo)
    if what and res.returncode != 0:
        sys.stderr.write(res.stdout + res.stderr)
        r.step.die(c.FAILED, f"{what} failed (git {' '.join(args[:2])}, exit {res.returncode}).")
    return res


def out(r, *args):
    return git(r, *args).stdout.strip()


def has_ref(r, ref):
    return git(r, "rev-parse", "-q", "--verify", ref).returncode == 0


def commits(r, rng):
    return out(r, "log", "--format=%h %s", rng).splitlines()


def dirty(r):
    """The clone has changes or untracked files, beyond what it ignored when the adoption started
    (a .gitignore line in the first stash makes those show as untracked)."""
    ignored = [p.rstrip("/") for p in data(r).get("ignored", [])]
    for line in git(r, "status", "--porcelain", "-z", "--untracked-files=all").stdout.split("\0"):
        path = line[3:]
        if line and not any(path == p or path.startswith(p + "/") for p in ignored):
            return True
    return False


def check_clone(r):
    """existingClone, before anything is created: this repo's clone, nothing in progress, no
    conflicts, and the branches adopting makes free (or this adoption's own)."""
    step = r.step
    origin = out(r, "remote", "get-url", "origin")
    if not re.search(rf"(^|[:/]){re.escape(r.repo)}$", origin.rstrip("/").removesuffix(".git")):
        step.die(c.REFUSED, f"existingClone {r.demo} isn't a clone of {r.repo} (its origin is {origin or 'not set'}).")
    op = c.in_progress_op(cwd=r.demo)
    if op:
        step.die(c.REFUSED, f"{r.demo} has a git {op} in progress. Finish or abort it, then re-run.")
    if out(r, "ls-files", "-u"):
        step.die(c.REFUSED, f"{r.demo} has unresolved conflicts. Resolve them, then re-run.")
    if not data(r).get("base"):
        taken = [b for b in (c.ADOPT_BRANCH, RESTORE_BRANCH, PRE_ADOPT_BRANCH) if has_ref(r, f"refs/heads/{b}")]
        if taken:
            step.die(c.REFUSED, f"{r.demo} already has branch {', '.join(taken)}, which adopting makes. Rename or delete it, then re-run.")


def start(r):
    """Before anything changes in the clone: what only it has, kept by name; the base recorded."""
    step, ad = r.step, data(r)
    if ad.get("stashing") and not ad.get("stash"):
        # Stopped between the stash and recording it: find it by its message.
        for line in out(r, "stash", "list", "--format=%H %gs").splitlines():
            sha, _, subject = line.partition(" ")
            if subject.endswith(STASH_MESSAGE):
                ad["stash"] = sha
                break
    ad.pop("stashing", None)
    # What git ignores in the clone as found (node_modules, a build's output, a .env): never in the
    # adoption's commit, though the scaffold's .gitignore may not ignore it.
    if "ignored" not in ad:
        ad["ignored"] = [p for p in git(r, "ls-files", "-z", "-o", "-i", "--exclude-standard", "--directory").stdout.split("\0") if p]
        save(r)
    if has_ref(r, "refs/heads/main") and commits(r, "origin/main..main"):
        # Committed on the clone's main and never pushed: not in origin's history, so kept by name.
        if has_ref(r, f"refs/heads/{PRE_ADOPT_BRANCH}") and out(r, "rev-parse", PRE_ADOPT_BRANCH) != out(r, "rev-parse", "main"):
            step.die(c.REFUSED, f"{r.demo} already has a {PRE_ADOPT_BRANCH} that isn't its main. Rename or delete it, then re-run.")
        git(r, "branch", "-q", "-f", PRE_ADOPT_BRANCH, "main", what=f"Keeping main's unpushed commits on {PRE_ADOPT_BRANCH}")
        ad["preAdoptMain"] = out(r, "rev-parse", PRE_ADOPT_BRANCH)
        step.warn(f"main has commits origin doesn't ({'; '.join(commits(r, 'origin/main..main')[:5])}): kept on {PRE_ADOPT_BRANCH}. "
                  "main becomes origin's once the adoption merges; bring them over from that branch when you need them.")
    if out(r, "status", "--porcelain") and not ad.get("stash"):
        ad["stashing"] = True
        save(r)
        git(r, "stash", "push", "-q", "-u", "-m", STASH_MESSAGE, what="Stashing the clone's work")
        ad["stash"] = out(r, "rev-parse", "refs/stash")
        ad.pop("stashing", None)
        step.ok(f"Stashed the clone's uncommitted work ({ad['stash'][:9]}); it comes back on {RESTORE_BRANCH}.")
    ad["base"] = out(r, "rev-parse", "origin/main")
    save(r)


def template_owned(r, base):
    """The files an earlier cre-demo-template scaffold made that no commit changed since: the
    template's, replaced by this scaffold (deleted when it no longer has them). Only when the repo's
    first commit is such a scaffold (TEMPLATE_SCAFFOLD_COMMIT); otherwise none."""
    roots = out(r, "rev-list", "--max-parents=0", base).split()
    if len(roots) != 1 or not TEMPLATE_SCAFFOLD_COMMIT.match(out(r, "log", "-1", "--format=%s", roots[0])):
        return []
    files = git(r, "ls-tree", "-r", "-z", "--name-only", roots[0]).stdout.split("\0")
    changed = set(git(r, "diff", "--name-only", "-z", roots[0], base).stdout.split("\0"))
    return sorted(f for f in files if f and f not in changed)


def scaffold(r, write_scaffold):
    """The scaffold phase: clone (when no existingClone), start, then ADOPT_BRANCH from the base with
    the scaffold written. A repeat (a failed scaffold or build, --from scaffold before the commit)
    starts the branch again from the base; the tree it finds is stashed first, never discarded."""
    step, ad = r.step, data(r)
    if not r.adopt_clone and not os.path.isdir(os.path.join(r.demo, ".git")):
        url = r.values.get("REPO_GIT_URL") or r.values.get("REPO_SSH_URL")
        if not url:
            step.die(c.FAILED, "The repo phase gave no URL to clone the repo from; re-run.")
        res = c.run(["git", "clone", "-q", url, r.demo], env={"GIT_TERMINAL_PROMPT": "0"})
        if res.returncode != 0:
            step.die(c.FAILED, f"Cloning {url} failed: {c.last_line(res.stderr)}")
        r.state.data["demoCreated"] = os.path.realpath(r.demo)
        ad["cloned"] = True
        save(r)
        step.ok(f"Cloned {r.repo} to {r.demo}, to adopt it.")
    if git(r, "fetch", "-q", "origin").returncode != 0:
        step.die(c.FAILED, "Could not fetch origin.")
    if not ad.get("base"):
        start(r)
    base = ad["base"]
    if has_ref(r, f"refs/heads/{c.ADOPT_BRANCH}"):
        mine = commits(r, f"{base}..refs/heads/{c.ADOPT_BRANCH}")
        elsewhere = [b for b in out(r, "branch", "--format=%(refname:short)", "--contains", f"refs/heads/{c.ADOPT_BRANCH}").splitlines()
                     if b != c.ADOPT_BRANCH]
        if mine and not elsewhere:
            # Only on this branch, which is made again from the base: kept by the builder first.
            step.die(c.REFUSED, f"{c.ADOPT_BRANCH} has commits that aren't the scaffold: {'; '.join(mine[:5])}. Setup makes that "
                     f"branch again from origin/main: keep them on a branch of their own (git branch <name> {c.ADOPT_BRANCH}), "
                     "then re-run.")
    if dirty(r):
        # An earlier attempt's tree (a scaffold or build that stopped), or edits since: kept, by name.
        # What the clone ignored stays where it is (its .gitignore line may be in the first stash).
        git(r, "stash", "push", "-q", "-u", "-m", ATTEMPT_MESSAGE, "--", ":/", *[f":(top,exclude){p}" for p in ad.get("ignored", [])],
            what="Stashing the clone's tree")
        sha = out(r, "rev-parse", "refs/stash")
        ad.setdefault("attempts", []).append(sha)
        save(r)
        step.warn(f"Stashed what the clone held before scaffolding again ({sha[:9]}: an earlier attempt's files, or edits made "
                  "since). Nothing in it is restored by setup: drop it once you don't need it (git stash list).")
    git(r, "switch", "-q", "-C", c.ADOPT_BRANCH, "--no-track", base, what=f"Making {c.ADOPT_BRANCH}")
    owned = template_owned(r, base)
    built = [f for f in git(r, "ls-tree", "-r", "-z", "--name-only", base, "--", *BUILD_OWNED).stdout.split("\0") if f]
    gone = sorted(set(owned) | set(built))
    for i in range(0, len(gone), 200):
        git(r, "rm", "-q", "--ignore-unmatch", "--", *gone[i:i + 200], what="Removing the files the template replaces")
    if owned:
        step.info(f"Replacing {len(owned)} file(s) an earlier cre-demo-template scaffold made and nothing changed since.")
    if built:
        step.warn("Removed what the repo tracked under " + ", ".join(f"{d}/" for d in BUILD_OWNED) + " (the project's build "
                  "owns them): " + ", ".join(built[:20]) + (" ..." if len(built) > 20 else "") + ". They stay in the history "
                  "before the adoption.")
    write_scaffold(r, r.demo)
    kept = [ln[3:] for ln in out(r, "status", "--porcelain").splitlines() if ln.startswith(" M")]
    if kept:
        step.warn("The scaffold's version replaced the repo's own in: " + ", ".join(kept[:20]) + (" ..." if len(kept) > 20 else "")
                  + " (the repo's stays in its history).")
    step.ok(f"Scaffolded {r.demo} on {c.ADOPT_BRANCH} (from origin/main {base[:9]}); the build and the MR/PR come next.")


def title(r):
    return f"chore: adopt the CRE template into {r.repo}"


def push(r, review):
    """The push phase: commit, then the provider's review step (`review(ad)`: push, MR/PR, merge;
    its outputs), then local main from origin. True once main has the adoption."""
    step, ad = r.step, data(r)
    if not ad.get("commit"):
        cur = out(r, "branch", "--show-current")
        if cur != c.ADOPT_BRANCH:
            step.die(c.REFUSED, f"The scaffold isn't committed yet, and {r.demo} is on {cur or 'a detached HEAD'}, not "
                     f"{c.ADOPT_BRANCH}. Switch back (git switch {c.ADOPT_BRANCH}), then re-run.")
        git(r, "add", "-A", what="Staging the scaffold")
        ignored = ad.get("ignored", [])
        for i in range(0, len(ignored), 200):
            git(r, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *ignored[i:i + 200], what="Leaving out what the clone ignored")
        res = git(r, "commit", "-q", "-m", title(r))
        if res.returncode != 0:
            sys.stderr.write(res.stdout + res.stderr)
            step.die(c.FAILED, "Committing the adopted scaffold failed (git's output above: a commit hook, or commit signing).")
        ad["commit"] = out(r, "rev-parse", "HEAD")
        save(r)
        step.ok(f"Committed the scaffold on {c.ADOPT_BRANCH} ({ad['commit'][:9]}).")
    if not ad.get("merged"):
        outs = review(ad)
        for key, field in (("ADOPT_PUSHED", "pushed"), ("ADOPT_REVIEW", "review"), ("ADOPT_MERGED", "merged")):
            if outs.get(key):
                ad[field] = outs[key]
        save(r)
        if not ad.get("merged"):
            return False
    if not ad.get("mainMoved"):
        move_main(r)
    return True


def move_main(r):
    """Local main to origin's, once the adoption merged. main's own unpushed commits were kept on
    PRE_ADOPT_BRANCH at the start; ones made since are refused, never dropped."""
    step, ad = r.step, data(r)
    if git(r, "fetch", "-q", "origin").returncode != 0:
        step.die(c.FAILED, "Could not fetch origin.")
    if has_ref(r, "refs/heads/main"):
        extra = [x for x in commits(r, "origin/main..main")
                 if not (ad.get("preAdoptMain") and git(r, "merge-base", "--is-ancestor", x.split()[0], ad["preAdoptMain"]).returncode == 0)]
        if extra:
            step.die(c.REFUSED, f"main has commits made during the adoption that origin doesn't: {'; '.join(extra[:5])}. Move them to "
                     "a branch (git branch <name> main), then re-run: main becomes origin's.")
    if out(r, "branch", "--show-current") == "main":
        if git(r, "reset", "-q", "--keep", "origin/main").returncode != 0:
            step.die(c.REFUSED, f"{r.demo} is on main with uncommitted edits that the adoption changes too. Commit or stash them, "
                     "then re-run.")
    else:
        git(r, "branch", "-q", "-f", "main", "origin/main", what="Moving main to origin's")
    git(r, "branch", "-q", "--set-upstream-to", "origin/main", "main")
    git(r, "config", "branch.main.crePushedSha", out(r, "rev-parse", "origin/main"))
    ad["mainMoved"] = True
    save(r)


def restore(r):
    """The restore phase: the stashed work back on RESTORE_BRANCH, made from the new main, file by
    file. A file the adoption left as the stash found it gets the stashed version, its mode (an
    executable, a symlink) and its deletion included; one the adoption changed too keeps the
    adoption's, the stashed version saved under .cre/adopt-conflicts/ (NEEDS=ADOPT_LOCAL_CHANGES).
    The stash is dropped only when all of it came back, staged versions included."""
    step, ad = r.step, data(r)
    if ad.get("restored"):
        return
    sha = ad.get("stash", "")
    if not sha:
        if out(r, "branch", "--show-current") != "main" and git(r, "switch", "-q", "main").returncode != 0:
            step.die(c.REFUSED, f"Switching {r.demo} to main failed (uncommitted edits the switch would overwrite). Commit or stash "
                     "them, then re-run.")
        finish(r, [], [])
        return
    if has_ref(r, f"refs/heads/{RESTORE_BRANCH}"):
        git(r, "switch", "-q", RESTORE_BRANCH, what=f"Switching to {RESTORE_BRANCH}")
    else:
        git(r, "switch", "-q", "-c", RESTORE_BRANCH, "main", what=f"Making {RESTORE_BRANCH}")
    base, index, untracked = f"{sha}^1", f"{sha}^2", f"{sha}^3"
    conflicts, staged = [], []
    paths = set()
    for a, b in ((base, sha), (base, index)):
        paths |= {p for p in git(r, "diff", "--name-only", "--no-renames", "-z", a, b).stdout.split("\0") if p}
    for path in sorted(paths):
        new, was, idx = entry(r, sha, path), entry(r, base, path), entry(r, index, path)
        if idx != new and idx != was:
            staged.append(path)   # staged one way, edited another: only the stash has both
        put(r, path, new, was, conflicts)
    if has_ref(r, untracked):
        for path in git(r, "ls-tree", "-r", "-z", "--name-only", untracked).stdout.split("\0"):
            if path:
                put(r, path, entry(r, untracked, path), None, conflicts)
    finish(r, conflicts, staged)


def entry(r, ref, path):
    """(mode, bytes) of `path` in `ref`, None when it isn't there."""
    line = git(r, "ls-tree", "-z", ref, "--", path).stdout.split("\0")[0]
    if not line:
        return None
    mode, _, rest = line.partition(" ")
    obj = rest.split("\t")[0].split()[-1]
    blob = subprocess.run(["git", "cat-file", "blob", obj], cwd=r.demo, capture_output=True).stdout
    return mode, blob


def on_disk(r, path):
    here = os.path.join(r.demo, path)
    if os.path.islink(here):
        return "120000", os.readlink(here).encode()
    if not os.path.isfile(here):
        return None
    with open(here, "rb") as f:
        body = f.read()
    return ("100755" if os.stat(here).st_mode & 0o111 else "100644"), body


def put(r, path, new, was, conflicts):
    """`path` as the stash had it (`new`; None: deleted), when the adoption left it as the stash
    started from (`was`); otherwise the stashed version saved beside."""
    here = on_disk(r, path)
    if here == new:
        return
    if here != was:
        if new is not None:
            dst = os.path.join(r.demo, ".cre", "adopt-conflicts", path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(new[1])
        conflicts.append(path if new is not None else f"{path} (deleted in your work)")
        return
    full = os.path.join(r.demo, path)
    if os.path.lexists(full):
        os.remove(full)
    if new is None:
        return
    os.makedirs(os.path.dirname(full) or r.demo, exist_ok=True)
    if new[0] == "120000":
        os.symlink(new[1].decode(), full)
        return
    with open(full, "wb") as f:
        f.write(new[1])
    os.chmod(full, 0o755 if new[0] == "100755" else 0o644)


def finish(r, conflicts, staged):
    step, ad = r.step, data(r)
    sha = ad.get("stash", "")
    if conflicts or staged:
        why = []
        if conflicts:
            why.append("where the adoption changed the same file it keeps its version, yours saved under .cre/adopt-conflicts/: "
                       + ", ".join(conflicts))
        if staged:
            why.append("these were staged one way and edited another, and only the stash has both: " + ", ".join(staged))
        step.warn(f"Your uncommitted work is back on {RESTORE_BRANCH}, except: " + "; ".join(why)
                  + f". The stash ({sha[:9]}) is kept. Carry over what you still need, then commit with npx projen push.")
        step.need("ADOPT_LOCAL_CHANGES")
    elif sha:
        ref = next((ln.split(":")[0] for ln in out(r, "stash", "list", "--format=%gd:%H").splitlines() if ln.endswith(sha)), "")
        if ref:
            git(r, "stash", "drop", "-q", ref)
        step.ok(f"Your uncommitted work is back on {RESTORE_BRANCH} (uncommitted): commit it with npx projen push.")
    if ad.get("preAdoptMain"):
        step.warn(f"main's commits that origin didn't have are on {PRE_ADOPT_BRANCH}.")
    for s in ad.get("attempts", []):
        step.info(f"Stash {s[:9]} holds the clone's tree from before a repeated scaffold; drop it once you don't need it.")
    ad["restored"] = True
    save(r)
