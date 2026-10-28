#!/usr/bin/env python3
"""with-role.py <ROLE_ARN-or-empty> <command...>

Run a command with credentials for another account's role (external DNS). When the role is empty,
or already the identity in use, the command runs with the current credentials. Otherwise the
current credentials assume the role (role chaining: the DNS role trusts this account's deploy role,
see setup/dns.py) and the command gets the temporary credentials in its environment only.

Used the same way locally, in GitLab CI and in GitHub Actions, so the cross-account path does not
depend on CI-specific credential vending. This process is replaced by the command, so its exit code
and signals are the command's. Exit 2 without a command, 1 when the role can't be assumed.
"""
import os
import sys

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import crelib  # noqa: E402


def session_name():
    # A role session name is at most 64 characters (review N9).
    return f"cre-{os.environ.get('PROJECT_NAME') or 'deploy'}"[:60] + "-dns"


def main(argv):
    if len(argv) < 2:
        print("usage: with-role.py <role-arn|''> <command...>", file=sys.stderr)
        sys.exit(2)
    role, cmd = argv[0], argv[1:]
    if not role:
        crelib.exec_replace(cmd)
    r = crelib.run(["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"])
    caller = r.stdout.strip()
    if r.returncode != 0 or not caller:
        print(f"❌ with-role: no AWS credentials to assume {role} from. {crelib.last_line(r.stderr)}", file=sys.stderr)
        sys.exit(1)
    role_name = role.rsplit("/", 1)[-1]
    same_account = caller.split(":")[4:5] == role.split(":")[4:5]
    if f":assumed-role/{role_name}/" in caller and same_account:
        crelib.exec_replace(cmd)
    r = crelib.run(["aws", "sts", "assume-role", "--role-arn", role, "--role-session-name", session_name(),
                    "--duration-seconds", "3600", "--query", "Credentials.[AccessKeyId,SecretAccessKey,SessionToken]",
                    "--output", "text"])
    creds = r.stdout.split()
    if r.returncode != 0 or len(creds) != 3:
        print(f"❌ with-role: could not assume {role} from {caller}. Check the role's trust policy (setup/dns.py grants "
              f"this account's deploy role). {crelib.last_line(r.stderr)}", file=sys.stderr)
        sys.exit(1)
    env = {k: v for k, v in os.environ.items() if k != "AWS_PROFILE"}
    # Only here are the credentials chained (1 h): the command caps its waits by that, not when the
    # caller already held the role and nothing was assumed (review 10d D9).
    env.update(AWS_ACCESS_KEY_ID=creds[0], AWS_SECRET_ACCESS_KEY=creds[1], AWS_SESSION_TOKEN=creds[2], CRE_ROLE_CHAINED="1")
    crelib.exec_replace(cmd, env)


if __name__ == "__main__":
    main(sys.argv[1:])
