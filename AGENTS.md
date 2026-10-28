# AGENTS.md — cre-github-setup-test

Conventions and gotchas for agents working in this generated CRE demo. This demo was
scaffolded from `cre-demo-template`; build the actual demo on top of the green base via PDD.

The `/pdd` planning skill is linked **into this repo** at `.kiro/skills/pdd/` — a symlink to your
`cre-demo-template` clone, so it is never a stale copy (reached from Claude Code through the
`.claude/skills` symlink), so run `/pdd` here rather than in the template. It asks for the path to your
`cre-demo-template` clone — normally a sibling directory, which it auto-detects and asks you to
confirm. It reads four things live from there, and its Phase 1 blocks if any are missing:
`constructs/README.md`, `patterns.md`, `patterns/`, and `recipe/recipe.md`.

## Project Generation: projen

This repo is **projen-managed** and built on a CRE project type (`CreResilienceDemo`,
`CreFailureScenario` or `CreCustomerSandbox`) vendored at `projenrc/cre/`. The source of truth is
`.projenrc.ts`; after editing it, run `npx projen`. What never to edit by hand (generated files,
`projenrc/cre/`, `cre-constructs/`) is listed once, in `.kiro/steering/cre-project-structure.md`
("What not to edit").

The steering in `.kiro/steering/cre-*.md` covers what kind of project this is, the source layout,
tooling, `cre-tasks.md` (every task and switch) and stacks and CI/CD (`cre-pipelines.md`): those
are always loaded. The rest loads when a task needs it (Kiro reads it by its description; Claude
Code: `CLAUDE.md` lists when): the deploy variables and jobs (`cre-pipelines-detail.md`), the
construct library (`cre-constructs.md`), load generation, failure injection and fidelity. Git is
the `cre-git` skill (below).

## Stacks and the pipeline

The CDK app (`src/**/cdk/app.ts`) is the whole definition of what deploys: one `CreStack` per
deploy job, values between stacks as `CreCfnParameter`s. After changing a stack or a parameter, run
`npx projen` and commit what it regenerates with the code. The rules: `.kiro/steering/cre-pipelines.md`.

## Git workflow

Use the projen git tasks (`branch`, `commit`, `push`, `git:status`), never git push or MRs/PRs by
hand, and never commit or push without explicit approval. When a task stops, act on `code=<n>` on
its `RESULT` line (`npx projen` itself exits 1 for every failure), and don't repair git state by
hand. The forms and codes: the `cre-git` skill (`.kiro/skills/cre-git/SKILL.md`).

**Editing a skill leaves this repo**: the skills are symlinks into your template clone (what that
means and what to do: "Skills are shared tooling" in `.kiro/steering/cre-tooling.md`).

## Bug classes CRE demos keep re-hitting

Carried forward from demos already built on this template. These are **not** hypothetical — each
one shipped, was diagnosed the slow way, and cost hours. Read before writing code. Record this demo's own findings
in `docs/agents-local.md`, not here (see the end of this file).

1. **projen's dax shell is not bash.** `${VAR}` braces pass through **literally**, and `set -a`
   is unimplemented (`set: invalid option: -a`). A step that needs bash declares it on that step —
   `{ exec, shell: ['bash', '-c'] }` — which hands the command to bash as one argv element, so no
   quoting invariant applies; the deploy's head-bucket checks run that way (its stack steps are
   Python scripts now). Where a *fragment* had to be wrapped in `bash -c '…'` instead (the old
   deploy's `${VAR:-default}` env defaults), that wrapper has one invariant: **the generated
   command must contain no single quotes.** An inner `bash -c '…'` broke a real deploy with
   `unexpected EOF` seven phases in. Audit `.projen/tasks.json` after
   every regen and validate the **generated** `exec` string (`bash -n`), not the snippet you
   wrote — the project type's `deploy-tasks.test.ts` (in `cre-demo-template/packages/cre-projen`)
   does both checks for the deploy tree.
2. **Default botocore timeouts defeat design guarantees.** Hit *six* separate times in one demo.
   Every boto3 client needs an explicit
   `botocore.config.Config(connect_timeout=…, read_timeout=…, retries=…)` at **module scope**,
   sized under the caller's budget. A *hanging* call outlives the caller's own timeout, so the
   `except`/retry path never runs and the failure looks like a hang, not an error.
3. **Spec values that do not exist in reality.** Engine versions, prop names, and API fields that
   appear in a design doc but not in the SDK. Verify against the installed `aws-cdk-lib` `.d.ts`
   or the live API before trusting any design doc — including a design doc you wrote.
4. **Template-vs-deploy-contract mismatches that synth cannot catch.** A `CfnParameter` declared
   but never threaded through the deploy breaks the *deploy*, not the build. The project type
   closes this: every parameter without a default must be a `CreCfnParameter` (the synth refuses
   others), the deploy jobs are generated from them, and `check:stack-plan` fails the build when a
   template and its job disagree. Don't write your own threading test; don't bypass these with a
   plain `CfnParameter` with no default.
5. **IAM grants that are necessary but not sufficient.** `secretsmanager:GetSecretValue` without
   `kms:Decrypt` on a CMK-encrypted secret fails as *"Access to KMS is not allowed."* A failure
   knob a control plane both reads (status) and flips needs **both** grants. Worse when the caller degrades instead of raising —
   one demo answered from a stub for days and looked healthy.
6. **UI fields nothing supplies.** A SPA reading `foo.bar` when the backend returns a bare list
   renders `—` or `?` rather than failing. If the UI reads a field, something must produce it —
   and a test fixture claiming to mirror a real payload **must actually mirror it**, or the test
   is green against a fiction.
7. **An HTTP front door is NOT a secure browser context.** `crypto.randomUUID()` and friends throw
   there; called at module scope they kill the whole SPA, which looks exactly like a backend
   outage. A UI served over plain **HTTP** (no certificate) is not a secure context — never add a
   secure-context-only browser API to it without adding TLS first. CloudFront Signer, the default
   front door, serves HTTPS.
8. **Never pattern-match LLM prose to drive state.** In agentic demos the model paraphrases tool
   output, so prose-matching silently stops firing. Emit a structured event from the tool instead.
9. **A projen spawn step's `env` travels exactly one hop, and a task tree only your mode runs
   has never been run.** `deploy → deploy:upload → deploy:s3` is two hops, so a per-region
   `{ spawn: 'deploy:upload', env: { AWS_REGION } }` renders a correct `tasks.json` and still
   publishes every region to the ambient CI bucket; the failure surfaces one phase later as a
   CloudFormation *"S3 error: The specified bucket does not exist"*. Three demos paid for this.
   Put per-region values in the leaf step as synth-time literals (what `deploy:s3` does now) or
   in a *task*-level `env`, never in a spawn step's `env` — and give any opt-in task tree an
   **execution** test, because a declaration test was green while the deploy was broken.

### Operational facts worth knowing early

- **Endpoints go behind CloudFront Signer (Midway) by default** (`auth/cloudfront-signer`).
  If a demo uses `AllowedCidrSecurityGroup` instead, every deploy resets `AllowedCidr` to
  `0.0.0.0/32` (a deny-all default); re-open it after each deploy and close it when done.
- **`yarn` (berry), never `npm install`** — an `npm install` corrupts `yarn.lock`.
- **Python 3.11+** for anything agentic (`strands-agents` needs ≥3.10; system `python3` may be
  3.9).

### Process that works

Prove a new test **fails without its fix** before landing it — several tests have been green for
the wrong reason until checked that way. And verify claims against the **live account** rather
than inferring them from a green pipeline: the worst defects found so far were only findable that
way.

## Detailed Documentation

`CLAUDE.md` imports this file for Claude Code (`@AGENTS.md`) and adds the steering it must read itself.

**Project-specific conventions and findings go in `docs/agents-local.md`** (create it), never in
this file or `CLAUDE.md`: `npx projen update-template` refreshes both from the template while
they are unedited, and leaves an edited one as it is (writing the template's new version to
`.cre/template/AGENTS.md` to compare with), so an addition here stops the template's new bug
classes reaching this demo. Read `docs/agents-local.md` too when it exists.
