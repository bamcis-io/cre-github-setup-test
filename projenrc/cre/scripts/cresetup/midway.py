"""HTTPS behind Midway (gitlab.aws.dev, its Management Portal): curl, with every cookie in memory.

`Session(prime_url)` visits prime_url once, through Midway's sign-in redirects, and keeps the
cookies the front door sets; every request after sends them. A write without the front door's own
session cookie is redirected to sign-in, so a session is needed. Only a GET follows redirects: curl
keeps a -X method and its body across one, which would send a write to the sign-in host.
~/.midway/cookie is only read; cookies, headers (a token) and bodies go to curl through pipes,
never a file or curl's command line.

  s = Session("https://gitlab.aws.dev/api/v4/version")
  final, error = s.prime()                                           # the final URL, or curl's error
  r = s.request("GET", url, headers=["Authorization: Bearer ..."])   # -> Response
"""
import dataclasses
import os
import re
import subprocess
import threading

TIMEOUT = 60
CONNECT_TIMEOUT = 5    # one of a host's addresses can drop the connect; curl tries the next after this
MARK = "\n@@cre-http@@"
PIPE_ARG = re.compile(r"(@?)<<(\w+)>>")


def cookie_path():
    return os.path.expanduser(os.environ.get("MIDWAY_COOKIE") or "~/.midway/cookie")


def read_cookie():
    """The Midway cookie's text, or "" when there is none."""
    try:
        with open(cookie_path()) as f:
            return f.read()
    except OSError:
        return ""


@dataclasses.dataclass(frozen=True)
class Response:
    status: int        # 0 when curl itself failed
    url: str           # the final URL (after redirects)
    text: str
    error: str = ""    # curl's reason when status is 0


def feed(fd, blob):
    """Write blob to fd and close it. curl exiting unread is not an error: the flush on close
    raises then too, when blob fit Python's buffer."""
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
    except BrokenPipeError:
        pass


def curl(args, feeds=None, collect=(), timeout=TIMEOUT):
    """Run curl with each argument that is exactly `<<name>>` (or `@<<name>>`) replaced by a /dev/fd
    pipe: each `feeds[name]` is written to one, each `collect` name read back from one.
    Returns (exit code, stdout, stderr, {name: text})."""
    data = {n: v.encode(errors="surrogateescape") for n, v in (feeds or {}).items()}
    keep, threads, got = [], [], {}

    def read(fd, name):
        with os.fdopen(fd, "rb") as f:
            got[name] = f.read().decode(errors="replace")

    paths = {}
    for name in list(data) + list(collect):
        r, w = os.pipe()
        if name in data:
            threads.append(threading.Thread(target=feed, args=(w, data[name])))
            keep.append(r)
        else:
            threads.append(threading.Thread(target=read, args=(r, name)))
            keep.append(w)
        paths[name] = f"/dev/fd/{keep[-1]}"

    def place(a):
        m = PIPE_ARG.fullmatch(a)
        return m.group(1) + paths[m.group(2)] if m and m.group(2) in paths else a

    argv = ["curl", "--max-time", str(timeout), "--connect-timeout", str(CONNECT_TIMEOUT)] + [place(a) for a in args]
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=keep)
    except OSError as e:
        p = None
        err = f"curl: {e.strerror}"
    for fd in keep:          # the child holds its ends now; without a child, EOF and EPIPE end the threads
        os.close(fd)
    for t in threads:
        t.start()
    if p is None:
        for t in threads:
            t.join()
        return 127, "", err, {}
    try:
        out, err = p.communicate(timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    for t in threads:
        t.join()
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace"), got


def last_line(err, code):
    lines = [x for x in err.strip().splitlines() if x.strip()]
    return lines[-1] if lines else f"curl exited {code}"


class Session:
    def __init__(self, prime_url, timeout=TIMEOUT):
        self.prime_url, self.timeout, self.jar = prime_url, timeout, ""

    def prime(self):
        """Visit prime_url through the sign-in redirects. (final URL, "") or ("", curl's error)."""
        code, out, err, got = curl(["-sS", "-L", "-o", os.devnull, "-w", "%{url_effective}", "-b", "<<mid>>", "-c", "<<jar>>",
                                    self.prime_url], feeds={"mid": read_cookie()}, collect=("jar",), timeout=self.timeout)
        if got.get("jar"):
            self.jar = got["jar"]
        return (out.strip(), "") if code == 0 else ("", last_line(err, code))

    def request(self, method, url, headers=(), body=None, follow=None):
        """follow: redirects are followed for a GET only, unless set."""
        args = ["-sS", "-o", "-", "-w", MARK + "%{http_code} %{url_effective}", "-X", method, "-b", "<<jar>>", "-c", "<<out>>"]
        feeds = {"jar": self.jar or read_cookie()}
        if method == "GET" if follow is None else follow:
            args.append("-L")
        if headers:
            args += ["-H", "@<<hdr>>"]
            feeds["hdr"] = "".join(h + "\n" for h in headers)
        if body is not None:
            args += ["--data-binary", "@<<body>>"]
            feeds["body"] = body
        code, out, err, got = curl(args + [url], feeds=feeds, collect=("out",), timeout=self.timeout)
        if got.get("out"):
            self.jar = got["out"]
        text, _, tail = out.rpartition(MARK)
        status, _, final = tail.strip().partition(" ")
        if code != 0 or not status.isdigit():
            return Response(0, "", "", last_line(err, code))
        return Response(int(status), final, text)
