#!/usr/bin/env python3
"""npx projen skills:doctor — report whether this demo's vended skills actually resolve.

The skills in .kiro/skills/ are SYMLINKS into the sibling cre-demo-template clone, so a pull of that
clone (update-template does one) updates every project at once. The cost of that is a failure mode
with no error message: if the template is renamed, moved, or absent (a fresh clone of the project on
its own), every link dangles, `git status` stays clean, and the skills simply never appear in the
harness. Nothing tells you why /pdd stopped existing. This script tells you.

Every link is reported, not just the first bad one. Exit 0 all resolve | 1 something doesn't.
"""
import os
import sys

SKILLS_DIR = os.path.join(".kiro", "skills")
REPAIR = """
To repair: the links are relative and expect the cre-demo-template clone to be a SIBLING of this
project, under the directory name recorded in projenrc/cre/SOURCE.json (templateDirName):

  <parent>/
    cre-demo-template/     <- the clone
    <this-project>/

Put the clone back there (or clone it there), then re-link every vended skill from it:

  npx projen update-template                       # finds the recorded or sibling clone
  npx projen update-template --from <your-clone>   # or name the clone explicitly

If you do not have the template and only need to build/deploy, ignore this: skills are agent
tooling, and nothing in the build or the CI pipeline reads .kiro/."""


def main():
    print(f"Vended skills ({SKILLS_DIR})")
    if not os.path.isdir(SKILLS_DIR):
        print(f"  ERROR  no {SKILLS_DIR} directory — was this demo scaffolded by cre-demo-init?")
        return 1
    # Dotfiles aren't skills: Finder drops a .DS_Store here when the folder is opened (review 10c E7).
    entries = sorted(n for n in os.listdir(SKILLS_DIR) if not n.startswith("."))
    if not entries:
        print(f"  ERROR  {SKILLS_DIR} is empty")
        return 1
    fail = False
    for name in entries:
        p = os.path.join(SKILLS_DIR, name)
        if os.path.islink(p):
            ok = os.path.exists(p)
            print(f"  {'ok      ' if ok else 'DANGLING'}  {name:<14} -> {os.readlink(p)}")
            fail |= not ok
        elif os.path.isdir(p):
            # A real directory: either a demo-local skill (fine) or a stale pre-symlink copy (not).
            print(f"  copy      {name:<14} (real directory, not a link to the template)")
        else:
            print(f"  ?         {name:<14} (neither a symlink nor a directory)")
            fail = True

    # The harness hop: Claude Code reads .claude/skills, Kiro reads .kiro/skills. One link joins
    # them, so a broken hop here hides every skill from Claude Code while Kiro still sees them all.
    print("\nHarness hop (.claude/skills)")
    hop = os.path.join(".claude", "skills")
    if os.path.islink(hop):
        ok = os.path.exists(hop)
        print(f"  {'ok      ' if ok else 'DANGLING'}  -> {os.readlink(hop)}")
        fail |= not ok
    elif os.path.isfile(hop):
        print("  ERROR  .claude/skills is a FILE containing a path, not a symlink.")
        print("         This is the Windows checkout failure: git config core.symlinks true, then re-checkout.")
        fail = True
    elif os.path.isdir(hop):
        print("  copy   .claude/skills is a real directory (not a link to .kiro/skills)")
    else:
        print("  ERROR  .claude/skills missing — Claude Code will find no skills in this repo")
        fail = True

    if fail:
        print(REPAIR)
        return 1
    print("\nAll vended skills resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
