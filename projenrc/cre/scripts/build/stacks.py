#!/usr/bin/env python3
"""The project's deployed stacks, against the stack plan (cre-stacks.json). Uses your AWS credentials
(the ambient ones, or --profile), in the account setup recorded (cre-setup.json accountId).

  npx projen stacks [--profile p]                     each planned stack's status, and orphans: deployed
                                                      stacks of this project main's plan no longer has
                                                      (renamed or removed); a stack this branch adds or
                                                      removes is marked "not yet on main"
  npx projen stacks:outputs [--stack id] [--profile p]  the deployed stacks' outputs (URLs, ids),
                                                      as `<stack> <Key>=<Value>`
  npx projen teardown [--orphans] [--stack id ...] [--profile p] [--dry-run] [--allow-production[=<account>]]
                                                      delete stacks, in reverse deploy order. Running it
                                                      is the decision, so it doesn't ask; --dry-run
                                                      prints what it would delete and deletes nothing.
                                                      In a production account (crelib.is_prod) it
                                                      refuses (10) without --allow-production.
                                                      Default: every planned stack. --orphans takes
                                                      them from main's committed plan, refuses while
                                                      this branch's plan differs from it, and deletes
                                                      the newest first; --stack with it picks orphans.

A stack is this project's when its name is the project or `<project>-…` AND its AssetsBucketName
parameter is `<project>-<its region>` (what the deploy passes), so another project whose name starts
with this one's is never touched. Stacks with `assumeRole` (another account's DNS) are listed but
never deleted here: delete them with that account's credentials.

Assets buckets and the setup roles are never deleted (setup made them; other projects may share
the roles). Resources a stack retains on delete (a bucket with RemovalPolicy RETAIN) stay.

Exit codes (the setup contract): 0 done | 10 refused (no plan, unknown stack, the wrong account) |
11 credentials missing or expired | 20 a deletion failed.
Last stdout line: RESULT code=<n> step=<stacks|outputs|teardown>
"""
import argparse
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import crelib  # noqa: E402

# The setup contract's codes, from the one place they are defined (review 10b F4).
OK, REFUSED, CREDENTIALS, FAILED = crelib.OK, crelib.REFUSED, crelib.CREDENTIALS, crelib.FAILED


class Stop(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


say = crelib.say   # a plain line on stderr (crelib's printer)


class Aws:
    def __init__(self, profile):
        self.base = ["aws"] + (["--profile", profile] if profile else [])

    def run(self, *args):
        r = subprocess.run([*self.base, *args, "--output", "json"], stdin=subprocess.DEVNULL, capture_output=True, text=True)
        # A lapse mid-run (a teardown waits on deletes for minutes) is 11, never a failed call (20)
        # or, in a delete's wait, a DELETE_FAILED of a stack CloudFormation is still deleting (review 10f D1).
        if r.returncode != 0 and crelib.credentials_lapsed(r.stderr):
            raise Stop(CREDENTIALS, f"The AWS credentials expired during aws {' '.join(args[:3])}: "
                       f"{(crelib.last_line(r.stderr) or '?')}\n   Nothing was concluded from it (a stack being deleted "
                       "carries on in CloudFormation). Refresh them, then re-run: npx projen stacks shows where each stack is.")
        return r

    def json(self, *args):
        r = self.run(*args)
        if r.returncode != 0:
            raise Stop(FAILED, f"aws {' '.join(args[:2])} failed: {(crelib.last_line(r.stderr) or '?')}")
        return json.loads(r.stdout or "null")

    def account(self):
        r = self.run("sts", "get-caller-identity")
        if r.returncode != 0:
            raise Stop(CREDENTIALS, "AWS credentials missing or expired (sts get-caller-identity failed). Refresh them, or pass --profile.")
        d = json.loads(r.stdout)
        return d["Account"], d["Arn"]

    def describe(self, region, name):
        """The stack, or None when it doesn't exist."""
        r = self.run("cloudformation", "describe-stacks", "--region", region, "--stack-name", name)
        if r.returncode != 0:
            if "does not exist" in r.stderr:
                return None
            raise Stop(FAILED, f"describe-stacks {name} ({region}) failed: {(crelib.last_line(r.stderr) or '?')}")
        return json.loads(r.stdout)["Stacks"][0]


def load(path):
    try:
        return crelib.load_json(path)
    except crelib.BadFile as e:
        raise Stop(REFUSED, f"{e}.")


# A stack's own deploy finished: anything else (in progress, rolled back, failed) means main's
# version of it isn't the one running.
DEPLOYED = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"}


def committed_plan(strict=False):
    """(stacks, ref) of cre-stacks.json as committed on origin's default branch: what the pipeline
    deploys. (None, why) when that can't be read (no origin, no such file there). A branch's working
    plan is not yet deployed, so a stack it renames is no orphan until it merges (review 10c A8).
    strict (a delete): a failed fetch refuses, never falls back to the copy fetched last, which may
    miss a stack a teammate's merge added and deployed (review 10d D2)."""
    def git(*a):
        return subprocess.run(["git", *a], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if git("rev-parse", "--git-dir").returncode != 0 or git("remote", "get-url", "origin").returncode != 0:
        return None, "not a git checkout with an origin"
    head = git("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").stdout.strip() or "origin/main"
    branch = head.split("/", 1)[1] if "/" in head else "main"
    if git("fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}").returncode != 0:
        if strict:
            raise Stop(REFUSED, f"Could not fetch origin/{branch}, so main's current stack plan is unknown and nothing is judged an "
                       "orphan from an older copy. Refresh your git credentials (GitLab: mwinit -f -k ~/.ssh/id_ecdsa.pub), then re-run.")
        say(f"⚠️  Could not fetch origin/{branch}; using the copy fetched last.")
    r = git("show", f"{head}:cre-stacks.json")
    if r.returncode != 0:
        return None, f"{head} has no cre-stacks.json ({(crelib.last_line(r.stderr) or '?')})"
    try:
        return json.loads(r.stdout).get("stacks", []), head
    except ValueError as e:
        return None, f"{head}:cre-stacks.json isn't valid JSON ({e})"


def not_in_plan(stacks, ids, extra=""):
    """The refusal for --stack ids the plan doesn't have, naming the ones it does: `--stack edge`
    was refused as "Not in the plan: edge." without saying that ids are `<project>-edge` (review 10f A6)."""
    have = [s["id"] for s in stacks]
    named = [next((f"{i} (did you mean {h}?)" for h in have if h.endswith(f"-{i}")), i) for i in sorted(ids)]
    return Stop(REFUSED, f"Not in the plan: {', '.join(named)}{extra}. --stack takes the full stack id; the plan has "
                f"{', '.join(have) or 'no stacks'}.")


def keys(stacks):
    return {(s["id"], s["region"]) for s in stacks}


def context(args):
    plan = load("cre-stacks.json")
    if plan is None:
        raise Stop(REFUSED, "No cre-stacks.json here; run from the project root (npx projen runs there).")
    setup = load("cre-setup.json") or {}
    project = os.environ.get("PROJECT_NAME") or setup.get("project")
    if not project:
        raise Stop(REFUSED, "PROJECT_NAME is not set and cre-setup.json has no project.")
    aws = Aws(args.profile)
    account, arn = aws.account()
    want = setup.get("accountId")
    if want and account != want:
        # 10, as setup says it: the credentials work, they are another account's, and refreshing them
        # (what 11 tells an agent) changes nothing (review 10f D5).
        raise Stop(REFUSED, f"These credentials are for account {account} ({arn}), but the project deploys to {want} "
                   "(cre-setup.json). Use that account's credentials (--profile).")
    regions = setup.get("regions", "")
    regions = regions.split() if isinstance(regions, str) else list(regions)
    stacks = plan.get("stacks", [])
    known = regions + [s["region"] for s in stacks]
    partition = crelib.partition_of(known[0]) if known else "aws"
    regions = list(dict.fromkeys(known + crelib.control_plane_regions(partition)))
    return plan, stacks, project, aws, regions, account


def is_ours(stack, project, region, planned=()):
    """Named like this project's stacks, and deployed from its assets bucket: `<prefix>-<region>`,
    where the prefix is ASSETS_BUCKET_PREFIX when set (it can differ from the project name, e.g.
    after a bucket-name collision; review 10b D14). A REVIEW_IN_PROGRESS shell (a failed first
    create) has no parameters: matched when it is one of the planned stacks (a name alone could be
    another project's, `demo-two` beside `demo`)."""
    name = stack["StackName"]
    if name != project and not name.startswith(project + "-"):
        return False
    if stack.get("StackStatus") == "REVIEW_IN_PROGRESS" and not stack.get("Parameters"):
        return name in planned
    params = {p["ParameterKey"]: p.get("ParameterValue") for p in stack.get("Parameters", [])}
    prefix = os.environ.get("ASSETS_BUCKET_PREFIX") or project
    return params.get("AssetsBucketName") in (f"{prefix}-{region}", f"{project}-{region}")


def deployed(aws, regions, project, planned=()):
    """{(name, region): summary} of every stack of this project, in every region it may use."""
    out = {}
    for region in regions:
        token = None
        while True:
            args = ["cloudformation", "describe-stacks", "--region", region]
            if token:
                args += ["--next-token", token]
            d = aws.json(*args)
            for s in d.get("Stacks", []):
                if is_ours(s, project, region, planned):
                    out[(s["StackName"], region)] = s
            token = d.get("NextToken")
            if not token:
                break
    return out


def cmd_status(args):
    plan, stacks, project, aws, regions, account = context(args)
    main_stacks, ref = committed_plan()
    if main_stacks is None:
        say(f"⚠️  {ref}: orphans are judged from this checkout's plan, which may not be what main deploys.")
        main_stacks, ref = stacks, ""
    found = deployed(aws, regions, project, [st["id"] for st in stacks + main_stacks])
    here, on_main = keys(stacks), keys(main_stacks)
    rows = []
    for s in stacks:
        d = found.get((s["id"], s["region"]))
        role = s.get("assumeRole")
        notes = []
        if role:
            # This account's listing can't have it, so its status was never read: NOT_DEPLOYED stated
            # what nobody checked, and read as "missing" (review 10f D3).
            var = role.get("variable", "") if isinstance(role, dict) else str(role)
            notes.append(f"in another account ({var}): not checked from here; use that account's credentials")
        if ref and (s["id"], s["region"]) not in on_main:
            notes.append(f"added by this branch, not yet on {ref}")
        rows.append((s["id"], s["region"], d["StackStatus"] if d else ("NOT_CHECKED" if role else "NOT_DEPLOYED"),
                     (d.get("LastUpdatedTime") or d.get("CreationTime") or "")[:19] if d else "", ", ".join(notes)))
    for s in main_stacks:
        if (s["id"], s["region"]) not in here:
            d = found.get((s["id"], s["region"]))
            rows.append((s["id"], s["region"], d["StackStatus"] if d else "NOT_DEPLOYED", "",
                         f"removed by this branch, not yet on {ref}: still deployed by main"))
    orphans = sorted(k for k in found if k not in on_main and k not in here)
    say(f"Account {account}, project {project}:")
    w = max([len(r[0]) for r in rows] + [len(o[0]) for o in orphans] + [5])
    for r in rows:
        say(f"  {r[0]:<{w}}  {r[1]:<14} {r[2]:<24} {r[3]}{'  (' + r[4] + ')' if r[4] else ''}")
    if not rows:
        say("  (the plan has no stacks)")
    for name, region in orphans:
        say(f"  {name:<{w}}  {region:<14} {found[(name, region)]['StackStatus']:<24} ORPHAN: deployed, in neither main's plan nor this one")
    if orphans:
        say("Orphans keep running (and costing) until deleted: npx projen teardown --orphans  (--dry-run lists them first).")
    for name, region in orphans:
        print(f"ORPHAN={name}@{region}")
    return OK


def cmd_outputs(args):
    plan, stacks, project, aws, regions, account = context(args)
    want = set(args.stack or [])
    unknown = want - {s["id"] for s in stacks}
    if unknown:
        raise not_in_plan(stacks, unknown)
    n = 0
    for s in stacks:
        if want and s["id"] not in want:
            continue
        if s.get("assumeRole"):
            say(f"{s['id']}: in another account; read its outputs with that account's credentials.")
            continue
        d = aws.describe(s["region"], s["id"])
        if not d:
            say(f"{s['id']}: not deployed.")
            continue
        for o in sorted(d.get("Outputs", []), key=lambda o: o["OutputKey"]):
            print(f"{s['id']} {o['OutputKey']}={o['OutputValue']}")
            n += 1
    say(f"{n} output(s).")
    return OK


def delete(aws, name, region):
    """Delete and wait. Returns "" when gone, else why not."""
    r = aws.run("cloudformation", "delete-stack", "--region", region, "--stack-name", name)
    if r.returncode != 0:
        return crelib.last_line(r.stderr) or "delete-stack failed"
    w = aws.run("cloudformation", "wait", "stack-delete-complete", "--region", region, "--stack-name", name)
    if w.returncode == 0:
        return ""
    if "Max attempts exceeded" in w.stderr:
        # The waiter gave up (about an hour), not CloudFormation (review 10f D1).
        return ("still DELETE_IN_PROGRESS when the wait gave up: CloudFormation is still deleting it. Check it with npx projen "
                "stacks, and re-run teardown once it has finished or failed")
    ev = aws.run("cloudformation", "describe-stack-events", "--region", region, "--stack-name", name)
    reasons = []
    if ev.returncode == 0:
        for e in json.loads(ev.stdout).get("StackEvents", [])[:30]:
            if e.get("ResourceStatus") == "DELETE_FAILED":
                reasons.append(f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')}")
    return "DELETE_FAILED" + (": " + "; ".join(reasons[:5]) if reasons else "")


def refuse_production(args, account):
    """teardown deletes when run, so a production account (crelib.is_prod: Isengard's
    classification, or one it can't classify) needs the same decision as setup: --allow-production,
    for that account (review 10g D1). A dry run deletes nothing and needs none."""
    if args.dry_run:
        return
    try:
        prod, why = crelib.is_prod(account)
    except crelib.ProdCheckLapsed as e:
        raise Stop(CREDENTIALS, str(e))
    except crelib.ProdCheckUnavailable as e:
        raise Stop(FAILED, str(e))
    if not prod:
        return
    given = args.allow_production or []
    if "" in given or account in given:
        say(f"⚠ Account {account} is production ({why}): deleting, as --allow-production says.")
        return
    raise Stop(REFUSED, f"Account {account} is production ({why}), and teardown deletes when run. Show the user what "
               f"npx projen teardown --dry-run lists, name the account, and ask. Only if they agree, re-run with "
               f"--allow-production={account}. Never pass it without their yes.")


def cmd_teardown(args):
    plan, stacks, project, aws, regions, account = context(args)
    refuse_production(args, account)
    found = deployed(aws, regions, project, [st["id"] for st in stacks])
    planned_ids = [s["id"] for s in stacks]
    # With --orphans, --stack picks orphans (one out of several, review 10g A3); alone, planned stacks.
    unknown = set(args.stack or []) - set(planned_ids) if not args.orphans else set()
    if unknown:
        raise not_in_plan(stacks, unknown, " (an orphan: --orphans --stack <id>)")
    if args.stack and not args.orphans:
        # Stacks pass values as outputs → parameters, not exports, so CloudFormation doesn't stop the
        # delete of a stack whose outputs a deployed stack still uses: refuse it here (review 10b D9).
        chosen = set(args.stack)
        users = {s["id"]: [o["id"] for o in stacks if s["id"] in (o.get("needs") or []) and o["id"] not in chosen
                           and (o["id"], o["region"]) in found] for s in stacks if s["id"] in chosen}
        blocked = {k: v for k, v in users.items() if v}
        # A stack in another account (assumeRole) isn't listed by this account's describe-stacks, so it
        # can't be counted as deployed: named, not silently passed (review 10c D7).
        for s in stacks:
            if s.get("assumeRole") and s["id"] not in chosen:
                took = [n for n in (s.get("needs") or []) if n in chosen]
                if took:
                    say(f"⚠  {s['id']} deploys to another account and takes values from {', '.join(took)}; it can't be checked from "
                        f"here. If it is deployed there, delete it first (with that account's credentials).")
        if blocked:
            raise Stop(REFUSED, "Still used by deployed stacks: " + "; ".join(f"{k} (by {', '.join(v)})" for k, v in blocked.items())
                       + ". Delete those first (add them with --stack), or run teardown without --stack.")
    targets, skipped = [], []
    # Why teardown would refuse now. A real run refuses before deleting anything; --dry-run still
    # lists what it would delete, then says why it won't yet (exit 10): the preview the steering
    # tells the agent to show (review 10g A1).
    refusal = ""
    if args.orphans:
        main_stacks, ref = committed_plan(strict=True)
        if main_stacks is None:
            # Judging orphans from the checkout's own plan deleted, on a branch that renamed a stack,
            # the old stack main still deploys (review 10g D2): only a dry run lists them that way.
            refusal = (f"{ref}, so which stacks main no longer deploys can't be told and none are deleted. Fix that (an "
                       "origin whose default branch has cre-stacks.json committed), or delete planned stacks with --stack.")
            main_stacks, ref = stacks, "this checkout's plan"
        elif keys(main_stacks) != keys(stacks):
            added = sorted(f"{i}@{r}" for i, r in keys(stacks) - keys(main_stacks))
            removed = sorted(f"{i}@{r}" for i, r in keys(main_stacks) - keys(stacks))
            refusal = (f"This checkout's stack plan differs from {ref}'s (" + "; ".join(
                x for x in (f"adds {', '.join(added)}" if added else "", f"removes {', '.join(removed)}" if removed else "") if x)
                + "). Orphans are what main no longer deploys: merge and deploy this change first, then run teardown --orphans.")
        # A stack main renamed is an orphan only once main's own stacks are deployed: after a merge
        # whose deploy failed or is still running, the old copy is what the deployed stacks still use
        # (review 10d D1). A stack in another account can't be seen from here, so it isn't counted.
        undeployed = [f"{s['id']}@{s['region']} ({found[(s['id'], s['region'])]['StackStatus'] if (s['id'], s['region']) in found else 'not deployed'})"
                      for s in main_stacks if not s.get("assumeRole")
                      and found.get((s["id"], s["region"]), {}).get("StackStatus") not in DEPLOYED]
        if undeployed and not refusal:
            refusal = (f"The stacks of {ref} aren't all deployed yet: {', '.join(undeployed)}. Its deploy failed or is still "
                       "running, and until it finishes an old stack may still be in use. Deploy main first (follow it with npx "
                       "projen watch, or retry it with npx projen ci:rerun), then run teardown --orphans.")
        # A renamed stack's replacement is created after it, so newest first deletes dependents first.
        targets = sorted((k for k in found if k not in keys(main_stacks)),
                         key=lambda k: found[k].get("CreationTime", ""), reverse=True)
        if args.stack:
            missing = sorted(set(args.stack) - {k[0] for k in targets})
            if missing:
                raise Stop(REFUSED, f"Not orphans: {', '.join(missing)}. The orphans are: "
                           + (", ".join(f"{n} ({r})" for n, r in targets) or "none") + ". Nothing was deleted.")
            targets = [k for k in targets if k[0] in args.stack]
    else:
        for s in reversed(stacks):   # the reverse of the deploy order: dependents first
            if args.stack and s["id"] not in args.stack:
                continue
            if s.get("assumeRole"):
                skipped.append(s["id"])
            elif (s["id"], s["region"]) in found:
                targets.append((s["id"], s["region"]))
    for sid in skipped:
        say(f"Skipping {sid}: it deploys to another account (assumeRole). Delete it there: "
            f"aws cloudformation delete-stack --stack-name {sid} --profile <that account>")
    if refusal and not args.dry_run:
        raise Stop(REFUSED, refusal)
    if not targets:
        say("Nothing to delete.")
    else:
        say(f"Account {account}: {'would delete' if args.dry_run else 'deleting'}, in this order:")
        for name, region in targets:
            say(f"  {name} ({region}, {found[(name, region)]['StackStatus']})")
    if args.dry_run:
        if targets:
            say("(dry run) Nothing was deleted. Their resources go too when deleted, except what a stack retains; buckets and setup roles stay.")
        if refusal:
            raise Stop(REFUSED, f"A real run would refuse now: {refusal}")
        return OK
    if not targets:
        return OK
    failed = []
    for name, region in targets:
        say(f"Deleting {name} ({region})...")
        why = delete(aws, name, region)
        if why:
            failed.append(f"{name}: {why}")
            say(f"  ❌ {why}")
            if not args.orphans:
                break   # a dependent left in place blocks the stacks it needs
        else:
            say("  ✅ deleted")
    if failed:
        raise Stop(FAILED, "Not deleted: " + "; ".join(failed) + ". Fix the resource named, then re-run (deleted stacks are skipped).")
    return OK


USAGE = {"status": "npx projen stacks [--profile p]",
         "outputs": "npx projen stacks:outputs [--stack id ...] [--profile p]",
         "teardown": "npx projen teardown [--orphans] [--stack id ...] [--profile p] [--dry-run] [--allow-production[=<account>]]"}


class Parser(argparse.ArgumentParser):
    """A usage error is the setup contract's 10 with a RESULT line, not argparse's bare exit 2
    (review 10c D5)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        say(f"❌ {message}")
        print(f"RESULT code={REFUSED} step={self.step}", flush=True)
        sys.exit(REFUSED)


def main(argv):
    # One parser per command: a switch a command doesn't use (`stacks --dry-run`) is refused, not ignored.
    command = argv[0] if argv and argv[0] in ("status", "outputs", "teardown") else ""
    step = {"status": "stacks", "outputs": "outputs", "teardown": "teardown"}.get(command, "stacks")
    p = Parser(prog=f"npx projen {({'status': 'stacks', 'outputs': 'stacks:outputs'}).get(command, command or 'stacks')}",
               usage=USAGE.get(command, USAGE["status"]))
    p.step = step
    p.add_argument("command", choices=["status", "outputs", "teardown"], help=argparse.SUPPRESS)
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""), help="the AWS profile (default: $AWS_PROFILE or the default credentials)")
    if command in ("outputs", "teardown"):
        p.add_argument("--stack", action="append", help="only this stack (repeatable); with --orphans, only this orphan" if command == "teardown" else "only this stack (repeatable)")
    if command == "teardown":
        p.add_argument("--orphans", action="store_true", help="only the stacks main's plan no longer has, newest first")
        p.add_argument("--dry-run", dest="dry_run", action="store_true", help="print what would be deleted, in order, and delete nothing")
        p.add_argument("--allow-production", dest="allow_production", action="append", nargs="?", const="", metavar="ACCOUNT",
                       help="delete in a production account (crelib.is_prod); =<account> for only that one. Only after the user agrees")
    args = p.parse_args(argv)
    for k in ("stack", "orphans", "dry_run", "allow_production"):
        if not hasattr(args, k):
            setattr(args, k, None if k == "stack" else False)
    step = {"status": "stacks", "outputs": "outputs", "teardown": "teardown"}[args.command]
    try:
        code = {"status": cmd_status, "outputs": cmd_outputs, "teardown": cmd_teardown}[args.command](args)
    except Stop as s:
        say(f"❌ {s.msg}")
        code = s.code
    print(f"RESULT code={code} step={step}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
