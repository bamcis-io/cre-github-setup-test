#!/usr/bin/env python3
"""deploy:stack — deploy one CloudFormation stack from the template the `deploy:s3` task uploaded.

Always --template-url, so the 51200-byte local template body limit never applies. `aws cloudformation
deploy` has no --template-url, so this drives change sets: create-change-set → wait → execute →
poll until the stack transitions → bounded wait for the end. An empty change set is a success.

Required env:
  AWS_REGION          the stack's region; its assets bucket is in the same region
  STACK_NAME          the CloudFormation stack name
  TEMPLATE_NAME       the template's base name (no extension) under the prefix
  ASSETS_BUCKET       the region's assets bucket (filled by the upload)
  ASSETS_PREFIX       the run's key prefix (a trailing / is added)

Optional env:
  STACK_PARAMETERS    the stack's parameters, one `Key=Value` per line (split on the first =; values
                      may hold commas, spaces, backslashes). Written to a JSON parameters file, so the
                      CLI's shorthand parser never sees them. AssetsBucketName / AssetsBucketPrefix
                      are always added.
  OUTPUTS_FILE        the dotenv file for the stack's outputs (default dist/$STACK_NAME.env; GitLab
                      reads it with `artifacts: reports: dotenv:`)
  OUTPUTS_PREFIX      prefix for every output's key (STACK_<ID>), so jobs' dotenvs don't collide
  OUTPUTS             space-separated output names to write (unset: every output; GitLab caps a
                      dotenv's variable count, so jobs pass only the ones later stacks take)
  CAPABILITIES        default CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
  CHANGESET_PREFIX    default ci
  CLEANUP_ON_FAILURE  "true": delete the uploaded prefix when the deploy fails (off: the prefix is
                      shared by every stack of the run)
  ROLE_ARN            CloudFormation's service role (empty: CFN_EXECUTION_ROLE_ARN)
  ASSUME_ROLE         a role to assume first (role chaining, with-role.py), e.g. the DNS account's
  REQUIRED_VARS       space-separated variables that must be non-empty
  REQUIRED_OUTPUTS    space-separated `<VAR>=<stack>/<Output>`: earlier stacks' outputs this one takes
  STACK_WAIT_SECONDS  this job's whole wait on CloudFormation (default 6000, under the 2-hour GitLab
                      deploy job timeout, so a slow stack fails here, with its events, instead of the
                      job being killed). One deadline covers waiting out another run and our own.
  STACK_POLL_SECONDS  default 15
  CRE_CHAINED_WAIT_SECONDS  the wait's cap under chained credentials (CRE_ROLE_CHAINED, which
                      with-role.py sets when it assumed ASSUME_ROLE: they last 1 h). Default 3300,
                      so the wait ends, naming why, before they expire. Tests lower it.

Every stack job sets these from the stack plan (stack-jobs.ts); this script checks them and
deploys. It looks nothing up. Exit 0 deployed (or no changes) | 1 anything else.
"""
import json
import os
import random
import re
import sys
import time

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import crelib  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXPIRED = crelib.CREDENTIALS_LAPSED   # one copy of how the CLI says it (review 10f D1)
THROTTLED = re.compile(r"Throttl|Rate exceeded|RequestLimitExceeded")


class Fail(Exception):
    """Ends the deploy (exit 1); the message is printed, then cleanup runs if asked."""


def err(msg):
    print(msg, file=sys.stderr, flush=True)


def say(msg):
    print(msg, flush=True)


def s3_domain(region):
    # The S3 domain differs by partition (review #11): China, the European Sovereign Cloud. The
    # partition comes from crelib's one prefix table (review 10c G1).
    return {"aws-cn": "amazonaws.com.cn", "aws-eusc": "amazonaws.eu"}.get(crelib.partition_of(region), "amazonaws.com")


def account_of(arn):
    parts = arn.split(":")
    return parts[4] if len(parts) > 4 else ""


def preflight(env):
    """The checks before AWS is called; may re-exec this script under ASSUME_ROLE."""
    # An unset CI secret or variable arrives as an empty string, which would otherwise surface as a
    # CloudFormation parameter error (or a deploy as the wrong role) much later.
    for v in env.get("REQUIRED_VARS", "").split():
        if not env.get(v):
            raise Fail(f"{v} is set but EMPTY - an unset CI secret or variable arrives as an empty string. Set it and re-run.")
    for r in env.get("REQUIRED_OUTPUTS", "").split():
        var, _, what = r.partition("=")
        if var not in env:
            stack, _, output = what.partition("/")
            raise Fail(f"stack {stack} has no output {output} (needed by {env.get('STACK_NAME') or 'this stack'})")
    role_arn = env.get("ROLE_ARN") or env.get("CFN_EXECUTION_ROLE_ARN") or ""
    assume = env.get("ASSUME_ROLE", "")
    # A stack deployed into another account needs that account's execution role: an empty optional
    # executionRole falls back to CFN_EXECUTION_ROLE_ARN, this account's (review N13).
    if assume and role_arn:
        target, owner = account_of(assume), account_of(role_arn)
        if target and owner and target != owner:
            raise Fail(f"{env.get('STACK_NAME') or 'this stack'} deploys into account {target} (as {assume}), but its execution "
                       f"role {role_arn} is account {owner}'s: CloudFormation there can't use it. Set the stack's executionRole "
                       f"variable (e.g. DNS_CFN_EXECUTION_ROLE_ARN) to a role in {target} (npx projen setup:dns makes it).")
    if assume and not env.get("CRE_ROLE_ASSUMED"):
        crelib.exec_replace([sys.executable, os.path.join(HERE, "with-role.py"), assume, sys.executable, os.path.abspath(__file__)],
                            {**os.environ, "CRE_ROLE_ASSUMED": "1", "ROLE_ARN": role_arn})
    for k in ("AWS_REGION", "STACK_NAME", "TEMPLATE_NAME", "ASSETS_BUCKET", "ASSETS_PREFIX"):
        if not env.get(k):
            raise Fail(f"{k} is required")
    return role_arn


class Deploy:
    def __init__(self, env, role_arn):
        self.env = env
        self.region = env["AWS_REGION"]
        self.stack = env["STACK_NAME"]
        self.role_arn = role_arn
        self.prefix = env["ASSETS_PREFIX"].rstrip("/") + "/"
        self.bucket = env["ASSETS_BUCKET"]
        self.poll = float(env.get("STACK_POLL_SECONDS") or 15)
        # One deadline for every wait in this job: two separate 6000 s waits outlived the job timeout
        # and the credentials (review 10b D3).
        wait = float(env.get("STACK_WAIT_SECONDS") or 6000)
        # Under an assumed role (with-role.py: 1 h, role chaining's maximum) the wait ends before the
        # credentials do, so it times out naming why, not as "credentials expired" (review 10c D8).
        # CRE_ROLE_CHAINED: set by with-role.py only when it really assumed the role. CRE_ROLE_ASSUMED
        # only says with-role.py ran (it is the re-exec guard), which is also true when the caller
        # already held the role and its own, longer, credentials are in use (review 10d D9).
        self.chained = bool(env.get("CRE_ROLE_CHAINED"))
        if self.chained:
            wait = min(wait, float(env.get("CRE_CHAINED_WAIT_SECONDS") or 3300))
        self.deadline = time.time() + wait
        self.cleanup = env.get("CLEANUP_ON_FAILURE") == "true"
        self.url = (f"https://{self.bucket}.s3.{self.region}.{s3_domain(self.region)}/{self.prefix}"
                    f"{env['TEMPLATE_NAME']}.json")

    def cfn(self, *args):
        return crelib.run(["aws", "cloudformation", *args[:1], "--region", self.region, *args[1:]])

    def status(self):
        """The stack's status, "" when it doesn't exist. Any other failure (throttling past a few
        retries, expired or missing credentials, a denied call) stops the deploy: read as "absent",
        it would create a stack that exists, or pass with no outputs."""
        tries = 0
        while True:
            r = self.cfn("describe-stacks", "--stack-name", self.stack, "--query", "Stacks[0].StackStatus", "--output", "text")
            out = (r.stdout + r.stderr).strip()
            if r.returncode == 0:
                return r.stdout.strip()
            if "does not exist" in out:
                return ""
            if THROTTLED.search(out):
                tries += 1
                if tries < 5:
                    time.sleep(tries * 5)
                    continue
            if EXPIRED.search(out):
                raise Fail(f"describe-stacks {self.stack} failed: {out}\nThe job's AWS credentials expired. If a stack update was "
                           "under way, CloudFormation carries on regardless: re-run this job and it waits for the stack to settle, "
                           "then deploys.")
            raise Fail(f"describe-stacks {self.stack} failed: {out}")

    def print_failures(self):
        err(f"Failed resources of {self.stack} (latest first):")
        r = self.cfn("describe-stack-events", "--stack-name", self.stack, "--query",
                     "StackEvents[?contains(ResourceStatus, `FAILED`)] | [0:10].[Timestamp,LogicalResourceId,ResourceType,"
                     "ResourceStatus,ResourceStatusReason]", "--output", "text")
        err(r.stdout.rstrip("\n") if r.returncode == 0 else "  (could not read the stack events)")

    def settled(self):
        """Wait while the stack is *_IN_PROGRESS (not REVIEW_IN_PROGRESS), until the job's deadline."""
        while True:
            st = self.status()
            if st in ("", "REVIEW_IN_PROGRESS") or not st.endswith("_IN_PROGRESS"):
                return st
            if time.time() >= self.deadline:
                why = (" (capped at the assumed role's 1-hour session: role chaining allows no longer)" if self.chained else "")
                raise Fail(f"Stack {self.stack} still {st} after waiting{why}; giving up. Re-run this job: it waits for the stack "
                           "to settle, then deploys.")
            time.sleep(self.poll)

    def remove_prefix(self):
        if self.cleanup:
            say(f"Cleaning up s3://{self.bucket}/{self.prefix} after failure")
            crelib.stream(["aws", "s3", "rm", f"s3://{self.bucket}/{self.prefix}", "--recursive"])

    def params_file(self):
        """Caller-supplied pairs first, then AssetsBucket* (package.py rewrote nested templates to
        require them). Real newlines from every caller, so a backslash in a value stays one."""
        params = []
        lines = (self.env.get("STACK_PARAMETERS") or "").split("\n") + [f"AssetsBucketName={self.bucket}",
                                                                          f"AssetsBucketPrefix={self.prefix}"]
        for line in lines:
            if not line.strip():
                continue
            if "=" not in line:
                raise Fail(f"Malformed STACK_PARAMETERS line (missing =): {line!r}")
            key, _, value = line.partition("=")
            params.append({"ParameterKey": key.strip(), "ParameterValue": value})
        path = os.path.join("dist", f"{self.stack}.params.json")
        os.makedirs("dist", exist_ok=True)
        with open(path, "w") as f:
            json.dump(params, f)
        return path

    def run(self):
        initial = self.status()
        # Still running from another run (a retried or timed-out job, a concurrent pipeline): a change
        # set can't be created until it settles, so wait for it, then deploy from where it ended.
        if initial != "REVIEW_IN_PROGRESS" and initial.endswith("_IN_PROGRESS"):
            say(f"Stack {self.stack} is {initial} (another deploy, or one that was interrupted); waiting for it to finish.")
            initial = self.settled()
            say(f"Stack {self.stack} is now {initial or '<gone>'}.")
        if initial == "UPDATE_ROLLBACK_FAILED":
            err(f"Stack {self.stack} is UPDATE_ROLLBACK_FAILED: CloudFormation refuses any update until the rollback is continued. "
                f"Fix the resource named below, then: aws cloudformation continue-update-rollback --region {self.region} "
                f"--stack-name {self.stack}   and re-run.")
            self.print_failures()
            return 1
        if initial == "DELETE_FAILED":
            err(f"Stack {self.stack} is DELETE_FAILED; delete what blocks it (below), then delete the stack and re-run.")
            self.print_failures()
            return 1
        # A first create that failed leaves ROLLBACK_COMPLETE (or ROLLBACK_FAILED), which CloudFormation
        # refuses to update. The stack never existed successfully, so deleting it loses nothing (`cdk
        # deploy` does the same).
        if initial in ("ROLLBACK_COMPLETE", "ROLLBACK_FAILED"):
            say(f"Stack {self.stack} is {initial} from a failed first create; deleting it before retrying.")
            for op in (("delete-stack",), ("wait", "stack-delete-complete")):
                r = crelib.run(["aws", "cloudformation", *op, "--region", self.region, "--stack-name", self.stack])
                if r.returncode != 0:
                    raise Fail(f"Could not delete {self.stack}: {crelib.last_line(r.stderr)}")
            initial = self.status()
        cs_type = "CREATE" if initial in ("", "REVIEW_IN_PROGRESS") else "UPDATE"
        name = f"{self.env.get('CHANGESET_PREFIX') or 'ci'}-{int(time.time())}-{random.randint(0, 32767)}"
        params = self.params_file()
        say(f"Deploying {self.stack} to {self.region} ({cs_type})\n  template:   {self.url}\n  changeset:  {name}\n"
            f"  params:     {params}\n  initial:    {initial or '<stack does not exist>'}")

        caps = (self.env.get("CAPABILITIES") or "CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND").split()
        r = self.cfn("create-change-set", "--stack-name", self.stack, "--change-set-name", name, "--change-set-type", cs_type,
                     "--template-url", self.url, "--parameters", f"file://{params}", "--capabilities", *caps,
                     *(["--role-arn", self.role_arn] if self.role_arn else []))
        if r.returncode != 0:
            raise Fail(f"create-change-set failed: {(r.stderr or r.stdout).strip()}")
        cs = ("--stack-name", self.stack, "--change-set-name", name)
        wait = crelib.run(["aws", "cloudformation", "wait", "change-set-create-complete", "--region", self.region, *cs])
        if wait.returncode != 0:
            # The waiter fails on an empty change set (no differences): that one is done, not failed.
            d = self.cfn("describe-change-set", *cs, "--query", "{Status:Status,StatusReason:StatusReason}", "--output", "json")
            desc = (d.stdout or d.stderr).strip()
            if re.search(r"didn't contain changes|No updates", desc):
                say("No changes to deploy — treating empty change-set as success.")
                self.cfn("delete-change-set", *cs)
                # An empty CREATE leaves a REVIEW_IN_PROGRESS stack shell: remove it so the next run starts fresh.
                # delete-stack only starts the delete: waited for, or the outputs read found the shell
                # still there and failed "Unexpected stack status: REVIEW_IN_PROGRESS" (review 10g D4).
                if cs_type == "CREATE":
                    for op in (("delete-stack",), ("wait", "stack-delete-complete")):
                        r = crelib.run(["aws", "cloudformation", *op, "--region", self.region, "--stack-name", self.stack])
                        if r.returncode != 0:
                            raise Fail(f"Could not remove the empty {self.stack} shell: {crelib.last_line(r.stderr)}")
            else:
                raise Fail(f"Change-set creation failed:\n{desc}")
        else:
            self.execute(name, cs, initial)
        return self.outputs()

    def execute(self, name, cs, initial):
        say(f"Executing change-set {name}")
        r = self.cfn("execute-change-set", *cs)
        if r.returncode != 0:
            raise Fail(f"execute-change-set failed: {(r.stderr or r.stdout).strip()}")
        # Poll until the stack actually leaves its prior status: a wait against a stack already in
        # CREATE_COMPLETE / UPDATE_COMPLETE can return before CloudFormation switched it to
        # _IN_PROGRESS. A small update can also go COMPLETE → IN_PROGRESS → COMPLETE between two
        # polls, so the change set's own ExecutionStatus reaching an end proves it ran too. The 600 s
        # limit turns any remaining hang into a clear failure.
        say(f"Waiting for stack to begin transitioning from {initial or '<none>'}")
        limit = time.time() + 600
        # A first create starts from the REVIEW_IN_PROGRESS shell create-change-set made, not from
        # nothing: seeing it right after execute-change-set is "not started yet" (review 10d D7).
        before = (initial, "REVIEW_IN_PROGRESS") if not initial else (initial,)
        while True:
            current = self.status()
            if current and current not in before:
                say(f"Stack transitioned to: {current}")
                break
            x = self.cfn("describe-change-set", *cs, "--query", "ExecutionStatus", "--output", "text")
            ex = x.stdout.strip() if x.returncode == 0 else ""
            if ex in ("EXECUTE_COMPLETE", "EXECUTE_FAILED") and not (not initial and current == "REVIEW_IN_PROGRESS"):
                say(f"Change-set execution finished ({ex}) between polls; stack is {current or '<gone>'}")
                break
            if time.time() >= limit:
                raise Fail(f"Stack never left {initial or '<none>'} within 600s of executing {name} (change-set: {ex or '<unknown>'})")
            time.sleep(min(5, self.poll))
        # Our own bounded wait (the CLI waiters stop at 60 minutes, GitLab's default job timeout).
        try:
            final = self.settled()
        except Fail:
            self.print_failures()
            raise
        if final not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            err(f"Stack {self.stack} ended {final or '<gone>'}, not CREATE_COMPLETE / UPDATE_COMPLETE.")
            self.print_failures()
            raise Fail("")

    def outputs(self):
        out_file = self.env.get("OUTPUTS_FILE") or os.path.join("dist", f"{self.stack}.env")
        os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
        st = self.status()
        if st == "":
            say(f"Stack {self.stack} not present after deploy (empty CREATE); no outputs to capture.")
            # Still write the file: the stack job publishes it as its artifact for the jobs that need it.
            open(out_file, "w").close()
            return 0
        if st not in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
            raise Fail(f"Unexpected stack status: {st}")
        r = self.cfn("describe-stacks", "--stack-name", self.stack, "--query", "Stacks[0].Outputs", "--output", "json")
        if r.returncode != 0:
            raise Fail(f"Could not read {self.stack}'s outputs: {crelib.last_line(r.stderr)}")
        outputs = json.loads(r.stdout or "null") or []
        wanted = self.env.get("OUTPUTS")
        if wanted is not None:
            keep = set(wanted.split())
            outputs = [o for o in outputs if o["OutputKey"] in keep]
        prefix = self.env.get("OUTPUTS_PREFIX", "")
        if self.env.get("GITLAB_CI"):
            # GitLab expands `$NAME` in a dotenv value when the next job uses it (an unknown name
            # becomes empty) and trims leading/trailing spaces, so the next stack would get another
            # value than locally or on GitHub (review 10c D3). Refused, naming the output.
            bad = [o["OutputKey"] for o in outputs if "$" in o["OutputValue"] or o["OutputValue"] != o["OutputValue"].strip()]
            if bad:
                raise Fail(f"Output(s) {', '.join(bad)} of {self.stack} contain a `$` or leading/trailing spaces, which GitLab "
                           "would change on the way to the next stack (it expands $NAME in job variables and trims spaces). Change "
                           "the output's value (e.g. encode it, or pass the parts separately).")
        # One line per output in the env file (a dotenv artifact, $GITHUB_ENV, the local deploy's
        # file): a newline in a value can't go through (review 10d D9). crelib.output_vars refuses it,
        # the same for the local deploy (review 10f D4).
        try:
            lines = crelib.output_vars(outputs, prefix, self.stack)
        except ValueError as e:
            raise Fail(str(e))
        with open(out_file, "w") as f:
            for key, value in lines:
                f.write(f"{key}={value}\n")
        say(f"Stack outputs written to {out_file}")
        # GitLab refuses a dotenv artifact over its size limit (5 KB by default), and the job then
        # fails at the upload with a message that doesn't name the outputs (review 10b D18).
        size = os.path.getsize(out_file)
        if self.env.get("GITLAB_CI") and size > 5120:
            err(f"⚠  {out_file} is {size} bytes: GitLab's default dotenv limit is 5 KB (5120). If the artifact upload fails, "
                f"the later stacks take too many or too long outputs from {self.stack}: pass fewer (each "
                "CreCfnParameter({ from }) adds one).")
        return 0


def main():
    env = dict(os.environ)
    try:
        role_arn = preflight(env)
    except Fail as e:
        err(str(e))
        return 1
    d = Deploy(env, role_arn)
    try:
        return d.run()
    except Fail as e:
        if str(e):
            err(str(e))
        d.remove_prefix()
        return 1


if __name__ == "__main__":
    sys.exit(main())
