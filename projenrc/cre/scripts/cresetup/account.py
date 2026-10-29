#!/usr/bin/env python3
"""The AWS account a CRE project deploys into, set up through CloudFormation.

  account.py --project <name> --provider gitlab|github --profile <p> [--partition aws]
             [--namespace <gitlab group>] [--github-subject repo:<owner>/<repo> [--github-environment AWS]]
             [--dns-profile <p>] [--allow-production <account>]...

In a project, `npx projen setup:aws` passes --project, --provider, --partition and the trust
(--namespace, or --github-repo and a public repo's --github-environment) from .projenrc.ts; give
it only --profile, --dns-profile and --allow-production. Each switch but --allow-production is
taken once: a second value is refused (10).

1. Synthesize the setup CDK app (packages/cre-projen/setup-cdk) for this project; it checks the input.
2. Who and where: the caller's account (and with --dns-profile the DNS account's), and whether
   Isengard says each is production (a production or unclassified account needs
   --allow-production <account>).
3. Deploy the IAM stack CreSetup-<project> with a change set, in the partition's control-plane
   region, and wait for it. No region list: the runner may use this project's names in any region,
   and the deploy makes each region's assets bucket the first time a stack goes there.
4. With --dns-profile (the project's hosted zones are in that separate account): deploy the stack
   CreSetupDns-<project> there, with the roles the project's DNS deploys and certificates use.

Exit: 0 done; 10 refused (nothing changed); 11 credentials; 20 a call failed (a re-run continues).
Last stdout line `RESULT code=<n>`; before it ACCOUNT_ID, RUNNER_ROLE_ARN,
CFN_EXECUTION_ROLE_ARN (and DNS_ACCOUNT_ID, DNS_ROLE_ARN, DNS_CFN_EXECUTION_ROLE_ARN with a DNS
account), and DECISION=<json> for each decision needed.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import aws as awslib  # noqa: E402
import cfn  # noqa: E402
import proc  # noqa: E402
import prod as prodlib  # noqa: E402
from setup_args import SingleValue  # noqa: E402

OK, REFUSED, CREDS, FAILED = cfn.OK, cfn.REFUSED, cfn.CREDS, cfn.FAILED
CDK_APP = os.path.join(os.path.dirname(os.path.dirname(HERE)), "setup-cdk")
SYNTH_TIMEOUT = 300
CONTROL_PLANE = {"aws": "us-east-1", "aws-us-gov": "us-gov-west-1", "aws-cn": "cn-north-1", "aws-eusc": "eusc-de-east-1"}


def synth(a, home, out, run=proc.run):
    ctx = {"project": a.project, "provider": a.provider, "home": home}
    if a.namespace:
        ctx["gitlabNamespace"] = a.namespace
    if a.github_subject:
        ctx["githubSubject"] = a.github_subject
    if a.github_environment:
        ctx["githubEnvironment"] = a.github_environment
    if a.dns_profile:
        ctx["dns"] = "true"
    # --no-install: the package's pinned CDK CLI, never one npx would fetch.
    cmd = ["npx", "--no-install", "cdk", "synth", "--quiet", "--no-notices", "--output", out] + \
        [x for k, v in ctx.items() for x in ("--context", f"{k}={v}")]
    r = run(cmd, SYNTH_TIMEOUT, cwd=CDK_APP)
    if not r.ok:
        # Synth calls no AWS API, so nothing changed: the input (or the toolchain) is refused.
        raise cfn.Failed(REFUSED, "cdk synth failed:\n" + (r.err or r.out).strip()[-2000:])
    # The stacks' names are the CDK app's: read them from what it synthesized, never rebuilt here.
    with open(os.path.join(out, "manifest.json")) as f:
        artifacts = json.load(f)["artifacts"]
    return {sid: (artifacts[sid]["properties"]["stackName"], os.path.join(out, f"{sid}.template.json"))
            for sid in ("Iam", "Dns") if sid in artifacts}


def identity(aws, home, a, what):
    who = aws.call("sts", "get-caller-identity", region=home)
    if not who.ok:
        raise cfn.Failed(CREDS if who.kind == "creds" else FAILED, f"Reading the {what} identity: {who.message}")
    account, partition = who.data["Account"], who.data["Arn"].split(":")[1]
    if a.provider == "gitlab" and partition != "aws":
        raise cfn.Failed(REFUSED, f"GitLab's credential vendor is in partition aws only; account {account} is in {partition}.")
    return account


def production(accounts, a, is_prod):
    """Every account's answer first, then every decision at once: one re-run approves them all."""
    needed = []
    for account in accounts:
        p = is_prod(account)
        if p.kind != "ok":
            raise cfn.Failed(CREDS if p.kind == "creds" else FAILED, p.why)
        if p.prod is not False and account not in a.allow_production:
            print("DECISION=" + json.dumps({"field": "allowProduction", "approve": account,
                                            "what": f"Account {account} is {'production' if p.prod else 'not classified'} ({p.why})"}))
            needed.append(account)
    if needed:
        raise cfn.Failed(REFUSED, "Needs " + " ".join(f"--allow-production {x}" for x in needed) + ".")


def setup(a, aws, say, is_prod=prodlib.is_prod, run=proc.run, dns_aws=None):
    home = CONTROL_PLANE[a.partition]
    out = tempfile.mkdtemp(prefix="cre-setup-cdk-")
    try:
        return deploy(a, aws, say, is_prod, synth(a, home, out, run=run), home, dns_aws)
    finally:
        shutil.rmtree(out, ignore_errors=True)


def deploy(a, aws, say, is_prod, stacks, home, dns_aws):
    """After synth, so bad input is refused before any AWS or Midway call."""
    say("✓  synthesized")
    account = identity(aws, home, a, "AWS")
    dns_account = identity(dns_aws, home, a, "DNS account") if dns_aws else ""
    if dns_account == account:
        raise cfn.Failed(REFUSED, f"--dns-profile is account {account}, the app's own: a zone there needs no DNS account. "
                                  "Leave --dns-profile out.")
    production([account] + ([dns_account] if dns_account else []), a, is_prod)
    say(f"ℹ  account {account}{f', DNS account {dns_account}' if dns_account else ''}, control plane {home}")
    stack, template = stacks["Iam"]
    cfn.deploy_stack(aws, stack, template, home, say)
    o = cfn.outputs(aws, stack, home)
    result = {"ACCOUNT_ID": account, "RUNNER_ROLE_ARN": o["RunnerRoleArn"], "CFN_EXECUTION_ROLE_ARN": o["CfnExecutionRoleArn"]}
    if dns_account:
        stack, template = stacks["Dns"]
        cfn.deploy_stack(dns_aws, stack, template, home, say, parameters={"AppAccountId": account})
        d = cfn.outputs(dns_aws, stack, home)
        result.update(DNS_ACCOUNT_ID=dns_account, DNS_ROLE_ARN=d["DnsDeployRoleArn"],
                      DNS_CFN_EXECUTION_ROLE_ARN=d["DnsExecRoleArn"])
    return result


def parse(argv):
    ap = argparse.ArgumentParser(prog="account.py")
    ap.add_argument("--project", required=True, action=SingleValue)
    ap.add_argument("--provider", required=True, choices=("gitlab", "github"), action=SingleValue)
    ap.add_argument("--partition", default="aws", choices=sorted(CONTROL_PLANE), action=SingleValue,
                    help="where the account is (its control-plane region)")
    ap.add_argument("--profile", default="", action=SingleValue)
    ap.add_argument("--namespace", default="", action=SingleValue, help="GitLab group path")
    ap.add_argument("--github-subject", default="", action=SingleValue,
                    help="the repo's OIDC subject prefix, as github.py setup printed it (OIDC_SUBJECT)")
    ap.add_argument("--github-repo", default="", action=SingleValue,
                    help="<owner>/<repo>: read its OIDC subject prefix from GitHub (gh's login) instead of --github-subject")
    ap.add_argument("--github-environment", default="", action=SingleValue,
                    help="GitHub: the deploy jobs' environment (a public repo's AWS); its subject is trusted instead of main's")
    ap.add_argument("--dns-profile", default="", action=SingleValue, help="the separate DNS account where the project's hosted zones are")
    ap.add_argument("--allow-production", action="append", default=[], metavar="ACCOUNT")
    a = ap.parse_args(argv)
    if a.github_subject and a.github_repo:
        ap.error("--github-subject or --github-repo, not both")
    return a   # the rest of the input is checked by the CDK app at synth, before anything changes


def main(argv=None, aws_factory=awslib.Aws, is_prod=prodlib.is_prod, run=proc.run, github_factory=None):
    t0 = time.time()

    def say(msg):
        print(f"[{time.strftime('%H:%M:%S')} +{time.time() - t0:.1f}s] {msg}", file=sys.stderr, flush=True)

    def done(code):
        print(f"RESULT code={code}")
        return code

    try:
        a = parse(argv)
    except SystemExit as e:
        if e.code in (0, None):
            raise
        return done(REFUSED)
    try:
        if a.github_repo:
            # The subject names the repo's and owner's IDs (immutable subjects): GitHub's, never built here.
            import github
            from github_api import GitHub
            a.github_subject = github.oidc_subject((github_factory or GitHub)(), a.github_repo)
            say(f"ℹ  {a.github_repo}'s OIDC subject: {a.github_subject}")
        outputs = setup(a, aws_factory(a.profile), say, is_prod=is_prod, run=run,
                        dns_aws=aws_factory(a.dns_profile) if a.dns_profile else None)
    except cfn.Failed as e:
        say(f"❌ {e}")
        return done(e.code)
    except Exception as e:  # noqa: BLE001: a bug is still 20, never a bare traceback
        say(f"❌ account.py failed: {type(e).__name__}: {e}")
        return done(FAILED)
    for k, v in outputs.items():
        print(f"{k}={v}")
    say("✅ done")
    return done(OK)


if __name__ == "__main__":
    sys.exit(main())
