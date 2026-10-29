"""Exit codes and the one failure every cresetup step raises.

  must(reply, what) -> reply.data ({} when empty), or raises Failed with the code its kind means

A reply is anything with .ok, .kind (ok | refused | creds | failed | ...) and .message.
"""
OK, REFUSED, CREDS, FAILED = 0, 10, 11, 20


class Failed(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def must(reply, what):
    if not reply.ok:
        code = {"refused": REFUSED, "creds": CREDS}.get(reply.kind, FAILED)
        raise Failed(code, f"{what}: {reply.message}")
    return reply.data or {}
