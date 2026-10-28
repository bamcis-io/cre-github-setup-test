#!/usr/bin/env python3
"""check:deploy-prereqs — the stacks can deploy with what setup created, before anything merges.

    check-deploy-prereqs.py [cre-stacks.json] [cre-setup.json] [cdk.out]

Offline: compares the stack plan with what setup recorded in cre-setup.json (written by setup and
updated by every `npx projen setup:aws` / `setup:repo`):

  - every stack's region has an assets bucket: it is in `regions` (the partition's control-plane
    region always has one: us-east-1 in `aws`);
  - every variable a stack needs (`fromVariable`, an assumed or execution role) is a CI/CD variable
    setup set: it is in `ciVariables`. Optional ones only warn.

A gap fails the build with the command that closes it, instead of a green build whose deploy fails
after the merge. No cre-setup.json (a project from before it existed, or the template's own tests):
a warning, not a failure. A cre-setup.json without `ciVariables` (setup didn't record them): the
regions are checked, and each variable the stacks need is named in a warning it couldn't check,
never a ✅ (review 10c A7).
"""
import json
import os
import sys

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import crelib  # noqa: E402


def always_bucketed(regions):
    """setup:aws always makes a bucket in the partition's control-plane region(s) (us-east-1 in `aws`)."""
    return set(crelib.control_plane_regions(crelib.partition_of(regions[0]) if regions else "aws"))


def load(path):
    try:
        return crelib.load_json(path)
    except crelib.BadFile as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)


def secret_variables(plan, cdk_out):
    """{variable: [stack/parameter, ...]} for every noEcho parameter fed from a deploy variable."""
    out = {}
    manifest = os.path.join(cdk_out, "manifest.json") if cdk_out else ""
    if not manifest or not os.path.isfile(manifest):
        return out
    with open(manifest) as f:
        artifacts = json.load(f).get("artifacts", {})
    templates = {}
    for aid, a in artifacts.items():
        if a.get("type") == "aws:cloudformation:stack":
            props = a.get("properties", {})
            templates[props.get("stackName", aid)] = os.path.join(cdk_out, props["templateFile"])
    for s in plan.get("stacks", []):
        path = templates.get(s["id"])
        if not path or not os.path.isfile(path):
            continue
        with open(path) as f:
            params = json.load(f).get("Parameters", {})
        for name, src in (s.get("parameters") or {}).items():
            if src.get("kind") == "env" and str(params.get(name, {}).get("NoEcho", "")).lower() == "true":
                out.setdefault(src["variable"], []).append(f"{s['id']}/{name}")
    return out


def role_variables(plan):
    """The variables that carry a role ARN (assumed or execution role)."""
    out = set()
    for st in plan.get("stacks", []):
        for key in ("assumeRole", "executionRole"):
            if (st.get(key) or {}).get("variable"):
                out.add(st[key]["variable"])
    return out


def set_flag(var, secret, github, masked=False):
    """The setup:repo flag that sets one variable, in the provider's syntax: GitHub has repository
    variables (`--var`) and secrets (`--secret`); GitLab has one kind with :protected / :masked.
    A masked value (a noEcho parameter's) is read from a file, so it is never on the command line."""
    value = "@<file holding it>" if masked else "<value>"
    if github:
        return f"--secret {var}={value}" if secret or masked else f"--var {var}={value}"
    return f"--var {var}={value}" + (":protected:masked" if masked else ":protected" if secret else "")


def flags(need, roles, secrets, github):
    """The setup:repo flags for these variables: a role ARN protected, a noEcho parameter's value
    masked (review 10d C1: a missing secret was printed as a plain variable)."""
    return " ".join(set_flag(v, v in roles, github, masked=v in secrets) for v in need)


def problems(plan, setup, cdk_out=None):
    """(errors, warnings): lists of messages."""
    errors, warnings = [], []
    stacks = plan.get("stacks", [])
    regions = setup.get("regions", "")
    listed = regions.split() if isinstance(regions, str) else list(regions)
    have = set(listed) | always_bucketed(listed or [s["region"] for s in stacks if s.get("region")])
    missing = sorted({s["region"] for s in stacks if s.get("region") and s["region"] not in have})
    if missing:
        users = {r: [s["id"] for s in stacks if s.get("region") == r] for r in missing}
        errors.append("No assets bucket for region(s) " + ", ".join(f"{r} (stacks {', '.join(users[r])})" for r in missing)
                      + ".\n   Create them: npx projen setup:aws   (it adds the regions cre-stacks.json deploys to, and records "
                      "them in cre-setup.json; commit that).")
    exp = setup.get("upgradeTokenExpires")
    if exp:
        # The GitLab upgrade job's token lasts a year, and only a finalize run rotates it; nothing
        # else says it is about to die (review 10c C9).
        import datetime
        try:
            left = (datetime.date.fromisoformat(exp) - datetime.date.today()).days
        except ValueError:
            left = None
        if left is not None and left < 30:
            warnings.append(f"The weekly upgrade's GitLab token {'expired' if left < 0 else f'expires in {left} day(s)'} ({exp}). "
                            "Rotate it (ask the user): npx projen setup:repo finalize")
    known = setup.get("ciVariables")
    secrets = secret_variables(plan, cdk_out)
    if known is None:
        env = plan.get("environment", {})
        need = list(env.get("required", [])) + list(env.get("optional", []))
        if need:
            github = setup.get("provider") == "github"
            roles = role_variables(plan)
            warnings.append("cre-setup.json doesn't record which CI/CD variables setup set, so these weren't checked: "
                            + ", ".join(need) + ". A missing one stops the deploy after the merge. If one isn't set yet, set it "
                            "(which also records it): npx projen setup:repo prepare "
                            + flags(need, roles, secrets, github))
    else:
        env = plan.get("environment", {})
        known = set(known)
        req = [v for v in env.get("required", []) if v not in known]
        opt = [v for v in env.get("optional", []) if v not in known]
        github = setup.get("provider") == "github"
        roles = role_variables(plan)
        if req:
            errors.append("The stacks need CI/CD variable(s) setup never set: " + ", ".join(req) + ".\n   Set them: npx projen setup:repo "
                          "prepare " + flags(req, roles, secrets, github) + "\n   (it records them in "
                          "cre-setup.json; commit that).")
        # A zone in another account (setup:dns recorded dnsAccountId): its roles are optional in the
        # plan, but without them the DNS stack deploys into the wrong account (review 10c A12).
        dns_roles = [v for v in opt if setup.get("dnsAccountId") and v in ("DNS_ROLE_ARN", "DNS_CFN_EXECUTION_ROLE_ARN")]
        opt = [v for v in opt if v not in dns_roles]
        if dns_roles:
            errors.append(f"The hosted zone is in account {setup['dnsAccountId']} (setup:dns), but CI/CD variable(s) "
                          + ", ".join(dns_roles) + " aren't set: the DNS stack would deploy into this account, where the zone "
                          "isn't.\n   Set them (setup:dns printed the values): npx projen setup:repo prepare "
                          + " ".join(set_flag(v, True, github) for v in dns_roles))
        if opt:
            warnings.append("Optional variable(s) not set by setup (the stacks fall back without them): " + ", ".join(opt) + ".")
        hidden = set(setup.get("secretVariables") or [])
        for var, users in sorted(secrets.items()):
            if var in known and var not in hidden:
                errors.append(f"{var} feeds a noEcho parameter ({', '.join(users)}), but it is a plain CI/CD variable, shown in job "
                              f"logs.\n   Set it masked: npx projen setup:repo prepare {set_flag(var, True, github, masked=True)}")
    return errors, warnings


def main(argv):
    plan_path = argv[1] if len(argv) > 1 else "cre-stacks.json"
    setup_path = argv[2] if len(argv) > 2 else "cre-setup.json"
    cdk_out = argv[3] if len(argv) > 3 else "cdk.out"
    plan = load(plan_path)
    if plan is None:
        print(f"❌ {plan_path} not found; run npx projen.", file=sys.stderr)
        return 1
    setup = load(setup_path)
    if setup is None:
        print(f"⚠️  No {setup_path}: can't check the stacks against what setup created (regions, CI variables).", file=sys.stderr)
        return 0
    errors, warnings = problems(plan, setup, cdk_out)
    for w in warnings:
        print(f"⚠️  {w}", file=sys.stderr)
    for e in errors:
        print(f"❌ {e}", file=sys.stderr)
    if errors:
        return 1
    print(f"✅ Deploy prerequisites: {len(plan.get('stacks', []))} stack(s) have their buckets"
          + (" and CI variables." if setup.get("ciVariables") is not None else ". CI variables: not checked (the warning above)."),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
