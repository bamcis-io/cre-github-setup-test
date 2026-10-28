"""Shared library for the provider-neutral git tasks (branch, git:status, commit, push). Python 3.9+,
standard library only; imported by branch.py, status.py, commit.py and push.py.

Contract every task follows:
  - Read-only preflight first; nothing is changed until it passes.
  - Exit codes: 0 done (incl. no-op) | 10 refused, nothing changed | 20 validation or a commit hook
    failed | 30 conflict, the repo restored | 40 incomplete: this run already changed something (a
    branch made, a commit, a push) but didn't finish; re-running the same command resumes.
    A refusal after something was changed is reported as 40, never 10, with what was done.
  - The last line on stdout is always:
      RESULT code=<n> state=<STATE> branch=<b> sha=<sha> mr=<url|none>
  - Human messages go to stderr.
"""
import contextlib
import fnmatch
import shutil
import json
import os
import re
import signal
import subprocess
import time
import traceback
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "setup"))
import crelib as c  # noqa: E402

OK, REFUSED, FAILED, CONFLICT, INCOMPLETE = 0, 10, 20, 30, 40
MAX_TITLE = 255   # GitLab's MR title limit (GitHub's is 256)


class Die(Exception):
    def __init__(self, code, state, msg):
        super().__init__(msg)
        self.code, self.state, self.msg = code, state, msg


# crelib's printers, so the git tasks draw each severity as setup does (review 10c G5).
info, ok, warn = c.info, c.ok, c.warn


def indent(items, pad="     "):
    return "\n".join(pad + i for i in items)


# ---- children and signals ---------------------------------------------------------------------------
# A signal used to raise straight out of subprocess.run, which answers any exception by SIGKILLing
# the child: git died holding its *.lock files (a later run then read a stale lock as "origin
# unreachable"), `git stash push` died half done, and the build's process tree kept running after
# the task said INTERRUPTED (review 10d B5, B9). Now a signal that arrives while a child runs is
# held until the child exits, and only then stops the task, at a point the rollback understands.
# Local git commands are left to finish (they take milliseconds and leave the repo consistent);
# a network command or the build, which can run for minutes, is sent SIGTERM (git removes its locks
# on SIGTERM; the build gets it as a process group).
NETWORK = {"fetch", "push", "pull", "ls-remote"}
_SIGNAL = {"pending": 0, "child": None, "forward": False, "group": False, "hold": False, "parent": 0}

# How often a running child is checked for a dead parent (parent_gone), in seconds.
PARENT_POLL = 2


def parent_gone():
    """The process that started this task (npx projen) is gone: projen runs a task with spawnSync,
    which never signals the child, so killing `npx projen push` by its pid left the task running
    headless, building and pushing with no one reading it, and the next run refused LOCKED (review
    10g B10). Taken as a SIGTERM: the run stops at a point the rollback understands."""
    return bool(_SIGNAL["parent"]) and os.getppid() != _SIGNAL["parent"]


def check_parent():
    """Once per run (the rollback and the RESULT line after it run git too)."""
    if parent_gone():
        _SIGNAL["parent"] = 0
        warn("The command that started this task (npx projen) is gone; stopping as if it were interrupted.")
        on_signal(signal.SIGTERM, None)


@contextlib.contextmanager
def signals_held():
    """A rollback (or the final unlock) in progress: a signal is recorded, never raised, until it
    ends. A second SIGTERM or Ctrl-C used to raise out of the rollback itself, leaving the new
    branch made and the work in a stash while the task said "nothing changed" (review 10g B3). The
    run is already stopping, so a signal that arrived meanwhile is dropped at the end."""
    was = _SIGNAL["hold"]
    _SIGNAL["hold"] = True
    try:
        yield
    finally:
        _SIGNAL["hold"] = was
        if not was:
            _SIGNAL["pending"] = 0


def on_signal(signum, frame):
    if _SIGNAL["hold"]:
        _SIGNAL["pending"] = _SIGNAL["pending"] or signum
        return
    if _SIGNAL["child"] is not None:
        _SIGNAL["pending"] = _SIGNAL["pending"] or signum
        if _SIGNAL["forward"]:
            try:
                (os.killpg if _SIGNAL["group"] else os.kill)(_SIGNAL["child"].pid, signal.SIGTERM)
            except OSError:
                pass
        return
    raise Die(FAILED, "INTERRUPTED", f"Stopped by signal {signum}.")


def run_child(cmd, *, input=None, capture=True, text=True, stdout=None, stderr=None, forward=False, group=False):
    """subprocess.run that a signal never SIGKILLs (see above). Returns a CompletedProcess; a
    missing executable is 127 (review 10b F2). Raises Die INTERRUPTED after the child exits when a
    signal arrived meanwhile."""
    kw = {"errors": "replace"} if text else {}
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE if capture else stdout, stderr=subprocess.PIPE if capture else stderr,
                             text=text, start_new_session=group, **kw)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, 127, "" if text else b"", f"{cmd[0]}: command not found ({e})" if text else b"")
    _SIGNAL.update(child=p, forward=forward, group=group)
    build = record_group(p.pid) if group else ""
    try:
        while True:
            try:
                out, err = p.communicate(input, timeout=PARENT_POLL)
                break
            except subprocess.TimeoutExpired:
                input = None   # already written; communicate() must not be given it twice
                check_parent()
    finally:
        _SIGNAL["child"] = None
        if build:
            try:
                os.remove(build)
            except OSError:
                pass
    check_parent()
    if _SIGNAL["pending"] and not _SIGNAL["hold"]:
        signum, _SIGNAL["pending"] = _SIGNAL["pending"], 0
        raise Die(FAILED, "INTERRUPTED", f"Stopped by signal {signum}.")
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


# ---- git -----------------------------------------------------------------------------------------

def git(*args, input=None):
    """Never raises on git's exit code; a missing git is rc 127 (review 10b F2)."""
    return run_child(["git", *args], input=input, forward=bool(args) and args[0] in NETWORK)


def gout(*args):
    """stdout of a git command, stripped; "" when it failed."""
    r = git(*args)
    return r.stdout.strip() if r.returncode == 0 else ""


def succeeds(*args):
    return git(*args).returncode == 0


def git_streamed(*args):
    """A git command whose output (hooks, remote messages) goes straight to stderr. Returns the code."""
    return run_child(["git", *args], capture=False, stdout=sys.stderr, stderr=sys.stderr,
                     forward=bool(args) and args[0] in NETWORK).returncode


def head():
    return gout("rev-parse", "-q", "--verify", "HEAD")


def short(sha):
    return sha[:9] if sha else "none"


def rev_list(*args):
    return gout("rev-list", *args).split()


def print_local_state():
    """The branch and the tree as this clone knows them, read without origin or the API."""
    up = gout("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    counts = gout("rev-list", "--left-right", "--count", "HEAD...@{upstream}").split() if up else []
    lines = [f"Current:        {gout('branch', '--show-current') or '<detached>'}",
             f"Upstream:       {up + (f' ({counts[0]} ahead, {counts[1]} behind, as last fetched)' if len(counts) == 2 else '') if up else '<none>'}",
             f"Working tree:   {'uncommitted changes present' if is_dirty() else 'clean'}"]
    print("\n".join(lines), file=sys.stderr)


def is_dirty():
    return bool(gout("status", "--porcelain"))


# ---- arguments -----------------------------------------------------------------------------------

class Args:
    def __init__(self):
        self.files, self.allow, self.positional = [], [], []
        self.message = self.branch = self.title = ""


# Every switch the git tasks take (shared parser). task-reference.ts documents which task uses which;
# a jest test keeps the two equal.
GIT_FLAGS = {"--files": "files", "--message": "message", "-m": "message", "--branch": "branch", "-b": "branch",
             "--title": "title", "-t": "title", "--allow-secret-path": "allow"}


# The switches each git task takes, and how many positional arguments (review 10c B12/F10: a switch
# a task doesn't use is refused, not silently ignored; task-reference.ts documents the same sets).
TASK_SWITCHES = {"branch": {"branch"}, "git:status": set(), "commit": {"message", "files", "branch", "allow"},
                 "push": {"message", "title", "branch", "files", "allow"}}
TASK_POSITIONALS = {"branch": 1}
REPEATABLE = {"files", "allow"}


def parse_args(argv, task=None):
    """--files may repeat; each value is whitespace-separated, or newline-separated when a path
    contains a space. --name=value works too. With `task`, a switch that task doesn't take, a
    repeated one-value switch or an extra positional argument is refused (BAD_ARGS)."""
    a = Args()
    seen = set()
    i = 0
    while i < len(argv):
        x = argv[i]
        if x == "--":
            a.positional += argv[i + 1:]
            break
        if x in ("-h", "--help"):
            # Only in switch position: `--message -h` is a message (review 10c B8). The task's own
            # usage is the calling script's docstring; its last line is never a RESULT line, which
            # a caller would read as the run's outcome.
            usage = (getattr(sys.modules.get("__main__"), "__doc__", "") or "").strip()
            print(usage + "\n(usage only: nothing was run)")
            sys.exit(0)
        name, eq, inline = x.partition("=") if x.startswith("--") else (x, "", "")
        flags = GIT_FLAGS
        if name in flags:
            if eq:
                val, i = inline, i + 1
            elif i + 1 < len(argv):
                val, i = argv[i + 1], i + 2
            else:
                raise Die(REFUSED, "BAD_ARGS", f"{name} needs a value")
            key = flags[name]
            if task is not None and key not in TASK_SWITCHES.get(task, set(GIT_FLAGS.values())):
                raise Die(REFUSED, "BAD_ARGS", f"{task} doesn't take {name}.")
            if key in seen and key not in REPEATABLE:
                raise Die(REFUSED, "BAD_ARGS", f"{name} was given twice; give it once.")
            seen.add(key)
            if key == "files":
                if "\n" in val:
                    a.files += [ln for ln in val.splitlines() if ln]
                elif " " in val.strip() and exists_as_typed(val):
                    # One path with a space in it, quoted as one (review 10c B11); a deleted one too
                    # (tracked, not on disk), which was split in two (review 10g B12).
                    a.files.append(val)
                else:
                    a.files += val.split()
            elif key == "allow":
                a.allow.append(val)
            else:
                setattr(a, key, val)
        elif x.startswith("-") and x != "-":
            raise Die(REFUSED, "BAD_ARGS", f"Unknown argument: {x}")
        else:
            a.positional.append(x)
            i += 1
    if "files" in seen and not a.files:
        # --files that names nothing would fall back to every change: the opposite of passing it
        # (review 10f B5). A blank value beside real paths is harmless.
        raise Die(REFUSED, "BAD_ARGS", "--files was given no paths. Name the paths to commit (or '.' for every change).")
    if task is not None and len(a.positional) > TASK_POSITIONALS.get(task, 0):
        extra = a.positional[TASK_POSITIONALS.get(task, 0):]
        raise Die(REFUSED, "BAD_ARGS", f"{task} doesn't take {' '.join(extra)} (unexpected argument).")
    return a


def check_message_and_title(a):
    """Before anything changes: git refuses a push option with a newline only AFTER the commit,
    and GitLab rejects an over-long title only after the push (the MR is then never opened)."""
    if not a.message.strip():
        raise Die(REFUSED, "BAD_ARGS", "--message is required.")
    if "\n" in a.title:
        raise Die(REFUSED, "BAD_ARGS", "--title must be a single line (git push options cannot contain newlines).")
    a.title = (a.title or next((ln for ln in a.message.splitlines() if ln.strip()), "")).strip()
    if len(a.title) > MAX_TITLE:
        raise Die(REFUSED, "BAD_ARGS", f"The MR/PR title is {len(a.title)} characters; GitLab allows {MAX_TITLE}. "
                  "Shorten --title (or the message's first line).")


# ---- the task: state, result, lock ---------------------------------------------------------------

class Task:
    def __init__(self, name):
        self.name = name
        self.state, self.branch, self.sha, self.mr = "UNKNOWN", "", "", "none"
        self.done = []          # what this run changed, for an incomplete (40) report
        self.held = ""          # a stash holding the user's uncommitted work right now, named on any stop
        self.args = Args()
        self.provider = None
        self.default = ""
        self.lock_dir = ""
        # classification
        self.cls = self.current = self.carry = self.note = self.start_head = ""
        self.merged_reason = ""
        self.merged_head = ""      # the head the provider says the MR/PR merged at (classify, MERGED)
        self.seen = ""             # origin/<current> before this run's fetch (push sets it)
        self.state_override = ""   # a provider's finding that replaces the final state (MERGED)

    def result(self, code):
        sha = self.sha or head() or "none"
        print(f"RESULT code={code} state={self.state} branch={self.branch or 'none'} sha={sha} mr={self.mr or 'none'}", flush=True)

    def lock(self):
        """One run per clone (all worktrees share the common git dir). mkdir is atomic; a lock whose
        process is gone is reclaimed."""
        d = os.path.join(gout("rev-parse", "--path-format=absolute", "--git-common-dir") or ".git", "cre-git.lock")

        def take():
            # The lock appears with its pid already in it (a directory renamed into place), so no
            # one sees a lock without an owner and takes it as stale (review 10b B20).
            tmp = f"{d}.{os.getpid()}.new"
            shutil.rmtree(tmp, ignore_errors=True)
            os.mkdir(tmp)
            with open(os.path.join(tmp, "pid"), "w") as f:
                # With the process's start time: a pid the system later gave to an unrelated live
                # process no longer reads as this run still holding the lock (review 10d B13).
                f.write(f"{os.getpid()} {proc_start(os.getpid())}".rstrip())
            try:
                os.rename(tmp, d)
                return True
            except OSError:
                shutil.rmtree(tmp, ignore_errors=True)
                return False

        if not take():
            pid, started = lock_owner(d)
            if pid and owner_alive(pid, started):
                raise Die(REFUSED, "LOCKED", locked_message(pid, started, d))
            # Stale: moved aside under a name of our own first, so two runs reclaiming it at once
            # never delete each other's fresh lock.
            aside = f"{d}.{os.getpid()}.stale"
            try:
                os.rename(d, aside)
            except OSError:
                aside = ""
            if aside:
                # What was moved may be another run's fresh lock, taken between our read of the dead
                # pid and the rename: then it is theirs, put back, and we wait our turn (review 10c B7).
                moved, moved_start = lock_owner(aside)
                if (moved, moved_start) != (pid, started) and moved and owner_alive(moved, moved_start):
                    try:
                        os.rename(aside, d)
                    except OSError:
                        pass
                    raise Die(REFUSED, "LOCKED", locked_message(moved, moved_start, d))
                try:
                    stop_left_build(aside)
                except Die:
                    # Put back, so the next run finds the build again and retries.
                    try:
                        os.rename(aside, d)
                    except OSError:
                        pass
                    raise
                shutil.rmtree(aside, ignore_errors=True)
            if not take():
                raise Die(REFUSED, "LOCKED", f"Could not take the lock at {d} (another run took it just now?). Re-run.")
        self.lock_dir = _LOCK["dir"] = d

    def unlock(self):
        _LOCK["dir"] = ""
        if self.lock_dir:
            for f in ("pid", "build"):
                try:
                    os.remove(os.path.join(self.lock_dir, f))
                except OSError:
                    pass
            try:
                os.rmdir(self.lock_dir)
            except OSError:
                pass
            self.lock_dir = ""


def locked_message(pid, started, d):
    """LOCKED for a lock whose owner was just confirmed alive: who it is, and never "remove it if
    stale" (it isn't: a lock whose process is gone is reclaimed by the next run on its own, and
    removing a live one lets two runs build and push at once, review 10g B10)."""
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
        words = r.stdout.split() if r.returncode == 0 else []
        # The interpreter by name, not its full path, so the script it runs stays readable.
        command = " ".join([os.path.basename(words[0])] + words[1:])[:300] if words else ""
    except (OSError, subprocess.SubprocessError):
        command = ""
    who = f"pid {pid}" + (f", started {started}" if started else "") + (f": {command}" if command else "")
    return (f"Another git task is running in this clone ({who}). Wait for it to end (it prints its RESULT line), then re-run; "
            f"to stop it, kill -TERM {pid} (it puts back what it changed) and re-run. Never remove {d}: it is reclaimed on its "
            "own once that process is gone.")


def lock_owner(d):
    """(pid, start time) written in a lock directory; (0, "") when unreadable. A lock written
    before the start time was recorded has only the pid."""
    try:
        pid, _, started = open(os.path.join(d, "pid")).read().strip().partition(" ")
        return int(pid), started.strip()
    except (OSError, ValueError):
        return 0, ""


def proc_start(pid):
    """The process's start time as ps prints it (`ps -o lstart=`, on macOS and Linux), "" when
    unknown."""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(r.stdout.split()) if r.returncode == 0 else ""


def owner_alive(pid, started):
    """The lock's process is still running: its pid is alive and, when the lock recorded a start
    time and ps can tell, it is the same process (not a later one given the same pid)."""
    if not pid_alive(pid):
        return False
    if not started:
        return True
    now = proc_start(pid)
    return not now or now == started


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# The lock directory this run holds, so a build (its own process group) is recorded in it: a run
# killed with SIGKILL (an agent's command timeout, an IDE's stop) can't stop its build, which kept
# writing into the tree while the next run reclaimed the lock and built beside it (review 10f B7).
_LOCK = {"dir": ""}


def record_group(pgid):
    """Writes `<pgid> <leader start>` to the lock's `build` file; its path, "" without a lock."""
    if not _LOCK["dir"]:
        return ""
    path = os.path.join(_LOCK["dir"], "build")
    try:
        with open(path, "w") as f:
            f.write(f"{pgid} {proc_start(pgid)}".rstrip())
    except OSError:
        return ""
    return path


def group_alive(pgid, started):
    """A process of the recorded group still runs. Its id can't be given to another group while one
    member lives; once the leader is gone that is the only case. A live leader started at another
    time is a later, unrelated group."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    if pid_alive(pgid) and started:
        now = proc_start(pgid)
        if now and now != started:
            return False
    return True


def stop_left_build(d):
    """The build a killed run left running (the stale lock's `build` file): SIGTERM to its group,
    then up to 15 s for it to stop. LOCKED when it doesn't."""
    try:
        with open(os.path.join(d, "build")) as f:
            pgid, _, started = f.read().strip().partition(" ")
        pgid = int(pgid)
    except (OSError, ValueError):
        return
    if pgid <= 1 or not group_alive(pgid, started.strip()):
        return
    info(f"A git task stopped without ending its build (process group {pgid}, still running): stopping it before this run builds.")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(150):
        if not group_alive(pgid, started.strip()):
            return
        time.sleep(0.1)
    raise Die(REFUSED, "LOCKED", f"The build a stopped git task left (process group {pgid}) is still running after SIGTERM. Stop it "
              f"(kill -TERM -{pgid}, or kill -KILL -{pgid}), then re-run.")


def held_note(t):
    if not t.held:
        return ""
    return (f"\n   Your uncommitted changes are NOT in the working tree: they are saved in the stash entry {t.held}. "
            f"Restore them with: git stash apply {t.held}")


def run_task(name, body):
    """Run body(task, argv); report and exit with the contract's code and RESULT line."""
    t = Task(name)
    code = OK

    # SIGTERM, a closed terminal (SIGHUP, review 10c B6) and Ctrl-C all stop the run the same way, at
    # a point the rollback understands (on_signal), so the work is put back, or named.
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, on_signal)
    _SIGNAL["parent"] = os.getppid()
    try:
        body(t, sys.argv[1:])
    except Die as d:
        code, t.state, msg = d.code, d.state, d.msg
        if t.done:
            # A refusal or an interrupt after something changed: the run is incomplete, not "nothing changed".
            if code == REFUSED or d.state == "INTERRUPTED":
                code = INCOMPLETE
            msg += f"\n   Already done by this run: {'; '.join(t.done)}. Fix the cause, then re-run the same command; it resumes."
        print(f"❌ {msg}{held_note(t)}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        code, t.state = (INCOMPLETE if t.done else FAILED), "INTERRUPTED"
        print("❌ Interrupted." + (f" Already done: {'; '.join(t.done)}." if t.done else "") + held_note(t), file=sys.stderr)
    except Exception:  # noqa: BLE001 - a bug must still end with the contract's code and RESULT line
        code, t.state = (INCOMPLETE if t.done else FAILED), "INTERNAL_ERROR"
        traceback.print_exc()
        print("❌ Internal error (above)." + (f" Already done: {'; '.join(t.done)}." if t.done else "") + held_note(t), file=sys.stderr)
    finally:
        if t.provider is not None:
            try:
                t.provider.close()
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                pass
        with signals_held():
            t.unlock()
    if not t.branch:
        t.branch = gout("branch", "--show-current")
    t.result(code)
    sys.exit(code)


# ---- environment ---------------------------------------------------------------------------------

def require_git():
    m = re.search(r"(\d+)\.(\d+)", gout("--version"))
    # merge-tree --write-tree (merged and conflict detection) needs git >= 2.38.
    if not m or (int(m.group(1)), int(m.group(2))) < (2, 38):
        raise Die(REFUSED, "GIT_TOO_OLD", f"{gout('--version') or 'git'} is too old; these tasks need git >= 2.38 (merge-tree --write-tree).")


def require_identity():
    """A commit, a merge and a stash all need user.name and user.email. Without them git refuses
    each one, which the tasks reported as "a commit hook rejected it", after a build of minutes
    (review 10d B13). Checked first, before anything changes."""
    r = git("var", "GIT_COMMITTER_IDENT")
    a = git("var", "GIT_AUTHOR_IDENT")
    if r.returncode != 0 or a.returncode != 0:
        raise Die(REFUSED, "GIT_IDENTITY", "git has no user identity, so it can't commit, merge or stash. Nothing was changed. Set it, then re-run:\n"
                  '   git config --global user.name "Your Name"\n   git config --global user.email you@amazon.com')


def provider_name():
    """Set by the projen class in the task env; otherwise derived from origin, so the tasks also work
    in a repo that isn't projen-managed (cre-demo-template itself)."""
    if os.environ.get("CRE_GIT_PROVIDER"):
        return os.environ["CRE_GIT_PROVIDER"]
    url = gout("remote", "get-url", "origin")
    return "github" if "github.com" in url else "gitlab" if "gitlab" in url else "unknown"


def preamble(t, argv):
    """Arguments, repo root (caller-relative --files re-anchored), git version, origin, lock,
    provider, and no half-finished git operation."""
    t.args = parse_args(argv, t.name)
    prefix = gout("rev-parse", "--show-prefix")
    root = gout("rev-parse", "--show-toplevel")
    if not root:
        raise Die(REFUSED, "NOT_A_REPO", "Not inside a git repository.")
    # Paths are relative to where the command ran. Under `npx projen` that is always the repo root
    # (projen runs tasks there), so they are repo-root relative in practice.
    if prefix:
        t.args.files = [f if os.path.isabs(f) else prefix + f for f in t.args.files]
    os.chdir(root)
    require_git()
    if t.name != "git:status":   # status only reads
        require_identity()
    if t.args.files:
        # Before anything (a build takes minutes, and a new branch would make this a 40): a typo, an
        # ignored path or one outside the repo is refused now (review 10c B11).
        check = glob_specs(addable(t.args.files))
        r = git("add", "--dry-run", "--", *check) if check else None
        if r is not None and r.returncode != 0:
            raise Die(REFUSED, "BAD_PATHSPEC", f"git add refuses the --files paths ({' '.join(t.args.files)}): {r.stderr.strip()}")
    if not succeeds("remote", "get-url", "origin"):
        raise Die(REFUSED, "NO_ORIGIN", "No 'origin' remote is configured.")
    t.lock()
    name = provider_name()
    import cregit_providers as providers
    if name == "gitlab":
        t.provider = providers.GitLab(t)
        try:
            t.provider.api()   # the token and the project, before anything changes (review 10e B1)
        except Die:
            if t.name == "git:status":
                # Status only reads: what is known without the API is said first, so an agent
                # still learns the branch and the tree (review 10f B11).
                print_local_state()
            raise
    elif name == "github":
        t.provider = providers.GitHub(t)
    else:
        raise Die(REFUSED, "UNKNOWN_PROVIDER", f"Cannot tell GitHub from GitLab (origin: {gout('remote', 'get-url', 'origin') or 'none'}). "
                  "Set CRE_GIT_PROVIDER=gitlab|github.")
    op = in_progress_op()
    if op:
        how = "Finish it (git bisect reset)" if op == "bisect" else f"Finish it (git {op} --continue) or abort it (git {op} --abort)"
        raise Die(REFUSED, "IN_PROGRESS", f"A git {op} is in progress. {how}, then re-run.")
    unmerged = sorted(set(ln.split("\t", 1)[1] for ln in gout("ls-files", "-u").splitlines() if "\t" in ln))
    if unmerged:
        # A conflict left by `git stash pop` / `apply` has no *_HEAD file, and `git add` would mark it resolved.
        raise Die(REFUSED, "UNMERGED_FILES", "These files have unresolved conflicts:\n" + indent(unmerged)
                  + "\n   Resolve them (remove the <<<<<<< / >>>>>>> markers), git add them, then re-run.")
    left = task_stashes()
    if left:
        # Work an earlier run put in a stash and couldn't put back (it was killed, or a conflict):
        # not in the tree, so a push now would go without it (review 10c B6).
        # A warning, not a refusal: the documented way out of a conflict is `git stash apply`, which
        # keeps the entry, and the re-run must then go ahead.
        listing = indent([f"{ref} {msg}  →  git stash apply {sha}" for sha, ref, msg in left])
        warn("A git task left work in a stash. If it isn't in your working tree yet, it isn't part of this run: apply it "
             "first, and tell the user. Once applied (or not wanted), drop it: git stash drop <ref>.\n" + listing)


def task_stashes():
    """[(sha, ref, message)] of the stash entries the git tasks made (message starts "cre-git:")."""
    out = []
    for line in gout("stash", "list", "--format=%H%x09%gd%x09%gs").splitlines():
        sha, ref, msg = (line.split("\t") + ["", ""])[:3]
        if "cre-git:" in msg:
            out.append((sha, ref, msg))
    return out


# A git operation in progress here (am, rebase, merge, cherry-pick, revert, bisect): crelib owns the
# one list, which setup's adoption check uses too (review 10h C18).
def in_progress_op():
    return c.in_progress_op(run_git=git)


def fetch():
    """Fails CLOSED: every branch decision depends on fresh remote state."""
    r = git("fetch", "--quiet", "--prune", "origin")
    if r.returncode != 0:
        err = r.stderr.strip()
        # Two branches on origin whose names differ only in case can't both be stored on a
        # case-insensitive file system: named, with the fix, not left as "unreachable" (review 10f B8).
        # Looked for on any failure: git's wording differs by version ("cannot lock ref", "unable to
        # update local ref", "only differ in casing").
        ls = git("ls-remote", "--heads", "origin")
        if ls.returncode == 0:
            heads = [ln.split("\t", 1)[1][len("refs/heads/"):] for ln in ls.stdout.splitlines() if "\trefs/heads/" in ln]
            groups = {}
            for h in heads:
                groups.setdefault(h.casefold(), []).append(h)
            twins = [" and ".join(v) for v in groups.values() if len(v) > 1]
            if twins:
                raise Die(REFUSED, "BRANCH_CASE_CLASH", "git fetch origin failed: origin has branches whose names differ only in case ("
                          + "; ".join(twins) + "), which this file system stores as one file. Delete one of them on origin (git push "
                          "origin --delete <name>, or in the MR/PR's web page), then re-run.\n" + err)
        raise Die(REFUSED, "ORIGIN_UNREACHABLE", "git fetch origin failed, so the remote state is unknown and no branch decision is safe:\n"
                  + err)


def default_branch():
    """Explicit override > what the remote says > local origin/HEAD > main/master. "" when the remote
    has no branches at all (a brand-new repo)."""
    # GL_PUSH_TARGET was the legacy gl-push skill's name for this; the git tasks never documented it (10c G9).
    override = os.environ.get("CRE_GIT_TARGET")
    if override:
        return override
    for line in git("ls-remote", "--symref", "origin", "HEAD").stdout.splitlines():
        if line.startswith("ref:"):
            return line.split()[1].replace("refs/heads/", "", 1)
    h = gout("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if h:
        return h[len("origin/"):] if h.startswith("origin/") else h
    for b in ("main", "master"):
        if succeeds("rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}"):
            return b
    return "main" if git("ls-remote", "--heads", "origin").stdout.strip() else ""


def is_protected(t, b):
    return b in ("main", "master", t.default)


def remote_branch(b):
    """(present, sha) via ls-remote. Exit 2 from --exit-code is "no such ref"; anything else is a
    failure, never an absence (that once made an unreachable origin read as "deleted on merge")."""
    r = git("ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{b}")
    if r.returncode == 0:
        return True, r.stdout.split()[0]
    if r.returncode == 2:
        return False, ""
    raise Die(REFUSED, "ORIGIN_UNREACHABLE", f"git ls-remote origin failed (exit {r.returncode}); cannot tell whether {b} exists on the remote.")


def local_branch_exists(b):
    return succeeds("show-ref", "--verify", "--quiet", f"refs/heads/{b}")


def case_twin(b):
    """The local branch whose name is b's apart from case ("" when none, or when it is b itself)."""
    for name in gout("for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines():
        if name != b and name.casefold() == b.casefold():
            return name
    return ""


# The weekly dependency upgrade's branches (GitLab's upgrade:main job, projen's GitHub upgrade-main
# workflow). The bot replaces them; a person's commit there is lost on GitHub and stalls the upgrade
# on GitLab (review 10c B15), so the tasks never push to them.
BOT_BRANCHES = ("cre/upgrade-main", "github-actions/upgrade-main")


def refuse_bot_branch(name):
    if name in BOT_BRANCHES:
        raise Die(REFUSED, "BOT_BRANCH", f"{name} belongs to the weekly dependency upgrade, which replaces it; a commit pushed there is "
                  f"overwritten (GitHub) or stops the upgrade (GitLab). Nothing was changed. To fix the upgrade, put the fix on a branch of "
                  f"your own: git switch -c fix/<description>, then push, and close the upgrade's MR/PR once yours merges.")


def validate_new_branch_name(t, name):
    if not name:
        raise Die(REFUSED, "NO_BRANCH_NAME", "A new branch is needed but no name was given. Pass --branch <type>/<description>.")
    if not succeeds("check-ref-format", "--branch", name):
        raise Die(REFUSED, "BAD_BRANCH_NAME", f"'{name}' is not a valid branch name.")
    if "@" in name or name == "HEAD" or name.startswith(("origin/", "refs/", "remotes/", "-")):
        raise Die(REFUSED, "BAD_BRANCH_NAME", f"'{name}' would be confused with another ref; use <type>/<description> (e.g. feat/add-edge).")
    if len(name) > 100:
        raise Die(REFUSED, "BAD_BRANCH_NAME", f"'{name}' is {len(name)} characters; keep branch names to 100.")
    if succeeds("show-ref", "--verify", "--quiet", f"refs/tags/{name}"):
        raise Die(REFUSED, "BAD_BRANCH_NAME", f"'{name}' is also a tag; pick another name.")
    if is_protected(t, name):
        raise Die(REFUSED, "BAD_BRANCH_NAME", f"'{name}' is the default/protected branch; pick a feature branch name.")
    refuse_bot_branch(name)
    same = case_twin(name)
    if same or local_branch_exists(name):
        # By the name it has: on macOS's file system Feat/X finds feat/x, and the message named the
        # one typed (review 10d B13). A name that differs only in case is refused everywhere: the
        # two can't both be checked out on a case-insensitive file system.
        have = same or name
        raise Die(REFUSED, "BRANCH_EXISTS", f"Branch {have} already exists locally"
                  + (f" (the same name as {name}, apart from case)" if have != name else "")
                  + ". A branch is never reused after its MR/PR merges; pick a "
                  "new name. If it is unmerged work you meant to continue, check it out and re-run without --branch.")
    # origin's branches, once: the name itself, and a name apart from case, which pushed breaks
    # `git fetch` in every macOS clone (two refs, one file: "cannot lock ref"), so every task there
    # stops as ORIGIN_UNREACHABLE (review 10f B8). From ls-remote: such a twin may be exactly what
    # this clone could not fetch. A listing that fails can't tell, so it refuses.
    r = git("ls-remote", "--heads", "origin")
    if r.returncode != 0:
        raise Die(REFUSED, "ORIGIN_UNREACHABLE", f"git ls-remote origin failed (exit {r.returncode}); cannot tell whether {name} exists on the remote.")
    remote_names = [ln.split("\t", 1)[1][len("refs/heads/"):] for ln in r.stdout.splitlines() if "\trefs/heads/" in ln]
    if name in remote_names:
        raise Die(REFUSED, "BRANCH_EXISTS", f"Branch {name} already exists on origin. Pick a new name.")
    twin = [x for x in remote_names if x != name and x.casefold() == name.casefold()]
    if twin:
        raise Die(REFUSED, "BRANCH_EXISTS", f"Branch {twin[0]} already exists on origin (the same name as {name}, apart from case). "
                  "Two names that differ only in case break git fetch on macOS and Windows. Pick a new name.")
    # A folder of the name that differs only in case from an existing branch's folder (`Feat/two`
    # beside `feat/one`): on macOS the ref file lands in the existing `feat/` folder, so git lists
    # it as feat/two while HEAD names Feat/two, every push fails, and once refs are packed the
    # branch reads as having no commits (review 10g B4). Whole names were checked; folders weren't.
    names = list(remote_names)
    names += gout("for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()
    names += [x[len("origin/"):] for x in gout("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/").splitlines()]
    folders = {"/".join(x.split("/")[:i]) for x in names for i in range(1, x.count("/") + 1)}
    for i in range(1, name.count("/") + 1):
        mine = "/".join(name.split("/")[:i])
        other = next((f for f in sorted(folders) if f != mine and f.casefold() == mine.casefold()), "")
        if other:
            raise Die(REFUSED, "BRANCH_NAME_CLASH", f"Branch name {name} puts it in folder {mine}/, but branches already live in "
                      f"{other}/ (the same name apart from case). On macOS and Windows those are one folder, and the branch "
                      f"couldn't be pushed. Use {other}{name[len(mine):]} or another name.")
    # Git keeps branches as paths: `feat` and `feat/x` can't both exist. Caught here, not as a
    # "conflict" from switch or a push that fails the same way on every re-run (review 10c B5).
    parts = name.split("/")
    clash = []
    for space, label in (("refs/heads", ""), ("refs/remotes/origin", " on origin")):
        clash += [f"{p}{label}" for p in ("/".join(parts[:i]) for i in range(1, len(parts)))
                  if succeeds("show-ref", "--verify", "--quiet", f"{space}/{p}")]
        clash += [f"{r}{label}" for r in gout("for-each-ref", "--format=%(refname:lstrip=" + str(space.count("/") + 1) + ")",
                                              f"{space}/{name}/").split()]
    if clash:
        raise Die(REFUSED, "BRANCH_NAME_CLASH", f"Branch name {name} can't sit beside {', '.join(clash)}: git stores branches as "
                  f"paths, so one name can't be both a branch and a folder of branches. Pick another name.")
    # A name whose MR/PR merged or closed is done, even with its branch deleted: reusing it makes the
    # old MR/PR the branch's latest, and a later run reads the new work as already merged (review
    # 10c B13). Known from the provider (GitHub's gh, GitLab's API with the required token).
    st = t.provider.branch_state(name, fresh=True) if t.provider else None
    if st in ("merged", "closed"):
        raise Die(REFUSED, "BRANCH_NAME_USED", f"Branch name {name} was used before: its MR/PR is {st}. A branch name is never "
                  "reused; pick a new one.")


# ---- pushed-SHA record ---------------------------------------------------------------------------
# Written after every successful push. Survives `git fetch --prune` deleting the remote-tracking ref
# after a merge, which is exactly when we need to know what we pushed.

def record_pushed(b, sha):
    git("config", f"branch.{b}.crePushedSha", sha)
    git("config", "--unset", f"branch.{b}.crePushingSha")


def record_pushing(b, sha):
    """Before a push: what this clone is about to push. An interrupt after origin took the ref but
    before the push returned leaves origin at exactly this commit, which the re-run must recognise as
    its own, not as someone else's branch (review 10d B8)."""
    git("config", f"branch.{b}.crePushingSha", sha)


def pushing_sha(b):
    return gout("config", "--get", f"branch.{b}.crePushingSha")


def pushed_sha(b):
    return gout("config", "--get", f"branch.{b}.crePushedSha")


def was_pushed(b):
    """Our own record, or upstream config pointing at the branch's OWN name on origin (a
    `git checkout -b feat origin/main` sets merge=refs/heads/main, which must not count)."""
    if pushed_sha(b):
        return True
    return gout("config", "--get", f"branch.{b}.remote") == "origin" and gout("config", "--get", f"branch.{b}.merge") == f"refs/heads/{b}"


def contained_in_default(t, commit):
    """Everything in <commit> is already in origin/<default>: merging it wouldn't change the default
    branch's tree. Catches squash merges, which ancestry checks miss."""
    if not succeeds("cat-file", "-e", f"{commit}^{{commit}}"):
        return False
    r = git("merge-tree", "--write-tree", f"refs/remotes/origin/{t.default}", commit)
    merge_tree_ok(r)
    merged = r.stdout.split("\n", 1)[0].strip() if r.stdout else ""
    return bool(merged) and merged == gout("rev-parse", f"refs/remotes/origin/{t.default}^{{tree}}")


def branch_merged(t, b, fresh=False):
    """(merged, deleted_unmerged). Sets t.merged_reason. Gated on the branch having been pushed: a
    fresh branch's tip IS origin/main and would otherwise always look contained.

    The provider's MR/PR state decides (GitHub's gh; GitLab's API, whose token is required): the
    tree heuristic below misreads a squash-merged branch once main has edited the same lines again
    (review 10b B4), so it is used only for a branch with no MR/PR at all (state "none")."""
    t.merged_reason = ""
    if not was_pushed(b):
        return False, False
    state = t.provider.branch_state(b, fresh=fresh)
    present, rsha = remote_branch(b)
    if state == "merged":
        t.merged_reason = "its MR/PR is merged"
        return True, False
    if state in ("open", "closed"):
        return False, not present
    p = pushed_sha(b) or rsha
    if not present:
        # Deleted on merge, or deleted unmerged: only its content landing in the default branch tells.
        if not p or contained_in_default(t, p):
            t.merged_reason = "the remote branch is gone (deleted on merge)"
            return True, False
        return False, True
    if contained_in_default(t, p):
        t.merged_reason = f"everything pushed is already in origin/{t.default} (merged; squash merges included)"
        return True, False
    return False, False


def merge_tree_ok(r):
    """merge-tree exits 0 (clean) or 1 (conflicts); anything else (a shallow clone, unrelated
    histories) means it couldn't tell, which must never read as "no conflict" or "not merged"."""
    if r.returncode in (0, 1):
        return
    hint = " This clone is shallow: git fetch --unshallow origin, then re-run." if gout("rev-parse", "--is-shallow-repository") == "true" else ""
    raise Die(REFUSED, "MERGE_TREE_FAILED", f"git merge-tree failed (exit {r.returncode}), so whether this branch merges into the default "
              f"branch is unknown: {(r.stderr or r.stdout).strip()[:300]}.{hint}")


def conflicts_with_default(t):
    """Files that conflict between HEAD and origin/<default>; [] when it merges."""
    r = git("merge-tree", "--write-tree", "-z", "--name-only", "--no-messages", f"refs/remotes/origin/{t.default}", "HEAD")
    merge_tree_ok(r)
    if r.returncode != 1:
        return []
    return [p for p in r.stdout.split("\0")[1:] if p]


# ---- classification ------------------------------------------------------------------------------

def patch_id(a, b):
    """git patch-id of the diff a..b: equal for the same change, whatever commits carry it."""
    diff = run_child(["git", "diff", "--binary", a, b], text=False).stdout
    if not diff:
        return ""
    return run_child(["git", "patch-id", "--stable"], input=diff, text=False).stdout.decode().split(" ")[0]


def squashed_tree(onto, c):
    """The tree a squash of c onto `onto` gives (git merge-tree, nothing written to the work tree);
    "" when it conflicts."""
    r = git("merge-tree", "--write-tree", onto, c)
    return r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else ""


def landed_base(t, b, p):
    """The commit of the merged head's history that the MR/PR's squash actually holds. The provider
    names the head it merged; if a push raced the merge, that head can be newer than what landed,
    and taking it as the base would call the late commits merged and drop them (review 10d B3).
    Compared by tree: each of the branch's commits, newest first, squashed onto the squash commit's
    own parent (main as it was at the merge), against the squash's tree. A patch-id comparison
    (10d B3's) hashed context lines, so once main had changed a line near the branch's edit nothing
    matched and every late commit was dropped (review 10g B1). Patch-id stays as a second try.
    Nothing matching is refused: guessing "everything landed" is the loss this exists to stop.
    Unknown (no squash commit named, a merge commit, not fetched): the merged head, as before."""
    landed = getattr(t.provider, "merged_commit", lambda _b: "")(b) if t.provider else ""
    if not landed:
        return p
    if not succeeds("cat-file", "-e", f"{landed}^{{commit}}"):
        git("fetch", "-q", "origin", landed)
        if not succeeds("cat-file", "-e", f"{landed}^{{commit}}"):
            return p
    parents = gout("rev-list", "--parents", "-n", "1", landed).split()[1:]
    if len(parents) != 1 or succeeds("merge-base", "--is-ancestor", p, landed):
        return p   # a merge commit (or p is in main's history): what merged is p's history
    base = gout("merge-base", p, parents[0])
    if not base:
        return p
    candidates = rev_list("--first-parent", p, "--not", base)   # p first
    want_tree = gout("rev-parse", f"{landed}^{{tree}}")

    def found(c):
        if c != p:
            warn(f"{b}'s MR/PR reports {short(p)} as merged, but what landed ({short(landed)}) holds only up to {short(c)}: the "
                 "commits after it move to the new branch.")
        return c
    for c in candidates:
        if squashed_tree(parents[0], c) == want_tree:
            return found(c)
    want = patch_id(parents[0], landed)
    for c in candidates:
        if want and patch_id(base, c) == want:
            return found(c)
    raise Die(REFUSED, "LANDED_UNKNOWN", f"{b}'s MR/PR merged as {short(landed)}, but that commit matches none of the branch's "
              f"commits, so which ones it holds can't be told (it was changed in the merge, or a push raced it). Nothing was "
              f"changed. Compare git log --oneline {short(base)}..{short(p)} with git show --stat {short(landed)}; move the commits "
              f"it lacks by hand (git switch -c <type>/<name> origin/{t.default}, git cherry-pick <them>), then push from there.")


# Local-branch reflog entries whose SHA is a value origin's branch had: the branch made from it, a
# fast-forward to it, a reset to it. Not a bare `pull` (it doesn't name what was pulled; a `git pull
# origin main` into the branch would read as a rewrite) nor a merge that made a commit: those SHAs
# are this clone's own (review 10f B1).
ORIGIN_VALUE_ENTRY = r"^(branch: Created from|merge|reset: moving to) (refs/remotes/)?origin/{b}(: Fast-forward)?$"


def remote_values(branch):
    """Every value this clone has seen origin's <branch> at: the reflog of refs/remotes/origin/<b>,
    which every fetch, pull and push of the branch writes, and the local branch's entries that
    record an origin value (ORIGIN_VALUE_ENTRY). Only the last fetch's value was a baseline before,
    so an IDE's background fetch or a `git pull` after a teammate's force-push hid the rewrite
    (review 10e B2). `fetch --prune` deletes the remote-tracking reflog with its ref when a teammate
    deletes the branch, so a clone that only tracks it kept no baseline and pushed back what the
    teammate's re-pushed branch had dropped (review 10g B2); the local branch's own entries survive
    the prune. 10f B1 cut them all; only the ones that name an origin value come back."""
    vals = gout("reflog", "show", "--format=%H", f"refs/remotes/origin/{branch}", "--").split()
    entry = re.compile(ORIGIN_VALUE_ENTRY.format(b=re.escape(branch)))
    for line in gout("reflog", "show", "--format=%H %gs", f"refs/heads/{branch}", "--").splitlines():
        sha, _, subject = line.partition(" ")
        merge = subject.startswith("merge ")
        if entry.match(subject) and (not merge or subject.endswith(": Fast-forward")):
            vals.append(sha)
    return vals


def rewritten_base(branch, seen, tip, later_ok=False):
    """A value origin's <branch> had that HEAD still contains and <tip> doesn't: the branch was
    rewritten (a force-push, a delete and re-push) and pushing or carrying HEAD would put back what
    was removed. "" when none. The baselines: the last SHA this clone pushed, what it had fetched of
    the branch before this run (a clone that only tracks a teammate's branch, 10d B6), and every
    earlier value of origin's branch its reflogs hold (an earlier fetch already saw the rewrite,
    10e B2; remote_values). later_ok (tip is what an MR/PR merged): a value that comes after tip is
    a push that raced the merge, new work for the new branch (10c B13), unless this clone saw
    origin's branch move off it afterwards (its last value seen lacks it): then it was removed.
    A removal this clone never saw, a rewind followed by the merge, looks exactly like the race."""
    vals = remote_values(branch)
    last_seen = seen or (vals[0] if vals else "")
    for base in dict.fromkeys(b for b in (pushed_sha(branch), seen, *vals) if b):
        if not (succeeds("cat-file", "-e", f"{base}^{{commit}}") and
                not succeeds("merge-base", "--is-ancestor", base, tip) and
                succeeds("merge-base", "--is-ancestor", base, "HEAD")):
            continue
        if later_ok and succeeds("merge-base", "--is-ancestor", tip, base) and \
                (not last_seen or succeeds("merge-base", "--is-ancestor", base, last_seen)):
            continue
        return base
    return ""


def refuse_post_merge_commits(t, b, p):
    """A commit pushed to origin/<b> after its MR/PR merged (a teammate's plain git push to the kept
    branch) is on no MR and never reaches main; moving on to a new branch without it would strand
    it, with no word (review 10d B4)."""
    if not succeeds("rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}"):
        return
    late = rev_list(f"{p}..refs/remotes/origin/{b}", "--not", "HEAD")
    if late:
        who = [gout("log", "-1", "--format=%h %an: %s", c) for c in late[:5]]
        raise Die(REFUSED, "POST_MERGE_COMMITS", f"origin/{b} has {len(late)} commit(s) pushed after its MR/PR merged, which are on no "
                  "MR/PR and would never reach main:\n" + indent(who) + f"\n   Nothing was changed. Bring them in (git merge "
                  f"origin/{b}), then re-run: they move to the new branch with your work.")


def classify(t):
    """Read-only (after fetch). Sets t.cls (BOOTSTRAP | DETACHED | ON_DEFAULT | MERGED |
    MERGED_UNKNOWN_BASE | CLOSED | ACTIVE | NEW), t.current, t.carry (a rev range to carry onto a
    new branch, "" for none), t.note."""
    t.current = gout("branch", "--show-current")
    t.carry = t.note = ""
    t.start_head = head()
    if not t.default:
        t.cls, t.note = "BOOTSTRAP", "origin has no branches yet; the first push goes directly to main."
        return
    if not t.current:
        t.cls, t.note = "DETACHED", "detached HEAD; a new branch is created at HEAD (its commits stay)."
        return
    b = t.current
    if is_protected(t, b):
        t.cls = "ON_DEFAULT"
        if succeeds("rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}") and rev_list(f"refs/remotes/origin/{b}..HEAD"):
            t.carry = f"refs/remotes/origin/{b}..HEAD"
            t.note = f"on {b} with unpushed commits; they move to a new branch."
        else:
            t.note = f"on {b}; work moves to a new branch from origin/{t.default}."
        return
    merged, deleted_unmerged = branch_merged(t, b)
    if merged:
        t.cls = "MERGED"
        # New work starts after what the MR/PR merged, not after what this clone last pushed: a push
        # can land after the merge (or on a reused name), and is then in neither (review 10c B13).
        p = t.provider.merged_head(b) if t.provider else ""
        t.merged_head = p
        if p and not succeeds("cat-file", "-e", f"{p}^{{commit}}"):
            git("fetch", "-q", "origin", p)
            if not succeeds("cat-file", "-e", f"{p}^{{commit}}"):
                raise Die(REFUSED, "MERGED_HEAD_UNKNOWN", f"{b}'s MR/PR merged at {short(p)}, which this clone can't fetch, so "
                          "what came after it can't be told. Nothing was changed. git fetch origin, then re-run.")
        p = p or pushed_sha(b)
        if p and succeeds("cat-file", "-e", f"{p}^{{commit}}"):
            p = landed_base(t, b, p)
            refuse_post_merge_commits(t, b, p)
            if rev_list(f"{p}..HEAD"):
                t.carry = f"{p}..HEAD"
        elif not contained_in_default(t, "HEAD"):
            # Pushed and merged, but no record of what was pushed (an older tool pushed it), so
            # post-merge commits can't be told from the merged ones.
            t.cls = "MERGED_UNKNOWN_BASE"
        t.note = f"{b} looks merged: {t.merged_reason}."
        return
    if deleted_unmerged:
        # Its MR/PR was closed and the branch deleted, or someone deleted it: what was pushed never
        # reached the default branch, so all of it (not only what came after the push) moves on.
        t.cls = "CLOSED"
        t.note = f"{b} was deleted on origin without being merged; its commits move to a new branch."
        if rev_list(f"refs/remotes/origin/{t.default}..HEAD"):
            t.carry = f"refs/remotes/origin/{t.default}..HEAD"
        return
    if was_pushed(b):
        # Only a branch we pushed can have a closed MR/PR of its own; a new branch that reuses an
        # old name must not inherit that MR's state.
        if t.provider.branch_state(b) == "closed":
            # Nothing of a closed MR/PR reached the default branch, so all of it moves on, as for a
            # deleted branch. Moving only what came after the last push left the MR's own commits
            # behind whenever its branch was kept, which on GitHub is always (review 10e B3).
            t.cls = "CLOSED"
            t.note = f"{b}'s MR/PR was closed without merging; a closed branch is done, and its commits move to a new branch."
            if rev_list(f"refs/remotes/origin/{t.default}..HEAD"):
                t.carry = f"refs/remotes/origin/{t.default}..HEAD"
            return
        t.cls, t.note = "ACTIVE", f"{b} is pushed and not merged; pushing continues it."
    else:
        t.cls, t.note = "NEW", f"{b} has never been pushed."


# ---- new branch ----------------------------------------------------------------------------------

def refuse_ignored_overwrite(ref, ref_name, what, done):
    """Refuse before a checkout or merge to `ref` when it tracks a path that is an ignored local file
    here (a .env, a scratch config): git treats ignored files as expendable and replaces them without
    a word, and `--no-overwrite-ignore` doesn't stop a merge (review 10c B18 for the switch, 10d B1
    for push's sync merge). Every path that moves the tree to another commit calls this."""
    ignored = set(p for p in git("ls-files", "-z", "-o", "-i", "--exclude-standard").stdout.split("\0") if p)
    if not ignored:
        return
    theirs = set(p for p in git("ls-tree", "-r", "-z", "--name-only", ref).stdout.split("\0") if p)
    hit = sorted(ignored & theirs)
    if hit:
        raise Die(REFUSED, "IGNORED_FILE_TRACKED", f"{ref_name} tracks these paths, where you have ignored local files that "
                  f"{what} would overwrite:\n" + indent(hit) + f"\n   {done} Move them aside (or keep a copy), then re-run.")


def stopped_pick(t, commits):
    """Which carried commit the cherry-pick stopped on, and which of them origin/<default> already
    has as the same change (a squash merge of exactly them, `git cherry`): a bare "conflicts" left
    the user to find it (review 10g B12)."""
    at = gout("rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD")
    same = [ln.split()[1] for ln in gout("cherry", f"refs/remotes/origin/{t.default}", commits[-1], f"{commits[0]}^").splitlines()
            if ln.startswith("- ")] if commits else []
    out = f"; it stopped on {short(at)} {gout('log', '-1', '--format=%s', at)}" if at else ""
    if same:
        out += (f"; origin/{t.default} already has the change of {', '.join(short(c) for c in same)} (squash-merged?): leave "
                f"{'it' if len(same) == 1 else 'them'} out (git switch -c <type>/<name> origin/{t.default}, then cherry-pick the others)")
    return out


def new_branch(t, name, carry_range):
    """Create <name> from origin/<default>, re-applying the commits in carry_range and the working
    changes. On any conflict the repository is put back exactly as it was (exit 30)."""
    orig = gout("branch", "--show-current")
    commits = []
    if carry_range:
        # A merge commit can hold changes of its own (a conflict resolution); carrying only the plain
        # commits would drop them (review 10b B24). Refused before anything changes.
        merges = [m for m in rev_list("--merges", carry_range, "--not", f"refs/remotes/origin/{t.default}") if merge_has_own_changes(m)]
        if merges:
            raise Die(REFUSED, "CARRY_HAS_MERGES", f"The commits to move to {name} include merge commit(s) ({', '.join(short(m) for m in merges[:5])}), "
                      f"whose own changes (a conflict resolution) can't be carried as plain commits. Nothing was changed. Rebase them "
                      f"onto origin/{t.default} yourself (git rebase origin/{t.default}), then re-run.")
        # Never commits the default branch already has (a merge of main into the branch brings them).
        commits = rev_list("--reverse", "--no-merges", carry_range, "--not", f"refs/remotes/origin/{t.default}")
    refuse_ignored_overwrite(f"refs/remotes/origin/{t.default}", f"origin/{t.default}", "the new branch",
                             "Nothing was changed.")
    stash, untracked = "", []
    our_pick = {"started": False}

    def undo():
        """Back to where the run started: no cherry-pick in progress, the original branch, no new
        branch, the work back in the tree. True when the work was restored (or there was none).
        t.held, not the local, says whether there is a stash: a stop can land as it is made."""
        with signals_held():
            return _undo()

    def _undo():
        held = t.held
        # Only a cherry-pick this run started (CHERRY_PICK_HEAD is set while ours stops on a
        # conflict): an abort of the user's own open sequence dropped their remaining picks (review
        # 10g B7; in_progress_op refuses one up front now).
        if succeeds("rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD") and our_pick["started"]:
            git("cherry-pick", "--abort")
        if held:
            clear_work(stash_untracked(held))
        if orig:
            git("switch", "-q", "--recurse-submodules", orig)
        else:
            git("switch", "-q", "--recurse-submodules", "--detach", t.start_head)
        git("branch", "-q", "-D", name)
        return not held or restore_work(t, held, stash_untracked(held))

    def rollback(why):
        if not undo():
            raise Die(CONFLICT, "CONFLICT", f"{why}. Restoring your changes also failed; they are safe in: git stash apply {stash}")
        raise Die(CONFLICT, "CONFLICT", f"{why}. Nothing changed: you are back on {orig or 'the detached HEAD'} with your work as it was.")

    try:
        # Inside the try: a stop while stashing is undone like any other (review 10d B5).
        stash, untracked = stash_work(t, f"cre-git: carrying work to {name}")
        # Never `git checkout main`: it fails when main is checked out in another worktree.
        # --recurse-submodules: otherwise a submodule stays at the old branch's commit, and staging the
        # work then reverts a bump main made (review 10b B9).
        if not succeeds("switch", "-q", "--recurse-submodules", "--no-track", "-c", name, f"refs/remotes/origin/{t.default}"):
            rollback(f"git switch -c {name} failed")
        our_pick["started"] = bool(commits)
        if commits and not succeeds("cherry-pick", "--allow-empty", "--keep-redundant-commits", *commits):
            rollback(f"Re-applying your commits onto origin/{t.default} conflicts ({len(commits)} commit(s) from {carry_range}"
                     + stopped_pick(t, commits) + ")")
        if stash and not restore_work(t, stash, untracked):
            rollback(f"Your uncommitted changes conflict with origin/{t.default}")
    except Die as d:
        if d.state == "CONFLICT":
            raise
        # An interrupt (SIGTERM) mid-way: put everything back, so "nothing changed" is true and the
        # re-run starts clean, instead of a half-made branch the re-run refuses (review 10c B2).
        undo()
        raise
    except KeyboardInterrupt:
        undo()
        raise
    # Last, so a rollback never has to undo it: the local default branch goes back to origin's. Its
    # unpushed commits now live on the new branch; left there, the next new branch would carry them
    # again. Fails harmlessly when it's checked out in another worktree.
    if orig == t.default:
        git("branch", "-q", "-f", t.default, f"refs/remotes/origin/{t.default}")
    what = f"created {name} from origin/{t.default}"
    if commits:
        what += f", carrying {len(commits)} commit(s)"
    if stash:
        what += " and your uncommitted changes"
    t.done.append(what)
    ok(what[0].upper() + what[1:] + ".")


def merge_has_own_changes(m):
    """A merge commit whose tree isn't what git merges on its own (a conflict resolution, an edit
    made in the merge). A clean merge (main merged into the branch) has none: dropping it is safe."""
    parents = gout("rev-list", "--parents", "-n", "1", m).split()[1:]
    if len(parents) != 2:
        return True
    r = git("merge-tree", "--write-tree", parents[0], parents[1])
    auto = r.stdout.split("\n", 1)[0].strip() if r.returncode == 0 else ""
    return auto != gout("rev-parse", f"{m}^{{tree}}")


def branch_at_head(t, name):
    """A detached HEAD: the new branch starts at HEAD, so its commits (and the working tree) stay."""
    if not succeeds("switch", "-q", "-c", name):
        raise Die(REFUSED, "BRANCH_FAILED", f"git switch -c {name} failed.")
    t.done.append(f"created {name} at the detached HEAD")
    ok(f"Created {name} at the detached HEAD.")


def stash_work(t, message):
    """Stash every uncommitted change, untracked files included. (sha, untracked paths), or
    ("", []) when there is nothing a stash holds. From here until restore_work the work is only in
    the stash, so t.held names it on any stop (a signal, an error)."""
    if not is_dirty():
        return "", []
    before = gout("rev-parse", "-q", "--verify", "refs/stash")
    try:
        pushed = succeeds("stash", "push", "-q", "--include-untracked", "-m", message)
    finally:
        # Even when a signal stops the run as the stash returns, the work is now only in the stash:
        # name it (t.held), so the rollback puts it back (review 10d B5).
        sha = c.run(["git", "rev-parse", "-q", "--verify", "refs/stash"]).stdout.strip()
        if sha and sha != before:
            t.held = sha
    if not pushed:
        raise Die(REFUSED, "STASH_FAILED", "git stash failed; nothing was changed.")
    # Only a submodule's own changes (nothing stashable): no new entry. The top of the stack is
    # then someone else's older stash, which must never be applied or dropped.
    if sha == before:
        return "", []
    return sha, stash_untracked(sha)


def stash_untracked(sha):
    """The paths a stash entry holds as untracked files."""
    if not succeeds("cat-file", "-e", f"{sha}^3"):
        return []
    # -z: a quoted name ("\303\251.txt") is not a path os.remove can find (review 10b B16).
    return [p for p in git("ls-tree", "-r", "-z", "--name-only", f"{sha}^3").stdout.split("\0") if p]


def clear_work(untracked):
    """Take a (partly) applied stash back out of the working tree. A path the stash held as
    untracked that HEAD now tracks (a teammate added it) is HEAD's: never deleted, or the next push
    commits the deletion (review 10c B14)."""
    git("reset", "-q", "--hard")
    tracked = set(gout("ls-files", "-z").split("\0")) if untracked else set()
    for p in untracked:
        if p in tracked:
            continue
        try:
            os.remove(p)
        except OSError:
            pass


def restore_work(t, sha, untracked, index=True):
    """Apply our stash and drop it, staged versions included when they still apply. False (the tree
    left clean, the stash kept) when it conflicts."""
    if index and succeeds("stash", "apply", "-q", "--index", sha):
        pass
    elif not succeeds("stash", "apply", "-q", sha):
        clear_work(untracked)
        return False
    elif index:
        # The staged versions didn't apply on the new base: the files are back, but what was staged
        # in them (git add -p) is now only in the working tree's version. Say which (review 10c B16).
        # The files that had something staged (index ≠ HEAD: <stash>^1..^2), not every file with
        # unstaged changes (^2..<stash>), which named files that never had anything staged (10d B11).
        partial = [p for p in gout("diff", "--name-only", f"{sha}^1", f"{sha}^2").splitlines() if p]
        if partial:
            warn("Staged parts of these files couldn't be kept on the new base; the files are back with all their "
                 "changes unstaged:\n" + indent(partial))
    drop_stash(sha)
    t.held = ""
    return True


def drop_stash(sha):
    """Drop exactly our entry: the stash stack is shared by every worktree of the clone."""
    for line in gout("stash", "list", "--format=%H %gd").splitlines():
        h, _, ref = line.partition(" ")
        if h == sha:
            git("stash", "drop", "-q", ref)
            return


def resolve_branch(t, requested):
    """The branch to work on, creating one when the state requires it. Sets t.branch."""
    if t.cls == "DETACHED":
        validate_new_branch_name(t, requested)
        branch_at_head(t, requested)
    elif t.cls in ("ON_DEFAULT", "MERGED", "CLOSED"):
        if not requested:
            raise Die(REFUSED, f"NEEDS_BRANCH_{t.cls}", f"{t.note} Re-run with --branch <type>/<description>.")
        validate_new_branch_name(t, requested)
        info(t.note)
        new_branch(t, requested, t.carry)
    elif t.cls == "MERGED_UNKNOWN_BASE":
        raise Die(REFUSED, "MERGED_UNKNOWN_BASE", f"{t.note} It has local commits, but no record of what was pushed, so the new commits "
                  f"can't be separated from the merged ones. Create a branch from origin/{t.default} yourself and cherry-pick the new commits.")
    elif requested and requested != t.current:
        info(f"Continuing {t.current} ({t.note}); --branch {requested} is only used when a new branch is needed.")
    t.branch = gout("branch", "--show-current")


# ---- validate / stage / commit -------------------------------------------------------------------

def is_projen():
    return os.path.isfile(".projen/tasks.json") or any(f.startswith(".projenrc.") for f in os.listdir("."))


def validate(t, why=""):
    """Re-synth, then build. Synth first so task-definition changes in .projenrc take effect."""
    if os.environ.get("CRE_SKIP_VALIDATE"):
        info("Validation skipped (CRE_SKIP_VALIDATE set).")
        return
    if not is_projen():
        info("Not a projen project; skipping projen validation. Run the project's own build/tests first.")
        return
    log = os.path.join(gout("rev-parse", "--path-format=absolute", "--git-common-dir"), "cre-git-validate.log")
    info(f"Validating{' ' + why if why else ''}: npx projen, then npx projen build (log: {log})")
    with open(log, "w") as f:
        for cmd, state, what in ((["npx", "projen"], "VALIDATE_FAILED", "npx projen (synth)"),
                                 (["npx", "projen", "build"], "BUILD_FAILED", "npx projen build")):
            f.flush()
            # Its own process group: an interrupt reaches every process of the build, not only npx.
            if run_child(cmd, capture=False, stdout=f, stderr=subprocess.STDOUT, forward=True, group=True).returncode != 0:
                f.flush()
                sys.stderr.write("\n".join(open(log, errors="replace").read().splitlines()[-30:]) + "\n")
                raise Die(FAILED, state, f"{what} failed. Nothing was staged or committed by this step.")


SOURCE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".css", ".scss", ".html", ".md", ".go", ".java",
                     ".rb", ".vue", ".svelte")

# Paths that hold credentials, matched against the file name, or (entries with a `/`) the
# repo-relative path. *.example / *.sample / *.template are always allowed.
SECRET_GLOBS = (".env .env.* .envrc *.pem *.key *.p12 *.pfx *.jks *.keystore id_rsa* id_ecdsa* id_ed25519* credentials credentials.* "
                ".netrc _netrc *.tfvars *.tfvars.json *.tfstate *.tfstate.* secrets.* *secret*.json *.har *cookie* cookies.txt "
                "*.kdbx .git-credentials .pgpass gitlab-token").split()
SECRET_PATH_GLOBS = ".aws/* */.aws/* .ssh/* */.ssh/* .docker/config.json */.docker/config.json .gitlab/access_token .kube/config".split()
# Config files often committed on purpose: refused only when they hold a credential.
SECRET_IF_TOKEN = (".npmrc", ".yarnrc.yml", ".pypirc")
# Well-known credential formats, looked for in every staged file's content.
SECRET_CONTENT = re.compile(c.SECRET_CONTENT_PATTERN.encode())
# A credential key with a value, unless the value is an environment reference: npm and yarn expand
# `${NPM_TOKEN}`, which is the documented way to commit these files (review 10d B13).
CONFIG_AUTH = re.compile(rb"(_auth|_authToken|password|token)[ \t]*[:=][ \t]*[\"']?(?!\$\{)[^\s\"']", re.I)


def is_secret_path(t, p):
    base = p.rsplit("/", 1)[-1]
    if p in t.args.allow or base.lower().endswith((".example", ".sample", ".template")):
        return False
    # A public key (id_ed25519.pub) and source files that only mention cookies (cookie-banner.ts)
    # aren't credentials (review 10c B12); a cookie jar of any other name still is.
    if base.lower().endswith(".pub") or ("cookie" in base.lower() and base.endswith(SOURCE_EXTENSIONS)):
        return False
    # Case-insensitively: .ENV and Secrets.JSON are the same files to a reader, and the same file on
    # macOS's default file system (review 10d B13).
    low, lowp = base.lower(), p.lower()
    return any(fnmatch.fnmatchcase(low, g) for g in SECRET_GLOBS) or any(fnmatch.fnmatchcase(lowp, g) for g in SECRET_PATH_GLOBS)


# A line that starts with a conflict marker (git writes them at column 0, with a space after).
CONFLICT_MARKERS = re.compile(rb"^(<<<<<<<|>>>>>>>) ", re.M)


# Content scanned for secrets per staged file; a bigger blob is refused for its size anyway.
SCAN_MAX_BYTES = 10 * 1024 * 1024


def staged_blobs():
    """(path, size, content) of every added, modified or type-changed STAGED file (renames as their
    new path), read in three git calls however many files there are (one subprocess per file took
    260 s for 3000 files, review 10b B11). Content is read only up to SCAN_MAX_BYTES (10 MB);
    a bigger blob is refused for its size anyway. Gitlinks are skipped (stage() refuses them)."""
    out = run_child(["git", "diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames", "--diff-filter=AMT"],
                    text=False).stdout.decode(errors="replace").split("\0")
    oids, paths = {}, []
    for meta, path in zip(out[0::2], out[1::2]):
        parts = meta.split()
        if len(parts) >= 4 and parts[1] != "160000" and path:
            oids[path] = parts[3]
            paths.append(path)
    return read_blobs([p for p in paths if p in oids], oids)


def read_blobs(order, oids):
    """(path, size, content) for each path in order, from its blob oid: two `git cat-file` calls
    however many blobs. Content only up to SCAN_MAX_BYTES (a bigger blob is refused for its size)."""
    if not order:
        return []
    limit = SCAN_MAX_BYTES
    checks = run_child(["git", "cat-file", "--batch-check=%(objectname) %(objectsize)"],
                       input="".join(oids[p] + "\n" for p in order).encode(), text=False).stdout.decode().splitlines()
    sizes = {}
    for p, line in zip(order, checks):
        try:
            sizes[p] = int(line.split()[1])
        except (IndexError, ValueError):
            sizes[p] = 0
    readable = [p for p in order if sizes[p] <= limit]
    raw = run_child(["git", "cat-file", "--batch"], input="".join(oids[p] + "\n" for p in readable).encode(),
                    text=False).stdout if readable else b""
    contents, pos = {}, 0
    for p in readable:
        nl = raw.index(b"\n", pos)
        size = int(raw[pos:nl].split()[2])
        contents[p] = raw[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return [(p, sizes[p], contents.get(p, b"")) for p in order]


def max_file_mb():
    """CRE_MAX_FILE_MB (default 10): the largest file a commit may hold, in MB."""
    try:
        return int(os.environ.get("CRE_MAX_FILE_MB") or 10)
    except ValueError:
        raise Die(REFUSED, "BAD_ARGS", f"CRE_MAX_FILE_MB must be a whole number of megabytes, not '{os.environ['CRE_MAX_FILE_MB']}'.")


def pushed_by_us():
    """Every commit this clone recorded as pushed (branch.<b>.crePushedSha, any branch) that it still
    has: their content reached origin once, so sending it again leaks nothing new."""
    out = gout("config", "--get-regexp", r"^branch\..*\.crepushedsha$")
    shas = [ln.split()[-1] for ln in out.splitlines() if ln.split()]
    return [c for c in dict.fromkeys(shas) if succeeds("cat-file", "-e", f"{c}^{{commit}}")]


def sent_revs(t):
    """The `git rev-list` arguments for the commits this push will send, read after classify() and
    before anything changes: a new branch from origin/<default> sends only the carried commits
    (t.carry), none of the old branch's; a continued branch (or a detached HEAD, or the bootstrap)
    sends HEAD. Either way minus origin's branches and what this clone pushed before. The scan ran on
    the old HEAD against origin's branches only, so after a squash merge deleted the branch it refused
    a file in the merged commits, which the new branch never carries (review 10g B6). None: nothing
    from the history is sent (only the commit the task makes, which stage() checks)."""
    if not head() or t.cls == "MERGED_UNKNOWN_BASE":
        return None
    if t.cls in ("ON_DEFAULT", "MERGED", "CLOSED"):
        if not t.carry:
            return None
        lo, _, hi = t.carry.partition("..")
        revs = [hi, "--not", lo]
    else:
        revs = ["HEAD", "--not"]
    return revs + ["--remotes=origin"] + pushed_by_us()


def unpushed_blobs(revs):
    """(path, oid, size, content) of every file version the commits `revs` select hold: each blob
    reachable from them, whichever commit made it (plain `git commit`, a commit carried from main,
    one whose file a later commit deleted: it stays in the history all the same, review 10f B2). The
    path is the first one git names for the blob."""
    named = run_child(["git", "rev-list", "--objects", *revs]).stdout.splitlines()
    paths = {}
    for line in named:
        oid, _, path = line.partition(" ")
        if path:
            paths.setdefault(oid, path)
    kinds = run_child(["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectmode)"],
                      input="".join(o + "\n" for o in paths)).stdout.splitlines() if paths else []
    oids, order = {}, []
    for line in kinds:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "blob" and parts[0] in paths:
            path = paths[parts[0]]
            # Two blobs can share a path (an edit): key each by "path@oid" so both are read.
            key = f"{path}\0{parts[0]}"
            oids[key] = parts[0]
            order.append(key)
    return [(k.split("\0")[0], oids[k], size, data) for k, size, data in read_blobs(order, oids)]


def adding_commit(revs, oid):
    """The short SHA of the commit among `revs` that added (or changed a file to) blob <oid>: the
    one to remove it from, not a later commit that deleted it (review 10g B6)."""
    return gout("log", "-1", "--format=%h", "--diff-filter=AMT", f"--find-object={oid}", *revs)


def refuse_unpushed_secrets(t):
    """Refuse, before anything changes, when a commit the push would send (sent_revs) holds a
    credential or a file over CRE_MAX_FILE_MB: stage() sees only the commit the task makes, and a
    `.env` committed with plain git, or a key committed on main and carried to the new branch, went
    to origin with code 0 (review 10f B2); so did a 30 MB file (review 10g B5)."""
    revs = sent_revs(t)
    if revs is None:
        return
    limit = max_file_mb() * 1024 * 1024
    secrets, large = {}, {}
    for p, oid, size, data in unpushed_blobs(revs):
        if size > limit and p not in t.args.allow:
            large.setdefault(p, (oid, size))
        elif is_secret_path(t, p) or has_secret_content(t, p, data):
            secrets.setdefault(p, oid)

    def where(p, oid, extra=""):
        sha = adding_commit(revs, oid)
        return f"{p}{extra}" + (f" (commit {sha})" if sha else "")
    if secrets:
        raise Die(REFUSED, "SECRET_IN_HISTORY", "Commits this push would send hold files that look like, or contain, credentials:\n"
                  + indent([where(p, secrets[p]) for p in sorted(secrets)[:10]])
                  + "\n   Nothing was committed or pushed. A credential in a pushed commit stays in the history for good: "
                  "remove it from those commits first (git rebase -i, or git reset --soft origin/<default> and commit again without it), "
                  "add the file to .gitignore and rotate the credential if it was real. For a file that is meant to be committed, "
                  "re-run with --allow-secret-path <path>.")
    if large:
        raise Die(REFUSED, "LARGE_FILE_IN_HISTORY", f"Commits this push would send hold files over {limit // 1024 // 1024} MB, which "
                  "stay in the history for good:\n"
                  + indent([where(p, oid, f" ({size // 1024 // 1024} MB)") for p, (oid, size) in sorted(large.items())[:10]])
                  + "\n   Nothing was committed or pushed. Remove them from those commits first (git rebase -i, or git reset --soft "
                  "origin/<default> and commit again without them) and add them to .gitignore (build output belongs in dist/ or "
                  "cdk.out/). For a file that must be committed, re-run with --allow-secret-path <path> (CRE_MAX_FILE_MB raises the limit).")


def refuse_push(t):
    """Every refusal `push` makes from what classify() found, before anything changes; git:status
    calls it too, so it never says "commit and push" where push refuses (review 10g B8: BOT_BRANCH,
    BRANCH_EXISTS_ON_ORIGIN and SECRET_IN_HISTORY were each added to push only). Read-only."""
    if t.cls in ("ACTIVE", "NEW"):
        refuse_bot_branch(t.current)
    if t.cls == "NEW":
        present, rsha = remote_branch(t.current)
        # This clone's own interrupted first push (origin is at what it was pushing) is resumed by
        # push, not refused (review 10d B8).
        if present and rsha != pushing_sha(t.current):
            # Never pushed from here, but origin has a branch of that name: someone else's. Pushing
            # would put this work into their MR/PR (review 10b B10).
            raise Die(REFUSED, "BRANCH_EXISTS_ON_ORIGIN", f"origin already has a branch named {t.current}, which this clone never pushed. "
                      f"Rename yours (git branch -m <type>/<new-name>) and re-run, or, if you mean to continue that branch: "
                      f"git branch -u origin/{t.current}, then re-run.")
    if t.cls == "MERGED" and t.carry and t.merged_head:
        # Rewritten before it merged (a teammate's force-push or delete-and-re-push): a commit it
        # removed is in neither what merged nor main, and carrying "everything after the merge" to
        # the new branch pushed it back with 0 (found by the two-clone model run, review 10g).
        base = rewritten_base(t.current, t.seen, t.merged_head, later_ok=True)
        if base:
            removed = rev_list(f"{t.merged_head}..{base}")
            raise Die(REFUSED, "REMOTE_REWRITTEN", f"origin/{t.current} was rewritten before it merged: {len(removed)} commit(s) you "
                      f"have ({', '.join(short(c) for c in removed[:5])}) are in neither what merged ({short(t.merged_head)}) nor "
                      f"origin/{t.default}, and carrying them to a new branch would put them back. Nothing was changed. Your own commits "
                      f"are git log --oneline {base[:9]}..HEAD. Look at what was removed (git log --oneline {short(t.merged_head)}..{base[:9]}), "
                      f"agree with whoever force-pushed, then move your own commits onto what merged "
                      f"(git rebase --onto {short(t.merged_head)} {base[:9]}) and re-run.")
    # Every commit this push would send, not only the one it makes (review 10f B2): after the fetch,
    # so what origin already has is known; after classify, so it is what the push really sends (a
    # new branch carries only t.carry, review 10g B6); before anything is committed, carried or pushed.
    refuse_unpushed_secrets(t)


def has_secret_content(t, p, data):
    """The STAGED content of <path> (data, read from the index) holds a credential."""
    if p in t.args.allow:
        return False
    if SECRET_CONTENT.search(data):
        return True
    return p.rsplit("/", 1)[-1].lower() in SECRET_IF_TOKEN and bool(CONFIG_AUTH.search(data))


def projen_managed():
    if not os.path.isfile(".projen/files.json"):
        return []
    try:
        files = json.load(open(".projen/files.json")).get("files", [])
    except (OSError, ValueError, AttributeError):
        files = []
    return [f for f in files if isinstance(f, str)] + [".projen/files.json", ".projen/tasks.json", ".projen/deps.json"]


LOCKFILES = ("yarn.lock", "package-lock.json", "pnpm-lock.yaml")


def staged_renames():
    """{old path: new path} for each rename staged (git mv)."""
    entries = git("diff", "--cached", "--name-status", "-M", "-z").stdout.split("\0")
    out, i = {}, 0
    while i < len(entries):
        st = entries[i]
        if st[:1] in ("R", "C") and i + 2 < len(entries):
            if st.startswith("R"):
                out[entries[i + 1]] = entries[i + 2]
            i += 3
        else:
            i += 2
    return out


def addable(files):
    """--files without the paths already staged as deleted (`git rm`, or the old path of a `git mv`):
    `git add <path>` refuses a path that is in neither the index nor the tree, though the deletion is
    exactly what to commit (review 10d B12). --no-renames: a git mv's old path is a deletion too,
    not an R that `git add` then refused as BAD_PATHSPEC (review 10f B11). The old path alone is
    refused, naming the new one."""
    renames = staged_renames()
    alone = [f"{f} (renamed to {renames[f]})" for f in files if f in renames and renames[f] not in files]
    if alone:
        raise Die(REFUSED, "BAD_PATHSPEC", "--files names a file by its old name: " + ", ".join(alone)
                  + ". Name the new path in --files (the old path goes with it), then re-run.")
    removed = set(staged("--no-renames", "--diff-filter=D"))
    return [f for f in files if f not in removed]


GLOB_CHARS = re.compile(r"[*?\[]")


def exists_as_typed(f):
    """<f> names a path as typed: on disk, or tracked (deleted from the tree)."""
    return os.path.lexists(f) or succeeds("ls-files", "--error-unmatch", "--", f":(literal){f}")


def is_literal(f):
    """A --files value that names a path as typed where git would read it as a pattern: glob
    characters (`app/[id]/page.tsx`, a Next.js route: that path, not the glob, or `app/i/` and
    `app/d/` went in too, review 10f B6), or a leading `:` (a file named `:x`, which git reads as
    pathspec magic, review 10g B12)."""
    return bool(GLOB_CHARS.search(f) or f.startswith(":")) and exists_as_typed(f)


def glob_specs(files, magic=""):
    """--files as shell-style globs: `*` stays inside one directory and `**` crosses them, as the
    user typed it to a shell. A plain git pathspec lets `src/*.ts` match src/sub/x.ts too (review
    10d B13). A path without wildcards matches as before; one that exists as typed is literal
    (is_literal); one already magic (`:(...)`) is as given. `magic` adds to each (`exclude`)."""
    if not magic:
        return [f if f == "." or (f.startswith(":") and not is_literal(f)) else f":({'literal' if is_literal(f) else 'glob'}){f}" for f in files]
    return [f":({magic},{'literal' if is_literal(f) else 'glob'}){f}" for f in files]


def staged(*extra):
    return [p for p in run_child(["git", "diff", "--cached", "--name-only", "-z", *extra]).stdout.split("\0") if p]


def stage(t):
    """Stage the requested paths plus every changed projen-managed file, so a commit never leaves out
    regenerated config. Refuses gitlinks, credentials and large files, leaving nothing staged."""
    managed = projen_managed()
    # What the user had staged, restored exactly on a refusal: a reset would drop a partly staged
    # version (git add -p) that exists nowhere else (review 10c B12, B16).
    saved = gout("write-tree")

    def unstage():
        if not (saved and succeeds("read-tree", saved)):
            git("reset", "-q")
    files = list(t.args.files)
    sources = []
    if files:
        # A rename (git mv) is its new path in --files: its old path goes with it, or the commit
        # keeps the file twice / refuses the staged deletion as "outside" (review 10c B12).
        for src, dst in staged_renames().items():
            if dst in files and src not in files:
                sources.append(src)   # already staged as deleted: excluded from "outside", not added
    if files:
        # --files limits the commit: something already staged outside them would go in too
        # (projen-managed files excepted: they're always staged).
        outside = [p for p in staged("--no-renames", "--", ".", *glob_specs(files + sources, "exclude")) if p not in managed]
        if outside:
            raise Die(REFUSED, "STAGED_OUTSIDE_FILES", "These are already staged but aren't in --files, so they would be committed too:\n"
                      + indent(outside) + "\n   Unstage them (git restore --staged <path>) or add them to --files, then re-run.")
    files = files or ["."]
    adds = glob_specs(addable(files))
    r = git("add", "--dry-run", "--", *adds) if adds else None
    if r is not None and r.returncode != 0:
        raise Die(REFUSED, "BAD_PATHSPEC", f"git add refused the --files paths ({' '.join(files)}): {r.stderr.strip()}")
    if adds and not succeeds("add", "--", *adds):
        raise Die(REFUSED, "BAD_PATHSPEC", f"git add failed for {' '.join(files)}.")
    # Lockfiles too: validation (npx projen) rewrites them with package.json, and a commit with one
    # but not the other breaks the next install (review 10b B19).
    managed = managed + [f for f in LOCKFILES if f not in managed]
    tracked = set(p for p in git("ls-files", "-z", "--", *managed).stdout.split("\0") if p) if managed else set()
    present = [f for f in managed if os.path.lexists(f) or f in tracked]
    if present:
        git("add", "-A", "--", *present)

    # A submodule at another commit than the branch records is staged as a bump; unless it was
    # named in --files that is almost always a stale checkout, not an intended change.
    named = set(files) | set(sources)
    bumped = [ln.partition("\t")[2] for ln in gout("diff", "--cached", "--raw", "--no-renames", "--diff-filter=M").splitlines()
              if len(ln.split()) > 1 and ln.split()[1] == "160000" and ln.partition("\t")[2] not in named]
    if bumped:
        unstage()
        raise Die(REFUSED, "SUBMODULE_CHANGED", "These submodules are checked out at another commit than the branch records, so the "
                  "commit would change them:\n" + indent(bumped) + "\n   Nothing more is staged (what you had staged is as it was). Run git submodule update (to match the "
                  "branch), or, to change a submodule on purpose, name it in --files. Then re-run.")
    gitlinks = []
    # --no-renames: a rename is its new path added (it may be a credential's name); T: a file that
    # became a gitlink (review 10b B3).
    for line in gout("diff", "--cached", "--raw", "--no-renames", "--diff-filter=AT").splitlines():
        meta, _, path = line.partition("\t")
        if len(meta.split()) > 1 and meta.split()[1] == "160000":
            gitlinks.append(path)
    if gitlinks:
        unstage()
        raise Die(REFUSED, "GITLINK", "Refusing to commit embedded git repositories (nested clones / agent worktrees):\n" + indent(gitlinks)
                  + "\n   Nothing more is staged (what you had staged is as it was). Add them to .gitignore (agent worktrees: .claude/worktrees/) and re-run.")
    try:
        max_mb = max_file_mb()
    except Die:
        unstage()
        raise
    secrets, large, markers = [], [], []
    for p, size, data in staged_blobs():
        if size > max_mb * 1024 * 1024 and p not in t.args.allow:
            large.append(f"{p} ({size // 1024 // 1024} MB)")
            continue
        if is_secret_path(t, p) or has_secret_content(t, p, data):
            secrets.append(p)
        elif CONFLICT_MARKERS.search(data) and p not in t.args.allow:
            markers.append(p)
    if markers:
        unstage()
        raise Die(REFUSED, "CONFLICT_MARKERS", "These files still contain merge-conflict markers (<<<<<<< / >>>>>>>):\n" + indent(markers)
                  + "\n   Nothing more is staged (what you had staged is as it was). Resolve the conflicts, then re-run (--allow-secret-path <path> for a file that "
                  "contains such lines on purpose).")
    if secrets:
        unstage()
        raise Die(REFUSED, "SECRET_PATH", "Refusing to commit files that look like, or contain, credentials:\n" + indent(secrets)
                  + "\n   Nothing more is staged (what you had staged is as it was). Add them to .gitignore (and remove any real credential from the file), or\n"
                  "   pass --allow-secret-path <path> for each one if it is intentional.")
    if large:
        unstage()
        raise Die(REFUSED, "LARGE_FILE", f"Refusing to commit files over {max_mb} MB (they stay in the history for good):\n" + indent(large)
                  + "\n   Nothing more is staged (what you had staged is as it was). Add them to .gitignore (build output belongs in dist/ or cdk.out/), or pass\n"
                  "   --allow-secret-path <path> for one that must be committed (CRE_MAX_FILE_MB raises the limit).")
    names = gout("diff", "--cached", "--name-status")
    if names:
        info("Staged:\n" + indent(names.splitlines()))


def commit(t):
    """True when a commit was made, False when there was nothing to commit."""
    if succeeds("diff", "--cached", "--quiet"):
        return False
    before = head()
    try:
        rc = git_streamed("commit", "-q", "-m", t.args.message)
    except Die:
        # A signal during a hook after the commit was written (post-commit) lands the commit: it is
        # said as done, not "interrupted" with nothing done (review 10f B9).
        if head() != before:
            t.done.append(f"committed {short(head())}")
        raise
    if rc != 0:
        raise Die(FAILED, "COMMIT_HOOK_FAILED", "git commit failed (a commit hook rejected it). Nothing was committed; your changes are still staged.")
    sha = head()
    t.done.append(f"committed {short(sha)}")
    ok(f"Committed {short(sha)}: {t.args.message.splitlines()[0]}")
    return True
