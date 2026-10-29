"""Which deploy variables feed a noEcho parameter, so setup stores them hidden (GitLab masked, a
GitHub secret) whatever switch set them.

The plan (cre-stacks.json) says which variable feeds which stack parameter; the synthesized
template in cdk.out/ says whether that parameter is NoEcho. Nothing is recorded for it: both are
what `npx projen` makes from the CDK app. No cre-stacks.json (a scaffold before its first build):
no stack takes a variable, so none is hidden.
"""
import json
import os

from result import REFUSED, Failed


def _load(path, what):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise Failed(REFUSED, f"{what} ({path}) can't be read: {e}. Run `npx projen` first.")


def noecho_variables(keys, project_dir="."):
    """The ones of `keys` that feed a NoEcho parameter of a planned stack."""
    plan_file = os.path.join(project_dir, "cre-stacks.json")
    if not keys or not os.path.isfile(plan_file):
        return set()
    plan = _load(plan_file, "The stack plan")
    fed = [(s["id"], name, src["variable"]) for s in plan.get("stacks", [])
           for name, src in (s.get("parameters") or {}).items() if src.get("kind") == "env" and src.get("variable") in keys]
    if not fed:
        return set()
    cdk_out = os.path.join(project_dir, "cdk.out")
    manifest = _load(os.path.join(cdk_out, "manifest.json"), "The synthesized app's manifest")
    templates = {a.get("properties", {}).get("stackName", aid): a["properties"]["templateFile"]
                 for aid, a in manifest.get("artifacts", {}).items() if a.get("type") == "aws:cloudformation:stack"}
    hidden, params = set(), {}
    for stack, name, var in fed:
        if stack not in templates:
            raise Failed(REFUSED, f"cdk.out has no template for stack {stack}, which takes {var}: it is older than "
                                  "cre-stacks.json. Run `npx projen` first.")
        if stack not in params:
            params[stack] = _load(os.path.join(cdk_out, templates[stack]), f"Stack {stack}'s template").get("Parameters", {})
        if str(params[stack].get(name, {}).get("NoEcho", "")).lower() == "true":
            hidden.add(var)
    return hidden
