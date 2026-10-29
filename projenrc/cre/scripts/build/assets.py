#!/usr/bin/env python3
"""The project's assets buckets: named from the project, the account and the region, and made by the
deploy itself the first time a region is used.

  assets.py upload --regions "<r1> <r2>"     every region's bucket made if missing, then
                                             dist/content synced to s3://<bucket>/$ASSETS_PREFIX
  assets.py bucket --region <r>              print the region's bucket name (this account's)

A region's bucket is `<B>-<account>-<region>-an`, in the S3 account regional namespace (only this
account can own the name), B the project name, or for a long one its first 23 characters and 8 of
its SHA-256 (setup-cdk/lib/names.ts, the same rule; a test keeps them equal). The account is the
one the credentials are for, so nothing about it is stored anywhere.

Each bucket is a CloudFormation stack, `CreAssets-<project>` in its region (capitalised, so neither
a stack of the project's own nor another project's `<name>-*` can take the name), deployed as
CFN_EXECUTION_ROLE_ARN. Its template is small and passed inline: no bucket is needed to make one.
`npx projen teardown` leaves it (the buckets hold the deploys' templates). A stack that is there with this template and these
parameters is left alone (one DescribeStacks per region); otherwise it is deployed. With
DNS_ROLE_ARN set, the DNS account's deploy role may read the templates (the project's `-dns` stack
deploys there from them).

Environment: PROJECT_NAME, ASSETS_PREFIX (upload), CFN_EXECUTION_ROLE_ARN and DNS_ROLE_ARN (optional).
Exit: 0 done; 1 a step failed (its message says which).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}[a-z0-9]$")
BUCKET_PREFIX_MAX = 63 - (1 + 12 + 1 + 14 + 3)   # S3's 63 minus `-<account>-<longest region>-an`
STACK_PREFIX = "CreAssets-"
OK_STATES = ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE")
WAIT, POLL = 900, 10   # another pipeline's deploy of the stack, waited out


class Fail(Exception):
    pass


def bucket_prefix(project):
    if not PROJECT.match(project):
        raise Fail(f"project name {project!r}: 3-42 lowercase letters, digits and hyphens")
    if len(project) <= BUCKET_PREFIX_MAX:
        return project
    return f"{project[:23].rstrip('-')}-{hashlib.sha256(project.encode()).hexdigest()[:8]}"


def bucket_name(project, account, region):
    return f"{bucket_prefix(project)}-{account}-{region}-an"


def stack_name(project):
    return STACK_PREFIX + project


def template(project):
    """The bucket stack: S3's own defaults stated (no public access, owner-enforced, SSE-S3)."""
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": f"Assets bucket of CRE project {project} (made by its deploy)",
        "Parameters": {"DnsRoleArn": {"Type": "String", "Default": ""}},
        "Conditions": {"DnsReads": {"Fn::Not": [{"Fn::Equals": [{"Ref": "DnsRoleArn"}, ""]}]}},
        "Resources": {
            "Bucket": {"Type": "AWS::S3::Bucket", "Properties": {
                "BucketNamePrefix": bucket_prefix(project), "BucketNamespace": "account-regional",
                "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True,
                                                   "RestrictPublicBuckets": True},
                "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                "BucketEncryption": {"ServerSideEncryptionConfiguration": [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
                "Tags": [{"Key": "cre:project", "Value": project}, {"Key": "cre:managed-by", "Value": "cre-deploy"}]}},
            "DnsReadsTemplates": {"Type": "AWS::S3::BucketPolicy", "Condition": "DnsReads", "Properties": {
                "Bucket": {"Ref": "Bucket"},
                "PolicyDocument": {"Version": "2012-10-17", "Statement": [{
                    # The account as principal, the role in a condition: a policy naming a role that is
                    # later re-created would otherwise stop matching it.
                    "Sid": "DnsAccountReadsTemplates", "Effect": "Allow", "Action": "s3:GetObject",
                    "Principal": {"AWS": {"Fn::Sub": ["arn:${AWS::Partition}:iam::${Account}:root",
                                                      {"Account": {"Fn::Select": [4, {"Fn::Split": [":", {"Ref": "DnsRoleArn"}]}]}}]}},
                    "Resource": {"Fn::Sub": "${Bucket.Arn}/*"},
                    "Condition": {"ArnEquals": {"aws:PrincipalArn": {"Ref": "DnsRoleArn"}}}}]}}},
        },
        "Outputs": {"BucketName": {"Value": {"Ref": "Bucket"}}},
    }


def aws(*args, region=None):
    cmd = ["aws", *args, "--output", "json"] + (["--region", region] if region else [])
    p = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={**os.environ, "AWS_PAGER": ""})
    return p.returncode, p.stdout, (p.stderr or p.stdout).strip()


def account():
    code, out, err = aws("sts", "get-caller-identity")
    if code:
        raise Fail(f"Reading the AWS account (sts get-caller-identity): {err.splitlines()[-1] if err else code}")
    return json.loads(out)["Account"]


def template_hash(body):
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def failed_events(name, region):
    """Why the stack's last operation failed: its FAILED events, newest first."""
    code, out, _ = aws("cloudformation", "describe-stack-events", "--stack-name", name, region=region)
    if code:
        return ""
    evs = [e for e in json.loads(out).get("StackEvents", []) if e.get("ResourceStatus", "").endswith("FAILED")][:5]
    return "; ".join(f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')}" for e in evs)


def settled(name, region, sleep=time.sleep, clock=time.monotonic):
    """(code, out, err) of describe-stacks once the stack isn't *_IN_PROGRESS (another deploy of it)."""
    deadline = clock() + WAIT
    while True:
        code, out, err = aws("cloudformation", "describe-stacks", "--stack-name", name, region=region)
        status = json.loads(out)["Stacks"][0]["StackStatus"] if code == 0 else ""
        if not status.endswith("_IN_PROGRESS") or status == "REVIEW_IN_PROGRESS":
            return code, out, err
        if clock() >= deadline:
            raise Fail(f"{name} in {region} is still {status} after {WAIT}s")
        sleep(POLL)


def ensure_bucket(project, region, dns_role, role_arn, say, sleep=time.sleep):
    """The region's bucket stack, deployed only when missing or not this template and parameters."""
    name = stack_name(project)
    body = json.dumps(template(project), sort_keys=True)
    wanted = {"DnsRoleArn": dns_role, "TemplateHash": template_hash(body)}
    code, out, err = settled(name, region, sleep=sleep)
    if code == 0:
        s = json.loads(out)["Stacks"][0]
        have = {p["ParameterKey"]: p.get("ParameterValue", "") for p in s.get("Parameters", [])}
        tags = {t["Key"]: t["Value"] for t in s.get("Tags", [])}
        if s["StackStatus"] in OK_STATES and have.get("DnsRoleArn", "") == dns_role and tags.get("cre:template") == wanted["TemplateHash"]:
            return
        if s["StackStatus"] == "ROLLBACK_COMPLETE":   # a first create that failed: CloudFormation can only delete it
            say(f"→  delete {name} in {region} (its first create rolled back)")
            for args in (("delete-stack",), ("wait", "stack-delete-complete")):
                c, _, e = aws("cloudformation", *args, "--stack-name", name, region=region)
                if c:
                    raise Fail(f"Deleting {name} in {region}: {e}")
    elif "does not exist" not in err:
        raise Fail(f"Reading stack {name} in {region}: {err.splitlines()[-1] if err else code}")
    say(f"→  deploy the assets bucket stack {name} in {region}")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(body)
    try:
        args = ["cloudformation", "deploy", "--stack-name", name, "--template-file", f.name, "--no-fail-on-empty-changeset",
                "--parameter-overrides", f"DnsRoleArn={dns_role}", "--tags", f"cre:project={project}",
                f"cre:template={wanted['TemplateHash']}"]
        if role_arn:
            args += ["--role-arn", role_arn]
        c, _, e = aws(*args, region=region)
        if c:
            why = failed_events(name, region)
            raise Fail(f"Deploying the assets bucket stack {name} in {region}: {why or e[-1500:]}")
    finally:
        os.unlink(f.name)


def upload(a, say):
    project, prefix = os.environ.get("PROJECT_NAME", ""), os.environ.get("ASSETS_PREFIX", "")
    if not project or not prefix:
        raise Fail("PROJECT_NAME and ASSETS_PREFIX must be set")
    regions = a.regions.split()
    acct = account()
    for r in regions:
        ensure_bucket(project, r, os.environ.get("DNS_ROLE_ARN", ""), os.environ.get("CFN_EXECUTION_ROLE_ARN", ""), say)
    for r in regions:
        b = bucket_name(project, acct, r)
        say(f"Syncing dist/content/ → s3://{b}/{prefix}")
        # --region explicit: no auto-discovery HEAD call, which a scoped policy needn't allow.
        p = subprocess.run(["aws", "s3", "sync", "--no-progress", "--region", r, "--exclude", "containers/*", "dist/content",
                            f"s3://{b}/{prefix}"], stdin=subprocess.DEVNULL)
        if p.returncode:
            raise Fail(f"Syncing to s3://{b}/{prefix} failed (exit {p.returncode})")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="assets.py")
    sub = ap.add_subparsers(dest="mode", required=True)
    u = sub.add_parser("upload", help="make every region's bucket if missing, then sync dist/content to it")
    u.add_argument("--regions", required=True, help="space-separated")
    b = sub.add_parser("bucket", help="print a region's bucket name, for this account")
    b.add_argument("--region", required=True)
    a = ap.parse_args(argv)

    def say(msg):
        print(msg, file=sys.stderr, flush=True)
    try:
        if a.mode == "upload":
            t0 = time.time()
            upload(a, say)
            say(f"Uploaded to {len(a.regions.split())} region(s) in {time.time() - t0:.1f}s")
        else:
            print(bucket_name(os.environ.get("PROJECT_NAME", ""), account(), a.region))
    except Fail as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
