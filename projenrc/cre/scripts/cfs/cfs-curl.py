#!/usr/bin/env python3
"""npx projen cfs:curl [--json] [-i] <GET|POST|PUT|PATCH|DELETE|HEAD> <url-or-/path> [body]

Call anything behind a CloudFront Signer (CFS) distribution from a terminal or an agent session,
with no browser: the Locust UI and its API (/stats/requests, /swarm, /stop), the demo's own pages
and routes (LoadGenerator.addWebPage, cre_pages.route), the edge's app routes.

  npx projen cfs:curl GET https://dxxxx.cloudfront.net/locust/stats/requests
  CFS_URL=https://dxxxx.cloudfront.net/locust npx projen cfs:curl GET /stats/requests
  CFS_URL=... npx projen cfs:curl POST /swarm 'user_count=10&spawn_rate=5'     a form body
  CFS_URL=... npx projen cfs:curl --json POST /api/flush '{"all": true}'       a JSON body

A path is appended to $CFS_URL (a trailing / on it is dropped, so EdgeLocustUrl works as it is).
A body is sent as a form (application/x-www-form-urlencoded) unless --json; a body given, even an
empty one (POST /inject/available ''), is sent with its Content-Length. GET, HEAD and DELETE take
no body: CloudFront refuses a GET with one (403, with the sign-in page). GET and HEAD follow
redirects; other methods don't (a redirected POST would become a GET).

Prerequisites: `mwinit -f` (~/.midway/cookie unexpired), and the distribution onboarded at
CloudFront Signer (npx projen cfs:onboard). Midway-only, so internal use only.

Each call does the handshake again (the signed cookies last minutes), so calls are self-contained.
The signed cookies never touch disk or the process list (crelib.curl pipes); the Midway cookie is
only read.

stdout: the response body, byte for byte (-i: the response headers first). stderr: the status
when it isn't 2xx, and why. Exit: 0 a 2xx or 3xx answer | 1 a 4xx or 5xx answer from the origin |
2 bad arguments | 11 no Midway cookie, or the CloudFront Signer handshake failed |
20 the request itself failed (curl's message).
"""
import argparse
import os
import re
import sys

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import crelib  # noqa: E402

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
NO_BODY = ("GET", "HEAD", "DELETE")


def fail(code, msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)


def target(arg):
    if re.match(r"^https?://", arg):
        return arg
    if arg.startswith("/"):
        base = (os.environ.get("CFS_URL") or "").rstrip("/")
        if not base:
            fail(2, "A path needs CFS_URL set to the distribution URL, e.g. https://dxxxx.cloudfront.net/locust "
                    "(the edge stack's EdgeLocustUrl).")
        return base + arg
    fail(2, f"'{arg}' is neither a URL nor a /path.")


def last_status(headers):
    """The final response's status code from curl's -D dump (one block per redirect hop)."""
    codes = re.findall(r"(?m)^HTTP/[\d.]+ (\d{3})", headers)
    return int(codes[-1]) if codes else 0


def main(argv):
    p = argparse.ArgumentParser(prog="cfs:curl", allow_abbrev=False, add_help=True)
    p.add_argument("--json", action="store_true", help="send the body as application/json")
    p.add_argument("-i", "--include", action="store_true", help="print the response headers before the body")
    p.add_argument("method")
    p.add_argument("url")
    p.add_argument("body", nargs="?")
    a = p.parse_args(argv)
    method = a.method.upper()
    if method not in METHODS:
        fail(2, f"Unknown method '{a.method}' (one of {' '.join(METHODS)}).")
    url = target(a.url)
    if a.body is not None and method in NO_BODY:
        fail(2, f"{method} takes no body: CloudFront refuses a {method} with one. Put the values in the URL's query string.")
    if not crelib.read_midway_cookie():
        fail(11, f"No Midway cookie at {crelib.midway_cookie()}. Run: {crelib.MWINIT_HINT}")

    try:
        cookie, _ = crelib.cfs_signed_cookies(url)
    except ValueError as e:
        fail(11, f"CFS handshake failed: {e}.")

    headers = [f"Cookie: {cookie}"]
    args = ["-sS", "-X", method, "-H", "@<<hdr>>", "-D", "<<resp>>"]
    if method == "HEAD":
        args = ["-sS", "-I", "-H", "@<<hdr>>", "-D", "<<resp>>"]
    if method in ("GET", "HEAD"):
        args.append("-L")
    feeds = {}
    if a.body is not None:
        headers.append("Content-Type: " + ("application/json" if a.json else "application/x-www-form-urlencoded"))
        args += ["--data-binary", "@<<body>>"]
        feeds["body"] = a.body
    feeds["hdr"] = "".join(h + "\n" for h in headers)
    if a.include and method != "HEAD":
        args.append("-i")
    code, _, _, got = crelib.curl(args + [url], feeds=feeds, collect=("resp",), passthrough=True, timeout=300)
    sys.stdout.flush()
    if code != 0:
        fail(20, f"{method} {url} failed (curl exit {code}).")
    status = last_status(got.get("resp", ""))
    last_hop = re.split(r"(?m)^HTTP/", got.get("resp", ""))[-1].lower()
    if status == 403 and "x-cache: error from cloudfront" in last_hop and "server: amazons3" in last_hop:
        # CloudFront served its 403 page. That is a refused signature, or the ORIGIN's own 403: the
        # distribution's custom error response replaces any 403 with the same page, so the two
        # can't be told apart (review 10c E2). The cookies were minted moments ago by this run, so
        # the origin is as likely: 1 (an answer), naming both, not 11 (re-authenticate).
        print(f"\n⚠  {method} {url} → HTTP 403 (CloudFront's error page): either the origin answered 403 (a wrong path or "
              "method on an API, a WAF, the app's own authorisation), or CloudFront refused the signed cookies just minted "
              "(check npx projen cfs:onboard --status). CloudFront caches that page for up to 5 minutes.", file=sys.stderr)
        sys.exit(1)
    if status >= 400:
        print(f"\n⚠️  {method} {url} → HTTP {status}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
