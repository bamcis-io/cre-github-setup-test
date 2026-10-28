"""Shared helpers for the CRE setup steps (aws_account, dns, repo_gitlab, repo_github, github_app)
and the end-to-end orchestrator (new_project). Python 3.9+, standard library only.

Contract every setup step follows, so an agent or a person can drive it deterministically:
  - It prints what it is about to change, then changes it. `--dry-run` prints only.
  - It is idempotent: anything already in the desired state is reported and skipped.
  - It never asks. A change to something possibly shared (an existing role's trust policy), or
    setup in a production account, is a decision (DECISIONS below): made beforehand with its
    switch, or `--force` for all of them, and refused (10) without it. The diff is printed either
    way. `--discovery` reads what applies and prints one `DECISION=<json>` line per decision,
    changing nothing, so the answers can carry them before the real run.
  - Values the next step needs are printed on stdout as `KEY=value` lines; everything else goes to
    stderr. The last stdout line is `RESULT code=<n> step=<name>`.
  - Exit codes:
       0  done (including "nothing to change")
      10  refused: bad or missing input, or a conflict that needs a human decision; nothing changed
      11  credentials missing or expired; the message names which and how to refresh them
      13  done as far as a script can go: a person must act on each `NEEDS=<what>` line (a browser
          step, an org owner's approval). The same command, re-run afterwards, carries on.
      20  an operation failed part way; the message says what was done and what wasn't

AWS, GitHub and GitLab are reached through their CLIs (`aws`, `gh`, `git`, `ssh`) and `curl`:
curl carries the Midway cookie jar through gitlab.aws.dev's sign-in redirects exactly as verified
live, and the offline harness (test/setup/run.sh) replaces each of these tools with a fake.
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

OK, REFUSED, CREDENTIALS, NEEDS, FAILED = 0, 10, 11, 13, 20   # 12 (confirmation) is gone: nothing asks

# The decisions setup never makes on its own: answers-file field (under `safety`) → switch, and when
# it applies. Each defaults to refused; `--force` makes all of them. A decision is about one subject
# (an account id, or role@account): a yes covers that subject only, so an answer given for one
# account never approves another (review 10g C1). `<switch>` alone approves any subject for that run;
# `<switch>=<subject>` and the answers' lists approve the named ones.
DECISIONS = {
    "allowProduction": ("--allow-production", "set up in an account is_prod() says is production (or can't classify)"),
    "updateRunnerTrustPolicy": ("--update-runner-trust-policy",
                                "add this project to an existing runner role's trust policy (other projects share the role)"),
    "updateDnsRoleTrustPolicy": ("--update-dns-role-trust-policy",
                                 "add this project's runner role to the existing CreDnsDeploy role's trust policy in the DNS account"),
    "adoptExistingRepo": ("--adopt-existing-repo",
                          "adopt an existing repo whose history setup didn't make: it gets this project's CI variables and role "
                          "trust, and the scaffold is merged into its main (the scaffold's version wins where both have a file)"),
}
# The decisions the repo steps (repo_gitlab.py, repo_github.py) meet.
REPO_DECISIONS = ("adoptExistingRepo",)

# The roles setup creates or uses when none is named: the one copy of these defaults (review 10f G3).
RUNNER_ROLE = {"gitlab": "GitLabRunner", "github": "CreGitHubDeploy"}
EXEC_ROLE = "CreCfnExecution"

# Names the deploy sets itself (stack-jobs.ts RESERVED_VARIABLES; a jest test keeps them equal).
# A project CI/CD variable outranks a GitLab job's own variables, so one of these would silently
# replace the generated value. STACK_* names carry earlier stacks' outputs and are reserved too.
RESERVED_VARS = ("STACK_NAME TEMPLATE_NAME AWS_REGION AWS_DEFAULT_REGION ASSETS_BUCKET OUTPUTS_PREFIX "
                 "OUTPUTS ROLE_ARN ASSUME_ROLE STACK_PARAMETERS REQUIRED_VARS REQUIRED_OUTPUTS "
                 "ASSETS_PREFIX PROJECT_NAME CRE_ROLE_ASSUMED").split()
RESERVED_PREFIX = "STACK_"

SETUP_DIR = os.path.dirname(os.path.abspath(__file__))


# ---- the step: messages, outputs, exit ---------------------------------------------------------

class Exit(Exception):
    """Ends a step with an exit code (the message is already printed)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---- messages (stderr) -----------------------------------------------------------------------------
# The one set of printers every script uses (review 10c G5), so a severity looks the same whichever
# script printed it. Flushed: under an agent's shell tool stderr is a pipe (block-buffered), and a
# message such as "ACTION NEEDED in the browser" must show while the step waits, not when it ends.

# The setup scripts' messages carry the wall-clock time and the seconds since the run started
# (`[18:48:26 +12.3s] ✅ ...`), so a log shows where a run spent its time. On for every cli()
# entry point (the setup steps and new_project.py, also update-template and cfs:onboard, which
# use it); the git tasks, which share these printers, stay unstamped. stderr only: the KEY=value,
# NEEDS, CHECK and RESULT lines on stdout are never stamped.
_STAMP = {"t0": None}


def stamp_messages():
    if _STAMP["t0"] is None:
        _STAMP["t0"] = time.time()


def elapsed():
    """Seconds since stamp_messages() (0 when the messages aren't stamped)."""
    return time.time() - _STAMP["t0"] if _STAMP["t0"] is not None else 0.0


def _emit(mark, msg):
    head = f"[{time.strftime('%H:%M:%S')} +{elapsed():.1f}s] " if _STAMP["t0"] is not None else ""
    print(f"{head}{mark}{msg}", file=sys.stderr, flush=True)


def info(msg):
    _emit("ℹ️  ", msg)


def ok(msg):
    _emit("✅ ", msg)


def warn(msg):
    _emit("⚠️  ", msg)


def plan(msg):
    _emit("→  ", msg)


def say(msg):
    """A plain line on stderr (a heading, a quoted log), no severity mark."""
    _emit("", msg)


class Step:
    """One setup step's run: its flags (dry_run, discovery, the decisions made), the KEY=value
    outputs, the NEEDS and the decisions discovery found.

    echo=True (a script run from the command line) prints each output on stdout as it is made;
    the orchestrator runs steps with echo=False and reads `outputs`.
    """

    def __init__(self, name, echo=True):
        self.name = name
        self.echo = echo
        self.dry_run = False
        self.discovery = False
        self.allowed = {}        # DECISIONS key -> set of approved subjects, or ANY
        self.found = []          # discovery: the decisions that apply, in the order met
        self.checks = []         # discovery: every safety check met, decision or not (CHECK lines)
        self.print_found = True  # False: the caller prints them (new_project merges its steps')
        self.outputs = {}
        self.needs = []
        self._cleanups = []

    # messages (stderr): crelib's one set of printers (below)
    def info(self, msg):
        info(msg)

    def ok(self, msg):
        ok(msg)

    def warn(self, msg):
        warn(msg)

    def plan(self, msg):
        plan(msg)

    def out(self, key, value):
        value = "" if value is None else str(value)
        # The first value of a key is the one callers read; a step never sets a key twice on purpose.
        self.outputs.setdefault(key, value)
        if self.echo:
            print(f"{key}={value}", flush=True)

    def need(self, what):
        """A person must act (exit 13): printed as NEEDS=<what>."""
        if what not in self.needs:
            self.needs.append(what)
        if self.echo:
            print(f"NEEDS={what}", flush=True)

    def die(self, code, msg):
        _emit("❌ ", msg)
        raise Exit(code)

    def made(self, key, subject):
        approved = self.allowed.get(key)
        return approved is ANY or (approved is not None and subject in approved)

    def check(self, key, subject, status, what, change="", approve=""):
        """Discovery: one safety check's outcome (a CHECK=<json> line), so the final safety check
        before setup can say what each answer leads to, a change or none. status: `change` (a
        decision, recorded by decide()), `in-place` (already so: nothing changed), `create` (setup
        makes it new: nothing shared changes) or `not-needed` (the check doesn't apply)."""
        if not self.discovery:
            return
        d = {"field": key, "subject": subject, "status": status, "what": what, "change": change, "approve": approve}
        self.checks.append(d)
        if self.echo and self.print_found:
            print(f"CHECK={json.dumps(d)}", flush=True)

    def decide(self, key, subject, what, shown=""):
        """A decision (DECISIONS[key]) about `subject` (an account id, role@account) that `what`
        needs; `shown` is the change (a policy diff), printed either way. Discovery: recorded and
        printed as a DECISION line (with `approve`, the subject to record); False (nothing is applied
        in discovery). Made for this subject: True. Not made: refused (10), saying a person decides."""
        switch, _ = DECISIONS[key]
        if shown:
            say(shown)
        if self.discovery:
            d = {"field": key, "switch": switch, "approve": subject, "what": what, "change": shown}
            self.found.append(d)
            self.check(key, subject, "change", what, shown, subject)
            if self.echo and self.print_found:
                print(f"DECISION={json.dumps(d)}", flush=True)
            return False
        if self.made(key, subject):
            self.info(f"{what}: allowed ({switch}{'' if self.allowed.get(key) is ANY else '=' + subject}).")
            return True
        if self.dry_run:
            self.info(f"(dry run) {what}: needs {switch}={subject}.")
            return False
        answers = os.environ.get("CRE_ANSWERS_FILE")
        if answers:
            # new_project's check already named every decision; this is the accounts changing since.
            self.die(REFUSED, f"{what}. The answers don't approve it: show the builder the change above and ask; only if they "
                     f'agree, add "{subject}" to "safety": {{"{key}": [...]}} in {answers}, then re-run.')
        self.die(REFUSED, f"{what}. Setup doesn't decide this on its own: show the user the change above and ask. Only if they "
                 f"agree, re-run with {switch}={subject}. Never pass it, or --force, without their yes.")

    def apply(self, what, fn, *args, **kwargs):
        """Run fn unless --dry-run (then print the plan and return None)."""
        if self.dry_run:
            self.plan(f"(dry run) {what}")
            return None
        self.plan(what)
        return fn(*args, **kwargs)

    def on_exit(self, fn):
        self._cleanups.append(fn)

    def close(self):
        while self._cleanups:
            try:
                self._cleanups.pop()()
            except Exception:  # cleanup must never mask the step's own result
                pass

    def final_code(self, code):
        return NEEDS if code == OK and self.needs else code


def invoke(name, fn, argv, echo=True):
    """Run a step function fn(step, argv) and return (code, step)."""
    step = Step(name, echo=echo)
    try:
        fn(step, argv)
        code = OK
    except Exit as e:
        code = e.code
    except KeyboardInterrupt:
        print("❌ Interrupted.", file=sys.stderr)
        code = FAILED
    except Exception:  # a bug or an unexpected environment: still a setup exit code and a RESULT line
        import traceback
        traceback.print_exc()
        _emit("❌ ", f"{name} failed unexpectedly (the traceback above). Re-run; if it repeats, report it.")
        code = FAILED
    finally:
        step.close()
    return step.final_code(code), step


def cli(name, fn):
    """The command-line entry point of a step: prints the RESULT line and exits with its code."""
    stamp_messages()
    code, _ = invoke(name, fn, sys.argv[1:], echo=True)
    info(f"{name} ended (exit {code}) after {elapsed():.1f}s.")
    print(f"RESULT code={code} step={name}", flush=True)
    sys.exit(code)


class _HelpFormatter(argparse.HelpFormatter):
    """Wraps help text at spaces only: argparse's default breaks at hyphens, which split a switch
    named in the text (`--exec-` / `role`)."""

    def _split_lines(self, text, width):
        import textwrap
        return textwrap.wrap(self._whitespace_matcher.sub(" ", text).strip(), width, break_on_hyphens=False)


class Parser(argparse.ArgumentParser):
    """argparse that refuses bad input with exit 10 (the setup contract), not argparse's 2."""

    def __init__(self, step, prog, **kw):
        kw.setdefault("formatter_class", _HelpFormatter)
        super().__init__(prog=prog, allow_abbrev=False, **kw)
        self._step = step

    def error(self, message):
        self._step.die(REFUSED, f"{self.prog}: {message}")


def common_flags(p):
    """--dry-run: print the plan, change nothing."""
    p.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")


ANY = object()   # a decision made for every subject (the bare switch, --force)


def decision_flags(p, keys):
    """The switches of the DECISIONS a step can meet, --force (all of them) and --discovery. Only
    the step's own: a decision it never meets isn't a switch it takes. `<switch>` approves any
    subject for this run; `<switch>=<subject>` (repeatable) only that one."""
    for k in keys:
        switch, why = DECISIONS[k]
        p.add_argument(switch, dest=f"decide_{k}", action="append", nargs="?", const="", metavar="SUBJECT",
                       help=f"allowed to {why}; =<subject> (the DECISION line's approve) for only that one")
    p.add_argument("--force", action="store_true", help=f"make every decision above ({', '.join(DECISIONS[k][0] for k in keys)})")
    p.add_argument("--discovery", action="store_true",
                   help="print one DECISION=<json> line per decision this run needs (with its change), and change nothing")


def take_decisions(step, a, keys):
    """The decision switches and --force onto the step."""
    step.discovery = a.discovery
    step.allowed = {}
    for k in keys:
        given = getattr(a, f"decide_{k}") or []
        if a.force or "" in given:
            step.allowed[k] = ANY
        elif given:
            step.allowed[k] = set(given)


def add_decisions(step, safety):
    """The answers' `safety` (field -> list of approved subjects) onto the step."""
    for k, subjects in (safety or {}).items():
        if step.allowed.get(k) is ANY:
            continue
        step.allowed[k] = step.allowed.get(k, set()) | set(subjects)


def decision_argv(allowed, keys):
    """The switches for `allowed` among a child step's `keys` (new_project passes them on): the bare
    switch for ANY, one `<switch>=<subject>` per approved subject."""
    out = []
    for k in keys:
        v = allowed.get(k)
        if v is ANY:
            out.append(DECISIONS[k][0])
        elif v:
            out += [f"{DECISIONS[k][0]}={x}" for x in sorted(v)]
    return out


def repo_flags(p, kind):
    """The flags repo_gitlab.py and repo_github.py share; `kind` is "project" or "repo"."""
    p.add_argument("--dir", default="", help="the demo's directory (default: the directory holding cre-setup.json)")
    p.add_argument("--message", default=SCAFFOLD_MESSAGE,
                   help="finalize: the first commit's message, when the scaffold has no commit yet")
    p.add_argument("--no-create", dest="no_create", action="store_true",
                   help=f"prepare: refuse (10) when the {kind} doesn't exist, instead of creating it (new_project.py after the first push)")
    p.add_argument("--recreate", action="store_true",
                   help=f"prepare: create the {kind} even though this clone pushed to it before (it was deleted since); "
                        "without it prepare refuses (10)")
    decision_flags(p, REPO_DECISIONS)


def take_repo_decisions(step, a):
    """repo_flags' decisions onto the step: prepare's only (--discovery reads them there); finalize
    and publish meet none."""
    if a.discovery and a.mode != "prepare":
        step.die(REFUSED, "--discovery is for setup:repo prepare (where the repo's history is read).")
    if a.mode == "publish" and (a.force or getattr(a, "decide_adoptExistingRepo", None)):
        step.die(REFUSED, "setup:github-public takes no decision switches: running it is the decision.")
    if a.mode == "finalize" and (a.force or getattr(a, "decide_adoptExistingRepo", None)):
        # finalize pushes only into an empty repo; adopting one with commits is new_project.py's.
        step.die(REFUSED, "setup:repo finalize takes no decision switches: --adopt-existing-repo is prepare's (finalize "
                 "pushes only into an empty repo).")
    take_decisions(step, a, REPO_DECISIONS)


def move_account_flag(p, help="allow a different account from the one cre-setup.json recorded (after a teardown)"):
    p.add_argument("--move-account", dest="move_account", action="store_true", help=help)


def require_repo_dir(step, d):
    """finalize's --dir: the scaffolded demo's git repository."""
    if not d or not os.path.isdir(os.path.join(d, ".git")):
        step.die(REFUSED, "--dir must be the scaffolded demo's git repository.")


def scaffold_pushed(step, d):
    """True (and said) when finalize's first push happened already: only the settings are checked."""
    if first_push_done(d):
        step.ok("The scaffold was pushed already; checking the repository settings only.")
        return True
    return False


# ---- processes -----------------------------------------------------------------------------------

def run(cmd, *, input=None, cwd=None, env=None):
    """Run a command; returns CompletedProcess with text stdout/stderr. Never raises on exit code.

    stdin is closed unless `input` is given, so a child can never wait on a prompt nobody sees
    (its output is captured). Undecodable bytes are replaced. A missing executable is returncode
    127, like a shell."""
    full_env = None
    if env:
        full_env = {**os.environ, **env}
    try:
        return subprocess.run(cmd, input=input, stdin=None if input is not None else subprocess.DEVNULL, cwd=cwd,
                              env=full_env, capture_output=True, text=True, errors="replace")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found ({e})")


def last_line(text):
    lines = [x for x in (text or "").strip().splitlines() if x.strip()]
    return lines[-1] if lines else ""


def stream(cmd, *, cwd=None, env=None, log=None):
    """Run a command with its output shown as it happens (log: appended to that file instead);
    returns the exit code, 127 when the executable is missing. For long commands (a build, a
    deploy wait) whose progress a person or agent should see; `run` captures instead."""
    full_env = {**os.environ, **env} if env else None
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        if log:
            with open(log, "a") as f:
                return subprocess.run(cmd, cwd=cwd, env=full_env, stdout=f, stderr=subprocess.STDOUT).returncode
        return subprocess.run(cmd, cwd=cwd, env=full_env).returncode
    except FileNotFoundError:
        print(f"{cmd[0]}: command not found", file=sys.stderr)
        return 127


def exec_replace(cmd, env=None):
    """Replace this process with cmd (the command's signals and exit code become this process's).
    env: the complete environment (os.environ when None). Exits 127 when cmd isn't found."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvpe(cmd[0], cmd, os.environ if env is None else env)
    except FileNotFoundError:
        print(f"{cmd[0]}: command not found", file=sys.stderr)
        sys.exit(127)


# Never copied, and never deleted from the destination, by mirror(): local tooling state.
MIRROR_EXCLUDES = ("__pycache__", ".pytest_cache", "*.pyc", ".DS_Store", "Thumbs.db", "*.swp")
# Scripts only the template runs (under the package's scripts/): never vended into a project, and
# removed from one that has an old copy, so no agent takes them for project tools (review 10d G7).
# update-template.sh is the shim an old project's vendored copy runs FROM THE TEMPLATE CLONE; a
# project never runs its own (review 10g G6).
# cresetup/ is the setup rewrite, run from the template clone only until projects switch to it.
TEMPLATE_ONLY_SCRIPTS = ("setup/new_project.py", "template/update-template.sh", "cresetup")


def drop_template_only(scripts_dir):
    """Remove TEMPLATE_ONLY_SCRIPTS from a project's copy of the package's scripts/."""
    for rel in TEMPLATE_ONLY_SCRIPTS:
        p = os.path.join(scripts_dir, *rel.split("/"))
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        elif os.path.lexists(p):
            os.remove(p)


def _excluded(rel, name, patterns):
    """A pattern is a name glob matched at any depth, or `/name` anchored at the top."""
    import fnmatch
    for p in patterns:
        if p.startswith("/"):
            if rel == p[1:]:
                return True
        elif fnmatch.fnmatchcase(name, p):
            return True
    return False


def git_ignored(root):
    """Paths under <root> (relative to it) that git ignores there and that aren't tracked: files,
    and whole ignored directories. Empty outside a git work tree."""
    if not os.path.isdir(root):
        return set()
    r = run(["git", "-C", root, "ls-files", "-z", "-o", "-i", "--exclude-standard", "--directory"])
    return {x.rstrip("/") for x in r.stdout.split("\0") if x} if r.returncode == 0 else set()


def mirror(src, dest, exclude=MIRROR_EXCLUDES, skip_ignored=False):
    """Make <dest> a copy of <src> (what `rsync -a --delete` did, without needing rsync; review 10c
    G7): every file copied with its mode and times, symlinks kept as links, and anything in <dest>
    that <src> doesn't have removed. An excluded path is neither copied nor removed. skip_ignored:
    a file git ignores in <src>'s repository isn't copied (local scratch, a build's output), and one
    git ignores in <dest>'s isn't removed (review 10g B12). Returns the number of files copied."""
    copied = 0
    os.makedirs(dest, exist_ok=True)
    src_ignored = git_ignored(src) if skip_ignored else set()
    dest_ignored = git_ignored(dest) if skip_ignored else set()

    def remove(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    def walk(rel):
        nonlocal copied
        s_dir, d_dir = os.path.join(src, rel), os.path.join(dest, rel)
        names = sorted(os.listdir(s_dir))
        keep = set()
        for name in names:
            r = os.path.join(rel, name) if rel else name
            if _excluded(r, name, exclude) or r in src_ignored:
                continue
            keep.add(name)
            s, d = os.path.join(s_dir, name), os.path.join(d_dir, name)
            if os.path.islink(s):
                if os.path.lexists(d):
                    remove(d)
                os.symlink(os.readlink(s), d)
            elif os.path.isdir(s):
                if os.path.lexists(d) and (os.path.islink(d) or not os.path.isdir(d)):
                    remove(d)
                os.makedirs(d, exist_ok=True)
                walk(r)
                shutil.copystat(s, d)
            else:
                if os.path.lexists(d) and (os.path.islink(d) or os.path.isdir(d)):
                    remove(d)
                shutil.copy2(s, d)
                copied += 1
        for name in os.listdir(d_dir):
            r = os.path.join(rel, name) if rel else name
            if name not in keep and not _excluded(r, name, exclude) and r not in dest_ignored:
                remove(os.path.join(d_dir, name))

    walk("")
    return copied


def edit_distance(a, b):
    """Edits between two names, a swap of neighbours counting as one (us-esat-2 is one from us-east-2)."""
    d = [[i if j == 0 else j if i == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def nearest(name, choices, most=2):
    """The choice fewest edits from name (at most `most`), or ""."""
    best = sorted((edit_distance(name, c), c) for c in choices)
    return best[0][1] if best and best[0][0] <= most else ""


# S3 refuses these bucket-name prefixes (and `-s3alias` / `--ol-s3` / `--x-s3` suffixes, which a
# `<project>-<region>` name can't end in) (review 10c C13).
BUCKET_RESERVED_PREFIXES = ("xn--", "sthree-", "amzn-s3-demo-")


def project_name_problem(name):
    """Why <name> can't prefix this project's S3 buckets (`<name>-<region>`), or ""."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}[a-z0-9]", name):
        return "3-42 lowercase letters, digits or hyphens (it prefixes S3 bucket names)"
    if name.startswith(BUCKET_RESERVED_PREFIXES):
        return f"S3 refuses bucket names starting with {', '.join(BUCKET_RESERVED_PREFIXES)}"
    return ""


def check_account(step, aws, sf, moving=False):
    """In a project, the account must be the one cre-setup.json records (accountId): a repair in
    another account (the wrong --profile, or none and the default credentials) made buckets and
    roles there and exited 0, with CI still on the old one (review 10c C1). --move-account means it."""
    if not getattr(aws, "profile", ""):
        step.info(f"No --profile: using the default credentials, {getattr(aws, 'caller', '') or 'unknown identity'} "
                  f"(account {aws.account}).")
    want = (sf.data or {}).get("accountId") if sf is not None else ""
    if want and aws.account and aws.account != want and not moving:
        step.die(REFUSED, f"These credentials are for account {aws.account}, but this project was set up in {want} "
                 "(cre-setup.json accountId). Pass the --profile for that account. To move the project to this account on "
                 f"purpose: tear the old stacks down first (npx projen teardown, with {want}'s credentials), then "
                 + move_buckets_note(want) + " Then re-run with --move-account.")


def move_buckets_note(old):
    """What a move to another account needs besides the teardown (review 10g C5): the teardown keeps
    the assets buckets, whose names the moved run must create again."""
    return (f"empty and delete the project's assets buckets in {old} (<project>-<region>, one per region; aws s3 rb "
            "s3://<project>-<region> --force, with its credentials): S3 bucket names are global, so the move can't create "
            "them here while they exist there.")


def require_tools(step, *names):
    """Exit 10 naming the missing tools (not 11: there is nothing to log in to)."""
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        step.die(REFUSED, f"Missing tools: {' '.join(missing)}. Install them (macOS: brew install {' '.join(missing)}), then re-run.")


def open_url(url):
    """Open a URL in a GUI browser when there is one (macOS, or a Linux desktop); never a console
    browser, which would block. Returns False when nothing was opened."""
    if os.environ.get("CRE_NO_BROWSER"):
        return False
    if sys.platform == "darwin" and shutil.which("open"):
        # macOS `open` hands the URL to the browser and exits at once: its exit code says whether
        # it worked (it fails outside a logged-in desktop session, e.g. over SSH).
        return run(["open", url]).returncode == 0
    if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("xdg-open"):
        cmd = ["xdg-open", url]
    else:
        return False
    # Detached, with no pipes: a browser xdg-open starts would otherwise hold them until it exits.
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError:
        return False


def write_json(path, doc, sort_keys=True):
    with open(path, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=sort_keys)
        f.write("\n")


def tmpdir(step):
    d = tempfile.mkdtemp(prefix="cre-setup-")
    step.on_exit(lambda: shutil.rmtree(d, ignore_errors=True))
    return d


# ---- cre-setup.json ------------------------------------------------------------------------------

class SetupFile:
    """The project's recorded setup values (cre-setup.json at the project root, written by the
    scaffold), so `npx projen setup:*` in a project needs no flags it already knows. Flags always
    win. $CRE_SETUP_FILE overrides the location."""

    def __init__(self, step):
        self.step = step
        self.path = os.environ.get("CRE_SETUP_FILE") or ""
        if not self.path:
            top = run(["git", "rev-parse", "--show-toplevel"])
            cand = os.path.join(top.stdout.strip(), "cre-setup.json") if top.returncode == 0 else ""
            if cand and os.path.isfile(cand):
                self.path = cand
        self.data = {}
        if self.path and os.path.isfile(self.path):
            try:
                with open(self.path) as f:
                    self.data = json.load(f)
            except (OSError, ValueError) as e:
                step.die(REFUSED, f"{self.path} isn't valid JSON ({e}). Fix it, or set CRE_SETUP_FILE to another file.")
        self.used = []

    @property
    def dir(self):
        """The project directory, "" when there is no real cre-setup.json (CRE_SETUP_FILE=/dev/null)."""
        return os.path.dirname(self.path) if self.path and os.path.isfile(self.path) else ""

    def default(self, args, attr, key):
        """Set args.<attr> from the recorded value when the flag wasn't given."""
        if getattr(args, attr) in (None, "", []):
            v = self.data.get(key)
            if v not in (None, ""):
                setattr(args, attr, v)
                self.used.append(key)

    def report(self):
        if self.used:
            self.step.info(f"From {self.path}: {' '.join(self.used)}")

    def set(self, key, value):
        """Record a value (only in a project that has a cre-setup.json)."""
        if not self.path or not os.path.isfile(self.path) or value in (None, "") or self.step.dry_run:
            return
        # The file as it is now, not as it was read: a branch switch (setup:github-public) or another
        # step may have changed it since.
        try:
            with open(self.path) as f:
                current = json.load(f)
            if isinstance(current, dict):
                self.data = current
        except (OSError, ValueError):
            pass
        if self.data.get(key) != value:
            self.data[key] = value
            write_json(self.path, self.data, sort_keys=False)  # a committed file: keep its order


# ---- AWS -----------------------------------------------------------------------------------------

# How the AWS CLI says the credentials lapsed or are gone, by key, SSO or credential_process (review
# 10e C8). One copy: stacks.py and deploy-stack.py had their own, shorter ones (review 10f D1).
CREDENTIALS_LAPSED = re.compile(r"ExpiredToken|InvalidClientTokenId|RequestExpired|security token included in the request is expired"
                                r"|Error when retrieving credentials|SSO session .*(has )?expired|Unable to locate credentials"
                                r"|Token has expired and refresh failed")


def credentials_lapsed(stderr):
    return bool(CREDENTIALS_LAPSED.search(stderr or ""))


class BadFile(Exception):
    """A project file (cre-stacks.json, cre-setup.json) that exists but isn't what npx projen or
    setup writes: each caller says it with its own exit, never as "empty" (review 10f G2)."""


def load_json(path):
    """A project JSON file: None when missing, BadFile when unreadable or not JSON (stacks.py and
    check-deploy-prereqs.py each had this function, review 10f G2)."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise BadFile(f"{path} can't be read ({e})")


def read_plan(path="cre-stacks.json"):
    """cre-stacks.json's stacks: [] when the file is missing (no stacks yet). BadFile when it can't
    be read or isn't the plan: an interrupted `npx projen` truncates it, and reading that as "no
    stacks" sent cfs:onboard after a missing CreEdgeStack and let setup:aws skip the plan's regions."""
    try:
        with open(path) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        raise BadFile(f"{path} can't be read ({e}): run npx projen to write it again")
    stacks = doc.get("stacks") if isinstance(doc, dict) else None
    if not isinstance(stacks, list):
        raise BadFile(f"{path} has no list of stacks: run npx projen to write it again")
    return [s for s in stacks if isinstance(s, dict)]


def output_vars(outputs, prefix, stack, only=None):
    """A stack's CloudFormation outputs as the variables its job writes for later stacks:
    `<prefix>_<OUTPUTKEY>=value`, only the `only` ones when given. A value with a line break can't go
    through a one-line-per-variable file: refused, naming it. The one copy of this rule for CI
    (deploy-stack.py) and the local deploy, which turned the newline into a space (review 10f D4).
    Returns [(key, value)]; raises ValueError with the message."""
    kept = [o for o in outputs or [] if only is None or o["OutputKey"] in only]
    multi = [o["OutputKey"] for o in kept if "\n" in o["OutputValue"] or "\r" in o["OutputValue"]]
    if multi:
        raise ValueError(f"Output(s) {', '.join(multi)} of {stack} contain a line break, which the outputs file (one "
                         "KEY=value line each) can't carry to the next stack. Change the output's value (e.g. join the lines, "
                         "or encode it).")
    return [((prefix + "_" if prefix else "") + o["OutputKey"].upper(), o["OutputValue"]) for o in kept]


# ---- is this a production account ------------------------------------------------------------
# One question, one entry point (is_prod), and the answer's source behind it: Isengard's account
# classification for Amazon accounts. Someone outside Amazon adds their own check to PROD_CHECKS and
# picks it with CRE_PROD_CHECK; nothing else changes.

ISENGARD_URL = "https://isengard-service.amazon.com"


class ProdCheckLapsed(Exception):
    """The check's own credentials lapsed (no Midway session): 11, never "treated as production"."""


class ProdCheckUnavailable(Exception):
    """The check couldn't be asked right now (unreachable, a 5xx, throttled): 20, re-run; never read
    as "production" or "Midway lapsed" (review 10g C3)."""


# The (profile, account, caller) lines Aws.require already printed in this process.
_ACCOUNT_SAID = set()


def isengard_is_prod(account_id):
    """Isengard's GetAWSAccountClassification (Midway-authenticated, read-only): (IsProduction,
    why); (None, why) when Isengard answers it doesn't know the account. Raises ProdCheckLapsed for
    Midway's sign-in (a redirect to midway-auth, or Isengard's Unauthenticated) and
    ProdCheckUnavailable for anything that says nothing about the account: curl failing, an HTTP
    5xx or 429, a throttle or internal error, a page that isn't Isengard's JSON."""
    code, out, err, _ = curl(["-s", "-L", "--max-time", "20", "-b", "<<mid>>", "-X", "POST",
                              "-H", "X-Amz-Target: com.amazon.isengard.coral.IsengardService.GetAWSAccountClassification",
                              "-H", "Content-Encoding: amz-1.0", "-H", "Content-Type: application/json; charset=UTF-8",
                              "-d", json.dumps({"AWSAccountID": account_id}), "-w", "\n%{http_code} %{url_effective}",
                              os.environ.get("CRE_ISENGARD_URL", ISENGARD_URL)],
                             feeds={"mid": read_midway_cookie()}, timeout=30)
    if code != 0:
        why = last_line(err) or f"curl exit {code}"
        raise ProdCheckUnavailable(f"Isengard can't be reached to classify account {account_id} ({why}): "
                                   + curl_hint(why, "check the network or VPN, then re-run."))
    body, _, tail = out.rpartition("\n")
    status, _, final = tail.partition(" ")
    lapsed = ProdCheckLapsed(f"Isengard needs a Midway session to classify the account. Run: {MWINIT_HINT}   then re-run.")
    if "midway-auth" in final:
        raise lapsed
    try:
        d = json.loads(body)
    except ValueError:
        d = None
    kind = str(d.get("__type") or d.get("message") or "") if isinstance(d, dict) else ""
    if "Unauthenticated" in kind:
        raise lapsed
    if not isinstance(d, dict) or status.startswith("5") or status == "429" or \
            any(k in kind for k in ("Throttl", "InternalFailure", "ServiceUnavailable", "InternalServer")):
        raise ProdCheckUnavailable(f"Isengard didn't classify account {account_id} (HTTP {status or '?'}"
                                   f"{', ' + kind if kind else ''}): it is unavailable or busy. Re-run in a minute.")
    c = d.get("AWSAccountClassification")
    if not isinstance(c, dict) or not isinstance(c.get("IsProduction"), bool):
        return None, f"Isengard has no classification for it ({kind[:120] or str(d)[:120]})"
    return c["IsProduction"], "Isengard: IsProduction" if c["IsProduction"] else "Isengard: not production"


PROD_CHECKS = {"isengard": isengard_is_prod}
_PROD_ANSWERS = {}   # (check, account) -> (prod, why): asked once per account per process


def is_prod(account_id):
    """(True/False, why) for an AWS account. An account the check can't classify counts as
    production: when unsure, setup acts as if it were (and --allow-production must say otherwise).
    Asked once per account per process: new_project runs every step in-process, and one call per
    `Aws.require()` was 4 to 6 per run, each a chance to fail mid-run (review 10g C3). A lapse or an
    outage isn't kept: the next call asks again."""
    name = os.environ.get("CRE_PROD_CHECK") or "isengard"
    check = PROD_CHECKS.get(name)
    if check is None:
        return True, f"CRE_PROD_CHECK={name} is no known check ({', '.join(PROD_CHECKS)}), so it is treated as production"
    key = (name, account_id)
    if key not in _PROD_ANSWERS:
        prod, why = check(account_id)
        _PROD_ANSWERS[key] = (True, f"{why}; treated as production") if prod is None else (prod, why)
    return _PROD_ANSWERS[key]


class Aws:
    """The aws CLI with one explicit profile (AWS_PROFILE semantics, but explicit so a stray env
    var can't silently point setup at a different account)."""

    def __init__(self, step, profile):
        self.step = step
        self.profile = profile or ""
        self.account = self.caller = ""
        self.partition = "aws"

    def call(self, *args):
        cmd = ["aws"] + (["--profile", self.profile] if self.profile else []) + list(args)
        r = run(cmd)
        # Credentials that lapse mid-step must say so (11), not read as "the role is missing" or a
        # failed call (review 10b C18).
        # A credential_process or SSO profile lapses with its own wording (review 10e C8).
        if r.returncode != 0 and credentials_lapsed(r.stderr):
            self.step.die(CREDENTIALS, f"The AWS credentials for profile '{self.profile or '<default>'}' expired or are invalid "
                          f"(aws {' '.join(args[:2])}): {last_line(r.stderr)}\n   Refresh them, then re-run: the steps done so far are kept.")
        return r

    def text(self, *args):
        """stdout of a successful call (stripped), else None."""
        r = self.call(*args)
        return r.stdout.strip() if r.returncode == 0 else None

    def json(self, *args):
        r = self.call(*args)
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout or "null")
        except ValueError:
            return None

    def require(self):
        """Check the credentials work; sets account, caller, partition. Dies 11 with how to refresh."""
        r = self.call("sts", "get-caller-identity", "--query", "[Account,Arn]", "--output", "text")
        parts = r.stdout.split() if r.returncode == 0 else []
        if len(parts) != 2 and re.search(r"config profile \(.*\) could not be found", r.stderr or ""):
            # A typo, not lapsed credentials: refreshing changes nothing, the answers do (review 10f C7).
            self.step.die(REFUSED, f"There is no AWS profile '{self.profile}' in ~/.aws/config or ~/.aws/credentials. Fix the "
                          "profile name (awsProfile in the answers file, or --profile), then re-run.")
        if len(parts) != 2:
            self.step.die(CREDENTIALS, f"AWS credentials for profile '{self.profile or '<default>'}' are missing or expired:\n"
                          f"   {(r.stderr or r.stdout).strip()}\n"
                          "   Refresh them (for example: isengard / ada credentials update for that profile), then re-run.")
        self.account, self.caller = parts
        # The partition (aws, aws-us-gov, aws-eusc, ...) from the caller's own ARN: every ARN setup
        # builds uses it, including policy.py's, so nothing assumes commercial AWS.
        self.partition = self.caller.split(":")[1] if self.caller.count(":") >= 2 else "aws"
        os.environ["CRE_PARTITION"] = self.partition
        # Once per process: new_project's own check and each step it runs read the same profile, and
        # --discovery printed the line once per reader (review 10g A4).
        said = (self.profile, self.account, self.caller)
        if said not in _ACCOUNT_SAID:
            _ACCOUNT_SAID.add(said)
            self.step.info(f"AWS account {self.account} as {self.caller} (profile {self.profile or '<default>'})")
        try:
            prod, why = is_prod(self.account)
        except ProdCheckLapsed as e:
            self.step.die(CREDENTIALS, str(e))
        except ProdCheckUnavailable as e:
            self.step.die(FAILED, str(e))
        if prod:
            self.step.warn(f"Account {self.account} is production ({why}). Setup creates S3 buckets and IAM roles in it.")
            self.step.decide("allowProduction", self.account, f"Setting up in production account {self.account} ({why})")
        elif not any(x["field"] == "allowProduction" and x["subject"] == self.account for x in self.step.checks):
            self.step.check("allowProduction", self.account, "not-needed", f"Account {self.account} isn't production ({why})")
        return self

    def role_arn(self, name):
        """The role's ARN as IAM returns it ("" if the role doesn't exist). ARNs are never assembled
        from a name: IAM finds a role case-insensitively (GitlabRunner finds GitLabRunner) but STS,
        trust-policy ARN conditions and the GitLab credential vendor compare the ARN exactly, and a
        role may carry a path."""
        r = self.call("iam", "get-role", "--role-name", name, "--query", "Role.Arn", "--output", "text")
        if r.returncode != 0:
            # Only NoSuchEntity means "missing": any other failure (a denied call, throttling) read as
            # missing made setup try to create a role that exists (review 10e C8).
            if "NoSuchEntity" in r.stderr or "cannot be found" in r.stderr:
                return ""
            self.step.die(FAILED, f"iam get-role {name} failed: {last_line(r.stderr)}")
        v = r.stdout.strip()
        return "" if v in ("", "None") else v

    def can(self, principal, action, resource, passed_to=None):
        """Read-only check that a role's own policies allow an action. True when allowed, or when the
        simulation can't run (no permission to simulate): advice only, so unknown isn't a gap."""
        args = ["iam", "simulate-principal-policy", "--policy-source-arn", principal, "--action-names", action,
                "--resource-arns", resource]
        if passed_to:
            args += ["--context-entries",
                     f"ContextKeyName=iam:PassedToService,ContextKeyValues={passed_to},ContextKeyType=string"]
        v = self.text(*args, "--query", "EvaluationResults[0].EvalDecision", "--output", "text")
        return v is None or v == "allowed"


# ---- GitLab (Midway only) ------------------------------------------------------------------------

TRUST_QUOTA_CODE = "L-C07B4B0D"                          # IAM "Role trust policy length"


def check_trust_size(step, aws, name, doc, way_out=""):
    """IAM refuses a trust policy over the account's limit (2048 characters by default, whitespace
    not counted). Every project a shared runner role trusts adds to it: say so before the call.
    `way_out`: the role's own last option (a new runner role helps the runner's trust, not
    CreDnsDeploy's, review 10c C12)."""
    size = len(json.dumps(doc, separators=(",", ":")))
    r = aws.call("service-quotas", "get-service-quota", "--service-code", "iam", "--quota-code", TRUST_QUOTA_CODE,
                 "--region", IAM_CONTROL_PLANE.get(getattr(aws, "partition", "") or "aws", "us-east-1"),
                 "--query", "Quota.Value", "--output", "text")
    try:
        limit = int(float(r.stdout.strip())) if r.returncode == 0 else 2048
    except ValueError:
        limit = 2048
    if size > limit:
        step.die(REFUSED, f"{name}'s trust policy would be {size} characters; this account allows {limit} (IAM quota "
                 f"'Role trust policy length', {TRUST_QUOTA_CODE}). Each project it trusts adds to it. Remove projects that no "
                 "longer exist from its trust, raise the quota (Service Quotas, IAM, up to 4096), or "
                 + (way_out or "give this project its own runner role (--runner-role <new name>; setup creates it)") + ".")
    if size > limit * 0.85:
        step.warn(f"{name}'s trust policy is {size} of {limit} characters: a few more projects and IAM refuses it.")


# IAM's limit on all of a role's inline policies together (characters, whitespace not counted).
INLINE_POLICY_LIMIT = 10240


def check_inline_size(step, aws, role, name, doc):
    """Before put-role-policy: the role's inline policies, with this one replaced, stay under IAM's
    aggregate limit. A shared zone-account role gets one per project, which used to fill it at
    about 7 projects with a bare LimitExceeded (review 10b C9)."""
    names = aws.json("iam", "list-role-policies", "--role-name", role, "--query", "PolicyNames", "--output", "json") or []
    total = len(json.dumps(doc, separators=(",", ":")))
    for other in names:
        if other == name:
            continue
        d = aws.json("iam", "get-role-policy", "--role-name", role, "--policy-name", other, "--query", "PolicyDocument", "--output", "json")
        total += len(json.dumps(d or {}, separators=(",", ":")))
    if total > INLINE_POLICY_LIMIT:
        step.die(REFUSED, f"{role}'s inline policies would total {total} characters; IAM allows {INLINE_POLICY_LIMIT} per role. "
                 f"It has one per project ({len(names)} now): delete those of projects that no longer exist "
                 f"(aws iam delete-role-policy --role-name {role} --policy-name cre-dns-<project>), then re-run.")
    if total > INLINE_POLICY_LIMIT * 0.85:
        step.warn(f"{role}'s inline policies are {total} of {INLINE_POLICY_LIMIT} characters: a few more projects and IAM refuses them.")


# Known credential formats: the git tasks scan every staged file for them (cregit.SECRET_CONTENT), and
# setup:github-public scans the whole history with `git log -G` before a repo goes public. One
# string for both, POSIX-ERE-safe (no \d, no lookarounds) for git (review 10b C11).
SECRET_CONTENT_PATTERN = (r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|glpat-[A-Za-z0-9_-]{20}|"
                          r"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|xox[baprs]-[A-Za-z0-9-]{10,}|npm_[A-Za-z0-9]{36}|"
                          r"_authToken *= *[^ $]|aws_secret_access_key *= *[A-Za-z0-9/+]{40}|"
                          # The environment-variable spelling (.envrc, a shell script, CI vars), quoted or not (review 10d B13).
                          r"AWS_SECRET_ACCESS_KEY *[=:] *[\"']?[A-Za-z0-9/+]{40}|"
                          # A Netscape cookie-file row (a Midway cookie is one: a live session), with
                          # literal tabs, as both Python and git's ERE read them.
                          "[A-Za-z0-9._-]+\t(TRUE|FALSE)\t/[^\t]*\t(TRUE|FALSE)\t[0-9]+\t")

# The CloudFront Signer bindle a project uses unless it names its own (aws-cre). The same value as
# DEFAULT_BINDLE_ID in src/components/deploy-tasks.ts (a test keeps them equal).
DEFAULT_BINDLE = "amzn1.bindle.resource.v4o66fxvd3zfcqvcmhxq"
# What to run when the Midway cookie or the SSH certificate signed with it has lapsed.
MWINIT_HINT = "mwinit -f -k ~/.ssh/id_ecdsa.pub"
# An AWS region name (us-east-2, us-gov-west-1, eusc-de-east-1), as stack-plan.ts checks it.
REGION_RE = r"[a-z]{2,4}(-[a-z]+)+-[0-9]+"

# Each partition's IAM and Route 53 control-plane regions: where a CreIamStack / CreDnsStack deploys,
# so where setup must make a bucket whatever the project's regions. The same tables as
# src/components/stack-plan.ts (a jest test keeps them equal).
IAM_CONTROL_PLANE = {"aws": "us-east-1", "aws-us-gov": "us-gov-west-1", "aws-cn": "cn-north-1", "aws-eusc": "eusc-de-east-1"}
DNS_CONTROL_PLANE = {"aws": "us-east-1", "aws-us-gov": "us-gov-west-1", "aws-cn": "cn-northwest-1", "aws-eusc": "eusc-de-east-1"}


def partition_of(region):
    """The partition a region name belongs to (as stack-plan.ts partitionOf)."""
    for prefix, partition in (("cn-", "aws-cn"), ("us-gov-", "aws-us-gov"), ("eusc-", "aws-eusc")):
        if region.startswith(prefix):
            return partition
    return "aws"


def control_plane_regions(partition):
    """The regions that always need an assets bucket in a partition (IAM, then DNS; one in most)."""
    return list(dict.fromkeys([IAM_CONTROL_PLANE.get(partition, "us-east-1"), DNS_CONTROL_PLANE.get(partition, "us-east-1")]))


GITLAB_HOST = os.environ.get("CRE_GITLAB_HOST", "gitlab.aws.dev")
GITLAB_SSH = os.environ.get("CRE_GITLAB_SSH", "git@ssh.gitlab.aws.dev")
GITLAB_API = os.environ.get("CRE_GITLAB_API", f"https://{GITLAB_HOST}/api/v4")
MANAGE_API = os.environ.get("CRE_GITLAB_MANAGE_API", "https://manage.gitlab.aws.dev")


def midway_cookie():
    return os.environ.get("MIDWAY_COOKIE") or os.path.expanduser("~/.midway/cookie")


def read_midway_cookie():
    """The Midway cookie file's text ("" when it can't be read). Only ever read, never written."""
    try:
        with open(midway_cookie(), errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ---- curl with in-memory files -------------------------------------------------------------------
# Cookies, tokens and request bodies never touch disk: curl reads and writes them through pipes
# (/dev/fd/N), so nothing is left behind by a killed run and nothing can be committed.

HTTP_MARK = "\n__CRE_HTTP__"


def curl_hint(why, otherwise="Check the network, then re-run."):
    """What to do about a request curl couldn't make: a missing curl isn't the network (review 10g
    B12)."""
    if re.search(r"curl: (command not found|can't run it)", why or ""):
        return "curl isn't installed (or can't run): install it, then re-run."
    return otherwise


def curl(args, feeds=None, collect=(), timeout=None, passthrough=False):
    """Run curl with in-memory files. An argument that is exactly `<<name>>` or `@<<name>>` becomes
    /dev/fd/N of a pipe: one curl reads (`feeds`: name → text, e.g. a cookie jar for -b, headers for
    -H @<<h>>, a body for --data-binary @<<b>>) or one curl writes (`collect`: names, e.g.
    -c <<jar>> or -D <<hdrs>>). Nothing else is rewritten, so a URL or header value that contains
    `<<x>>` goes through as typed (review 10c E3); a placeholder naming no feed is a ValueError.
    Feed text is encoded with surrogateescape, so bytes that aren't UTF-8 (from argv) go through
    unchanged (E4). A feed named by two arguments is one pipe, read once: name each use separately.
    Returns (returncode, stdout, stderr, {collected name: text}). Never raises on curl's exit code;
    127 when curl is missing, 28 (curl's own timeout code) when `timeout` seconds pass.
    passthrough=True: curl writes straight to this process's stdout and stderr (bytes untouched,
    e.g. an image); stdout and stderr come back empty."""
    import threading
    feeds = feeds or {}
    known = set(feeds) | set(collect)
    for a in args:
        m = re.fullmatch(r"@?<<(\w+)>>", a)
        if m and m.group(1) not in known:
            raise ValueError(f"curl: argument {a!r} names no feed or collect")
    payload = {name: (text or "").encode(errors="surrogateescape") for name, text in feeds.items()}
    fds, child, writers, readers, got = {}, [], [], {}, {}
    started = False
    try:
        for name in feeds:
            r, w = os.pipe()
            fds[name] = r
            child.append(r)
            writers.append((w, payload[name]))
        for name in collect:
            r, w = os.pipe()
            fds[name] = w
            child.append(w)
            readers[name] = r

        def place(a):
            m = re.fullmatch(r"(@?)<<(\w+)>>", a)
            return f"{m.group(1)}/dev/fd/{fds[m.group(2)]}" if m else a
        argv = [place(a) for a in args]
        try:
            sys.stdout.flush()
            pipe = None if passthrough else subprocess.PIPE
            p = subprocess.Popen(["curl"] + argv, stdin=subprocess.DEVNULL, stdout=pipe,
                                 stderr=pipe, pass_fds=child)
            started = True
        except OSError as e:
            # Missing, or not executable (PermissionError): one answer, 127 (review 10c B10).
            return 127, "", ("curl: command not found" if isinstance(e, FileNotFoundError) else f"curl: can't run it ({e})"), {}
    finally:
        for fd in child:   # the child holds its own copies
            os.close(fd)
        if not started:    # no curl to feed or drain: every parent end closes here too (E3)
            for w, _ in writers:
                os.close(w)
            for r in readers.values():
                os.close(r)

    def feed(w, data):
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(w, view):]
        except OSError:   # curl never read it (an early failure): nothing to do
            pass
        finally:
            os.close(w)

    def drain(name, r):
        chunks = []
        while True:
            b = os.read(r, 65536)
            if not b:
                break
            chunks.append(b)
        os.close(r)
        got[name] = b"".join(chunks).decode(errors="replace")

    threads = [threading.Thread(target=feed, args=wd, daemon=True) for wd in writers]
    threads += [threading.Thread(target=drain, args=(n, r), daemon=True) for n, r in readers.items()]
    for t in threads:
        t.start()
    try:
        out, err = p.communicate(timeout=timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        code = 28
    for t in threads:
        t.join(5)
    return code, (out or b"").decode(errors="replace"), (err or b"").decode(errors="replace"), got


class MidwaySession:
    """A Midway-fronted site's cookies, in memory: the Midway cookie plus the session cookie the
    front door sets on the first request (`prime`). Every request reads them, and keeps what the
    site sets, through pipes; ~/.midway/cookie is only read and no other cookie file is made.

    Why a session at all: a GET survives the front door's sign-in redirect, but a write that
    arrives without the front door's own session cookie is redirected to sign-in, and curl's -L
    turns it into a GET (GitLab answers 404 or 405)."""

    def __init__(self, prime_url, timeout=None):
        self.prime_url = prime_url
        self.jar = ""
        # Seconds per request (None: curl's own defaults, which wait minutes on a dead network).
        self.timeout = timeout
        # Why the last request got "000": curl's last stderr line, e.g. "curl: (6) Could not
        # resolve host" (review 10d B13). "" after a request curl completed.
        self.error = ""

    def _failed(self, code, err):
        lines = [l for l in (err or "").strip().splitlines() if l.strip()]
        if code == 28 and not lines:
            return f"curl: no answer within {self.timeout} seconds"
        return lines[-1].strip() if lines else f"curl exited {code}"

    def prime(self):
        """Visit prime_url through the sign-in redirects; returns the final URL ("" on failure)."""
        code, out, err, got = curl(["-sS", "-L", "-o", os.devnull, "-w", "%{url_effective}", "-b", "<<mid>>",
                                    "-c", "<<jar>>", self.prime_url], feeds={"mid": read_midway_cookie()}, collect=("jar",),
                                   timeout=self.timeout)
        if got.get("jar"):
            self.jar = got["jar"]
        self.error = "" if code == 0 else self._failed(code, err)
        return out.strip() if code == 0 else ""

    def request(self, method, url, headers=None, body=None, follow=True, response_headers=False, timeout=None):
        """(status, final url, body text, response headers text). Status "000" when curl failed.
        headers: header lines (they go on a pipe, never on curl's command line); body: text."""
        args = ["-sS", "-o", "-", "-w", HTTP_MARK + "%{http_code} %{url_effective}", "-X", method,
                "-b", "<<jar>>", "-c", "<<jar_out>>"]
        feeds = {"jar": self.jar or read_midway_cookie()}
        collect = ["jar_out"]
        if follow:
            args.append("-L")
        if headers:
            args += ["-H", "@<<hdr>>"]
            feeds["hdr"] = "".join(h.rstrip("\n") + "\n" for h in headers)
        if body is not None:
            args += ["--data-binary", "@<<body>>"]
            feeds["body"] = body
        if response_headers:
            args += ["-D", "<<resp>>"]
            collect.append("resp")
        code, out, err, got = curl(args + [url], feeds=feeds, collect=collect,
                                   timeout=self.timeout if timeout is None else timeout)
        if got.get("jar_out"):
            self.jar = got["jar_out"]
        text, _, tail = out.rpartition(HTTP_MARK)
        status, _, final = tail.strip().partition(" ")
        if code != 0 or not status.isdigit():
            self.error = self._failed(code, err)
            return "000", "", "", got.get("resp", "")
        self.error = ""
        return status, final, text, got.get("resp", "")


# ---- CloudFront Signer (Midway) ------------------------------------------------------------------

CFS_SIGNER = "https://cloudfrontsigner.ninjas.security.a2z.com/v2/authorize"
# What CloudFront Signer's own sign-in page carries: a request that reaches it was refused (no or
# bad signed cookies), whatever its HTTP status.


def cfs_signed_cookies(target_url):
    """(cookie header value, expiry epoch) for a CloudFront Signer distribution, from the Midway
    cookie alone, as a browser gets them: random rfp, nonce = SHA-384(rfp), then the authorize
    endpoint through Midway sign-in, which redirects back to the distribution with the signed
    parameters in the URL fragment. The policy covers the whole distribution (https://<dist>/*).
    Raises ValueError (the reason) when no signed parameters come back. Nothing is written."""
    import hashlib
    from urllib.parse import parse_qsl, quote
    rfp = os.urandom(8).hex()
    nonce = hashlib.sha384(rfp.encode()).hexdigest()
    signer = os.environ.get("CFS_SIGNER") or CFS_SIGNER
    url = f"{signer}?encodedTargetUrl={quote(target_url, safe='')}&cfsNonce={nonce}"
    code, out, err, _ = curl(["-sS", "-L", "--max-redirs", "15", "-o", os.devnull, "-w", "%{url_effective}",
                              "-b", "<<mid>>", url], feeds={"mid": read_midway_cookie()}, timeout=60)
    final = out.strip()
    if code != 0:
        raise ValueError(f"cannot reach CloudFront Signer ({last_line(err) or f'curl exit {code}'})")
    q = dict(parse_qsl(final.split("#", 1)[1])) if "#" in final else {}
    if not (q.get("policy") and q.get("sig") and q.get("kpid")):
        host = re.sub(r"^https?://([^/]+).*", r"\1", final)
        if "midway" in host:
            raise ValueError(f"Midway did not sign you in (the session lapsed). Run: {MWINIT_HINT}   then re-run")
        raise ValueError(f"CloudFront Signer returned no signed cookies (ended at {final.split('#')[0] or 'nothing'}): "
                         "is the distribution onboarded (npx projen cfs:onboard) and are you in its bindle?")
    cookie = (f"CloudFront-Policy={q['policy']}; CloudFront-Signature={q['sig']}; CloudFront-Key-Pair-Id={q['kpid']}; "
              f"CloudFront-Expiration={q.get('exp', '')}; CloudFront-RFP={rfp}")
    try:
        exp = int(q.get("exp", "0"))
    except ValueError:
        exp = 0
    return cookie, exp


def midway_problem(path):
    """Why the Midway cookie at `path` can't be used ("" when it can): missing, unreadable (with
    the reason), or expired. Callers name it instead of guessing "missing or expired" (10d B13)."""
    if not os.path.lexists(path):
        return f"there is no Midway cookie at {path}"
    try:
        with open(path, "rb"):
            pass
    except OSError as e:
        return f"the Midway cookie at {path} can't be read ({e.strerror})"
    if os.path.isfile(path) and os.path.getsize(path) == 0:
        return f"the Midway cookie at {path} is empty"
    if midway_expiry(path) <= time.time():
        return f"the Midway cookie at {path} has expired"
    return ""


def midway_expiry(path):
    """The latest expiry (epoch seconds) of the Midway session cookie, 0 when there is none. A byte
    that isn't UTF-8 (a stray character in another row) is replaced, never a crash (review 10d B13)."""
    best = 0
    try:
        with open(path, errors="replace") as f:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 7 and "midway-auth.amazon.com" in cols[0] and cols[5] == "session":
                    try:
                        best = max(best, int(cols[4]))
                    except ValueError:
                        pass
    except OSError:
        return 0
    return best


# ---- where the GitLab API token file is ---------------------------------------------------------
# The token itself is never copied anywhere. Its path is recorded once per machine, as a link in
# the template clone (.agents/gitlab-token, gitignored; made by new_project.py from /cre-demo-init's
# answer), and every GitLab project gets the same link at .cre/gitlab-token (gitignored; made by
# attach-template.py at scaffold and on every update-template). No default path is assumed: where a
# builder keeps the file, and what it's called, is theirs.

TEMPLATE_TOKEN_LINK = os.path.join(".agents", "gitlab-token")
PROJECT_TOKEN_LINK = os.path.join(".cre", "gitlab-token")


def read_json(path):
    """A JSON object file as a dict: {} when it is missing, unreadable or not an object."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def json_str(d, key):
    """d[key] when it is a string, else ""."""
    v = d.get(key, "")
    return v if isinstance(v, str) else ""


def is_template_clone(path):
    return bool(path) and os.path.isdir(os.path.join(path, "packages", "cre-projen", "src"))


def recorded_template(project_root):
    """(the templatePath a project records, its projenrc/cre/SOURCE.json): .cre/template.json (per
    machine) since #35, SOURCE.json's templatePath in older projects."""
    src = read_json(os.path.join(project_root, "projenrc", "cre", "SOURCE.json"))
    rec = json_str(read_json(os.path.join(project_root, ".cre", "template.json")), "templatePath") or json_str(src, "templatePath")
    return rec, src


def project_template_dir(project_root):
    """The template clone a project uses, the one way every script finds it (review 10c G6): the
    recorded one, else the sibling directory SOURCE.json names (`templateDirName`, a plain name).
    "" when neither is a clone."""
    rec, src = recorded_template(project_root)
    if is_template_clone(rec):
        return rec
    name = json_str(src, "templateDirName") or "cre-demo-template"
    if "/" in name or name in ("..", "."):
        return ""
    sibling = os.path.join(os.path.dirname(os.path.abspath(project_root)), name)
    return sibling if is_template_clone(sibling) else ""


def template_dir():
    """The cre-demo-template clone: this file's own (run from the template), or the project's
    (`project_template_dir`, run from a project's vendored copy). "" when unknown."""
    # setup → scripts → cre-projen | cre → packages | projenrc → the template | the project
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(SETUP_DIR))))
    if os.path.isfile(os.path.join(root, "recipe", "recipe.md")) and os.path.isdir(os.path.join(root, "skeleton")):
        return root
    return project_template_dir(root)


def template_token_record():
    """The template's record of the token file's path (a link), "" when the template is unknown.
    $CRE_TOKEN_RECORD replaces it (the offline harnesses: never the real clone's record)."""
    if os.environ.get("CRE_TOKEN_RECORD"):
        return os.environ["CRE_TOKEN_RECORD"]
    tpl = template_dir()
    return os.path.join(tpl, TEMPLATE_TOKEN_LINK) if tpl else ""


def gitlab_token_file(project_dir=None):
    """(path, where) of the GitLab token file, ("", "") when none is known. In order:
    $CRE_GITLAB_TOKEN_FILE; the project's .cre/gitlab-token link (project_dir, else the current
    directory); the template's .agents/gitlab-token record. A link is returned even when it dangles,
    so the message names what it points at."""
    if os.environ.get("CRE_GITLAB_TOKEN_FILE"):
        return expand_path(os.environ["CRE_GITLAB_TOKEN_FILE"]), "$CRE_GITLAB_TOKEN_FILE"
    here = project_dir or os.getcwd()
    link = os.path.join(here, PROJECT_TOKEN_LINK)
    if os.path.lexists(link):
        return link, PROJECT_TOKEN_LINK
    # A linked worktree (agent tools work in them) has no .cre/ of its own: the link is in the
    # primary worktree's (review 10f B10).
    primary = primary_worktree(here)
    if primary and os.path.lexists(os.path.join(primary, PROJECT_TOKEN_LINK)):
        return os.path.join(primary, PROJECT_TOKEN_LINK), f"{PROJECT_TOKEN_LINK} of the primary worktree ({primary})"
    rec = template_token_record()
    if rec and os.path.lexists(rec):
        return rec, f"the template's {TEMPLATE_TOKEN_LINK}"
    return "", ""


def primary_worktree(path):
    """The primary worktree of the git checkout at <path>, when <path> is a linked worktree of it;
    "" otherwise (not git, or already the primary one)."""
    r = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    lines = r.stdout.split("\n") if r.returncode == 0 else []
    if len(lines) < 2 or not lines[0].endswith("/.git"):
        return ""
    primary = os.path.dirname(lines[0])
    return primary if os.path.realpath(primary) != os.path.realpath(lines[1]) else ""


def recorded_token_target():
    """The token file the template records (what its link points at), "" when none."""
    rec = template_token_record()
    return os.readlink(rec) if rec and os.path.islink(rec) else ""


def read_token_file(path):
    """(token, "") or ("", what is wrong with the file)."""
    if os.path.islink(path) and not os.path.exists(path):
        return "", f"points at {os.readlink(path)}, which doesn't exist"
    try:
        with open(path) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return "", f"can't be read ({e})"
    parts = text.split()
    if not parts:
        return "", "is empty"
    if len(parts) > 1:
        return "", "holds more than one word; it must hold only the token"
    return parts[0], ""


def expand_path(p):
    """A path as the builder typed it (~, ~user, $HOME, relative) → absolute."""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p.strip())))


def check_gitlab_token(step, token, path):
    """GitLab accepts the token: active, not expired, `api` scope. Warns when it expires within 30
    days. Needs the Midway cookie (the front door). Dies 11 naming the file otherwise."""
    with GitLab(step) as gl:
        gl.token = token
        status, d = gl.call("GET", "/personal_access_tokens/self")
        lapsed = getattr(gl, "last_lapsed", False)
    # Only GitLab's own refusal says anything about the token. A network failure or a GitLab outage
    # read as "expired, revoked or mistyped", and the builder replaced a good token (review 10f C5).
    if status == "000":
        step.die(FAILED, f"Can't reach GitLab to check the token in {path} ({(d or {}).get('message', 'curl failed')}): check the "
                 "network or VPN, then re-run. The token itself wasn't checked.")
    if lapsed:
        step.die(CREDENTIALS, f"GitLab sent the request to sign-in: the Midway session lapsed. Run: {MWINIT_HINT}   then re-run "
                 f"(the token in {path} wasn't checked).")
    if status.startswith("5"):
        step.die(FAILED, f"GitLab answered {status} while checking the token in {path}: GitLab itself failed. Re-run in a few "
                 "minutes; the token wasn't checked.")
    if status in ("401", "403", "404"):
        step.die(CREDENTIALS, f"GitLab refused the token in {path} ({status}): it is expired, revoked or mistyped.\n" + token_help())
    if status != "200" or not isinstance(d, dict):
        step.die(FAILED, f"GitLab answered {status} checking the token in {path}: {str(d)[:200]}")
    if d.get("active") is False or d.get("revoked"):
        step.die(CREDENTIALS, f"The token in {path} is no longer active.\n" + token_help())
    if "api" not in (d.get("scopes") or []):
        step.die(CREDENTIALS, f"The token in {path} lacks the 'api' scope (it has: {', '.join(d.get('scopes') or []) or 'none'}).\n"
                 + token_help())
    exp = d.get("expires_at")
    if exp:
        import datetime
        left = (datetime.date.fromisoformat(exp) - datetime.date.today()).days
        if left < 30:
            step.warn(f"The GitLab token in {path} expires in {left} day(s) ({exp}). Create a new one and replace the file's contents.")
    step.ok(f"GitLab token in {path} is valid ('{d.get('name', '?')}', api scope{', expires ' + exp if exp else ''}).")


def token_help(cwd=None):
    """How to make and link the token. In the template clone the link is .agents/gitlab-token (its
    .agents/ is gitignored, .cre/ isn't): the project's path there would be committed (review 10f F1)."""
    root = subprocess.run(["git", "-C", cwd or os.getcwd(), "rev-parse", "--show-toplevel"], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True).stdout.strip()
    in_template = bool(root) and os.path.isfile(os.path.join(root, "recipe", "recipe.md")) and \
        os.path.isdir(os.path.join(root, "skeleton"))
    link = (f"   In this template clone: ln -s <that file> {TEMPLATE_TOKEN_LINK}   (.agents/ is gitignored here; every\n"
            "   project made from this clone then finds it too).\n") if in_template else \
        (f"   For a project made before that: ln -s <that file> {PROJECT_TOKEN_LINK}   (in the project; .cre/ is gitignored).\n")
    return ("   Create a personal access token with the 'api' scope at\n"
            f"   https://{GITLAB_HOST}/-/user_settings/personal_access_tokens and save it in a file only you can read,\n"
            "   e.g.  (umask 077; mkdir -p <dir>; cat > <dir>/<file>)  then paste the token, Enter, Ctrl-D.\n"
            "   /cre-demo-init records where it is (gitlab.tokenFile in the answers) for every later project.\n"
            + link + "   For one run only: CRE_GITLAB_TOKEN_FILE=<that file>.")


def place_link(path, target):
    """Make <path> a symlink to <target>, replacing an older link (never a real file). Returns
    "created", "updated", "unchanged" or "blocked" (a real file or directory is there)."""
    if os.path.islink(path):
        if os.readlink(path) == target:
            return "unchanged"
        state = "updated"
    elif os.path.lexists(path):
        return "blocked"
    else:
        state = "created"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, path)
    return state


class GitLab:
    """GitLab REST calls with the Midway cookie plus an API token (gitlab.aws.dev requires both).

    Use as a context manager: a token minted over SSH is revoked on exit. The cookies live in
    memory only (MidwaySession)."""

    def __init__(self, step, lapse_fatal=False, timeout=None):
        """lapse_fatal: a lapsed Midway session or a refused token ends the step with 11 (setup),
        instead of a "401" every caller must recognise (review 10c C3). The git tasks and watch map
        401 to their own codes, so they leave it off. timeout: seconds per request (the git tasks
        set one, so a dead network fails in seconds, not minutes)."""
        self.step = step
        self.token = ""
        self.token_source = ""
        self.last_lapsed = False
        self.minted = False
        self.lapse_fatal = lapse_fatal
        self.session = MidwaySession(f"{GITLAB_API}/version", timeout=timeout)

    def require_midway(self):
        why = midway_problem(midway_cookie())
        if why:
            # Missing, unreadable or expired: said which (review 10d B13). "Midway cookie" stays in
            # the text, which agents and tests look for.
            self.step.die(CREDENTIALS, f"Midway cookie problem: {why}. Run: {MWINIT_HINT}   then re-run.")
        left = midway_expiry(midway_cookie()) - time.time()
        if left < 1200:
            # A run takes minutes; one that starts with minutes left lapses part way (review 10e C8).
            self.step.warn(f"The Midway session expires in {int(left // 60)} minutes, and this run may outlast it. Refresh it first: "
                           f"{MWINIT_HINT}")
        r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-T", GITLAB_SSH], input="")
        if "Welcome to GitLab" not in (r.stdout + r.stderr):
            self.step.die(CREDENTIALS, f"Cannot authenticate to GitLab over SSH ({GITLAB_SSH}); the Midway SSH certificate is missing or "
                          f"expired. Run: {MWINIT_HINT}   then re-run.")

    def __enter__(self):
        # Primed with a GET, so writes carry the front door's session cookie (MidwaySession).
        primed = self.session.prime()
        if self.lapse_fatal and not primed.startswith(f"https://{GITLAB_HOST}"):
            self.step.die(CREDENTIALS, f"GitLab's front door didn't let the session through ({primed or 'no answer'}): the Midway "
                          f"session lapsed. Run: {MWINIT_HINT}   then re-run (the steps done so far are kept).")
        return self

    def __exit__(self, *exc):
        self.revoke()
        self.session.jar = ""
        return False

    def get_token(self, mint=True):
        """An api token. Tried in order:
          1. (mint=True) Minted over the Midway-signed SSH session (gitlab-shell
             `personal_access_token`), held only in memory and revoked on exit. GitLab issues these
             only to an SSH *key* it has on record; a Midway certificate authenticates by username,
             so on gitlab.aws.dev today this is refused. Kept first so it starts working without a
             change if GitLab allows it.
          2. The token file gitlab_token_file() finds: $CRE_GITLAB_TOKEN_FILE, the project's
             .cre/gitlab-token, the template's record (never an assumed default path).
          3. $GITLAB_TOKEN, only when no token file is recorded.
        A stored token (2, 3) is never revoked."""
        why = "not tried"
        if mint:
            name = "cre-setup-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", GITLAB_SSH,
                     "personal_access_token", name, "api", "1"], input="")
            text = r.stdout + "\n" + r.stderr
            m = re.search(r"glpat-[A-Za-z0-9_-]+", text) or re.search(r"^\s*Token:\s*(\S+)", text, re.M)
            if m and r.returncode == 0:
                self.token = m.group(1) if m.lastindex else m.group(0)
                self.minted = True
                self.step.info("Using a one-day GitLab token minted over SSH (revoked on exit).")
                return
            why = next((ln[len("remote:"):].strip() for ln in text.splitlines()
                        if ln.startswith("remote:") and ln[len("remote:"):].strip().strip("=")), "") or "no output"
            why = f"minting one over SSH was refused ({why})"
        # A recorded token file (this run's $CRE_GITLAB_TOKEN_FILE, the project's .cre/gitlab-token,
        # the template's record) wins over an exported $GITLAB_TOKEN from other tooling, which setup
        # never checked: in a project too, not only in the scaffold (review 10c C4, 10d C4).
        path, where = gitlab_token_file()
        if path:
            token, err = read_token_file(path)
            if err:
                self.step.die(CREDENTIALS, f"The GitLab token file ({where}: {path}) {err}.\n" + token_help())
            self.token, self.token_source = token, f"the token file {path} ({where})"
            if mint:
                self.step.info(f"{why[0].upper()}{why[1:]}; using the token file ({where}).")
            return
        if os.environ.get("GITLAB_TOKEN"):
            self.token = os.environ["GITLAB_TOKEN"].strip()
            self.token_source = "$GITLAB_TOKEN"
            self.step.info(f"{why[0].upper()}{why[1:]}; no token file is recorded, so using $GITLAB_TOKEN." if mint
                           else "No token file is recorded; using $GITLAB_TOKEN.")
            return
        self.step.die(CREDENTIALS, "No GitLab API token" + (f": {why}, and none is recorded." if mint else " is recorded.")
                      + "\n" + token_help())

    def revoke(self):
        if self.token and self.minted:
            self.session.request("DELETE", f"{GITLAB_API}/personal_access_tokens/self",
                                 headers=[f"Authorization: Bearer {self.token}"])
            self.token = ""
            self.minted = False

    def call(self, method, path, body=None, raw=False):
        """(status, parsed body) of a REST call (raw=True: the body as text, e.g. a job log). Status "000" when curl itself failed. Never dies:
        the Midway cookie gets the request past the SSO front door, the token identifies you (without
        it GitLab answers 404 for anything private)."""
        # The token as `Authorization: Bearer`, which curl drops when a redirect leaves the host:
        # a PRIVATE-TOKEN header went on to the sign-in hosts with every redirect (review 10b C10).
        # The token and the body (variable values) go to curl through pipes, never its command line.
        headers = [f"Authorization: Bearer {self.token}"]
        if body is not None:
            headers.append("Content-Type: application/json")
        status, final, text, _ = self.session.request(method, f"{GITLAB_API}{path}", headers=headers,
                                                      body=None if body is None else json.dumps(body))
        # Redirected off GitLab (to Midway or Federate sign-in): the session lapsed. The sign-in
        # page answers 200 with HTML, which must never read as "no MR" or "no project".
        host = re.sub(r"^https?://([^/:]+).*", r"\1", final) if final else GITLAB_HOST
        lapsed = status != "000" and host != GITLAB_HOST
        if lapsed:
            status = "401"
        if raw:
            parsed = text
        elif status == "000":
            # curl failed (DNS, TLS, a timeout): say how, not just "000" (review 10d B13).
            parsed = {"message": self.session.error or "curl failed"}
        else:
            try:
                parsed = json.loads(text) if text.strip() else {}
            except ValueError:
                parsed = {"raw": text[:500]}
                if status.startswith("2"):
                    # A 2xx that isn't JSON is a sign-in or error page, not an API answer.
                    lapsed = True
                    status, parsed = "401", {"message": "GitLab answered with a page, not JSON: the Midway session lapsed", "raw": text[:200]}
        self.last_lapsed = lapsed   # a 401 that is Midway's, not the token's (check_gitlab_token, 10f C5)
        if self.lapse_fatal and lapsed:
            self.step.die(CREDENTIALS, f"The Midway session lapsed during {method} {path}. Run: {MWINIT_HINT}   then re-run "
                          "(the steps done so far are kept).")
        if self.lapse_fatal and status == "401":
            self.step.die(CREDENTIALS, f"GitLab refused the token ({self.token_source or 'none'}) for {method} {path} (401): it is "
                          "expired or revoked. Replace it, then re-run.\n" + token_help())
        return status, parsed

    def manage(self, method, path, body=None):
        """The GitLab Management Portal API (manage.gitlab.aws.dev), Amazon's front for actions
        GitLab's own API refuses here (create a project with a Bindle). Behind Midway via ALB OIDC:
        the cookie authenticates it. A GET first, through the Midway redirect, gets the portal's ALB
        session cookie: a POST without one is redirected to sign-in and answered 405. In memory,
        like every Midway session (MidwaySession)."""
        portal = MidwaySession(f"{MANAGE_API}/api/public/gitlab/groups")
        primed = portal.prime()
        status, _, text, _ = portal.request(method, f"{MANAGE_API}{path}", follow=False,
                                            headers=["Content-Type: application/json"] if body is not None else None,
                                            body=None if body is None else json.dumps(body))
        # A prime that never reached the portal, or a redirect (to sign-in) instead of an answer, is
        # the Midway session, not the request: 11, not an empty "failed (302)" (review 10c C3/SU-14).
        if self.lapse_fatal and (not primed.startswith(MANAGE_API) or status.startswith("3")):
            self.step.die(CREDENTIALS, f"The GitLab portal sent this to sign-in ({status}): the Midway session lapsed. Run: "
                          f"{MWINIT_HINT}   then re-run (the steps done so far are kept).")
        return status, text


def urlenc(s):
    from urllib.parse import quote
    return quote(s, safe="")


# ---- GitHub --------------------------------------------------------------------------------------

def gh(*args, input=None):
    return run(["gh"] + list(args), input=input)


def require_gh(step):
    if gh("auth", "status", "-h", "github.com").returncode != 0:
        step.die(CREDENTIALS, "The GitHub CLI isn't logged in. Run: gh auth login   then re-run.")


def gh_fail(step, code, what, output, owner=""):
    """Dies, with exit 11 when the problem is the login, not the request."""
    org = owner or "the organization"
    if "SAML" in output:
        step.die(CREDENTIALS, f"GitHub refused {what}: {org} uses SAML single sign-on and your gh login isn't authorized for it. "
                 f"Run: gh auth refresh -h github.com   and authorize {org} when GitHub asks (or "
                 f"https://github.com/settings/applications → GitHub CLI → Grant for {org}), then re-run. ({last_line(output)})")
    if "HTTP 401" in output or "Bad credentials" in output:
        step.die(CREDENTIALS, f"GitHub rejected your gh login ({what}: {last_line(output)}). Run: gh auth login   then re-run.")
    step.die(code, f"{what} failed: {last_line(output)}")


# Where GitHub sends the browser after the builder installs the App or changes which repos it
# covers (the App's Setup URL with "Redirect on update"; github_app.py puts both in the manifest).
# A fixed port, because the Setup URL is one fixed address on the App. Only a hint that the click
# happened: repo_github.py still checks the install as the App.
try:
    APP_SETUP_PORT = int(os.environ.get("CRE_APP_SETUP_PORT") or 47391)
except ValueError:   # a bad value must not break every script that imports this module
    APP_SETUP_PORT = 47391
APP_SETUP_URL = f"http://127.0.0.1:{APP_SETUP_PORT}/installed"


def github_api():
    return os.environ.get("CRE_GITHUB_API", "https://api.github.com")


def github_web():
    return os.environ.get("CRE_GITHUB_WEB", "https://github.com")


PROJEN_PERMISSIONS = ("contents", "pull_requests", "workflows")


def missing_permissions(perms):
    """The repository permissions projen's workflows need (write) missing from a permissions map."""
    perms = perms or {}
    return [k for k in PROJEN_PERMISSIONS if perms.get(k) != "write"]


class App:
    """Calls to GitHub as the App itself: a JWT signed with its private key (openssl, RS256)."""

    def __init__(self, step, slug, app_id, key_file):
        self.step, self.slug, self.id, self.key_file = step, slug, str(app_id), key_file

    def jwt(self):
        """GitHub accepts at most 10 minutes; issued 60 s in the past and valid 5 minutes, so a clock
        up to a minute off either way works."""
        def b64(b):
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        now = int(time.time())
        head = b64(b'{"alg":"RS256","typ":"JWT"}')
        body = b64(json.dumps({"iat": now - 60, "exp": now + 300, "iss": self.id}, separators=(",", ":")).encode())
        r = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key_file], input=f"{head}.{body}".encode(),
                           capture_output=True)
        if r.returncode != 0 or not r.stdout:
            self.step.die(REFUSED, f"Can't sign with {self.key_file}: it isn't an RSA private key. Use the .pem GitHub generated for {self.slug}.")
        return f"{head}.{body}.{b64(r.stdout)}"

    def get(self, path, method="GET"):
        """(status, body dict). Status "000" when GitHub couldn't be reached. The answer stays in memory."""
        code, out, _, _ = curl(["-sS", "-X", method, "-o", "-", "-w", HTTP_MARK + "%{http_code}", "-H", "@<<hdr>>",
                                "-H", "Accept: application/vnd.github+json", f"{github_api()}{path}"],
                               feeds={"hdr": f"Authorization: Bearer {self.jwt()}\n"})
        raw, _, tail = out.rpartition(HTTP_MARK)
        status = tail.strip() if code == 0 and tail.strip().isdigit() else "000"
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            body = {"message": raw[:200]}
        return status, body if isinstance(body, dict) else {}

    def fail(self, status, path, body):
        msg = body.get("message", "")
        if status == "401":
            self.step.die(REFUSED, f"GitHub rejected a token signed with {self.key_file} for App {self.slug} (ID {self.id}): {msg}. "
                          "The key isn't one of this App's (rotated, or another App's), or this machine's clock is off. "
                          f"Make a new key: {github_web()}/settings/apps/{self.slug} → Private keys → Generate a private key, "
                          f"and save it as {self.key_file} (github_app.py says how).")
        if status == "000":
            self.step.die(FAILED, f"Could not reach GitHub's API ({github_api()}{path}). Check the network, then re-run.")
        self.step.die(FAILED, f"GitHub answered {status} to {path}: {msg}")

    def installation_token(self, installation_id):
        """A short-lived token for the App's installation (an hour): to act as the App, never the
        builder (an approval of the builder's own PR, which GitHub refuses from its author)."""
        path = f"/app/installations/{installation_id}/access_tokens"
        status, body = self.get(path, method="POST")
        if status != "201" or not body.get("token"):
            self.fail(status, path, body)
        return body["token"]

    def check_key(self):
        """The key is this App's: GitHub accepts a token it signed (GET /app) and names this App."""
        status, body = self.get("/app")
        if status != "200":
            self.fail(status, "/app", body)
        if str(body.get("id")) != self.id:
            self.step.die(REFUSED, f"The key at {self.key_file} signs for App ID {body.get('id')}, not {self.slug} ({self.id}). Use {self.slug}'s key.")
        return body


# ---- secrets and variables -----------------------------------------------------------------------

def _reserved(step, key, flag):
    if key in RESERVED_VARS:
        step.die(REFUSED, f"{key} is a reserved name: the deploy sets it for every stack job, so a {flag} would override it.")
    if key.startswith(RESERVED_PREFIX):
        step.die(REFUSED, f"{key} is a reserved name: STACK_* variables carry earlier stacks' outputs.")


def value_from(step, flag, key, value):
    """A value given as `@<file>` is read from that file (one trailing newline dropped), so a secret
    needn't be on the command line, in `ps` and the shell history (review 10b C20). value None
    (`--{flag} KEY` with no `=`): from the environment variable KEY."""
    if value is None:
        if key not in os.environ:
            step.die(REFUSED, f"--{flag} {key}: no `=VALUE`, and no {key} environment variable to take it from.")
        return os.environ[key]
    if value.startswith("@"):
        path = expand_path(value[1:])
        try:
            with open(path) as f:
                text = f.read()
        except OSError as e:
            step.die(REFUSED, f"--{flag} {key}=@{value[1:]}: can't read {path} ({e.strerror}).")
        return text[:-1] if text.endswith("\n") else text
    return value


def parse_var(step, spec):
    """A GitLab `--var KEY=VALUE[:protected][:masked]` → (key, value, protected, masked). Masked
    (hidden in job logs) is for a secret a `noEcho` parameter takes; GitLab requires such a value to
    be at least 8 characters with no spaces."""
    protected = masked = False
    body = spec
    while True:
        if body.endswith(":protected"):
            protected, body = True, body[: -len(":protected")]
        elif body.endswith(":masked"):
            masked, body = True, body[: -len(":masked")]
        else:
            break
    if "=" not in body:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", body):
            step.die(REFUSED, f"--var needs KEY=VALUE[:protected][:masked], got '{spec.split('=')[0]}'")
        key, value = body, None
    else:
        key, value = body.split("=", 1)
    _reserved(step, key, "variable")
    return key, value_from(step, "var", key, value), protected, masked


def record_deploy_env(project_dir, pairs):
    """Write KEY=VALUE lines into <project>/.cre/deploy.env (gitignored, 0600), which the local
    `npx projen deploy` reads for the variables CI gets from its CI/CD variables. Existing keys are
    updated, others kept. Never pass a masked/secret value here."""
    if not project_dir or not pairs:
        return
    path = os.path.join(project_dir, ".cre", "deploy.env")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines, seen = [], set()
    if os.path.isfile(path):
        with open(path) as f:
            for line in f.read().splitlines():
                k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
                if k in pairs:
                    line = f"{k}={pairs[k]}"
                    seen.add(k)
                lines.append(line)
    else:
        lines.append("# Deploy variables for the local `npx projen deploy` (CI has them as CI/CD variables). Written by setup.")
    lines += [f"{k}={v}" for k, v in pairs.items() if k not in seen]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")


def record_ci_variables(sf, names, secret_names=()):
    """Add variable names (never values) to cre-setup.json's ciVariables / secretVariables, which
    check:deploy-prereqs compares with what the stacks need."""
    for key, new in (("ciVariables", names), ("secretVariables", secret_names)):
        if not new:
            continue
        cur = sf.data.get(key) or []
        merged = sorted(set(cur) | set(new))
        if merged != cur:
            sf.set(key, merged)


def parse_secret(step, spec):
    """A GitHub `--secret KEY=VALUE` → (key, value). Taken whole (no `:protected`: GitHub has no
    such flag); set later on stdin, never argv."""
    key, value = spec.split("=", 1) if "=" in spec else (spec, None)
    if value is None and spec not in os.environ:
        step.die(REFUSED, f"--secret needs KEY=VALUE (or KEY=@file, or KEY with the value in the environment), got '{spec}'")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        step.die(REFUSED, f"--secret name '{key}' must be letters, digits and _ (not starting with a digit).")
    if key.upper().startswith("GITHUB_"):
        step.die(REFUSED, f"--secret {key}: GitHub refuses secret names starting with GITHUB_.")
    _reserved(step, key, "secret")
    # GitLab's suffixes would become part of the value here (a role ARN ending in :protected).
    if value and re.search(r":(protected|masked)$", value):
        step.die(REFUSED, f"{key}: GitHub has no :protected / :masked. Use --secret {key}=<value> for a role ARN or "
                 f"a hidden value, --var {key}=<value> for a plain one.")
    return key, value_from(step, "secret", key, value)


# ---- repo bootstrap (both providers) -------------------------------------------------------------

def git(*args, cwd=None, env=None):
    return run(["git"] + list(args), cwd=cwd, env=env)


def require_git_identity(step, cwd=None):
    name = git("config", "user.name", cwd=cwd).stdout.strip()
    email = git("config", "user.email", cwd=cwd).stdout.strip()
    if not name or not email:
        step.die(REFUSED, 'git has no identity for the first commit. Run: git config --global user.name "<Your Name>" && '
                 "git config --global user.email <alias>@amazon.com   then re-run.")


def preflight_git(step, cwd):
    """Before the first commit and push: a git identity, and origin reachable without a prompt (an
    agent's shell has no terminal, so git can't ask for a password or a host key; it just fails)."""
    require_git_identity(step, cwd)
    url = git("config", "--get", "remote.origin.url", cwd=cwd).stdout.strip()
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")}
    r = git("ls-remote", "--heads", "origin", cwd=cwd, env=env)
    if r.returncode == 0:
        return
    why = last_line(r.stderr or r.stdout)
    if url.startswith("https://github.com/"):
        step.die(CREDENTIALS, f"git can't authenticate to {url} without a prompt ({why}). Let gh supply your credentials: "
                 "gh auth setup-git   then re-run.")
    if url.startswith("git@github.com:"):
        step.die(CREDENTIALS, f"git can't reach {url} over SSH ({why}). Add your SSH key to GitHub (gh ssh-key add ~/.ssh/id_ed25519.pub), "
                 "accept github.com's host key once (ssh -o StrictHostKeyChecking=accept-new -T git@github.com), or switch gh to "
                 "HTTPS (gh config set git_protocol https, then gh auth setup-git); then re-run.")
    if "gitlab.aws.dev" in url:
        step.die(CREDENTIALS, f"git can't reach {url} ({why}). Refresh your Midway SSH certificate: {MWINIT_HINT}   then re-run.")
    step.die(CREDENTIALS, f"git can't reach {url} without a prompt: {why}")


# The files a repo made in a provider's UI starts with ("Initialize with a README"): such a repo is
# as good as empty.
TRIVIAL_FILE = re.compile(r"(README[^/]*|LICENSE[^/]*|\.gitignore)")
# The scaffold's first commit (finalize --message's default). Earlier templates' first commits match
# adopt.TEMPLATE_SCAFFOLD_COMMIT (test_adopt keeps them in step).
SCAFFOLD_MESSAGE = "chore: initial scaffold"


def owned_clone(cwd, repo):
    """This machine pushed the scaffold to <repo> (owner/name or group/project): cwd is a clone of it
    whose main carries setup's record of the first push. The evidence that an existing repo with
    history is this project's own, not a colleague's of the same name."""
    if not cwd or not os.path.isdir(os.path.join(cwd, ".git")):
        return False
    url = git("config", "--get", "remote.origin.url", cwd=cwd).stdout.strip()
    pushed = git("config", "branch.main.crePushedSha", cwd=cwd).stdout.strip()
    return bool(pushed) and re.search(r"[:/]" + re.escape(repo) + r"(\.git)?/?$", url) is not None


def refuse_foreign(step, where, kind, who, cwd, repo, summary=""):
    """Before anything is written into an existing repo: go on when it is empty, a UI placeholder,
    provably this project's own, or the builder decided to adopt it (adoptExistingRepo for `repo`:
    work they started before the template). A same-named repo with someone else's history would
    otherwise get this builder's role ARNs (and App key), and the runner role would trust it.
    `summary`: what the history holds, shown with the decision. Prints REPO_STATE (empty | trivial |
    history | own): new_project.py adopts any repo that has a commit (adopt.py)."""
    own = kind == "history" and owned_clone(cwd, repo)
    step.out("REPO_STATE", "own" if own else kind)
    if kind in ("empty", "trivial") or own:
        step.check("adoptExistingRepo", repo, "not-needed", f"{where} " + (
            "is empty: the scaffold is its first commit" if kind == "empty" else
            "holds only a UI \"initialize\" commit (a README, LICENSE or .gitignore): the scaffold goes in through an MR/PR" if kind == "trivial" else
            "was set up from this clone before: nothing to decide"))
        return
    # Refused (10) unless made; discovery records it, a dry run says it is needed.
    step.decide("adoptExistingRepo", repo, f"{where} already has history{f' (last commit by {who})' if who else ''} that this "
                "setup didn't make. Adopt it only if it is the builder's own, not someone else's (their CI variables and role ARNs go into it, and "
                "the scaffold replaces its template files through an MR/PR); otherwise pick another project name", shown=summary)


def ci_variables_check(step, where, subject, names, why=""):
    """Discovery: the repo's own CI/CD variables (an adopted repo may have some), for the final
    safety check. A project variable outranks a pipeline job's value of the same name, so a reserved
    name is pointed out (review 10h decision 4); nothing is refused. `why`: they couldn't be read."""
    if why:
        step.check("ciVariables", subject, "in-place", f"{where}'s CI/CD variables couldn't be read ({why}), so none is shown")
        return
    if not names:
        return
    clash = sorted(n for n in names if n in RESERVED_VARS or n.startswith(RESERVED_PREFIX))
    shown = ", ".join(sorted(names)[:30]) + (" ..." if len(names) > 30 else "")
    step.check("ciVariables", subject, "in-place", f"{where} already has CI/CD variables: {shown}" + (
        f". The pipeline sets {', '.join(clash)} itself for every stack job, and the project's own value wins: delete "
        f"{'it' if len(clash) == 1 else 'them'} unless that is intended" if clash else ". None is a name the pipeline sets"))


def remote_state(cwd):
    """origin/main after a fetch: empty | history | unknown."""
    heads = git("ls-remote", "--heads", "origin", cwd=cwd)
    if heads.returncode == 0 and not heads.stdout.strip():
        return "empty"
    if git("fetch", "-q", "origin", cwd=cwd).returncode != 0:
        return "unknown"
    return "history"


# The branch an adopted repo's scaffold goes in through (adopt.py), always through an MR/PR.
ADOPT_BRANCH = "cre/adopt-template"


def history_summary(more, who, files):
    """What an existing repo holds, for the adopt decision: its top-level entries, its last author."""
    top = sorted({f.split("/")[0] + ("/" if "/" in f else "") for f in files})
    return (f"Its main{' (several commits)' if more else ' (one commit)'}, last by {who or 'unknown'}, holds: "
            + (", ".join(top[:30]) + (" ..." if len(top) > 30 else "") if top else "(no files read)"))


def bootstrap_push(step, cwd, message=SCAFFOLD_MESSAGE):
    """Commit the scaffold (if not yet) and push main as the first commit of an EMPTY repo. A repo
    with any commit is adopted by new_project.py instead (adopt.py: through an MR/PR), so one that
    has commits here is refused, unless it is this scaffold's own push (a re-run after the push
    landed but before it was recorded). Until the first push lands, anything changed since the
    scaffold commit (a re-run after a failed push, a fix made in between) is added to it."""
    preflight_git(step, cwd)
    if step.dry_run:
        step.plan("(dry run) commit the scaffold and push main as the first commit")
        return
    has_head = git("rev-parse", "-q", "--verify", "HEAD", cwd=cwd).returncode == 0
    dirty = bool(git("status", "--porcelain", cwd=cwd).stdout.strip())

    def commit(*args):
        r = git("commit", "-q", *args, cwd=cwd)
        if r.returncode != 0:
            sys.stderr.write(r.stdout + r.stderr)
        return r.returncode == 0

    if not has_head:
        git("add", "-A", cwd=cwd)
        if not commit("-m", message):
            step.die(FAILED, "The initial commit failed (git's output above: a commit hook, or commit signing).")
        step.ok(f"Committed the scaffold: {message}")
    elif dirty:
        git("add", "-A", cwd=cwd)
        if not commit("--amend", "--no-edit"):
            step.die(FAILED, "Adding the latest changes to the scaffold commit failed (git's output above).")
        step.ok("Added the changes made since the scaffold commit to it (nothing pushed yet).")
    state = remote_state(cwd)
    if state == "unknown":
        step.die(FAILED, "Could not fetch origin to inspect it.")
    if state == "history" and git("merge-base", "--is-ancestor", "origin/main", "HEAD", cwd=cwd).returncode != 0:
        step.die(REFUSED, "origin already has commits, and setup:repo finalize pushes only into an empty repo. Nothing was pushed. "
                 "A repo with commits is adopted by new_project.py (/cre-demo-init), through an MR/PR.")
    step.plan("git push -u origin main")
    r = git("push", "-q", "-u", "origin", "main", cwd=cwd, env={"GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        step.die(FAILED, "git push -u origin main failed (see above). Fix the cause and re-run: anything changed meanwhile is added before the push.")
    git("config", "branch.main.crePushedSha", git("rev-parse", "HEAD", cwd=cwd).stdout.strip(), cwd=cwd)


def push_adopt_branch(step, cwd, last):
    """Push ADOPT_BRANCH to origin, never over a commit setup didn't push: origin's branch must be
    absent, what setup pushed last (`last`), or already in the local branch (the builder pulled it).
    A lease on what was seen, so a push in between is refused too. Returns the local branch's sha."""
    local = git("rev-parse", "-q", "--verify", f"refs/heads/{ADOPT_BRANCH}", cwd=cwd).stdout.strip()
    if not local:
        step.die(FAILED, f"{cwd} has no branch {ADOPT_BRANCH} to push.")
    ls = git("ls-remote", "origin", f"refs/heads/{ADOPT_BRANCH}", cwd=cwd, env={"GIT_TERMINAL_PROMPT": "0"})
    if ls.returncode != 0:
        step.die(FAILED, f"Could not read origin's {ADOPT_BRANCH}: {last_line(ls.stderr)}")
    remote = ls.stdout.split()[0] if ls.stdout.strip() else ""
    if remote == local:
        return local
    if remote and remote != last:
        git("fetch", "-q", "origin", f"+refs/heads/{ADOPT_BRANCH}:refs/remotes/origin/{ADOPT_BRANCH}", cwd=cwd)
        if git("merge-base", "--is-ancestor", remote, local, cwd=cwd).returncode != 0:
            theirs = git("log", "--format=%h %s", f"{local}..{remote}", cwd=cwd).stdout.strip().splitlines()
            step.die(REFUSED, f"origin's {ADOPT_BRANCH} has commits setup didn't push: {'; '.join(theirs[:10]) or remote[:9]}. "
                     f"Nothing was pushed. Bring them into {cwd} (git switch {ADOPT_BRANCH} && git pull --no-rebase origin "
                     f"{ADOPT_BRANCH}), then re-run; or, if they aren't wanted, delete that branch on origin and re-run.")
    step.plan(f"git push origin {ADOPT_BRANCH} (lease: {remote[:9] or 'absent'})")
    r = git("push", "-q", f"--force-with-lease=refs/heads/{ADOPT_BRANCH}:{remote}", "origin",
            f"refs/heads/{ADOPT_BRANCH}:refs/heads/{ADOPT_BRANCH}", cwd=cwd, env={"GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        step.die(FAILED, f"Pushing {ADOPT_BRANCH} failed: {last_line(r.stderr)}. Re-run (a push that landed meanwhile is checked again).")
    return local


IN_PROGRESS_PATHS = ("rebase-apply/applying", "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
                     "REVERT_HEAD", "BISECT_LOG", "sequencer", "sequencer/todo")


def in_progress_op(cwd=None, run_git=None):
    """The git operation in progress in cwd (am, rebase, merge, cherry-pick, revert, bisect), "" when
    none. run_git: the git runner (the git tasks pass theirs, which holds signals)."""
    run_git = run_git or (lambda *args: git(*args, cwd=cwd))
    # Every path in one `rev-parse` (a worktree keeps some in its own git dir): a process start costs
    # tens of milliseconds here, and this ran nine.
    where = dict(zip(IN_PROGRESS_PATHS, run_git("rev-parse", *[a for p in IN_PROGRESS_PATHS for a in ("--git-path", p)]).stdout.splitlines()))

    def exists(p):
        path = where.get(p, "")
        return bool(path) and os.path.exists(path if os.path.isabs(path) or not cwd else os.path.join(cwd, path))
    if exists("rebase-apply/applying"):
        # `git am` shares rebase-apply/ with an apply-style rebase, but `git rebase --abort`
        # refuses it (review 10d B13).
        return "am"
    if exists("rebase-merge") or exists("rebase-apply"):
        return "rebase"
    for path, op in (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
        if exists(path):
            return op
    # A cherry-pick or revert of several commits whose stop the user committed past (`git commit`
    # instead of `--continue`): no *_HEAD left, but git's sequencer still holds the rest, and git
    # counts it as in progress (it refuses a switch). Its todo's first verb says which (review 10g B7).
    if exists("sequencer"):
        todo = where.get("sequencer/todo", "")
        try:
            with open(todo if os.path.isabs(todo) or not cwd else os.path.join(cwd, todo)) as f:
                first = next((ln.split()[0] for ln in f if ln.strip() and not ln.startswith("#")), "")
        except OSError:
            first = ""
        return "revert" if first == "revert" else "cherry-pick"
    return ""


def pushed_before(cwd):
    """This clone recorded the scaffold's first push (crePushedSha), whatever origin says now: the
    record that tells "the repo was deleted since" from "not made yet" (review 10f C1)."""
    return bool(cwd) and os.path.isdir(os.path.join(cwd, ".git")) and \
        bool(git("config", "branch.main.crePushedSha", cwd=cwd).stdout.strip())


def first_push_done(cwd):
    """The scaffold's first push landed (setup recorded it, and origin has a main): a re-run only
    re-checks the repo's settings. Re-running the first push would refuse origin's own history, and
    from a feature branch refuse to run at all (review 10b C2)."""
    if not git("config", "branch.main.crePushedSha", cwd=cwd).stdout.strip():
        return False
    r = git("ls-remote", "--heads", "origin", "main", cwd=cwd, env={"GIT_TERMINAL_PROMPT": "0"})
    return r.returncode == 0 and bool(r.stdout.strip())


def set_origin(step, cwd, url):
    if git("config", "--get", "remote.origin.url", cwd=cwd).stdout.strip() != url:
        git("remote", "remove", "origin", cwd=cwd)
        git("remote", "add", "origin", url, cwd=cwd)
        step.ok(f"origin → {url}")


def require_main(step, cwd):
    branch = git("branch", "--show-current", cwd=cwd).stdout.strip()
    if branch != "main":
        step.die(REFUSED, f"The scaffold must be on branch main for the first push (it is on '{branch}').")
