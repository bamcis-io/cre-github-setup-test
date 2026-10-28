#!/usr/bin/env python3
"""Run the deploy pipeline on this machine: the same jobs, in the same order, with the same variables.

    npx projen deploy [--stack <id>]... [--no-build] [--profile <p>]

Reads the jobs projen generated into cre-stacks.json (the same records the GitLab / GitHub pipeline
is generated from) and does what the CI runner does:

  0. Checks the credentials are for the account setup recorded (cre-setup.json accountId), and
     builds (`npx projen build`: synth, tests, packaging) unless --no-build: the pipeline deploys
     only what its build job made, and so does this.
  1. Gets every variable the jobs need: from the environment, then `.cre/deploy.env` (KEY=VALUE
     lines, gitignored; setup writes the values it set), then, on a terminal, a prompt. Without a
     terminal a missing required variable stops it, listing all of them.
  2. `npx projen deploy:upload` with the run's ASSETS_PREFIX (default `<project>/<UTC time>/`).
  3. `npx projen deploy:stack` once per stack, in deploy order, with exactly the variables its CI
     job has: the job's own (STACK_NAME, AWS_REGION, ROLE_ARN, STACK_PARAMETERS, ...) rendered the
     way the runner expands them, and the outputs of the stacks it needs: from the dist/<stack>.env
     files the stacks deployed in this run wrote (CI passes the same files between jobs), or, for a
     stack not deployed in this run (--stack), read from the deployed stack.
  4. `npx projen deploy:prune`, when the project prunes (not with --stack).

--stack <id> (repeatable) deploys only those stacks (still in deploy order). A stack that assumes a
role (a cross-account DNS stack) is checked first: when your credentials can't assume it (it
trusts the CI runner role), it stops before anything is deployed, naming the role; deploy the
others with --stack and let CI deploy that one.

Nothing here decides anything a CI job doesn't: a stack job's variables are the generated ones.
CRE_PROJEN overrides the projen command (default `npx projen`).
"""
import datetime
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "setup"))
import crelib  # noqa: E402  (one copy of the output-to-variable rule, review 10f D4)

PLAN = pathlib.Path("cre-stacks.json")
LOCAL_ENV = pathlib.Path(".cre/deploy.env")
SETUP = pathlib.Path("cre-setup.json")
VARIABLE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def die(msg):
    print(f"deploy: {msg}", file=sys.stderr)
    sys.exit(1)


def read_env_file(path):
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v
    return values


def render(value, env, outputs):
    """A job value as the CI runner expands it (stack-jobs.ts JobValue)."""
    kind = value["kind"]
    if kind == "literal":
        return value["value"]
    if kind == "var":
        got = env.get(value["name"], "")
        fallback = value.get("fallback")
        if not got and fallback is not None:
            got = env.get(fallback, "") if VARIABLE.match(fallback) else fallback
        return got
    if kind == "output":
        return outputs.get(value["variable"], "")
    if kind == "concat":
        return "".join(render(p, env, outputs) for p in value["parts"])
    if kind == "lines":
        return "\n".join(f"{k}={render(v, env, outputs)}" for k, v in value["lines"])
    die(f"unknown job value kind {kind!r} in {PLAN}; re-run npx projen.")


def gather_variables(jobs, env):
    """Every variable the jobs use; fill the missing required ones from .cre/deploy.env or a prompt."""
    required, used = [], []
    for spec in jobs:
        for v in spec["variables"]:
            if v not in used:
                used.append(v)
        for v in spec["env"]["REQUIRED_VARS"]["value"].split():
            if v not in required:
                required.append(v)
    saved = read_env_file(LOCAL_ENV)
    for v in used:
        if not env.get(v) and saved.get(v):
            env[v] = saved[v]
    missing = [v for v in required if not env.get(v)]
    if missing and sys.stdin.isatty():
        print(f"deploy: {len(missing)} variable(s) not set (environment or {LOCAL_ENV}):")
        for v in missing:
            env[v] = input(f"  {v} = ").strip()
        missing = [v for v in required if not env.get(v)]
    if missing:
        die(f"set {', '.join(missing)} (environment or {LOCAL_ENV}, KEY=VALUE lines). The CI pipeline gets them "
            "from CI/CD variables or secrets of the same names.")


def aws(args, env):
    return subprocess.run(["aws", *args, "--output", "json"], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def check_account(env):
    r = aws(["sts", "get-caller-identity"], env)
    if r.returncode != 0:
        die("AWS credentials missing or expired (sts get-caller-identity failed). Refresh them, or pass --profile.")
    ident = json.loads(r.stdout)
    want = ""
    if SETUP.is_file():
        try:
            want = json.loads(SETUP.read_text()).get("accountId", "")
        except ValueError:
            want = ""
    if want and ident["Account"] != want:
        die(f"these credentials are for account {ident['Account']} ({ident['Arn']}), but the project deploys to {want} "
            f"({SETUP}). Use that account's credentials (--profile).")
    print(f"deploy: as {ident['Arn']} (account {ident['Account']})", flush=True)


def check_assumable(stacks, jobs_by_id, env, outputs_none):
    """Every role a selected stack assumes can be assumed by these credentials, before anything deploys."""
    for s in stacks:
        spec = jobs_by_id[s["id"]]
        if "ASSUME_ROLE" not in spec["env"]:
            continue
        role = render(spec["env"]["ASSUME_ROLE"], env, outputs_none)
        if not role:
            continue
        # Probed the way the deploy assumes it (with-role.py): already holding that role counts, where
        # a bare assume-role from itself would fail (review 10b D23).
        with_role = pathlib.Path(__file__).resolve().parent / "with-role.py"
        r = subprocess.run([sys.executable, str(with_role), role, "aws", "sts", "get-caller-identity", "--output", "json"], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if r.returncode != 0:
            die(f"{s['id']} deploys as {role}, which your credentials can't assume (it trusts the CI runner role): "
                f"{(crelib.last_line(r.stderr) or '?')}\n  Deploy the other stacks with --stack, and let the pipeline deploy {s['id']}.")


def live_outputs(stack, env, prefix, only=None, role="", after=""):
    """A deployed stack's outputs as its job would have written them (dist/<stack>.env): only those
    its job's OUTPUTS lists, as in CI (review 10b D22). only=None: all of them. role: the stack's
    ASSUME_ROLE, when it is in another account (a cross-account CreDnsStack): read there (D10).
    after: the stack that deploys after it, for the message when it isn't deployed."""
    cmd = ["cloudformation", "describe-stacks", "--region", stack["region"], "--stack-name", stack["id"]]
    if role:
        with_role = pathlib.Path(__file__).resolve().parent / "with-role.py"
        r = subprocess.run([sys.executable, str(with_role), role, "aws", *cmd, "--output", "json"], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if r.returncode != 0:
            die(f"{stack['id']}'s outputs are needed, and it is in another account: reading them needs {role}, which these "
                f"credentials can't assume ({(crelib.last_line(r.stderr) or '?')}). Let the pipeline deploy this stack.")
    else:
        r = aws(cmd, env)
    if r.returncode != 0:
        die(f"{stack['id']} isn't deployed, and {after or 'the stack you asked for'} deploys after it (it needs its outputs, or "
            "depends on it): deploy it first, or include it with --stack.")
    try:
        return dict(crelib.output_vars(json.loads(r.stdout)["Stacks"][0].get("Outputs", []), prefix, stack["id"], only))
    except ValueError as e:
        die(str(e))


def parse(argv):
    import argparse
    class Parser(argparse.ArgumentParser):
        def error(self, message):   # the documented codes are 0 or 1, not argparse's 2 (review 10c D5)
            self.print_usage(sys.stderr)
            print(f"❌ {message}", file=sys.stderr)
            sys.exit(1)
    p = Parser(prog="npx projen deploy")
    p.add_argument("--stack", action="append", default=[])
    p.add_argument("--no-build", dest="build", action="store_false")
    p.add_argument("--profile", default="")
    return p.parse_args(argv)


def main(argv):
    a = parse(argv[1:])
    if not PLAN.is_file():
        die(f"no {PLAN}; run npx projen first.")
    plan = json.loads(PLAN.read_text())
    stacks = plan.get("stacks", [])
    jobs_by_id = {k: v["spec"] for k, v in plan.get("jobs", {}).items()}
    if not stacks:
        # A fresh scaffold: nothing to deploy yet (the CI deploy stage is the same no-op).
        print(f"{PLAN} lists no stacks: nothing to deploy. Add CreStacks to the CDK app and run npx projen.")
        return 0
    unknown = [x for x in a.stack if x not in jobs_by_id]
    if unknown:
        die(f"not in {PLAN}: {', '.join(unknown)} (stacks: {', '.join(s['id'] for s in stacks)}).")
    selected = [s for s in stacks if not a.stack or s["id"] in a.stack]
    projen = shlex.split(os.environ.get("CRE_PROJEN", "npx projen"))
    project = os.environ.get("PROJECT_NAME") or die("PROJECT_NAME is not set (npx projen deploy sets it).")

    env = dict(os.environ)
    if a.profile:
        env["AWS_PROFILE"] = a.profile
        env.pop("AWS_ACCESS_KEY_ID", None), env.pop("AWS_SECRET_ACCESS_KEY", None), env.pop("AWS_SESSION_TOKEN", None)
    check_account(env)
    gather_variables([jobs_by_id[s["id"]] for s in selected], env)
    check_assumable(selected, jobs_by_id, env, {})
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    env["ASSETS_PREFIX"] = env.get("ASSETS_PREFIX") or f"{project}/{stamp}/"
    pathlib.Path("dist").mkdir(exist_ok=True)

    def run(task, extra=None):
        print(f"\n=== {task}{(' ' + extra['STACK_NAME']) if extra and 'STACK_NAME' in extra else ''}", flush=True)
        r = subprocess.run([*projen, task], env={**env, **(extra or {})})
        if r.returncode != 0:
            die(f"{task} failed (exit {r.returncode}); later jobs did not run.")

    by_id = {s["id"]: s for s in stacks}
    chosen = {s["id"] for s in selected}
    # What the stacks need from ones this run doesn't deploy, read before the build and the upload,
    # so a stack that isn't deployed stops the run before anything is synced (review 10h D4).
    def read_need(need, after):
        need_env = jobs_by_id[need]["env"]
        prefix = render(need_env.get("OUTPUTS_PREFIX", {"kind": "literal", "value": ""}), env, {})
        listed = need_env.get("OUTPUTS")
        only = set(render(listed, env, {}).split()) if listed is not None else None
        role = render(need_env["ASSUME_ROLE"], env, {}) if "ASSUME_ROLE" in need_env else ""
        return live_outputs(by_id[need], env, prefix, only, role, after=after)
    live = {}
    for s in selected:
        for need in jobs_by_id[s["id"]]["needs"]:
            if need not in chosen and need not in live:
                live[need] = read_need(need, s["id"])
    if a.build:
        run("build")
    run("deploy:upload")
    deployed_now = set()
    for s in selected:
        spec = jobs_by_id[s["id"]]
        outputs = {}
        for need in spec["needs"]:
            f = pathlib.Path("dist") / f"{need}.env"
            if need in deployed_now and f.is_file():
                outputs.update(read_env_file(f))
            else:
                outputs.update(live[need] if need in live else read_need(need, s["id"]))
        job_env = {k: render(v, env, outputs) for k, v in spec["env"].items()}
        # Like GitLab's dotenv artifacts: the needed jobs' outputs are in the job's environment too.
        run("deploy:stack", {**outputs, **job_env})
        deployed_now.add(s["id"])
    tasks = json.loads(pathlib.Path(".projen/tasks.json").read_text()).get("tasks", {})
    if "deploy:prune" in tasks and not a.stack:
        run("deploy:prune")
    print(f"\ndeploy: {len(selected)} stack(s) deployed with prefix {env['ASSETS_PREFIX']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
