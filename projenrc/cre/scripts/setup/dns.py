#!/usr/bin/env python3
"""External DNS for a CRE project: a Route 53 zone the demo writes records (and ACM validation
records) into, in the deploy account or another account.

  dns.py --profile <app-profile> --project <name> --regions "<r1> ..." [--domain <fqdn>]
         (--zone-id <Z...> | --supernova people --alias <alias> | --supernova cre --zone-name <name>)
         [--dns-profile <zone-account-profile>] [--runner-role <name>] [--provider gitlab|github]
         [--bindle <id>] [--allow-production[=<account>]] [--update-dns-role-trust-policy[=CreDnsDeploy@<account>]]
         [--force] [--discovery] [--dry-run]

The runner role defaults to the provider's (GitLabRunner on GitLab, CreGitHubDeploy on GitHub;
--provider, default gitlab). In a project (npx projen setup:dns) --project, --regions, --provider,
--runner-role, --zone-id and --domain default to cre-setup.json.

Zone in the deploy account (no --dns-profile, or one for the same account): checks the zone and
that the domain is inside it (default <project>.<zone>). Prints DOMAIN and HOSTED_ZONE_ID.

Zone in another account (--dns-profile for it):
  - zone account: role `CreDnsDeploy`, assumable only by the deploy account's runner role and this
    project's certificate-validator roles (`<project>-acm-<region>`, by exact ARN), allowed to deploy
    `<project>-dns` and write records in this one zone; and `CreDnsCfnExecution`, the CloudFormation
    role there, limited to the zone. One inline policy per project on each.
  - deploy account: the assets buckets let the zone account read this project's templates. The
    runner role is not changed: it is checked (read-only) for sts:AssumeRole on CreDnsDeploy, which
    a runner role created by aws_account.py already has.
  Prints DOMAIN, HOSTED_ZONE_ID, DNS_ROLE_ARN, DNS_CFN_EXECUTION_ROLE_ARN. An existing CreDnsDeploy
  whose trust policy would change (other projects share it) needs --update-dns-role-trust-policy.

No zone yet (--supernova): creates the public hosted zone and the `Nova-DO-NOT-DELETE` role
SuperNova needs (trusted by nova.aws.internal; AmazonRoute53FullAccess + SecurityAudit), then
prints what to submit at https://supernova.amazon.dev, with DELEGATION=pending and NEEDS=DELEGATION
(exit 13). Re-run with --zone-id once delegation completes.

A production account (crelib.is_prod), deploy or zone account, needs --allow-production.
--discovery changes nothing: it prints one DECISION=<json> line per decision the run needs; before the
runner role exists, its ARN is simulated (the name at IAM's default path). --force makes every decision.

Never deletes a hosted zone: deleting a delegated zone by hand leaves the domain open to sniping.
"""
import json
import os
import shutil
import sys
import time

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402
import policy  # noqa: E402

NAME = "dns"
DNS_ROLE, DNS_EXEC, NOVA_ROLE = "CreDnsDeploy", "CreDnsCfnExecution", "Nova-DO-NOT-DELETE"
DECISION_KEYS = ("allowProduction", "updateDnsRoleTrustPolicy")


def parse(step, argv):
    p = c.Parser(step, "dns.py")
    for flag, text in (
            ("profile", "AWS profile for the deploy account (default: the default credentials)"),
            ("dns-profile", "AWS profile for the zone's account, when it isn't the deploy account"),
            ("project", "project name (default: cre-setup.json)"),
            ("regions", 'deploy regions, space-separated, e.g. "us-east-1 us-west-2" (default: cre-setup.json)'),
            ("domain", "the demo's domain, inside the zone (default: <project>.<zone>, or cre-setup.json)"),
            ("zone-id", "an existing Route 53 hosted zone id, Z... (default: cre-setup.json)"),
            ("supernova", "no zone yet: people or cre, the SuperNova domain to create a zone under"),
            ("alias", "--supernova people: your Amazon alias, for <alias>.people.aws.dev"),
            ("zone-name", "--supernova cre: the zone to create, <sub>.<cre org domain>"),
            ("runner-role", "the CI runner role (default GitLabRunner on GitLab, CreGitHubDeploy on GitHub)"),
            ("provider", "gitlab or github: picks the default --runner-role (default gitlab)")):
        p.add_argument(f"--{flag}", dest=flag.replace("-", "_"), default="", help=text)
    p.add_argument("--bindle", default="", help="--supernova: the owning team's Bindle id for the SuperNova form (default: cre-setup.json's bindleId, else $BINDLE_ID, else the aws-cre bindle)")
    c.move_account_flag(p)
    c.common_flags(p)
    c.decision_flags(p, DECISION_KEYS)
    a = p.parse_args(argv)
    step.dry_run = a.dry_run
    c.take_decisions(step, a, DECISION_KEYS)
    sf = c.SetupFile(step)
    for attr, key in (("project", "project"), ("regions", "regions"), ("provider", "provider"),
                      ("runner_role", "runnerRole")):
        sf.default(a, attr, key)
    # The project's own bindle (cre-setup.json) before $BINDLE_ID and the aws-cre default: the
    # SuperNova form names the bindle that owns the zone (review 10d C11).
    sf.default(a, "bindle", "bindleId")
    a.bindle = a.bindle or os.environ.get("BINDLE_ID") or c.DEFAULT_BINDLE
    if not a.supernova:
        sf.default(a, "zone_id", "hostedZoneId")
        sf.default(a, "domain", "domain")
    sf.report()
    a.setup_file = sf
    if isinstance(a.regions, list):
        a.regions = " ".join(a.regions)
    a.regions = a.regions.split()
    # DNS names are case-insensitive: compared with the zone's name as text below (review 10e C8).
    a.domain = a.domain.rstrip(".").lower()
    a.zone_name = a.zone_name.rstrip(".").lower()
    a.zone_id = a.zone_id.replace("/hostedzone/", "")
    provider = a.provider or "gitlab"
    if provider == "gitlab":
        a.runner_role = a.runner_role or c.RUNNER_ROLE["gitlab"]
    elif provider == "github":
        a.runner_role = a.runner_role or c.RUNNER_ROLE["github"]
    else:
        step.die(c.REFUSED, "--provider must be gitlab or github.")
    if not a.project or not a.regions:
        step.die(c.REFUSED, "--project and --regions are required.")
    if not a.zone_id and not a.supernova:
        step.die(c.REFUSED, "Give --zone-id, or --supernova people|cre to set up a new zone.")
    # The regions a stack can deploy to here: the project's, plus the partition's control-plane
    # region(s), where the DNS stack itself and the edge's certificate are (review 10b C8).
    a.deploy_regions = list(dict.fromkeys(a.regions + c.control_plane_regions(c.partition_of(a.regions[0]))))
    return a


def supernova(step, zone_aws, a):
    # Delegation is read from public DNS with dig: without it, it never reads as done, and every
    # re-run printed the SuperNova form again (review 10e C8).
    if shutil.which("dig") is None:
        step.die(c.REFUSED, "dig isn't installed: the delegation is read from public DNS with it. macOS has it; on Linux install "
                 "bind-utils (dnf/yum) or dnsutils (apt). Or, once SuperNova has delegated the zone, re-run with --zone-id <its id>.")
    if a.supernova == "people":
        if not a.alias:
            step.die(c.REFUSED, "--supernova people needs --alias <your alias>.")
        zone_name, org = f"{a.alias}.people.aws.dev", "People"
    elif a.supernova == "cre":
        if not a.zone_name:
            step.die(c.REFUSED, "--supernova cre needs --zone-name <sub>.<cre org domain> (check the org's domain on supernova.amazon.dev).")
        zone_name, org = a.zone_name, "cre"
    else:
        step.die(c.REFUSED, "--supernova must be people or cre.")
    existing = zone_aws.text("route53", "list-hosted-zones-by-name", "--dns-name", f"{zone_name}.", "--max-items", "1",
                             "--query", f"HostedZones[?Name=='{zone_name}.' && Config.PrivateZone==`false`].Id | [0]",
                             "--output", "text")
    if existing and existing != "None":
        zone_id = existing.replace("/hostedzone/", "")
        step.ok(f"Public hosted zone {zone_name} exists ({zone_id}).")
        if delegated(zone_aws, zone_id, zone_name):
            # SuperNova has delegated it (the public NS records are the zone's own): no need to edit
            # the answers to `zoneId` by hand (review 10b C30).
            step.ok(f"{zone_name} is delegated to it (the public NS records match); setting up the records.")
            return zone_id
    else:
        res = step.apply(f"create public hosted zone {zone_name}", zone_aws.call, "route53", "create-hosted-zone",
                         "--name", zone_name, "--caller-reference", f"cre-{a.project}-{int(time.time())}",
                         "--hosted-zone-config", f"Comment=CRE {a.project} (SuperNova delegated)",
                         "--query", "HostedZone.Id", "--output", "text")
        if res is not None and res.returncode != 0:
            step.die(c.FAILED, f"create-hosted-zone {zone_name} failed: {c.last_line(res.stderr)}")
        zone_id = res.stdout.strip().replace("/hostedzone/", "") if res is not None else "<created on a real run>"
    nova_arn = zone_aws.role_arn(NOVA_ROLE)
    if nova_arn:
        step.ok(f"Role {NOVA_ROLE} exists.")
    else:
        tmp = c.tmpdir(step)
        doc = os.path.join(tmp, "nova.json")
        c.write_json(doc, policy.nova_trust())
        res = step.apply(f"create role {NOVA_ROLE} (trusted by nova.aws.internal)", zone_aws.call, "iam", "create-role",
                         "--role-name", NOVA_ROLE, "--assume-role-policy-document", f"file://{doc}",
                         "--description", "SuperNova Route53 Access Role", "--query", "Role.Arn", "--output", "text")
        if res is not None:
            if res.returncode != 0:
                step.die(c.FAILED, f"create-role {NOVA_ROLE} failed: {c.last_line(res.stderr)}")
            nova_arn = res.stdout.strip()
        for pol in ("AmazonRoute53FullAccess", "SecurityAudit"):
            res = step.apply(f"attach {pol} to {NOVA_ROLE}", zone_aws.call, "iam", "attach-role-policy", "--role-name", NOVA_ROLE,
                             "--policy-arn", f"arn:{zone_aws.partition}:iam::aws:policy/{pol}")
            if res is not None and res.returncode != 0:
                step.die(c.FAILED, f"attach {pol} failed: {c.last_line(res.stderr)}")
    print(f"""
Submit the delegation at https://supernova.amazon.dev → New Sub-Domain Request:
  Company / Organization : AWS / {org}
  Domain                 : {zone_name}
  Bindle                 : {a.bindle}
  IAM role ARN for Nova  : {nova_arn or '<created on a real run>'}
  Hosted zone ID         : {zone_id}      (fill it in: left blank, SuperNova creates a second zone)
Then add the anti-spoofing TXT records from SuperNova's post-onboarding page, and re-run with
  --zone-id {zone_id}
Never delete this zone by hand; retire the domain through SuperNova.""", file=sys.stderr)
    step.out("HOSTED_ZONE_ID", zone_id)
    step.out("DELEGATION", "pending")
    step.need("DELEGATION")
    return ""


def delegated(zone_aws, zone_id, zone_name):
    """The public DNS answers the zone's own name servers for its name: the parent delegated it."""
    want = zone_aws.json("route53", "get-hosted-zone", "--id", zone_id, "--query", "DelegationSet.NameServers", "--output", "json") or []
    r = c.run(["dig", "+short", "NS", f"{zone_name}."])
    have = {x.strip().rstrip(".").lower() for x in r.stdout.split() if x.strip()} if r.returncode == 0 else set()
    return bool(have) and have == {x.rstrip(".").lower() for x in want}


def put_dns_policy(step, zone_aws, tmp, role, doc, project, note=""):
    """The per-project inline policy on a zone-account role this script owns; skipped when IAM
    already holds the same document, so a re-run changes nothing."""
    name = f"cre-dns-{project}"
    current = zone_aws.json("iam", "get-role-policy", "--role-name", role, "--policy-name", name,
                            "--query", "PolicyDocument", "--output", "json")
    if current is not None and not policy.diff(current, doc):
        step.ok(f"{role} already has the {name} policy.")
        return
    c.check_inline_size(step, zone_aws, role, name, doc)
    f = os.path.join(tmp, f"{role}.json")
    c.write_json(f, doc)
    res = step.apply(f"put inline policy {name} on {role}{(' ' + note) if note else ''}", zone_aws.call, "iam", "put-role-policy",
                     "--role-name", role, "--policy-name", name, "--policy-document", f"file://{f}")
    if res is not None and res.returncode != 0:
        step.die(c.FAILED, f"put-role-policy on {role} failed: {c.last_line(res.stderr)}")


def dns_trust_change(step, app_aws, zone_aws, a, runner_arn):
    """Read-only: (arn, current trust, changed, proposed) for CreDnsDeploy ("" / None when it
    doesn't exist yet). A trust over IAM's size limit is refused here, before anything changes."""
    role_arn = zone_aws.role_arn(DNS_ROLE)
    current = None
    if role_arn:
        current = zone_aws.json("iam", "get-role", "--role-name", DNS_ROLE, "--query", "Role.AssumeRolePolicyDocument", "--output", "json")
        if current is None:
            step.die(c.FAILED, f"get-role {DNS_ROLE} failed.")
    changed, proposed = policy.merge_dns_trust(current, app_aws.account, runner_arn, a.project, a.deploy_regions)
    if changed:
        c.check_trust_size(step, zone_aws, DNS_ROLE, proposed,
                           way_out=f"put this project's zone in another account (each project adds its runner role and a "
                                   f"certificate validator per region, {len(a.deploy_regions)} here; a new runner role doesn't help)")
    return role_arn, current, changed, proposed


def decide_dns_trust(step, zone_aws, a, current, proposed):
    """The decision to change the existing CreDnsDeploy's trust policy (other projects share it)."""
    return step.decide("updateDnsRoleTrustPolicy", f"{DNS_ROLE}@{zone_aws.account}",
                       f"Adding {a.project}'s runner role and certificate validators to the trust policy of {DNS_ROLE} in "
                       f"the DNS account {zone_aws.account}, which other projects share", policy.diff(current, proposed))


def discover(step, app_aws, zone_aws, a):
    """--discovery: the decisions this run needs, read-only (the production ones were met in
    require()). A runner role that doesn't exist yet has its ARN simulated: the name at IAM's
    default path, which is where aws_account.py creates it."""
    if zone_aws.account != app_aws.account:
        runner_arn = app_aws.role_arn(a.runner_role) or f"arn:{app_aws.partition}:iam::{app_aws.account}:role/{a.runner_role}"
        _, current, changed, proposed = dns_trust_change(step, app_aws, zone_aws, a, runner_arn)
        subject = f"{DNS_ROLE}@{zone_aws.account}"
        if current is None:
            step.check("updateDnsRoleTrustPolicy", subject, "create", f"{DNS_ROLE} doesn't exist in {zone_aws.account}: setup creates it, "
                       f"trusting only this project's runner role. No shared role changes.")
        elif not changed:
            step.check("updateDnsRoleTrustPolicy", subject, "in-place", f"{DNS_ROLE} in {zone_aws.account} already trusts {a.runner_role}: "
                       "no IAM change.")
        else:
            decide_dns_trust(step, zone_aws, a, current, proposed)
    step.ok(f"Discovery for {a.project}'s DNS: {len(step.found)} decision(s); nothing was changed.")


def cross_account(step, app_aws, zone_aws, a):
    tmp = c.tmpdir(step)
    buckets = [f"{a.project}-{r}" for r in a.deploy_regions]
    # Every ARN below is IAM's answer (get-role, or create-role's output), never built from a name.
    runner_arn = app_aws.role_arn(a.runner_role)
    if not runner_arn:
        step.die(c.REFUSED, f"Runner role {a.runner_role} doesn't exist in {app_aws.account}; run aws_account.py first, or pass --runner-role.")

    # CloudFormation's role in the zone account.
    exec_arn = zone_aws.role_arn(DNS_EXEC)
    if not exec_arn:
        doc = os.path.join(tmp, "cfn.json")
        c.write_json(doc, policy.cfn_trust())
        res = step.apply(f"create role {DNS_EXEC} in {zone_aws.account}", zone_aws.call, "iam", "create-role", "--role-name", DNS_EXEC,
                         "--assume-role-policy-document", f"file://{doc}", "--description", "CloudFormation role for CRE DNS stacks",
                         "--query", "Role.Arn", "--output", "text")
        if res is not None:
            if res.returncode != 0:
                step.die(c.FAILED, f"create-role {DNS_EXEC} failed: {c.last_line(res.stderr)}")
            exec_arn = res.stdout.strip()
    put_dns_policy(step, zone_aws, tmp, DNS_EXEC, policy.dns_exec_policy(a.zone_id, a.project, buckets), a.project,
                   f"(zone {a.zone_id} only)")

    # The role CI and the certificate Lambda assume.
    role_arn, current, changed, proposed = dns_trust_change(step, app_aws, zone_aws, a, runner_arn)
    trust_file = os.path.join(tmp, "dns-trust.json")
    c.write_json(trust_file, proposed)
    if current is None:
        res = step.apply(f"create role {DNS_ROLE} in {zone_aws.account}", zone_aws.call, "iam", "create-role", "--role-name", DNS_ROLE,
                         "--assume-role-policy-document", f"file://{trust_file}", "--description", "CRE external DNS deploy role",
                         "--query", "Role.Arn", "--output", "text")
        if res is not None:
            if res.returncode != 0:
                step.die(c.FAILED, f"create-role {DNS_ROLE} failed: {c.last_line(res.stderr)}")
            role_arn = res.stdout.strip()
    elif changed:
        step.info(f"Proposed trust policy change for {DNS_ROLE}:")
        if decide_dns_trust(step, zone_aws, a, current, proposed):
            res = step.apply(f"update trust policy of {DNS_ROLE}", zone_aws.call, "iam", "update-assume-role-policy",
                             "--role-name", DNS_ROLE, "--policy-document", f"file://{trust_file}")
            if res is not None and res.returncode != 0:
                step.die(c.FAILED, f"update-assume-role-policy on {DNS_ROLE} failed: {c.last_line(res.stderr)}")
    else:
        step.ok(f"{DNS_ROLE} already trusts this project.")
    put_dns_policy(step, zone_aws, tmp, DNS_ROLE,
                   policy.dns_role_policy(zone_aws.account, a.zone_id, a.project, exec_arn, buckets), a.project)

    # Deploy account: the runner must be allowed to assume the DNS role. A runner role setup created
    # has that in its cre-runner policy; a provided one is never changed, only checked.
    if role_arn and app_aws.can(runner_arn, "sts:AssumeRole", role_arn):
        step.ok(f"{a.runner_role} may assume {role_arn}.")
    elif role_arn:
        step.warn(f"{a.runner_role} isn't allowed to assume {role_arn}, so the DNS deploy phase will fail. Its owner needs to grant\n"
                  f"   sts:AssumeRole + sts:TagSession on {role_arn} (this role is not changed by setup).")
        step.out("RUNNER_PERMISSION_GAPS", f"sts:AssumeRole({role_arn})")
        step.need("RUNNER_PERMISSIONS")   # the role's owner must grant it; a re-run checks again (review 10c C6)
    for b in buckets:
        res = app_aws.call("s3api", "get-bucket-policy", "--bucket", b, "--query", "Policy", "--output", "text")
        if res.returncode != 0 and "NoSuchBucketPolicy" not in res.stderr:
            # Anything but "no policy" (access denied, throttling): never replace a policy we couldn't read.
            step.die(c.FAILED, f"Reading the bucket policy of {b} failed: {c.last_line(res.stderr)}")
        raw = res.stdout.strip() if res.returncode == 0 else ""
        try:
            cur = json.loads(raw) if raw and raw != "None" else None
        except ValueError:
            step.die(c.FAILED, f"The bucket policy of {b} isn't valid JSON: {raw[:200]}")
        changed, doc = policy.assets_bucket_policy(cur, b, a.project, zone_aws.account)
        if not changed:
            step.ok(f"s3://{b} already readable by {zone_aws.account}.")
            continue
        f = os.path.join(tmp, f"bp-{b}.json")
        c.write_json(f, doc)
        res = step.apply(f"let account {zone_aws.account} read s3://{b}/{a.project}/*", app_aws.call, "s3api", "put-bucket-policy",
                         "--bucket", b, "--policy", f"file://{f}")
        if res is not None and res.returncode != 0:
            step.die(c.FAILED, f"put-bucket-policy on {b} failed: {c.last_line(res.stderr)}")
    step.out("DNS_ROLE_ARN", role_arn)
    step.out("DNS_CFN_EXECUTION_ROLE_ARN", exec_arn)
    return role_arn, exec_arn


def hand_off(step, a, values, roles, zone_account, cross):
    """In a project, record the zone (cre-setup.json: what the certificate default and the build's
    check read) and print the one setup:repo command that sets the deploy's CI/CD variables. This
    script sets none: the scaffold does that itself, and after it nothing told the agent to (review
    10c A12)."""
    sf = a.setup_file
    if not sf.dir or step.dry_run:
        return
    sf.set("domain", values["DOMAIN"])
    sf.set("hostedZoneId", values["HOSTED_ZONE_ID"])
    sf.set("dnsAccountId", zone_account if cross else "")
    github = (a.provider or "gitlab") == "github"
    flags = []
    for k, v in values.items():
        if k in roles:
            flags.append(f"--secret {k}={v}" if github else f"--var {k}={v}:protected")
        else:
            flags.append(f"--var {k}={v}")
    step.info("The stacks read these as CI/CD variables, which this step doesn't set. With the user's agreement, set them "
              f"(and record them in cre-setup.json; commit it): npx projen setup:repo prepare {' '.join(flags)}")
    step.out("SETUP_REPO_COMMAND", f"npx projen setup:repo prepare {' '.join(flags)}")


def main(step, argv):
    a = parse(step, argv)
    app_aws = c.Aws(step, a.profile).require()
    c.check_account(step, app_aws, a.setup_file, a.move_account)
    zone_aws = app_aws
    if a.dns_profile:
        zone_aws = c.Aws(step, a.dns_profile).require()
    if step.discovery:
        discover(step, app_aws, zone_aws, a)
        return
    cross = zone_aws.account != app_aws.account

    if not a.zone_id:
        zone_id = supernova(step, zone_aws, a)
        if not zone_id:
            return
        a.zone_id = zone_id   # delegated: carry on as with --zone-id

    r = zone_aws.call("route53", "get-hosted-zone", "--id", a.zone_id, "--query", "HostedZone.Name", "--output", "text")
    if r.returncode != 0:
        step.die(c.REFUSED, f"Hosted zone {a.zone_id} not found in account {zone_aws.account}: {c.last_line(r.stderr)}")
    zone = r.stdout.strip().rstrip(".")
    zone = zone.lower()
    domain = a.domain or f"{a.project}.{zone}"
    if domain != zone and not domain.endswith(f".{zone}"):
        step.die(c.REFUSED, f"Domain {domain} is not inside zone {zone}.")
    step.ok(f"Zone {zone} ({a.zone_id}) in account {zone_aws.account}; domain {domain}.")
    step.out("DOMAIN", domain)
    step.out("HOSTED_ZONE_ID", a.zone_id)
    values = {"DOMAIN": domain, "HOSTED_ZONE_ID": a.zone_id}
    if not cross:
        step.info("Zone is in the deploy account: no extra roles needed.")
        hand_off(step, a, values, (), zone_aws.account, False)
        return
    role_arn, exec_arn = cross_account(step, app_aws, zone_aws, a)
    values.update(DNS_ROLE_ARN=role_arn, DNS_CFN_EXECUTION_ROLE_ARN=exec_arn)
    hand_off(step, a, values, ("DNS_ROLE_ARN", "DNS_CFN_EXECUTION_ROLE_ARN"), zone_aws.account, True)


if __name__ == "__main__":
    c.cli(NAME, main)
