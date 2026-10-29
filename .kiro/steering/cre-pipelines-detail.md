---
inclusion: auto
name: cre-pipelines-detail
description: Stack placement by region and partition, the deploy variables on GitLab and GitHub (names, protected and masked values, reserved names), the generated deploy jobs, npx projen deploy run locally, and the setup tasks (setup:aws, setup:repo) with their switches. Read when a stack must go in a specific region, when adding or debugging a deploy variable or role, when a deploy job fails, or before running a setup task.
---
# CI/CD in detail: placement, variables, deploy jobs

The essentials (the CDK app is the definition, `CreStack`, `CreCfnParameter`, Rules 1 to 3) are in
`cre-pipelines.md`. This file has what a specific change needs.

## Placement the synth and `npx projen` enforce

| Stack | Region | Partition |
|---|---|---|
| `CreIamStack` (shared IAM) | its partition's IAM control-plane region | any |
| `CreDnsStack` (public records) | its partition's Route 53 control-plane region | any |
| `CreEdgeStack` (CloudFront + CloudFront Signer) | us-east-1 | `aws` only |
| every other stack | any region in the project's `partition` | |

| partition | IAM control plane | Route 53 control plane |
|---|---|---|
| `aws` | us-east-1 | us-east-1 |
| `aws-us-gov` | us-gov-west-1 | us-gov-west-1 |
| `aws-cn` | cn-north-1 | cn-northwest-1 |
| `aws-eusc` | eusc-de-east-1 | eusc-de-east-1 |

`CreIamStack` and `CreDnsStack` also carry a CloudFormation Rule that refuses any other region at
deploy time. A CloudFront distribution can't use a VPC origin in another partition.

## Pipeline variables come from the stacks

Every variable the deploy reads is named by a stack (`fromVariable` parameters and roles, plus
`CFN_EXECUTION_ROLE_ARN` when a stack uses the default execution role). `cre-stacks.json` lists them
under `environment` (`required`, `optional`, and `defaulted`: those the deploy has a value for when
unset, `BINDLE_ID`), and fails fast on an empty required variable. The assets buckets need none: each
region's is named from the project, the account and the region (`<name>-<account>-<region>-an`), and
the deploy's upload makes it, as a `<project>-cre-assets` stack, the first time a stack goes there.

- **The same names on both providers.** The pipeline's own deploy role is `AWS_CREDS_TARGET_ROLE`
  everywhere (GitLab's credential vendor assumes it; on GitHub the OIDC credentials step does), and
  every other variable has the same name as a GitLab CI/CD variable or a GitHub repository
  variable or secret.
- **GitLab:** each stack job's `variables:` reference the project CI/CD variables by name. A
  protected variable (role ARNs, masked secrets) is scoped to the `AWS` environment, which only the
  deploy jobs declare, so the build and the weekly upgrade never get deploy credentials; a plain one
  reaches every job. A default (`BINDLE_ID`) is a pipeline variable, which a project variable overrides.
- **Reserved names.** A project CI/CD variable outranks a GitLab job's own variables, so a variable
  named like one the deploy sets would silently replace it. `npx projen` refuses a `fromVariable`,
  `assumeRole` or `executionRole` with one of those names, and setup refuses to create one:
  every stack job variable (below), `ASSETS_PREFIX`, `PROJECT_NAME`, `CRE_ROLE_ASSUMED`, and any
  `STACK_*` (the names outputs travel under).
- **GitHub:** each stack job's `env` is generated with the variables that stack uses, each read as
  `vars.X || secrets.X`: a repository variable for a plain value (shown in logs), a secret for a
  role ARN or anything hidden. Set them with `setup:repo --var X=…` / `--secret X=…`; don't
  edit the workflow.

## The deploy jobs

**Output values travel as job variables.** On GitLab a value with a `$` or leading/trailing spaces
would change on the way (GitLab expands `$NAME` and trims), so the stack job refuses such an
output, naming it: give the output another value (encode it, or pass the parts separately).

- **Deploy order:** every stack after the stacks it depends on (`CreCfnParameter` sources and
  `dependsOn`), otherwise in the order the app creates them. Stacks that need nothing from each
  other deploy in parallel in CI. A cycle fails `npx projen`.
- **Every stack job has the same variables, built into the pipeline**, and `deploy:stack`
  resolves nothing: `STACK_NAME`, `TEMPLATE_NAME`, `AWS_REGION`,
  `ASSETS_BUCKET`, `OUTPUTS_PREFIX`, `OUTPUTS`, `ROLE_ARN`, `ASSUME_ROLE`, `STACK_PARAMETERS`,
  `REQUIRED_VARS`, `REQUIRED_OUTPUTS`. `STACK_PARAMETERS` references the deploy variables and the
  earlier stacks' outputs directly. `cre-stacks.json` records every job's needs and variables.
- **Outputs travel as job data:** each stack job writes the outputs later stacks take to
  `dist/<stack>.env` (`<OUTPUTS_PREFIX>_<OUTPUT>`). GitLab passes it as a dotenv artifact to the jobs
  that need it; GitHub uploads it and the jobs that need it load it into their environment. At most
  20 per stack (the default dotenv limit of a self-managed GitLab).
- **Before deploying, a stack job checks its inputs:** an empty required variable or a missing output
  stops it, naming the variable, or the stack and output.
- **Roles:** every job runs as `AWS_CREDS_TARGET_ROLE`. A stack with an `assumeRole` assumes it
  from those credentials first (role chaining), so the other account's role trusts the runner role,
  which `setup:aws --dns-profile <p>` sets up.
- **Serialised, never cancelled mid-CloudFormation.** GitLab: each stack job is serialised across
  pipelines (`resource_group`), so the stacks of one pipeline still deploy in parallel. GitHub: one
  deploy run at a time (one workflow `concurrency` group); a run still waiting when a newer merge
  arrives is cancelled, and the newer run deploys both. `npx projen watch` follows that newer run
  for the cancelled commit, so a superseded deploy isn't reported as failed.

`npx projen deploy` runs the same jobs on your machine. It checks your credentials are for the
account in `cre-setup.json`, builds (`--no-build` skips), gets every variable the jobs need (your
environment, then `.cre/deploy.env`, which setup writes, then a prompt on a terminal), and runs
`deploy:upload`, `deploy:stack` once per stack in deploy order with exactly that job's variables,
then `deploy:prune`. `--stack <id>` deploys only that stack, reading the outputs it needs from the
deployed stacks. The identity differs from CI's (yours, not the runner role's): a stack whose
`assumeRole` trusts only the runner role (a cross-account `CreDnsStack`) is refused before anything
deploys; let CI deploy it.

**A DNS zone added after the scaffold.** The zone itself is the user's (an existing public hosted
zone). In the deploy account nothing needs setting up. In another account, `npx projen setup:aws
--dns-profile <that account's profile>` deploys the stack `CreSetupDns-<project>` there and prints
`DNS_ROLE_ARN` and `DNS_CFN_EXECUTION_ROLE_ARN`. Neither sets CI/CD variables: set `DOMAIN`,
`HOSTED_ZONE_ID` and, for a zone in another account, those two role ARNs with `setup:repo`.
Run them with the user's agreement. Until then the build fails for a
zone in another account (the roles are optional in the plan, but without them the DNS stack would
deploy into this account).

The `setup:repo` switch for each kind of value:

| | Plain value | Role ARN | `noEcho` parameter's value |
|---|---|---|---|
| GitLab | `--deploy-var X=v` | `--deploy-var X=arn:…` | `--deploy-var X=@<file holding it>` |
| GitHub | `--var X=v` (Actions variable) | `--secret X=arn:…` | `--secret X=@<file holding it>` |

A variable that feeds a `noEcho` parameter is stored hidden (masked on GitLab, a secret on GitHub)
whichever switch sets it: `setup:repo` reads which ones from `cre-stacks.json` and `cdk.out/`, and
refuses (10) until `npx projen` has made them.

A `noEcho` value is read from the file (`@<file>`, one trailing newline dropped) so it is never on
the command line, in `ps` or in the shell history. Write it to a file the user chooses, and delete
the file after the run.

## Setup tasks and their switches

Generated by `npx projen` from the project's tasks (the rest are in `cre-tasks.md`). They change the AWS
account, the DNS zone or the repository: ask the user first.

### `setup:aws`

`npx projen setup:aws [--profile <p>] [--dns-profile <p>] [--allow-production <account>]...`

The CI runner role and the CloudFormation execution role in the deploy account, and with --dns-profile the DNS roles in the hosted zone's account, as CloudFormation stacks (idempotent: an unchanged stack is a no-op).

- `--profile`: the deploy account's AWS profile
- `--dns-profile`: the profile of the separate account the project's hosted zones are in: that account gets the stack CreSetupDns-<project> (the roles the DNS stack deploys and validates certificates with). Leave it out when the zone is in the deploy account
- `--allow-production`: set up an account Isengard calls production, or can't classify (repeat for the DNS account). Refused (10) without it, before anything changes. Only after the user agrees
- `--project`: set by the task (.projenrc.ts `name`); a second value is refused
- `--provider`: set by the task (the project's provider); a second value is refused
- `--partition`: set by the task (.projenrc.ts `partition`, default aws); a second value is refused
- `--github-repo`: (GitHub) set by the task (`<owner>/<repo>` from `repository`): the runner role trusts its OIDC subject, read from GitHub (gh's login); a second value is refused
- `--github-subject`: (GitHub) the OIDC subject prefix itself, instead of --github-repo (template setup); not with this task
- `--github-environment`: (GitHub) set by the task for a public repo (`AWS`): the runner role trusts that environment's jobs instead of main's; a second value is refused

Deploys CreSetup-<project> (the runner role GitLabRunner-<project> or GitHubRunner-<project>, and the shared CreCfnExecution) in the partition's control-plane region with a change set; an unchanged stack is a no-op, so a re-run takes seconds. Prints ACCOUNT_ID, RUNNER_ROLE_ARN, CFN_EXECUTION_ROLE_ARN (and DNS_ACCOUNT_ID, DNS_ROLE_ARN, DNS_CFN_EXECUTION_ROLE_ARN): `setup:repo` puts them in the CI variables. No buckets: the deploy makes each region's. Exit 0 | 10 refused, nothing changed | 11 credentials or Midway | 20 a call failed (a re-run continues). Ask the user first.

### `setup:repo`

`npx projen setup:repo [--var KEY=VALUE]... [--secret KEY=VALUE]...`

Re-apply the GitHub repo's settings and set the CI/CD variables given (idempotent: only what differs changes). A variable that feeds a noEcho parameter is stored hidden.

- `--var`: (GitHub) an Actions variable (shown in logs), in the `AWS` environment for a public repo. `KEY=@<file>` reads the value from a file
- `--secret`: (GitHub) an Actions secret (role ARNs, anything hidden), in the `AWS` environment for a public repo. `KEY=@<file>` as for --var
- `--repo`: (GitHub) set by the task (`<owner>/<repo>` from `repository`); a second value is refused
- `--environment`: (GitHub) set by the task for a public repo (`AWS`); a second value is refused

Idempotent: re-applies the repo's settings (main's protection, merge settings; GitLab the weekly upgrade token and schedule; GitHub the App and, when public, the `AWS` environment) and writes only the variables that differ. A missing repo is refused, never created. A variable that feeds a `noEcho` stack parameter is stored hidden (masked on GitLab, a secret on GitHub) whichever switch set it, read from `cre-stacks.json` and `cdk.out/` (refused, 10, until `npx projen` has made them). GitLab refuses to mask some values (10, with its message). Exit 0 | 10 refused | 11 credentials or Midway | 20 a call failed (a re-run continues). Ask the user first.
