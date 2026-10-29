"""Is an AWS account production? One question, one answer, asked once per account per run.

`is_prod(account)` returns `Answer(prod, why)`. `prod` is True, False, or None when the check can't
classify the account; the plan treats None as production. A check that can't be asked right now
(no Midway session, Isengard unreachable) says so as `kind` "creds" or "failed", never as production.

Amazon accounts are classified by Isengard (GetAWSAccountClassification, Midway-authenticated,
read-only). is_prod() is the one place to change for another way of classifying accounts.
"""
import dataclasses
import json
import os

import proc

ISENGARD_URL = "https://isengard-service.amazon.com"
MWINIT = "mwinit -f -k ~/.ssh/id_ecdsa.pub"


@dataclasses.dataclass(frozen=True)
class Answer:
    kind: str           # ok | creds | failed
    prod: object = None  # True | False | None (ok only)
    why: str = ""


def midway_cookie():
    return os.path.expanduser(os.environ.get("MIDWAY_COOKIE") or "~/.midway/cookie")


def isengard(account, run=proc.run):
    """The cookie file is only read, by curl itself (`-b`); never copied. `-L` keeps the sign-in
    redirects' cookies in memory."""
    cookie = midway_cookie()
    if not os.path.isfile(cookie):
        return Answer("creds", why=f"no Midway session ({cookie} is missing). Run: {MWINIT}")
    r = run(["curl", "-s", "-L", "--max-time", "20", "-b", cookie,
             "-H", "X-Amz-Target: com.amazon.isengard.coral.IsengardService.GetAWSAccountClassification",
             "-H", "Content-Encoding: amz-1.0", "-H", "Content-Type: application/json; charset=UTF-8",
             "-d", json.dumps({"AWSAccountID": account}), "-w", "\n%{http_code} %{url_effective}", ISENGARD_URL],
            timeout=30)
    if not r.ok:
        return Answer("failed", why=f"Isengard can't be reached ({(r.err or '').strip() or f'curl exit {r.code}'}). "
                                    "Check the network or VPN, then re-run.")
    body, _, tail = r.out.rpartition("\n")
    status, _, final = tail.partition(" ")
    try:
        d = json.loads(body)
    except ValueError:
        d = None
    kind = str(d.get("__type") or d.get("message") or "") if isinstance(d, dict) else ""
    if "midway-auth" in final or "Unauthenticated" in kind:
        return Answer("creds", why=f"Isengard needs a Midway session to classify the account. Run: {MWINIT}")
    if not isinstance(d, dict) or status.startswith("5") or status == "429" or \
            any(k in kind for k in ("Throttl", "InternalFailure", "ServiceUnavailable", "InternalServer")):
        return Answer("failed", why=f"Isengard didn't classify account {account} (HTTP {status or '?'}"
                                    f"{', ' + kind if kind else ''}). Re-run in a minute.")
    c = d.get("AWSAccountClassification")
    if not isinstance(c, dict) or not isinstance(c.get("IsProduction"), bool):
        return Answer("ok", None, "Isengard has no classification for it")
    return Answer("ok", c["IsProduction"], "Isengard: production" if c["IsProduction"] else "Isengard: not production")


def is_prod(account, run=proc.run):
    return isengard(account, run=run)
