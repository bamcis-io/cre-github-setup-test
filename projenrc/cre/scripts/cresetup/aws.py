"""The aws CLI, wrapped: JSON in and out, and every failure sorted into one kind.

`Aws.call("cloudformation", "describe-stacks", ...)` returns a `Reply`:
  ok        the call succeeded; `data` is its parsed JSON (None when it printed nothing)
  creds     the credentials are missing or expired
  failed    anything else, including a timeout; `message` is the CLI's last line, where AWS's error
            code (e.g. "StackSetNotFoundException") is for a caller that needs it
"""
import dataclasses
import json
import os
import re

import proc

CALL_TIMEOUT = 60

CREDS = re.compile(r"\((ExpiredToken|ExpiredTokenException|InvalidClientTokenId|InvalidAccessKeyId|SignatureDoesNotMatch"
                   r"|UnrecognizedClientException|RequestExpired)\)|security token included in the request is expired"
                   r"|Error when retrieving credentials|SSO session .*expired|Unable to locate credentials"
                   r"|Token has expired and refresh failed|The config profile \(.*\) could not be found")


@dataclasses.dataclass(frozen=True)
class Reply:
    kind: str            # ok | creds | failed
    data: object = None
    message: str = ""

    @property
    def ok(self):
        return self.kind == "ok"


def classify(code, out, err):
    if code == 0:
        try:
            return Reply("ok", json.loads(out) if out.strip() else None)
        except ValueError:
            return Reply("ok", out.strip())
    text = (err or out or "").strip()
    last = text.splitlines()[-1] if text else f"exit {code}"
    if CREDS.search(text):
        return Reply("creds", message=last)
    return Reply("failed", message=last)


class Aws:
    """One profile. Every call runs `aws --output json`; `region` per call (IAM has none)."""

    def __init__(self, profile, timeout=CALL_TIMEOUT, runner=proc.run):
        self.profile = profile
        self.timeout = timeout
        self._run = runner

    def call(self, service, operation, *args, region=None, timeout=None):
        cmd = ["aws", service, operation, *args, "--output", "json"]
        if self.profile:
            cmd += ["--profile", self.profile]
        if region:
            cmd += ["--region", region]
        env = dict(os.environ, AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off")
        r = self._run(cmd, timeout or self.timeout, env=env)
        return classify(r.code, r.out, r.err)

