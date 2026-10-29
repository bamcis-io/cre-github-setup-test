"""Run one subprocess with a time limit. The only place setup starts a process.

Never raises for a command's own failure: the caller reads `code`. A command that runs past its
limit is killed and reported as code 124 with "timed out after Ns" in `err`, so a hung network
call can't stall an unattended run.
"""
import dataclasses
import subprocess

TIMEOUT = 124


@dataclasses.dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self):
        return self.code == 0


def run(cmd, timeout, env=None, cwd=None, input=None):
    """`cmd` is a list (never a shell string). stdin is `input`, or closed, so a command that would
    prompt fails at once instead of waiting unseen."""
    stdin = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd, **stdin)
    except subprocess.TimeoutExpired:
        return Result(TIMEOUT, "", f"{cmd[0]} timed out after {timeout}s")
    except OSError as e:
        return Result(127, "", f"{cmd[0]}: {e.strerror or e}")
    return Result(p.returncode, p.stdout, p.stderr)


@dataclasses.dataclass(frozen=True)
class BytesResult:
    code: int
    out: bytes
    err: str

    @property
    def ok(self):
        return self.code == 0


def run_bytes(cmd, timeout, input):
    """run() for a command whose stdout is binary (a signature)."""
    try:
        p = subprocess.run(cmd, input=input, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return BytesResult(TIMEOUT, b"", f"{cmd[0]} timed out after {timeout}s")
    except OSError as e:
        return BytesResult(127, b"", f"{cmd[0]}: {e.strerror or e}")
    return BytesResult(p.returncode, p.stdout, p.stderr.decode(errors="replace"))
