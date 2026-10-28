#!/usr/bin/env python3
"""Check the committed stack plan against the CDK app just synthesized.

    check-stack-plan.py [cre-stacks.json] [cdk.out] [--container-images on|off]

The pipeline is generated from cre-stacks.json, which `npx projen` writes from a synth of the CDK
app (CreApp records every CreStack's region, roles, parameter sources and the outputs other stacks
take, in cdk.out/cre-stacks.json). CI never regenerates files, so a stale plan would deploy with
a stale pipeline. Fails, listing every problem, when:
  - the app's stacks (cdk.out/cre-stacks.json) differ from the ones the committed plan was
    generated from: run `npx projen` and commit;
  - a stack has a fixed environment, isn't a CreStack under a CreApp (no AssetsBucketName /
    AssetsBucketPrefix parameters) or carries CDK metadata;
  - a parameter the plan passes isn't declared, or one without a default isn't passed;
  - an output the plan says later stacks take isn't in the template;
  - a CreIamStack / CreDnsStack template lacks its control-plane-region Rule;
  - a stack's deploy job (`jobs`, exactly what the pipeline job gets) passes other parameters,
    writes other outputs, deploys to another region or under another name, or needs other stacks;
  - a stack with `assumeRole` (another account) has a container image asset, which deploy:docker
    pushes to the deploy account (review 10c D6);
  - (`--container-images off`, from the project's `containerImages`) a stack has a container image
    asset (a LoadGenerator, any DockerImageAsset): nothing would build or push it, and the deploy
    would wait on a service whose image doesn't exist (review 10c A4).

Runs right after `cdk synth` (post-compile, and ci:build:cdk). Exit 0 clean, 1 with a report,
2 when the plan or the cloud assembly is missing.
"""
import json
import pathlib
import sys

ALWAYS_PASSED = {"AssetsBucketName", "AssetsBucketPrefix"}
# The stack kinds whose template must carry a control-plane-region Rule, and the Rule's logical id.
REGION_RULES = {"iam": ("IamControlPlaneRegion", "CreIamStack"), "dns": ("DnsControlPlaneRegion", "CreDnsStack")}
AGNOSTIC = "aws://unknown-account/unknown-region"


def synthesized_stacks(cdk_out):
    manifest = json.loads((cdk_out / "manifest.json").read_text())
    stacks = {}
    for artifact_id, a in manifest.get("artifacts", {}).items():
        if a.get("type") != "aws:cloudformation:stack":
            continue
        props = a.get("properties", {})
        name = props.get("stackName", artifact_id)
        stacks[name] = {
            "environment": a.get("environment"),
            "terminationProtection": props.get("terminationProtection", False),
            "template": json.loads((cdk_out / props["templateFile"]).read_text()),
        }
    return stacks


def check(plan, stacks, app_plan=None):
    problems = []
    if app_plan is not None and app_plan != plan.get("synthesized"):
        problems.append("the CDK app's stacks changed since cre-stacks.json and the pipeline were generated: "
                        "run npx projen and commit the result.")
    listed = {s["id"]: s for s in plan["stacks"]}
    for missing in sorted(set(listed) - set(stacks)):
        problems.append(f"{missing}: in cre-stacks.json, but the CDK app doesn't create it: run npx projen.")
    for extra in sorted(set(stacks) - set(listed)):
        problems.append(f"{extra}: the CDK app creates it, but cre-stacks.json doesn't list it, so nothing would deploy it: "
                        "run npx projen.")
    for sid in sorted(set(listed) & set(stacks)):
        spec, got = listed[sid], stacks[sid]
        t = got["template"]
        if got.get("terminationProtection"):
            # The deploy creates and updates stacks through change sets, which carry no termination
            # protection: the stack would look protected in the app and not be (review 10b D17).
            problems.append(f"{sid}: sets terminationProtection, which the pipeline's deploy doesn't apply; remove it. "
                            "(The protection doesn't stop npx projen teardown, which deletes when run and asks nobody: "
                            "show the user its --dry-run list first.)")
        if got["environment"] != AGNOSTIC:
            problems.append(f"{sid}: has a fixed environment ({got['environment']}); remove env from the stack "
                            "(its region prop says where it deploys; use Aws.REGION / Aws.ACCOUNT_ID inside).")
        params = t.get("Parameters", {})
        if not ALWAYS_PASSED <= set(params):
            problems.append(f"{sid}: no AssetsBucketName/AssetsBucketPrefix parameters: make it a CreStack.")
        resources = t.get("Resources", {})
        if "CDKMetadata" in resources or "CheckBootstrapVersion" in t.get("Rules", {}) or any(
                "aws:cdk:path" in (r.get("Metadata") or {}) for r in resources.values() if isinstance(r, dict)):
            problems.append(f"{sid}: carries CDK metadata: create the app with new CreApp() (from @cre/constructs).")
        passed = set(spec["parameters"])
        for p in sorted(passed - set(params)):
            problems.append(f"{sid}: the plan passes parameter {p}, which the template doesn't declare.")
        for p in sorted(set(params) - passed - ALWAYS_PASSED):
            if "Default" not in params[p]:
                problems.append(f"{sid}: parameter {p} has no default and no source; declare it as a CreCfnParameter.")
        rule = REGION_RULES.get(spec.get("kind"))
        if rule and rule[0] not in t.get("Rules", {}):
            problems.append(f"{sid}: a {rule[1]} must carry its {rule[0]} Rule.")
        outputs = set(t.get("Outputs", {}))
        for o in spec["outputs"]:
            if o not in outputs:
                problems.append(f"{sid}: later stacks take output {o}, which the template doesn't have.")
        problems += check_job(sid, spec, plan.get("jobs", {}).get(sid))
    return problems


def check_job(sid, spec, job):
    """The pipeline job's own variables against the plan and the template-checked parameters."""
    if job is None:
        return [f"{sid}: no deploy job in cre-stacks.json; run npx projen."]
    problems = []
    env = job.get("env", {})
    keys = [line.split("=", 1)[0] for line in env.get("STACK_PARAMETERS", "").split("\n") if line]
    if sorted(keys) != sorted(spec["parameters"]):
        problems.append(f"{sid}: its deploy job passes {sorted(keys)}, not the plan's parameters {sorted(spec['parameters'])}.")
    if env.get("OUTPUTS", "").split() != spec["outputs"]:
        problems.append(f"{sid}: its deploy job writes outputs {env.get('OUTPUTS', '').split()}, not the plan's {spec['outputs']}.")
    for k, want in (("STACK_NAME", sid), ("TEMPLATE_NAME", sid), ("AWS_REGION", spec["region"]), ("AWS_DEFAULT_REGION", spec["region"])):
        if env.get(k) != want:
            problems.append(f"{sid}: its deploy job sets {k}={env.get(k)!r}, not {want!r}.")
    if job.get("needs") != spec.get("needs"):
        problems.append(f"{sid}: its deploy job needs {job.get('needs')}, not {spec.get('needs')}.")
    return problems


def image_stacks(cdk_out):
    """{stack: number of container image assets}, from the cloud assembly's asset manifests."""
    out = {}
    for f in sorted(cdk_out.glob("*.assets.json")):
        n = len(json.loads(f.read_text()).get("dockerImages") or {})
        if n:
            out[f.name[: -len(".assets.json")]] = n
    return out


def main(argv):
    images = ""
    if "--container-images" in argv:
        i = argv.index("--container-images")
        images = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
        if images not in ("on", "off"):
            print("check-stack-plan: --container-images takes on or off.", file=sys.stderr)
            return 2
    plan_path = pathlib.Path(argv[1] if len(argv) > 1 else "cre-stacks.json")
    cdk_out = pathlib.Path(argv[2] if len(argv) > 2 else "cdk.out")
    if not plan_path.is_file() or not (cdk_out / "manifest.json").is_file():
        print(f"check-stack-plan: need {plan_path} and {cdk_out}/manifest.json; run npx projen and cdk synth first.", file=sys.stderr)
        return 2
    app_plan_file = cdk_out / "cre-stacks.json"
    app_plan = json.loads(app_plan_file.read_text())["stacks"] if app_plan_file.is_file() else None
    if app_plan is None:
        print(f"check-stack-plan: {app_plan_file} is missing: create the app with new CreApp() (from @cre/constructs).", file=sys.stderr)
        return 1
    plan = json.loads(plan_path.read_text())
    problems = check(plan, synthesized_stacks(cdk_out), app_plan)
    # Mismatches between the app and the plan, then what the plan can't deploy as it is: each list
    # under its own heading (review 10d A7: an image problem was headed "doesn't match").
    mismatch, problems = problems, []
    with_role = {st["id"] for st in plan.get("stacks", []) if st.get("assumeRole")}
    for sid in image_stacks(cdk_out):
        if sid in with_role:
            # deploy:docker pushes to the deploy account's ECR; a stack in another account looks for
            # the image there (review 10c D6).
            problems.append(f"{sid} deploys to another account (assumeRole) and has a container image asset, which deploy:docker "
                            "would push to this account's ECR, not that one's. Move the image into a stack in this account.")
    if images == "off":
        for sid, n in image_stacks(cdk_out).items():
            problems.append(f"{sid} has {n} container image asset(s) (a LoadGenerator or another DockerImageAsset), but the project "
                            "doesn't build images: nothing would build or push them, and the deploy would wait on a service whose "
                            "image doesn't exist. Set containerImages: true in .projenrc.ts, then run npx projen.")
    if not mismatch and not problems:
        print(f"check-stack-plan: the CDK app matches the {len(plan['stacks'])} stack(s) the pipeline deploys.")
        return 0
    for heading, items in (("the CDK app doesn't match cre-stacks.json (the pipeline's plan)", mismatch),
                           ("the pipeline can't deploy the plan as it is", problems)):
        if items:
            print(f"check-stack-plan: {heading}:", file=sys.stderr)
            for p in items:
                print(f"  - {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
