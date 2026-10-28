---
inclusion: always
---
# CRE tooling in this repo

Run these as projen tasks from the repo root. **Every task and every switch it takes is in
`cre-tasks.md`**, generated from this project's own tasks on every `npx projen` (so it lists exactly
the tasks there are); a task that takes arguments also prints its usage with `--help`. This file
says when to use which, what to ask the user first, and the exit codes.

## The loop

1. `npx projen branch <type>/<description>`: start the work.
2. Change the code. A new or rewired stack goes in the CDK app (`CreStack` + `region`,
   `CreCfnParameter` for every value it takes: `cre-pipelines.md`).
3. `npx projen`: regenerates the pipeline and `cre-stacks.json` from the app. Run it after any
   change to stacks, their parameters, or `.projenrc.ts`.
4. `npx projen build`: must pass.
5. `npx projen push --message "<type>(<scope>): <description>"`, with the user's approval. The MR
   merges when CI passes, and the pipeline deploys every stack.

| Task | Use it to |
|---|---|
| `npx projen branch` / `push` / `commit` / `git:status` | All git work. Never push, open MRs/PRs or arm auto-merge by hand. Full rules and exit codes: the `cre-git` skill |
| `npx projen watch` | Follow one MR/PR from its checks to the deploy of its merge commit (below). Read-only |
| `npx projen` | Regenerate everything: synthesizes the CDK app (no bundling, seconds), reads its stacks, writes `cre-stacks.json` and one deploy job per stack. When the app fails to compile or synth it still writes the files (from the committed plan) but **exits 1**: fix the app and run it again |
| `npx projen build` | Regenerate, type-check `src/` (`tsc --noEmit`), synth, check, test, lint, package. Must pass before anything is pushed. Lint runs with `--fix`, so a build can rewrite source files (import order, spacing): look at `git diff` after it. With `containerImages` and no Docker here it skips building the images (warned; CI builds them; `npx projen deploy` then stops at the missing image). `CRE_REQUIRE_IMAGES=1 npx projen build` makes that an error |
| `npx projen check:generated` | CI runs it first: fails when the committed generated files aren't what `npx projen` makes (fix: `npx projen`, commit the result). Skipped locally |
| `npx projen check:deploy-prereqs` | Part of every build: fails when a stack deploys to a region with no assets bucket, needs a CI/CD variable setup never set, or takes a secret (a `noEcho` parameter) from an unmasked variable. When `cre-setup.json` has no `ciVariables` (setup didn't record them) it can't check variables: it warns, naming each one, and that warning is not a pass: confirm with the user that each is set. It prints the `setup:aws` / `setup:repo` command that closes the gap, in this provider's syntax (they record it in `cre-setup.json`; commit that). A variable the deploy has a default for (`BINDLE_ID`) isn't asked for |
| `npx projen deploy` | The pipeline's deploy, on your machine: builds, uploads, deploys each stack in order with its CI job's variables, prunes old assets. One stack or a few: `--stack` (cre-tasks.md). Stops first when your credentials are for another account than `cre-setup.json`'s, or can't assume a stack's role (a cross-account DNS stack trusts only the CI runner role: let CI deploy it). Variables come from the environment, `.cre/deploy.env` (written by setup, gitignored) or a prompt |
| `npx projen stacks` | Each planned stack's deployed status, plus **orphans**: this project's stacks that main's committed plan no longer has (renamed or removed), which keep running until deleted. On a branch that adds or removes stacks, those are marked "not yet on main": main still deploys what the branch removes |
| `npx projen stacks:outputs` | The deployed stacks' outputs (`EdgeUrl`, `EdgeLocustUrl`, ids) as `<stack> <Key>=<Value>`; `--stack <id>` for one |
| `npx projen teardown` | Delete stacks. Running it deletes, without asking: show the user `--dry-run` first (the list, in order, and why a real run would refuse now), and run it only once they agree. In a production account it refuses without `--allow-production=<account>`, which only the user's yes gives. Which stacks, orphans and what it refuses: cre-tasks.md |
| `npx projen ci:rerun` | Retry the failed jobs of the deploy of main's head (never an older commit's), or the whole deploy (a redeploy without a commit). Then `npx projen watch --sha <sha>`. Switches: cre-tasks.md |
| `npx projen cfs:onboard` | Onboard the edge distribution at CloudFrontSigner, after the first deploy of the `CreEdgeStack`, and again after that stack is renamed or torn down and redeployed (a new distribution, a new domain: 403 until it is onboarded) (Midway only). Tell the user it's needed after that first edge deploy: until then the edge answers 403. Checking and undoing it: cre-tasks.md |
| `npx projen cfs:curl` | Call anything behind CloudFront Signer (a Locust UI, an operator page) with your Midway cookie, no browser (its forms: cre-tasks.md). A POST can change the demo's state (start a swarm, arm a failure): only when the user asks |
| `npx projen setup:aws` / `setup:repo` / `setup:dns` | Re-run or repair the account, repo or DNS setup (`python3 projenrc/cre/scripts/setup/*.py`; idempotent; defaults from `cre-setup.json`). `setup:aws` also makes buckets for every region in `cre-stacks.json`; `setup:repo prepare` sets the project's CI/CD variables (protected for a role ARN, masked for a secret: the forms, and the ones that keep a secret off the command line, are in cre-pipelines-detail.md). They change the AWS account or the repo: ask the user first | <!-- gitlab -->
| `npx projen setup:aws` / `setup:repo` / `setup:dns` / `setup:github-app` | Re-run or repair the account, repo, DNS or automation App setup (`python3 projenrc/cre/scripts/setup/*.py`; idempotent; defaults from `cre-setup.json`). `setup:aws` also makes buckets for every region in `cre-stacks.json`; `setup:repo prepare` sets the repository's variables and Actions secrets (role ARNs and anything hidden are secrets: the forms are in cre-pipelines-detail.md). They change the AWS account or the repo: ask the user first | <!-- github -->
| `npx projen setup:github-public` | GitHub, a private repo going public, only when the user asks. From a clean `main`: it refuses a history holding a credential, prints what becomes readable, then makes the repo public (running it is the decision: show the user `--dry-run` first), protects `main`, regenerates the workflows (GitHub auto-merge instead of the build workflow's merge job), pushes them as a PR and waits for it to merge. Re-running carries on where it stopped | <!-- github -->
| `npx projen update-template` | Refresh `projenrc/cre/` (the project type, its scripts and this steering), `cre-constructs/`, the skill links and `AGENTS.md` / `CLAUDE.md` from your `cre-demo-template` clone, show the template's CHANGELOG since this project's version, and rebuild. An edited `AGENTS.md` / `CLAUDE.md` is left as is, and the template's version goes to `.cre/template/<file>`: `diff AGENTS.md .cre/template/AGENTS.md`, merge what you want. Which clone it uses, and testing one on another branch: cre-tasks.md |
| `npx projen skills:doctor` | Check the `.kiro/skills` symlinks into the template resolve |
| `npx projen test:update` | Rewrite jest snapshots on purpose (`test` never does, so a changed snapshot fails until you run this and review the diff) |

**Setup never asks.** A production account and a change to a shared role's trust policy are
decisions `setup:aws` and `setup:dns` refuse (10) without, and so is setting up a repo with history
setup didn't make (`setup:repo prepare`): run the task with `--discovery` first (`setup:repo
prepare --discovery`; it changes nothing and prints one `DECISION=<json>` line each, with the change),
show the user each one, and run once with the switch for each they agree to, naming the subject the
line gives (`--allow-production=<account>`; a bare switch approves any).
Never `--force` on their behalf (cre-pipelines-detail.md).

**Ask the user before** `deploy`, `teardown`, `ci:rerun`, any `setup:*`, `cfs:onboard`
(and `--offboard`, which makes the edge answer 403), `cfs:curl POST` and the `locust-cfs` skill's
`lc.py` POSTs (and its `/stop`, `/stats/reset`), `update-template`, `commit` and `push`: each changes
the AWS account, the repo, what is deployed, or the running demo.

**Never run** `npx cdk deploy`, `cdk destroy`, `cdk diff` or `cdk watch` (they bypass the pipeline's
asset prefix, parameters and roles: use `deploy`, `teardown`, `stacks`). The project's own tasks
that are never run, the CI steps never run alone, and `upgrade` (only when the user asks) are the
last lines of `cre-tasks.md`, generated from this project's tasks.

**Exit codes.** Through `npx projen`, every failure exits **1**: projen's task runner replaces the
task's code, and prints its own `Task "…" failed` line on stderr after the task's output. The
task's code is `code=<n>` on its `RESULT` line, the last stdout line that starts with `RESULT`. Act
on that, never on `$?` (a 10 or a 13 reads as a plain failure otherwise). The codes the setup tasks,
`stacks`, `stacks:outputs` and `teardown` share:

| Code | Meaning |
|---|---|
| 0 | done (or nothing to change) |
| 10 | refused, nothing changed (also credentials for the wrong account: use that account's `--profile`) |
| 11 | credentials missing or expired: refresh them |
| 13 | (setup) a person must act on each `NEEDS=` line, then re-run |
| 20 | failed part way: the message says what was done |

Every other task's codes are in its `cre-tasks.md` entry, among them the two with no `RESULT`
line: `deploy`, and `cfs:curl`, whose stdout is the response body. The git tasks, `watch` and
`ci:rerun`: the `cre-git` skill.

## Watching an MR/PR to its deploy

Only when it's needed: the user asks whether a change deployed, or something is being
troubleshot. Not after every push: the pipeline merges and deploys on its own. `npx projen watch`
follows the current branch's MR/PR through its checks, the merge and the deploy of its merge
commit; the forms, what it keys each stage on and its exit codes are in the `cre-git` skill
("Following an MR/PR to its deploy"). Never look pipelines up yourself by "the latest one".
The weekly dependency upgrade: `npx projen watch --branch cre/upgrade-main` (GitLab) /
`--branch github-actions/upgrade-main` (GitHub).

## Defaults the design starts from

For a resilience demo, and a failure scenario's overlay (`src/deploy`). A customer sandbox
reproduces the customer's design first (`cre-customer-sandbox.md`, in a customer-sandbox project): these apply only where they
don't change what is being reproduced.

- **Constructs: `@cre/constructs` first** (`cre-constructs.md`). It is a dependency (what not to
  edit: `cre-project-structure.md`).
- **Auth: CloudFront Signer.** Any endpoint a person reaches (UI, operator page, Locust, API) goes
  behind the edge (a `CreEdgeStack` with the origin as its route). Another mechanism only when that doesn't fit
  (non-HTTP, a machine caller), recorded as a deviation in the design.
- **Certificates: one `*.<DOMAIN>` per region** (`ValidatedCertificate`), when `cre-setup.json` has
  a `domain`.
- **Stacks, wiring and CI: `cre-pipelines.md`.** Every top-level stack a `CreStack` with its
  `region`; every value from another stack or a CI/CD variable a `CreCfnParameter` (never
  exports); the pipeline is generated from the app and only calls projen tasks.
- **Dashboard: a `$PROJECT_NAME-dashboard` stack** (`CreDashboardStack`, `app.stackId('dashboard')`),
  with widgets designed for this project. The scaffold has no stacks; add it (required for a
  resilience demo).

## How deploys authenticate

Every deploy job runs as `AWS_CREDS_TARGET_ROLE` (the CI runner role) and hands CloudFormation a
service role (`CFN_EXECUTION_ROLE_ARN`, or the stack's `executionRole`) as `--role-arn`; the runner
role itself only drives CloudFormation, the assets buckets and ECR. A step that calls AWS directly
outside CloudFormation needs a permission the runner role lacks: attach a policy of its own to
the role, never edit `cre-runner` (setup brings that one up to date on every `setup:aws`, keeping
what it already grants, and every project on the role shares it). A stack in another account (a `CreDnsStack` with
`assumeRole: '$DNS_ROLE_ARN?'`) assumes that role from the runner role first.

## Skills are shared tooling

`.kiro/skills/*` (`pdd`, `cre-git`, `locust-cfs`) are **symlinks** into your `cre-demo-template` clone.
Editing one changes the template, and so every demo on the machine, and does not show in this
repo's `git status`. Commit such an edit in the template, on a branch, via an MR there.
`npx projen skills:doctor` shows where each skill resolves.
