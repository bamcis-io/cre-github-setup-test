#!/usr/bin/env python3
"""sync-constructs.py <cre-demo-template dir> <dest dir>

Install the template's construct library into a project as if it came from npm: the whole of
<template>/constructs/src/ is mirrored into <dest> (cre-constructs/ at the project root), with a
package.json naming it `@cre/constructs`, and the project depends on it as `link:./cre-constructs`:

    import { RegionalNetwork, LoadGenerator } from '@cre/constructs';

It is a dependency, not project source: never edit it in a project. Changes go to cre-demo-template
(constructs/), and `npx projen update-template` re-runs this. Files the template no longer has are
deleted (crelib.mirror). A hash manifest (.cre-manifest, verify-constructs.py) is written so the
build fails when the copy was edited by hand.

Used by the scaffold (new_project.py) and update-template.py. Exit 0 | 10 no construct library at
the template | 20 the copy failed.
"""
import json
import os
import shutil
import sys

sys.dont_write_bytecode = True  # never a __pycache__ in the template clone or projenrc/cre
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import crelib  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# This script's own files at the top of <dest> (package.json is written, README.md copied, the
# manifest written by verify-constructs.py) are neither copied from src nor deleted.
EXCLUDES = (*crelib.MIRROR_EXCLUDES, "package.json", ".cre-manifest", "/README.md")


def main(argv):
    if len(argv) != 2:
        print("usage: sync-constructs.py <template-dir> <dest-dir>", file=sys.stderr)
        return 10
    template, dest = argv
    src = os.path.join(template, "constructs", "src")
    if not os.path.isdir(src):
        print(f"❌ No construct library at {src}", file=sys.stderr)
        return 10
    try:
        crelib.mirror(src, dest, EXCLUDES)
    except OSError as e:
        print(f"❌ Copying the construct library failed: {e}", file=sys.stderr)
        return 20
    # The library's README (props, examples, the mandatory gotchas), where the steering points.
    readme = os.path.join(template, "constructs", "README.md")
    if os.path.isfile(readme):
        shutil.copyfile(readme, os.path.join(dest, "README.md"))
    with open(os.path.join(template, "constructs", "package.json")) as f:
        version = json.load(f)["version"]
    sha = crelib.git("-C", template, "rev-parse", "HEAD", cwd=None).stdout.strip() or "unknown"
    crelib.write_json(os.path.join(dest, "package.json"), {
        "name": "@cre/constructs",
        "version": version,
        "private": True,
        "description": "CRE construct library, installed from cre-demo-template/constructs/src. Do not edit: "
                       "change it in cre-demo-template and run `npx projen update-template`.",
        "main": "index.ts",
        "types": "index.ts",
        "creTemplateSha": sha,
    }, sort_keys=False)
    w = crelib.run([sys.executable, os.path.join(HERE, "verify-constructs.py"), "--write", dest])
    if w.returncode != 0:
        print(f"❌ Writing {dest}/.cre-manifest failed: {w.stderr.strip()}", file=sys.stderr)
        return 20
    print(f"✅ @cre/constructs {version} (template {sha[:9]}) → {dest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
