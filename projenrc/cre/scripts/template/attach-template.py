#!/usr/bin/env python3
"""attach-template.py <template-clone> <project-dir>

Ties a CRE project to the cre-demo-template clone it is built from:
  1. The breadcrumb, projenrc/cre/SOURCE.json (committed): the clone's directory name, its origin
     URL (`templateRemote`, without any user:token@), its default branch (`templateBranch`), the
     commit and the package version; and .cre/template.json (gitignored, per machine): the clone's
     real absolute path (`templatePath`). update-template reads both to find and pull the clone.
     Other keys are kept.
  2. The vended skills: .kiro/skills/<name> links to the clone's skills/<name> for each name in
     scripts/template/vended-skills.txt, so a skill the template adds, renames or drops reaches the
     project on its next update. The links are relative and committed; with the template as a
     sibling of the project they are `../../../<template-dir>/skills/<name>`. A link into the
     template's skills/ that is no longer vended is removed; anything else in .kiro/skills (the
     project's own skills) is never touched. .claude/skills -> .kiro/skills for Claude Code.
  3. GitLab projects: .cre/gitlab-token, a link to the token file the template records.

Run at scaffold time (new_project.py) and by update-template on every update. Idempotent.
Exit 0 done | 10 refused (not a template clone, not a project directory) | 20 a step failed.
"""
import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import crelib as c  # noqa: E402


read_json = c.read_json   # crelib's one JSON reader (review 10c G6)


def strip_userinfo(remote):
    # SOURCE.json is committed, and a repo can go public (review 10b C13).
    u = urlsplit(remote)
    if u.scheme in ("http", "https") and "@" in u.netloc:
        return urlunsplit(u._replace(netloc=u.netloc.rsplit("@", 1)[1]))
    return remote


def breadcrumb(step, tpl, proj, pkg):
    remote = c.git("-C", tpl, "remote", "get-url", "origin", cwd=None).stdout.strip()
    head = c.git("-C", tpl, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", cwd=None).stdout.strip()
    branch = head[len("origin/"):] if head.startswith("origin/") else head or "main"
    r = c.git("-C", tpl, "rev-parse", "HEAD", cwd=None)
    if r.returncode != 0:
        step.die(c.REFUSED, f"{tpl} is not a git repository.")
    sha = r.stdout.strip()
    with open(os.path.join(pkg, "package.json")) as f:
        ver = json.load(f)["version"]
    try:
        os.makedirs(os.path.join(proj, "projenrc", "cre"), exist_ok=True)
        os.makedirs(os.path.join(proj, ".cre"), exist_ok=True)
        path = os.path.join(proj, "projenrc", "cre", "SOURCE.json")
        d = read_json(path)
        # The clone's absolute path differs per machine: the gitignored .cre/template.json, not the
        # committed SOURCE.json (where it churned between teammates; #35).
        d.pop("templatePath", None)
        d.update({"templateDirName": os.path.basename(tpl), "templateRemote": strip_userinfo(remote),
                  "templateBranch": branch, "templateSha": sha, "packageVersion": ver})
        c.write_json(path, d, sort_keys=False)
        c.write_json(os.path.join(proj, ".cre", "template.json"), {"templatePath": tpl}, sort_keys=False)
    except OSError as e:
        step.die(c.FAILED, f"Writing the breadcrumb (projenrc/cre/SOURCE.json, .cre/template.json) failed: {e}")
    return sha, remote, branch


def link(path, target):
    """Make path a symlink to target (replacing a link)."""
    tmp = f"{path}.tmp{os.getpid()}"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, path)


def skills(step, tpl, proj, pkg):
    sk = os.path.join(proj, ".kiro", "skills")
    os.makedirs(sk, exist_ok=True)
    with open(os.path.join(pkg, "scripts", "template", "vended-skills.txt")) as f:
        want = [x.strip() for x in f if x.strip() and not x.strip().startswith("#")]
    rel = os.path.relpath(os.path.join(tpl, "skills"), sk)
    for s in want:
        if not os.path.isdir(os.path.join(tpl, "skills", s)):
            c.warn(f"The template has no skills/{s}; not linked.")
            continue
        p = os.path.join(sk, s)
        if os.path.lexists(p) and not os.path.islink(p):
            c.warn(f".kiro/skills/{s} is a real directory (the project's own skill?); left as is, not linked.")
            continue
        try:
            link(p, f"{rel}/{s}")
        except OSError as e:
            step.die(c.FAILED, f"Linking .kiro/skills/{s} failed: {e}")
    # Links into THIS template's skills/ that are no longer vended (removed from the list, or
    # gl-push / gh-push from an older scaffold). Any other link is the project's own.
    tpl_skills = os.path.realpath(os.path.join(tpl, "skills")) + os.sep
    for n in sorted(os.listdir(sk)):
        p = os.path.join(sk, n)
        if not os.path.islink(p) or n in want:
            continue
        t = os.readlink(p)
        resolved = os.path.realpath(os.path.join(sk, t))
        if t.startswith(rel + "/") or resolved.startswith(tpl_skills):
            os.remove(p)
            c.info(f"Unlinked .kiro/skills/{n} (the template no longer vends it).")
    os.makedirs(os.path.join(proj, ".claude"), exist_ok=True)
    hop = os.path.join(proj, ".claude", "skills")
    if not os.path.lexists(hop) or os.path.islink(hop):
        try:
            link(hop, "../.kiro/skills")
        except OSError as e:
            step.die(c.FAILED, f"Linking .claude/skills failed: {e}")


def token_link(step, tpl, proj):
    """The template records where this machine's GitLab token file is (.agents/gitlab-token, a link
    made by new_project.py); a GitLab project gets the same link at .cre/gitlab-token. Only the path
    is shared; the token is never copied. A real file at .cre/gitlab-token is left alone."""
    if read_json(os.path.join(proj, "cre-setup.json")).get("provider") != "gitlab":
        return
    rec = os.environ.get("CRE_TOKEN_RECORD") or os.path.join(tpl, c.TEMPLATE_TOKEN_LINK)
    lnk = os.path.join(proj, c.PROJECT_TOKEN_LINK)
    if not os.path.islink(rec):
        if not os.path.islink(lnk):
            c.warn(f"No GitLab token file is recorded in the template ({rec}), so .cre/gitlab-token isn't linked. The git "
                 f"tasks then can't check MR state. Link it: ln -s <your token file> {lnk}")
        return
    if os.path.lexists(lnk) and not os.path.islink(lnk):
        c.warn(f"{lnk} is a real file, not a link to the token file; left as is.")
        return
    target = os.readlink(rec)
    if os.path.islink(lnk) and os.readlink(lnk) == target:
        return
    try:
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
        link(lnk, target)
    except OSError as e:
        step.die(c.FAILED, f"Linking .cre/gitlab-token failed: {e}")
    c.info(f"Linked .cre/gitlab-token → {target} (the token file the template records).")


def main(step, argv):
    if len(argv) != 2:
        step.die(c.REFUSED, "usage: attach-template.py <template-clone> <project-dir>")
    if not os.path.isdir(argv[0]):
        step.die(c.REFUSED, f"No directory at {argv[0]}.")
    if not os.path.isdir(argv[1]):
        step.die(c.REFUSED, f"No directory at {argv[1]}.")
    tpl, proj = os.path.realpath(argv[0]), os.path.realpath(argv[1])
    pkg = os.path.join(tpl, "packages", "cre-projen")
    if not (os.path.isdir(os.path.join(pkg, "src")) and os.path.isdir(os.path.join(tpl, "skills"))):
        step.die(c.REFUSED, f"{tpl} is not a cre-demo-template clone (no packages/cre-projen/src or skills/).")
    if (proj + os.sep).startswith(tpl + os.sep):
        step.die(c.REFUSED, f"The project {proj} is inside the template clone; it must be its own directory.")
    if os.path.dirname(tpl) != os.path.dirname(proj):
        c.warn(f"The template clone ({tpl}) is not a sibling of the project ({proj}): the skill links work only on "
             "machines with this same layout.")
    sha, remote, branch = breadcrumb(step, tpl, proj, pkg)
    skills(step, tpl, proj, pkg)
    token_link(step, tpl, proj)
    # The default branch is what the clone is pulled from; the checked-out one can differ (a template
    # branch under test with --allow-branch), where "branch main" read as the checked-out one (review 10f A6).
    current = c.git("-C", tpl, "branch", "--show-current", cwd=None).stdout.strip() or "detached HEAD"
    where = f"default branch {branch}" + ("" if current == branch else f", checked out {current}")
    c.ok(f"Attached to the template clone at {tpl} ({sha[:9]}; origin {remote or 'none'}; {where}).")


if __name__ == "__main__":
    code, _ = c.invoke("attach-template", main, sys.argv[1:])
    sys.exit(code)
