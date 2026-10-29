"""Deploy a synthesized template as a stack, and wait for it.

  deploy_stack(aws, name, template, region, say, parameters={})
  outputs(aws, name, region) -> {OutputKey: OutputValue}

A stack goes through `aws cloudformation deploy`: a change set, created, waited for, executed and
waited for. A failure raises Failed with CloudFormation's own reasons for this deploy. "Not found"
is recognised by AWS's error text and "no changes" by the CLI's output, the only places they are.
"""
import json
import time

from result import CREDS, FAILED, OK, REFUSED, Failed, must  # noqa: F401 (account.py reads them here)

WAIT = 3600        # the longest a stack operation is waited for


def stack_status(aws, name, region):
    r = aws.call("cloudformation", "describe-stacks", "--stack-name", name, region=region)
    if not r.ok and "does not exist" in r.message:
        return None
    return must(r, f"Reading stack {name}")["Stacks"][0]["StackStatus"]


def deploy_stack(aws, name, template, region, say, parameters=None):
    status = stack_status(aws, name, region)
    if status == "ROLLBACK_COMPLETE":
        # A first create that failed and rolled back holds nothing; CloudFormation can only delete it.
        say(f"→  delete stack {name} (its first create rolled back)")
        must(aws.call("cloudformation", "delete-stack", "--stack-name", name, region=region), f"Deleting stack {name}")
        wait(aws, name, region, "stack-delete-complete", time.time())
    elif status == "UPDATE_ROLLBACK_FAILED":
        recover(aws, name, region, say)
    say(f"→  deploy stack {name}")
    start = time.time()
    r = aws.call("cloudformation", "deploy", "--stack-name", name, "--template-file", template,
                 "--capabilities", "CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "--no-fail-on-empty-changeset",
                 *(["--parameter-overrides", *(f"{k}={v}" for k, v in parameters.items())] if parameters else []),
                 region=region, timeout=2 * WAIT)
    if not r.ok:
        raise Failed(CREDS if r.kind == "creds" else FAILED, f"Deploying stack {name}: {reasons(aws, name, region, start) or r.message}")
    say(f"✓  stack {name}: " + ("no changes" if "No changes to deploy" in str(r.data) else "deployed"))


def recover(aws, name, region, say):
    """A rollback that failed: skip the shared-resource custom resources that failed it. They never
    delete anything, so skipping them is safe; any other resource fails with AWS's own error."""
    res = must(aws.call("cloudformation", "describe-stack-resources", "--stack-name", name, region=region),
               f"Reading stack {name}'s resources")["StackResources"]
    skip = [x["LogicalResourceId"] for x in res
            if x["ResourceType"] == "AWS::CloudFormation::CustomResource" and x["ResourceStatus"] == "UPDATE_FAILED"]
    say(f"→  finish stack {name}'s failed rollback" + (f", skipping {', '.join(skip)}" if skip else ""))
    start = time.time()
    must(aws.call("cloudformation", "continue-update-rollback", "--stack-name", name,
                  *(["--resources-to-skip", *skip] if skip else []), region=region), f"Continuing stack {name}'s rollback")
    wait(aws, name, region, "stack-rollback-complete", start)


def wait(aws, name, region, waiter, start):
    r = aws.call("cloudformation", "wait", waiter, "--stack-name", name, region=region, timeout=WAIT)
    if not r.ok:
        raise Failed(CREDS if r.kind == "creds" else FAILED, f"Stack {name} didn't finish ({waiter}): "
                     f"{reasons(aws, name, region, start) or r.message}")


def reasons(aws, name, region, start):
    """The failed events of this deploy (at or after `start`), newest first."""
    e = aws.call("cloudformation", "describe-stack-events", "--stack-name", name, region=region)
    if not e.ok:
        return ""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start - 1))
    return "; ".join(f"{x['LogicalResourceId']}: {x.get('ResourceStatusReason', '')}" for x in e.data.get("StackEvents", [])
                     if x["ResourceStatus"].endswith("_FAILED") and x.get("Timestamp", "") >= stamp)


def outputs(aws, name, region):
    d = must(aws.call("cloudformation", "describe-stacks", "--stack-name", name, region=region), f"Reading stack {name}")
    return {o["OutputKey"]: o["OutputValue"] for o in d["Stacks"][0].get("Outputs", [])}


