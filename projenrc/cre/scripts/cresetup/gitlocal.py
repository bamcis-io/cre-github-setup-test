"""git in a project's clone, and the record of what setup did there (.git/cre-setup.json).

  git(d, "status", ...)        -> proc.Result; raises Failed unless ok=False
  load(d) / save(d, state)     the record clone.py, scaffold.py and build.py share

git runs with no terminal: ssh in batch mode (a host-key or passphrase prompt fails at once instead
of waiting out the timeout), and no credential prompt. HTTPS to github.com goes through gh's login.
A refused login, over ssh or HTTPS, is credentials (11).
"""
import json
import os
import re

import proc
from result import CREDS, FAILED, REFUSED, Failed

GIT_TIMEOUT = 300
MWINIT = "mwinit -f -k ~/.ssh/id_ecdsa.pub"
SSH_REFUSED = re.compile(r"Permission denied \(publickey|Host key verification failed|no such identity")
NO_IDENTITY = re.compile(r"Please tell me who you are|unable to auto-detect email address")
HTTPS_REFUSED = re.compile(r"Authentication failed|could not read Username|terminal prompts disabled|HTTP Basic: Access denied")
# GitHub over HTTPS through gh's login (what credential_check.py checks), whatever else git is configured with.
ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes",
       "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "credential.https://github.com.helper", "GIT_CONFIG_VALUE_0": "",
       "GIT_CONFIG_KEY_1": "credential.https://github.com.helper", "GIT_CONFIG_VALUE_1": "!gh auth git-credential"}


def git(d, *args, ok=True, what=None, cwd=None):
    """git -C d <args>. ok=True raises Failed on a non-zero exit, with git's own message."""
    r = proc.run(["git", *(["-C", d] if d else []), *args], timeout=GIT_TIMEOUT, env={**os.environ, **ENV}, cwd=cwd)
    if ok and not r.ok:
        fail(r, what or f"git {args[0]}")
    return r


def fail(r, what):
    """Raise the Failed a failed git command means."""
    text = (r.err or r.out).strip()
    if SSH_REFUSED.search(text):
        raise Failed(CREDS, f"{what}: ssh refused ({text.splitlines()[-1]}). Run: {MWINIT}")
    if HTTPS_REFUSED.search(text):
        raise Failed(CREDS, f"{what}: the git host refused the login ({text.splitlines()[-1]}). Run: gh auth login -s repo,workflow")
    if NO_IDENTITY.search(text):
        raise Failed(REFUSED, f"{what}: git has no name or email for you: git config --global user.name / user.email, "
                              "then re-run.")
    raise Failed(FAILED, f"{what}: {text[-1500:]}")


def state_path(d):
    return os.path.join(d, ".git", "cre-setup.json")


def load(d):
    try:
        with open(state_path(d)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(d, state):
    tmp = state_path(d) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_path(d))


def started(d, step):
    """The record, for a step that runs after clone.py start: refused (10) when start didn't run here,
    or the clone is now on another branch than the one start picked."""
    state = load(d) if os.path.isdir(os.path.join(d, ".git")) else {}
    if not state.get("branch"):
        raise Failed(REFUSED, f"{d} wasn't started: run clone.py start first.")
    current = git(d, "branch", "--show-current").out.strip()
    if current != state["branch"] and not state.get("pushed"):
        raise Failed(REFUSED, f"{d} is on {current or 'a detached HEAD'}, not {state['branch']}: switch back to "
                              f"{state['branch']}, then re-run {step}.")
    return state
