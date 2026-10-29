"""Known credential formats, one string for every scan: the git tasks check each staged file
(cregit.SECRET_CONTENT, through crelib), and setup:github-public (github.py publish) runs
`git log -G` over the whole history before a repo goes public. POSIX-ERE-safe (no digit class, no
lookarounds) so git reads it as Python does (review 10b C11)."""

SECRET_CONTENT_PATTERN = (r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|glpat-[A-Za-z0-9_-]{20}|"
                          r"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|xox[baprs]-[A-Za-z0-9-]{10,}|npm_[A-Za-z0-9]{36}|"
                          r"_authToken *= *[^ $]|aws_secret_access_key *= *[A-Za-z0-9/+]{40}|"
                          # The environment-variable spelling (.envrc, a shell script, CI vars), quoted or not (review 10d B13).
                          r"AWS_SECRET_ACCESS_KEY *[=:] *[\"']?[A-Za-z0-9/+]{40}|"
                          # A Netscape cookie-file row (a Midway cookie is one: a live session), with
                          # literal tabs, as both Python and git's ERE read them.
                          "[A-Za-z0-9._-]+\t(TRUE|FALSE)\t/[^\t]*\t(TRUE|FALSE)\t[0-9]+\t")
