#!/usr/bin/env python3
"""Prepare an AWS account to receive a CRE project's deploys.

  aws_account.py --profile <p> --project <name> --regions "<r1> [r2 ...]" --provider gitlab|github
                 (--namespace <gitlab/group/path> | --github-repo <owner>/<repo>)
                 [--runner-role <name>] [--exec-role <name>] [--allow-production[=<account>]]
                 [--update-runner-trust-policy[=<role>@<account>]] [--force] [--discovery] [--dry-run]

In a project (npx projen setup:aws), every flag but --profile defaults to cre-setup.json.

Does, idempotently:
  1. Checks the profile's credentials (exit 11 if missing/expired); a production account (crelib.is_prod)
     needs --allow-production.
  2. One assets bucket per region, `<project>-<region>`, always including us-east-1 (the edge stack):
     private (Block Public Access on), bucket-owner-enforced, SSE-S3, tagged project=<name>. A name
     owned by another account stops (S3 names are global).
  3. The CloudFormation execution role (default `CreCfnExecution`), which every stack deploys with
     as --role-arn. An existing role is only verified: AdministratorAccess attached and
     cloudformation.amazonaws.com trusted (exit 10 otherwise; it is never changed). A missing one
     is created that way.
  4. The CI runner role (default `GitLabRunner` on GitLab, `CreGitHubDeploy` on GitHub), which
     uploads assets to S3, pushes images to ECR and passes the execution role to CloudFormation.
       - GitLab trust: the AWS Credential Vendor jump role, for this repo's group and project, and
         only for jobs on a protected branch (`GitLab:Project:ProtectedBranch = Yes`).
       - GitHub trust: the token.actions.githubusercontent.com OIDC provider (created if absent),
         for exactly this repo's `main` subject, in the form GitHub reports for the repo
         (immutable subjects for new repos), so the repo must exist first (repo_github.py prepare).
     An existing role gets only this project added to its trust (the diff printed; it needs
     --update-runner-trust-policy). A missing role is created with that trust and one account-wide
     inline policy, `cre-runner`, which every project using the role shares. On a role setup created
     (its description says so) every run brings `cre-runner` up to date by adding only: the
     execution roles it already passes and statements it didn't write stay (review 10g C2), and
     MaxSessionDuration is raised to 2 h. Extra permissions belong in a policy of their own, never
     in `cre-runner`. A role you provided: its permissions are never touched, only checked
     read-only (iam simulate-principal-policy), and gaps are warned about.

Prints: ACCOUNT_ID, ASSETS_BUCKET_PREFIX, RUNNER_ROLE_ARN, CFN_EXECUTION_ROLE_ARN
(and RUNNER_PERMISSION_GAPS when an existing runner role may lack something).

--discovery changes nothing: it prints one DECISION=<json> line for each decision the run needs
(field, switch, what, change), read from the account as it is now.
"""
import json
import os
import re
import sys

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402
import policy  # noqa: E402

NAME = "aws-account"
DECISION_KEYS = ("allowProduction", "updateRunnerTrustPolicy")


def parse(step, argv):
    p = c.Parser(step, "aws_account.py")
    p.add_argument("--profile", default="", help="AWS profile for the deploy account (default: the default credentials)")
    p.add_argument("--project", default="", help="project name: names the assets buckets and the runner-role trust (default: cre-setup.json)")
    p.add_argument("--regions", default="", help='deploy regions, space-separated, e.g. "us-east-1 us-west-2"; us-east-1 is always added (default: cre-setup.json)')
    c.move_account_flag(p)
    p.add_argument("--provider", default="", help="gitlab or github: which CI trust the runner role gets (default: cre-setup.json)")
    p.add_argument("--namespace", default="", help="GitLab group path the project lives in (gitlab; default: cre-setup.json)")
    p.add_argument("--github-repo", dest="github_repo", default="",
                   help="<owner>/<repo> whose main branch may assume the runner role (github; default: cre-setup.json)")
    p.add_argument("--runner-role", dest="runner_role", default="",
                   help="CI runner role name (default GitLabRunner on GitLab, CreGitHubDeploy on GitHub); must differ from --exec-role")
    p.add_argument("--exec-role", dest="exec_role", default="",
                   help="CloudFormation execution role name (default CreCfnExecution); an existing one is only verified")
    c.common_flags(p)
    c.decision_flags(p, DECISION_KEYS)
    a = p.parse_args(argv)
    step.dry_run = a.dry_run
    c.take_decisions(step, a, DECISION_KEYS)
    sf = c.SetupFile(step)
    for attr, key in (("project", "project"), ("regions", "regions"), ("provider", "provider"),
                      ("namespace", "namespace"), ("github_repo", "githubRepo"),
                      ("runner_role", "runnerRole"), ("exec_role", "execRole")):
        sf.default(a, attr, key)
    sf.report()
    if isinstance(a.regions, list):
        a.regions = " ".join(a.regions)
    a.regions = a.regions.split()
    # In a project: also every region a stack in cre-stacks.json deploys to, so a stack added in a
    # new region gets its bucket from a plain `npx projen setup:aws` (check:deploy-prereqs says so).
    plan_file = os.path.join(sf.dir, "cre-stacks.json") if sf.dir else ""
    if plan_file and os.path.isfile(plan_file):
        try:
            planned = [s.get("region") for s in c.read_plan(plan_file) if s.get("region")]
        except c.BadFile as e:
            # Skipping its regions without a word left their buckets unmade (review 10f G2).
            step.die(c.REFUSED, f"{e}, then re-run (the regions its stacks deploy to get their buckets from it).")
        extra = [r for r in dict.fromkeys(planned) if r not in a.regions]
        if extra:
            step.info(f"cre-stacks.json also deploys to {' '.join(extra)}; adding {'it' if len(extra) == 1 else 'them'}.")
            a.regions += extra
    a.setup_file = sf
    a.exec_role = a.exec_role or c.EXEC_ROLE
    if not a.project:
        step.die(c.REFUSED, "--project is required.")
    if not a.regions:
        step.die(c.REFUSED, "--regions is required (space-separated).")
    if c.project_name_problem(a.project):
        step.die(c.REFUSED, f"Project name '{a.project}': {c.project_name_problem(a.project)}.")
    if a.provider == "gitlab" and c.partition_of(a.regions[0]) != "aws":
        step.die(c.REFUSED, f"GitLab projects deploy only in the aws partition (the GitLab credential vendor runs there), not "
                 f"{c.partition_of(a.regions[0])} (review 10c C5).")
    if a.provider == "gitlab":
        if not a.namespace:
            step.die(c.REFUSED, "--namespace is required for GitLab (e.g. aws-cre/resilience-demos).")
        a.runner_role = a.runner_role or c.RUNNER_ROLE["gitlab"]
    elif a.provider == "github":
        if not a.github_repo:
            step.die(c.REFUSED, "--github-repo <owner>/<repo> is required for GitHub.")
        a.runner_role = a.runner_role or c.RUNNER_ROLE["github"]
    else:
        step.die(c.REFUSED, "--provider must be gitlab or github.")
    if a.runner_role.lower() == a.exec_role.lower():
        # IAM names are case-insensitive. One role for both gave CI the admin execution role directly,
        # with no PassRole between them (review 10e C2).
        step.die(c.REFUSED, f"The runner role and the CloudFormation execution role are both {a.exec_role}. The runner role is "
                 "what CI assumes; the execution role has AdministratorAccess and is only passed to CloudFormation. Keep "
                 "them apart: drop runnerRole (the default is the provider's runner role) or name another role.")
    return a


def github_sub_prefix(step, repo, missing_ok=False):
    """The repo's OIDC subject prefix, from GitHub: new repos use immutable subjects
    (`repo:<owner>@<id>/<repo>@<id>`), which can't be built from the name. So the repo must exist.
    missing_ok (discovery, before the repo is made): None when it can't be read."""
    c.require_tools(step, "gh")
    c.require_gh(step)
    r = c.gh("api", f"repos/{repo}/actions/oidc/customization/sub")
    if r.returncode != 0:
        out = r.stderr + r.stdout
        if "SAML" in out or "HTTP 401" in out or "Bad credentials" in out:
            # The login, not a missing repo: 11 with how to fix it (review 10d C11).
            c.gh_fail(step, c.CREDENTIALS, f"reading {repo}'s OIDC subject settings", out, repo.split("/")[0])
        if missing_ok:
            return None
        step.die(c.REFUSED, f"Could not read {repo}'s OIDC subject settings: {c.last_line(r.stderr or r.stdout)}. "
                 f"Create the repo first (repo_github.py prepare --repo {repo}), then re-run.")
    try:
        d = json.loads(r.stdout)
    except ValueError:
        step.die(c.FAILED, f"GitHub returned an unreadable OIDC subject setting for {repo}: {r.stdout[:200]}")
    if d.get("use_default") is False:
        step.die(c.REFUSED, f"{repo} uses a custom OIDC subject template; the deploy role trusts only the default subject. "
                 f"Reset it: gh api -X PUT repos/{repo}/actions/oidc/customization/sub -F use_default=true")
    prefix = d.get("sub_claim_prefix") or f"repo:{repo}"
    step.info(f"GitHub OIDC subject for {repo}: {prefix}:ref:refs/heads/main")
    return prefix


def check_regions(step, aws, regions):
    """Refuse a region this account can't use before anything is made: one that doesn't exist (a
    typo of the right shape, `us-esat-2`) or an opt-in region not enabled here. Without it the
    bucket step failed part way with an error that read like bad credentials (review 10c A9).
    Read-only; when the account can't be asked (no ec2:DescribeRegions), it warns and goes on."""
    # Asked in the partition's IAM control-plane region, which every account has enabled.
    home = c.IAM_CONTROL_PLANE.get(getattr(aws, "partition", "") or "aws", "us-east-1")
    r = aws.call("ec2", "describe-regions", "--all-regions", "--region", home,
                 "--query", "Regions[].[RegionName,OptInStatus]", "--output", "text")
    if r.returncode != 0:
        step.warn(f"Could not list this account's regions ({c.last_line(r.stderr)}); not checking {' '.join(regions)}.")
        return
    status = dict(line.split("\t", 1) for line in r.stdout.strip().splitlines() if "\t" in line)
    unknown = [x for x in regions if x not in status]
    if unknown:
        hints = []
        for x in unknown:
            near = c.nearest(x, status)
            hints.append(f"{x}{f' (did you mean {near}?)' if near else ''}")
        step.die(c.REFUSED, f"Not a region of this account's partition: {', '.join(hints)}. Fix the region in --regions or the "
                 "stack's region prop, then re-run.")
    # Being enabled or disabled takes minutes to hours, and the bucket step failed part way until it
    # settled: wait, rather than fail later at create-bucket (review 10f C7).
    changing = [f"{x} ({status[x].strip()})" for x in regions if status[x].strip() in ("opting-in", "opting-out")]
    if changing:
        step.die(c.REFUSED, f"Region(s) {', '.join(changing)} are still changing their opt-in status in account {aws.account}. "
                 "Wait until the AWS console (Account settings, AWS Regions) shows them Enabled, then re-run.")
    off = [x for x in regions if status[x].strip() == "not-opted-in"]
    if off:
        step.die(c.REFUSED, f"Opt-in region(s) not enabled in account {aws.account}: {' '.join(off)}. Enable them (AWS console, "
                 "Account settings, AWS Regions; an account administrator's change), or deploy elsewhere, then re-run.")


def create_args(b, region):
    args = ["s3api", "create-bucket", "--bucket", b, "--region", region]
    return args + (["--create-bucket-configuration", f"LocationConstraint={region}"] if region != "us-east-1" else [])


def ensure_buckets(step, aws, project, regions, old_account=""):
    # Plus the partition's control-plane region(s) whatever the project's regions: the IAM and DNS
    # stacks deploy there (and in `aws`, the edge stack: us-east-1), and need their templates and
    # Lambda code in a bucket in that region.
    bucket_regions = list(regions)
    for cp in c.control_plane_regions(getattr(aws, "partition", "") or "aws"):
        if cp not in bucket_regions:
            bucket_regions.append(cp)
            step.info(f"Also preparing a {cp} bucket: the partition's IAM/DNS stacks (and the edge, in us-east-1) deploy there.")
    for region in bucket_regions:
        b = f"{project}-{region}"
        r = aws.call("s3api", "head-bucket", "--bucket", b, "--expected-bucket-owner", aws.account)
        err = r.stderr + r.stdout
        if r.returncode == 0:
            step.ok(f"Bucket {b} exists in this account.")
        elif re.search(r"\b(404|Not Found|NoSuchBucket)\b", err):
            res = step.apply(f"create bucket {b} in {region}", aws.call, *create_args(b, region))
            if res is not None and res.returncode != 0:
                step.die(c.FAILED, f"create-bucket {b} failed: {c.last_line(res.stderr)}")
        elif re.search(r"\b(403|Forbidden|AccessDenied)\b", err):
            # A HEAD has no error body: the 403 is another account's bucket, or this role may not read
            # buckets (no s3:ListBucket, an SCP denying S3 in the region), and all read as "name taken"
            # (review 10f C6). create-bucket, what setup does next anyway, answers which.
            if step.dry_run:
                step.die(c.REFUSED, f"Bucket {b}: S3 answered 403 to head-bucket, so either another account owns that name, or "
                         "this role may not read buckets (s3:ListBucket, or an SCP denying S3 there). A real run tells which.")
            res = step.apply(f"create bucket {b} in {region}", aws.call, *create_args(b, region))
            why = (res.stderr + res.stdout) if res is not None else ""
            if res is not None and res.returncode != 0:
                if "BucketAlreadyExists" in why:
                    old = old_account
                    if old:
                        # The move's own old buckets, most likely: teardown keeps them (review 10g C5).
                        step.die(c.REFUSED, f"Bucket name {b} is taken by another account (S3 names are global), most likely {old}, "
                                 f"which this project is moving from: delete it there (aws s3 rb s3://{b} --force, with {old}'s "
                                 "credentials), then re-run. If it isn't the old account's, pick a different project name.")
                    step.die(c.REFUSED, f"Bucket name {b} is already taken by another account (S3 names are global). Pick a different "
                             "project name.")
                if "BucketAlreadyOwnedByYou" in why:
                    step.die(c.FAILED, f"Bucket {b} is this account's, but this role may not read it (head-bucket: 403). It needs "
                             "s3:ListBucket on it, or an SCP or the bucket policy denies it. Fix the permission, then re-run.")
                step.die(c.FAILED, f"S3 refused both reading and creating {b}: {c.last_line(why)}. A service control policy or "
                         "this role's permissions deny S3 here; fix that (or use another --profile), then re-run.")
        else:
            step.die(c.FAILED, f"head-bucket {b} failed: {err.strip()}")
        # Re-applied every run: cheap, and it repairs a bucket created by hand without them. Encryption
        # and tags only add to what is there: an existing default (a KMS key someone set) and tags
        # from other tooling are kept (review 10b C19).
        settings = [
            (f"block public access on {b}", ["put-public-access-block", "--public-access-block-configuration",
                                             "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"]),
            (f"bucket-owner-enforced ownership on {b}", ["put-bucket-ownership-controls", "--ownership-controls",
                                                         "Rules=[{ObjectOwnership=BucketOwnerEnforced}]"]),
        ]
        enc = aws.call("s3api", "get-bucket-encryption", "--bucket", b)
        if enc.returncode != 0 and "ServerSideEncryptionConfigurationNotFound" in enc.stderr:
            settings.append((f"default SSE-S3 encryption on {b}", ["put-bucket-encryption", "--server-side-encryption-configuration",
                                                                  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}']))
        tags = aws.json("s3api", "get-bucket-tagging", "--bucket", b, "--query", "TagSet", "--output", "json") or []
        if {"Key": "project", "Value": project} not in tags:
            merged = [t for t in tags if t.get("Key") != "project"] + [{"Key": "project", "Value": project}]
            settings.append((f"tag {b} project={project}", ["put-bucket-tagging", "--tagging", json.dumps({"TagSet": merged})]))
        for what, args in settings:
            res = step.apply(what, aws.call, "s3api", args[0], "--bucket", b, *args[1:])
            if res is not None and res.returncode != 0:
                step.die(c.FAILED, f"{what} failed: {c.last_line(res.stderr)}")


EXEC_DESCRIPTION = "CloudFormation service role for CRE project deploys"   # marks an execution role setup created


# One copy of each IAM change's apply-then-die-on-failure (review 10f G3): the two roles repeated them.
def apply_iam(step, aws, label, failed, *args):
    """step.apply of `aws iam <args>`; dies FAILED naming `failed` when IAM refuses. None in a dry run."""
    res = step.apply(label, aws.call, "iam", *args)
    if res is not None and res.returncode != 0:
        step.die(c.FAILED, f"{failed} failed: {c.last_line(res.stderr)}")
    return res


def create_role(step, aws, label, name, trust_file, description, *extra):
    """The new role's ARN as IAM answers it; "" in a dry run."""
    res = apply_iam(step, aws, label, f"create-role {name}", "create-role", "--role-name", name, "--assume-role-policy-document",
                    f"file://{trust_file}", "--description", description, *extra, "--query", "Role.Arn", "--output", "text")
    return res.stdout.strip() if res is not None else ""


def admin_arn(aws):
    return f"arn:{aws.partition}:iam::aws:policy/AdministratorAccess"


def attached_policies(aws, name):
    return (aws.text("iam", "list-attached-role-policies", "--role-name", name, "--query",
                     "AttachedPolicies[].PolicyArn", "--output", "text") or "").split()


def ensure_exec_role(step, aws, tmp, name):
    """Provided (exists): only verified, never changed. Missing: created. The ARN is always IAM's
    answer; in a dry run of a role that doesn't exist yet it is empty."""
    arn = aws.role_arn(name)
    if arn:
        trust = aws.json("iam", "get-role", "--role-name", name, "--query", "Role.AssumeRolePolicyDocument", "--output", "json")
        if trust is None:
            step.die(c.FAILED, f"get-role {name} failed.")
        attached = attached_policies(aws, name)
        admin = admin_arn(aws)
        owned = aws.text("iam", "get-role", "--role-name", name, "--query", "Role.Description", "--output", "text") == EXEC_DESCRIPTION
        if admin not in attached and owned:
            # Setup made it and stopped before the attach (review 10b C15): finish what it started.
            apply_iam(step, aws, f"attach AdministratorAccess to {name} (setup created it; the attach didn't finish)",
                      f"attach-role-policy on {name}", "attach-role-policy", "--role-name", name, "--policy-arn", admin)
            attached.append(admin)
        if admin not in attached:
            step.die(c.REFUSED, f"Execution role {name} exists but doesn't have AdministratorAccess attached. CloudFormation deploys every\n"
                     "   resource with it, so it needs admin. Attach it, or pass --exec-role <another role> (or a new name to create one).")
        if "cloudformation.amazonaws.com" not in json.dumps(trust):
            step.die(c.REFUSED, f"Execution role {name} doesn't trust cloudformation.amazonaws.com, so CloudFormation can't use it as --role-arn.\n"
                     "   Add that service to its trust policy, or pass --exec-role <another role>.")
        step.ok(f"Execution role {name} exists, has AdministratorAccess and trusts CloudFormation.")
        return arn
    doc = os.path.join(tmp, "cfn-trust.json")
    c.write_json(doc, policy.cfn_trust())
    arn = create_role(step, aws, f"create role {name} (trusted by cloudformation.amazonaws.com)", name, doc, EXEC_DESCRIPTION)
    if not arn:
        return ""
    apply_iam(step, aws, f"attach AdministratorAccess to {name}", f"attach-role-policy on {name}",
              "attach-role-policy", "--role-name", name, "--policy-arn", admin_arn(aws))
    return arn


def find_github_oidc_provider(aws):
    """The GitHub OIDC provider's ARN as IAM lists it, "" when there is none. Read-only: the run and
    --discovery both look it up here (review 10g G4)."""
    listed = aws.text("iam", "list-open-id-connect-providers", "--query", "OpenIDConnectProviderList[].Arn", "--output", "text") or ""
    return next((x for x in listed.split() if x.endswith(f"/{policy.GITHUB_OIDC_HOST}")), "")


def assumed_github_oidc_provider(aws):
    """The ARN the provider gets when created: for a dry run or discovery before it exists."""
    return f"arn:{aws.partition}:iam::{aws.account}:oidc-provider/{policy.GITHUB_OIDC_HOST}"


def github_oidc_provider(step, aws):
    """The provider's ARN as IAM lists it (or as create returns it), never assembled."""
    found = find_github_oidc_provider(aws)
    if found:
        step.ok("GitHub OIDC provider exists.")
        return found
    res = step.apply("create the GitHub Actions OIDC provider", aws.call, "iam", "create-open-id-connect-provider",
                     "--url", f"https://{policy.GITHUB_OIDC_HOST}", "--client-id-list", "sts.amazonaws.com",
                     "--query", "OpenIDConnectProviderArn", "--output", "text")
    if res is None:
        return assumed_github_oidc_provider(aws)
    if res.returncode != 0:
        step.die(c.FAILED, f"create-open-id-connect-provider failed: {c.last_line(res.stderr)}")
    return res.stdout.strip()


RUNNER_DESCRIPTION = "CI deploy role for CRE projects"   # marks a runner role setup created (and so owns)
# How long a deploy job's credentials may last: a CloudFront or certificate stack can take longer
# than the 1 h default (review 10b D3). The GitHub workflow asks for this only when the role allows it.
RUNNER_SESSION_SECONDS = 7200


def runner_session(step, aws, name):
    """The runner role's maximum session (seconds), raised to RUNNER_SESSION_SECONDS on a role setup
    created. A provided role is its owner's: only read."""
    raw = aws.text("iam", "get-role", "--role-name", name, "--query", "Role.MaxSessionDuration", "--output", "text")
    try:
        have = int(raw or 3600)
    except ValueError:
        have = 3600
    owned = aws.text("iam", "get-role", "--role-name", name, "--query", "Role.Description", "--output", "text") == RUNNER_DESCRIPTION
    if have >= RUNNER_SESSION_SECONDS or not owned:
        if have < RUNNER_SESSION_SECONDS:
            step.warn(f"{name} allows {have // 60} min sessions, so a deploy job's credentials expire then (a CloudFront stack can "
                      f"take longer). Its owner can raise it: aws iam update-role --role-name {name} --max-session-duration "
                      f"{RUNNER_SESSION_SECONDS}")
        return have
    res = apply_iam(step, aws, f"allow {RUNNER_SESSION_SECONDS // 3600} h sessions on {name}", f"update-role {name}",
                    "update-role", "--role-name", name, "--max-session-duration", str(RUNNER_SESSION_SECONDS))
    return RUNNER_SESSION_SECONDS if res is not None else have
TRUST_QUOTA_CODE = c.TRUST_QUOTA_CODE
check_trust_size = c.check_trust_size   # shared with dns.py (review 10b C9)


def repair_runner_policy(step, aws, tmp, name, exec_role_arn):
    """A runner role setup created has the account-wide `cre-runner` inline policy. One that lost it,
    or differs from this template's, gets this template's statements, the change printed; it only
    ever adds (policy.merge_runner_policy): every execution role it already passes, and any statement
    the template doesn't write, stay (the role is shared). A role setup didn't create (no
    RUNNER_DESCRIPTION) is its owner's: only checked, never changed."""
    desc = aws.text("iam", "get-role", "--role-name", name, "--query", "Role.Description", "--output", "text")
    if desc != RUNNER_DESCRIPTION:
        return
    r = aws.call("iam", "get-role-policy", "--role-name", name, "--policy-name", "cre-runner", "--query", "PolicyDocument", "--output", "json")
    have = None
    if r.returncode == 0:
        try:
            have = json.loads(r.stdout)
        except ValueError:
            have = None
    elif "NoSuchEntity" not in r.stderr:
        step.die(c.FAILED, f"get-role-policy cre-runner on {name} failed: {c.last_line(r.stderr)}")
    want = policy.merge_runner_policy(have, aws.account, exec_role_arn)
    if have == want:
        return
    # Setup owns this policy (it made the role and wrote it): brought to this template's version
    # without a decision, because the merge only adds (nothing another project relies on goes).
    step.info(f"{name} (created by setup) {'has no' if have is None else 'needs additions to its'} cre-runner policy "
              "(only additions: every execution role and statement it has stay); putting it on:")
    print(policy.diff(have or {}, want), file=sys.stderr)
    pol = os.path.join(tmp, "runner-repair.json")
    c.write_json(pol, want)
    apply_iam(step, aws, f"put inline policy cre-runner on {name}", f"put-role-policy cre-runner on {name}",
              "put-role-policy", "--role-name", name, "--policy-name", "cre-runner", "--policy-document", f"file://{pol}")


def runner_trust_change(step, aws, a, provider_arn, sub_prefix):
    """Read-only: (arn, current trust, changed, proposed) for the runner role ("" / None when it
    doesn't exist yet), with the warnings about what it already trusts. An existing role whose trust
    a CloudFormation stack manages is refused when it would change."""
    name = a.runner_role
    arn = aws.role_arn(name)
    current = None
    if arn:
        current = aws.json("iam", "get-role", "--role-name", name, "--query", "Role.AssumeRolePolicyDocument", "--output", "json")
        if current is None:
            step.die(c.FAILED, f"get-role {name} failed.")
        step.info(f"Runner role {name} exists; only its trust policy is checked and, if needed, changed.")
    if a.provider == "gitlab":
        changed, proposed = policy.merge_gitlab_trust(current, a.namespace, a.project)
        exposed = policy.unguarded_projects(current, a.namespace)
        if exposed:
            # Not changed: other projects' pipelines might depend on it. Its owner decides.
            step.warn(f"{name}'s trust also has a statement for {a.namespace} without the protected-branch condition "
                      f"(aws:PrincipalTag/GitLab:Project:ProtectedBranch = Yes), so any branch's job of {', '.join(exposed)} "
                      f"can assume it. {a.project} is trusted only from protected branches. Ask the role's owner to add the "
                      "condition to that statement once those projects deploy only from a protected main.")
            step.out("UNGUARDED_RUNNER_TRUST", " ".join(exposed))
    else:
        changed, proposed = policy.merge_github_trust(current, provider_arn, a.github_repo, sub_prefix)
        open_stmts = policy.unconditioned_github_statements(current, provider_arn)
        if open_stmts:
            # Kept as they are (they may be someone's), but they let any GitHub repository take this role.
            step.warn(f"{name}'s trust has statement(s) {', '.join(open_stmts)} for GitHub's OIDC provider with no `sub` condition: "
                      "ANY repository on GitHub can assume this role through them. Its owner should add a sub condition or remove them.")
            step.out("UNGUARDED_RUNNER_TRUST", ",".join(open_stmts))
    if changed:
        check_trust_size(step, aws, name, proposed)
    if current is not None and changed:
        # A role a CloudFormation stack made: that stack's next update puts its own trust back, and
        # CI loses its credentials some day later, far from this run (review 10f C7). The trust
        # belongs in that stack's template.
        tags = aws.json("iam", "list-role-tags", "--role-name", name, "--query", "Tags", "--output", "json")
        stack = next((t.get("Value") for t in (tags or []) if t.get("Key") in ("aws:cloudformation:stack-name",
                                                                                "aws:cloudformation:stack-id")), "")
        if stack:
            step.die(c.REFUSED, f"{name}'s trust is managed by the CloudFormation stack {stack}: a change made here would be undone "
                     f"by that stack's next update. Add {a.project}'s trust in that stack's template (the change is printed "
                     "below), or give this project its own runner role (runnerRole in the answers, or --runner-role).\n"
                     + policy.diff(current, proposed))
        if tags is None:
            step.warn(f"Could not read {name}'s tags (iam:ListRoleTags), so whether a CloudFormation stack manages its trust "
                      "isn't known.")
    return arn, current, changed, proposed


def decide_runner_trust(step, aws, a, current, proposed):
    """The decision to change an existing runner role's trust policy (other projects share it)."""
    name = a.runner_role
    admin = admin_arn(aws) in attached_policies(aws, name)
    if admin:
        # A deliberate choice is possible (a sandbox account), but it must be seen as one (review 10e C2).
        step.warn(f"{name} has AdministratorAccess: with this trust, CI jobs of {a.project} get admin credentials directly.")
    return step.decide("updateRunnerTrustPolicy", f"{name}@{aws.account}",
                       f"Adding {a.project} to the trust policy of runner role {name}, which other projects share"
                       + (" (it has AdministratorAccess: CI would get admin)" if admin else ""),
                       policy.diff(current, proposed))


def ensure_runner_role(step, aws, tmp, a, exec_role_arn, provider_arn, sub_prefix):
    """Provided (exists): only its trust policy changes (this repo added, a decision); its
    permissions are the owner's, checked and warned about. Missing: created with the trust and the
    account-wide `cre-runner` policy (no per-project policies)."""
    name = a.runner_role
    arn, current, changed, proposed = runner_trust_change(step, aws, a, provider_arn, sub_prefix)
    trust_file = os.path.join(tmp, "trust-new.json")
    c.write_json(trust_file, proposed)
    if current is None:
        arn = create_role(step, aws, f"create runner role {name}", name, trust_file, RUNNER_DESCRIPTION,
                          "--max-session-duration", str(RUNNER_SESSION_SECONDS))
        if not arn:
            return ""
        pol = os.path.join(tmp, "runner.json")
        c.write_json(pol, policy.runner_policy(aws.account, exec_role_arn))
        apply_iam(step, aws, f"put inline policy cre-runner on {name} (S3, ECR, CloudFormation, pass {a.exec_role})",
                  f"put-role-policy cre-runner on {name}", "put-role-policy", "--role-name", name, "--policy-name", "cre-runner",
                  "--policy-document", f"file://{pol}")
        return arn
    if changed:
        step.info(f"Proposed trust policy change for {name} (shared by every project that uses it):")
        if decide_runner_trust(step, aws, a, current, proposed):
            apply_iam(step, aws, f"update trust policy of {name}", f"update-assume-role-policy on {name}",
                      "update-assume-role-policy", "--role-name", name, "--policy-document", f"file://{trust_file}")
    else:
        step.ok(f"{name} already trusts this project.")
    repair_runner_policy(step, aws, tmp, name, exec_role_arn)
    # Read-only check of what the deploy needs; a gap is the role owner's to fix, so warn, don't change.
    p, r0 = aws.partition, a.regions[0]
    bucket = f"arn:{p}:s3:::{a.project}-{r0}"
    missing = []
    if not aws.can(arn, "s3:PutObject", f"{bucket}/x"):
        missing.append(f"s3:PutObject({bucket}/*)")
    if not aws.can(arn, "s3:ListBucket", bucket):
        missing.append(f"s3:ListBucket({bucket})")
    if not aws.can(arn, "ecr:PutImage", f"arn:{p}:ecr:{r0}:{aws.account}:repository/x"):
        missing.append("ecr:PutImage")
    if not aws.can(arn, "cloudformation:CreateChangeSet", f"arn:{p}:cloudformation:{r0}:{aws.account}:stack/{a.project}/x"):
        missing.append("cloudformation:CreateChangeSet")
    if exec_role_arn and not aws.can(arn, "iam:PassRole", exec_role_arn, "cloudformation.amazonaws.com"):
        missing.append(f"iam:PassRole({exec_role_arn})")
    if missing:
        step.warn(f"{name} may not allow: {' '.join(missing)}. CI deploys will fail until the role's owner grants these.")
        step.out("RUNNER_PERMISSION_GAPS", " ".join(missing))
        # A person must act (the role's owner), and the same command re-checks: exit 13, not a
        # green 0 that the first deploy disproves (review 10c C6).
        step.need("RUNNER_PERMISSIONS")
    else:
        step.ok(f"{name} can upload assets, push images, deploy stacks and pass {a.exec_role}.")
    return arn


def discover(step, aws, a):
    """--discovery: the decisions this run needs, read-only (the production one was met in
    aws.require()). Before the repo exists, a GitHub repo's OIDC subject and the OIDC provider are
    simulated: a new repo is never in the trust yet, so the decision is the same."""
    provider_arn = sub_prefix = None
    if a.provider == "github":
        provider_arn = find_github_oidc_provider(aws) or assumed_github_oidc_provider(aws)
        sub_prefix = github_sub_prefix(step, a.github_repo, missing_ok=True)
        if sub_prefix is None:
            # Simulated, and said so: the real run reads the subject GitHub gives the new repo, the
            # immutable repo:<owner>@<id>/<repo>@<id> form, never this one (review 10g C6).
            sub_prefix = f"repo:{a.github_repo}"
            step.info(f"{a.github_repo} doesn't exist yet: the trust shown uses {sub_prefix} as its OIDC subject. The real run "
                      "uses the subject GitHub gives the new repo (repo:<owner>@<id>/<repo>@<id>); the decision is the same.")
    arn, current, changed, proposed = runner_trust_change(step, aws, a, provider_arn, sub_prefix)
    subject = f"{a.runner_role}@{aws.account}"
    if current is None:
        step.check("updateRunnerTrustPolicy", subject, "create", f"Runner role {a.runner_role} doesn't exist in {aws.account}: setup "
                   f"creates it, trusting only {a.project}. No shared role changes.")
    elif not changed:
        step.check("updateRunnerTrustPolicy", subject, "in-place", f"Runner role {a.runner_role} already trusts {a.project}: no IAM change.")
    else:
        decide_runner_trust(step, aws, a, current, proposed)
    step.ok(f"Discovery for {a.project} in {aws.account}: {len(step.found)} decision(s); nothing was changed.")


def main(step, argv):
    a = parse(step, argv)
    aws = c.Aws(step, a.profile).require()
    c.check_account(step, aws, a.setup_file, a.move_account)
    if step.discovery:
        discover(step, aws, a)
        return
    tmp = c.tmpdir(step)
    sub_prefix = provider_arn = None
    if a.provider == "github":
        sub_prefix = github_sub_prefix(step, a.github_repo)
    check_regions(step, aws, a.regions)
    # The account a --move-account leaves, whose buckets hold the names (review 10g C5).
    # new_project names it in CRE_MOVED_FROM (its child has no cre-setup.json to read yet).
    old = os.environ.get("CRE_MOVED_FROM", "") or \
        ((a.setup_file.data or {}).get("accountId", "") if a.move_account and a.setup_file is not None else "")
    ensure_buckets(step, aws, a.project, a.regions, old_account=old if old != aws.account else "")
    exec_arn = ensure_exec_role(step, aws, tmp, a.exec_role)
    if a.provider == "github":
        provider_arn = github_oidc_provider(step, aws)
    runner_arn = ensure_runner_role(step, aws, tmp, a, exec_arn, provider_arn, sub_prefix)
    session = runner_session(step, aws, a.runner_role) if runner_arn else 3600
    if not step.dry_run:
        a.setup_file.set("regions", " ".join(a.regions))   # what check:deploy-prereqs reads
        if a.move_account:
            a.setup_file.set("accountId", aws.account)
        a.setup_file.set("runnerSessionSeconds", session)   # what the GitHub deploy workflow asks for
    step.out("RUNNER_SESSION_SECONDS", session)
    step.out("ACCOUNT_ID", aws.account)
    step.out("ASSETS_BUCKET_PREFIX", a.project)
    step.out("RUNNER_ROLE_ARN", runner_arn)
    step.out("CFN_EXECUTION_ROLE_ARN", exec_arn)
    if step.dry_run:
        # Nothing was changed: "is ready" read as done (review 10g C6).
        step.ok(f"Dry run for {a.project} in {aws.account}: the changes above are what a real run would make; nothing was changed.")
    else:
        step.ok(f"Account {aws.account} is ready for {a.project}.")


if __name__ == "__main__":
    c.cli(NAME, main)
