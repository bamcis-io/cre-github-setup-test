#!/usr/bin/env python3
"""npx projen cfs:onboard  [--domain <dxxxx.cloudfront.net>] [--profile <aws-profile>] [--status | --offboard]

Onboard the project's CloudFront distribution at CloudFrontSigner, the one-time step after the
first edge deploy that otherwise needs the web portal (https://cloudfrontsigner.ninjas.security.a2z.com).

The portal's API is Midway-only: every call carries the Midway cookie plus the portal's own session
cookie (a crelib.MidwaySession, in memory only), and writes also carry an anti-CSRF token that
GET /get-csrf-token returns in its `anti-csrftoken-a2z` header (both read from the portal's own
bundle). Calls used:
  GET  /get-domain-owner?domain=<d>   200 = onboarded (owner returned), 404 = not onboarded
  POST /onboard   {"Domain": "<d>"}   -> {"DomainOnboarded": true}
  POST /offboard  {"Domain": "<d>"}   -> {"DomainOffboarded": true}

Domain: --domain, else the EdgeDomainName output of the project's CreEdgeStack (the one stack of
kind edge in cre-stacks.json; always us-east-1), read with the AWS CLI (--profile or the ambient
credentials).

stdout: ONBOARDED=true|false (--status), then RESULT code=<n> step=cfs-onboard.
Exit codes (the setup contract): 0 onboarded (or already) / offboarded / status printed | 10 bad
input or no domain | 11 Midway session missing or expired | 20 the portal refused or failed.
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import crelib as c  # noqa: E402

DOMAIN_RE = re.compile(r"^[a-z0-9]+\.cloudfront\.net$|^([a-z0-9-]+\.)+[a-z]{2,}$")


def edge_domain(step, profile):
    try:
        stacks = c.read_plan()
    except c.BadFile as e:
        step.die(c.REFUSED, f"No --domain, and {e}. Or pass --domain <dxxxx.cloudfront.net>.")
    edges = [s["id"] for s in stacks if s.get("kind") == "edge"]
    if not edges:
        step.die(c.REFUSED, "No --domain, and cre-stacks.json has no CreEdgeStack. Pass --domain <dxxxx.cloudfront.net>.")
    if len(edges) > 1:
        step.die(c.REFUSED, f"cre-stacks.json has several edge stacks ({' '.join(edges)}); pass --domain for the one to onboard.")
    cmd = ["aws"] + (["--profile", profile] if profile else []) + [
        "cloudformation", "describe-stacks", "--region", "us-east-1", "--stack-name", edges[0],
        "--query", "Stacks[0].Outputs[?OutputKey=='EdgeDomainName'].OutputValue", "--output", "text"]
    r = c.run(cmd)
    domain = r.stdout.strip() if r.returncode == 0 else ""
    if not domain or domain == "None":
        step.die(c.REFUSED, f"No --domain, and could not read EdgeDomainName from stack {edges[0]} in us-east-1"
                 f"{f' (profile {profile})' if profile else ''}. Deploy the edge first, or pass --domain.")
    return domain


def main(step, argv):
    p = c.Parser(step, "cfs:onboard")
    p.add_argument("--domain", default="", help="the distribution's domain, dxxxx.cloudfront.net (default: CreEdgeStack's EdgeDomainName output)")
    p.add_argument("--profile", default="", help="AWS profile to read the edge stack's output with (default: the ambient credentials)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="print ONBOARDED=true|false, change nothing")
    g.add_argument("--offboard", action="store_true", help="remove the distribution from CloudFront Signer: the edge then answers 403")
    a = p.parse_args(argv)
    mode = "status" if a.status else "offboard" if a.offboard else "onboard"
    base = (os.environ.get("CFS_PORTAL_URL") or "https://cloudfrontsigner.ninjas.security.a2z.com").rstrip("/")

    domain = a.domain or edge_domain(step, a.profile)
    if not DOMAIN_RE.match(domain):
        step.die(c.REFUSED, f"'{domain}' is not a domain name.")
    why = c.midway_problem(c.midway_cookie())
    if why:
        step.die(c.CREDENTIALS, f"Midway session problem: {why}. Run: {c.MWINIT_HINT}   then re-run.")

    portal = c.MidwaySession(f"{base}/")
    if not portal.prime():
        step.die(c.FAILED, f"Cannot reach {base}.")
    status, _, text, _ = portal.request("GET", f"{base}/get-domain-owner?domain={quote(domain, safe='')}",
                                        headers=["Accept: application/json"])
    if status in ("401", "403"):
        step.die(c.CREDENTIALS, f"CloudFrontSigner rejected the Midway session ({status}). Run: {c.MWINIT_HINT}   and re-run.")
    if status not in ("200", "404"):
        step.die(c.FAILED, f"get-domain-owner for {domain} returned {status}: {text[:300]}")
    onboarded = status == "200"

    if mode == "status":
        if onboarded:
            step.ok(f"{domain} is onboarded: {text[:300]}")
        else:
            step.info(f"{domain} is not onboarded.")
        step.out("ONBOARDED", "true" if onboarded else "false")
        return
    if mode == "onboard" and onboarded:
        step.ok(f"{domain} is already onboarded at CloudFrontSigner.")
        return
    if mode == "offboard" and not onboarded:
        step.ok(f"{domain} is not onboarded; nothing to offboard.")
        return

    status, _, _, headers = portal.request("GET", f"{base}/get-csrf-token", headers=["anti-csrftoken-a2z-request: true"],
                                           response_headers=True)
    tokens = re.findall(r"(?im)^anti-csrftoken-a2z:\s*(\S+)", headers)
    if status != "200" or not tokens:
        step.die(c.FAILED, f"Could not get a CSRF token from CloudFrontSigner ({status}).")
    status, _, text, _ = portal.request("POST", f"{base}/{mode}",
                                        headers=["Content-Type: application/json", f"anti-csrftoken-a2z: {tokens[-1]}"],
                                        body=json.dumps({"Domain": domain}))
    field = "DomainOnboarded" if mode == "onboard" else "DomainOffboarded"
    try:
        answer = json.loads(text)
    except ValueError:
        answer = {}
    if status == "200" and isinstance(answer, dict) and answer.get(field) is True:
        step.ok(f"{'Onboarded' if mode == 'onboard' else 'Offboarded'} {domain} at CloudFrontSigner.")
        return
    msg = answer.get("Message", "") if isinstance(answer, dict) else ""
    step.die(c.FAILED, f"CloudFrontSigner {mode} of {domain} failed ({status}): {msg or text[:300] or 'no message'}")


if __name__ == "__main__":
    c.cli("cfs-onboard", main)
