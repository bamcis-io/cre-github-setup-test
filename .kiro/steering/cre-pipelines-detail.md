---
inclusion: auto
name: cre-pipelines-detail
description: Stack placement by region and partition, the deploy variables on GitLab and GitHub (names, protected and masked values, reserved names), the generated deploy jobs, npx projen deploy run locally, and the setup tasks (setup:aws, setup:dns, setup:repo) with their switches. Read when a stack must go in a specific region, when adding or debugging a deploy variable or role, when a deploy job fails, or before running a setup task.
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
unset, `BINDLE_ID`); the deploy also always needs `ASSETS_BUCKET_PREFIX`, and fails fast on an
empty required variable.

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
  role ARN or anything hidden. Set them with `setup:repo prepare --var X=…` / `--secret X=…`; don't
  edit the workflow.

## The deploy jobs

**Output values travel as job variables.** On GitLab a value with a `$` or leading/trailing spaces
would change on the way (GitLab expands `$NAME` and trims), so the stack job refuses such an
output, naming it: give the output another value (encode it, or pass the parts separately).

- **Deploy order:** every stack after the stacks it depends on (`CreCfnParameter` sources and
  `dependsOn`), otherwise in the order the app creates them. Stacks that need nothing from each
  other deploy in parallel in CI. A cycle fails `npx projen`.
- **Every stack job has the same variables, built into the pipeline**, and `deploy:stack`
  resolves nothing: `STACK_NAME`, `TEMPLATE_NAME`, `AWS_REGION`, `AWS_DEFAULT_REGION`,
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
  which `setup:dns` sets up.
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

**A DNS zone added after the scaffold.** `npx projen setup:dns` makes the zone's roles and records
the zone in `cre-setup.json`, but sets no CI/CD variables: it prints the one `setup:repo prepare`
command for `DOMAIN`, `HOSTED_ZONE_ID` and, for a zone in another account, `DNS_ROLE_ARN` and
`DNS_CFN_EXECUTION_ROLE_ARN`. Run it with the user's agreement. Until then the build fails for a
zone in another account (the roles are optional in the plan, but without them the DNS stack would
deploy into this account).

The `setup:repo prepare` switch `check:deploy-prereqs` prints, by kind of value:

| | Plain value | Role ARN | `noEcho` parameter's value |
|---|---|---|---|
| GitLab | `--var X=v` | `--var X=arn:…:protected` | `--var X=@<file holding it>:protected:masked` |
| GitHub | `--var X=v` (repository variable) | `--secret X=arn:…` | `--secret X=@<file holding it>` |

A `noEcho` value is read from the file (`@<file>`, one trailing newline dropped) so it is never on
the command line, in `ps` or in the shell history. Write it to a file the user chooses, and delete
the file after the run.

## Setup tasks and their switches

Generated by `npx projen` from the project's tasks (the rest are in `cre-tasks.md`). They change the AWS
account, the DNS zone or the repository: ask the user first.

### `setup:aws`

`npx projen setup:aws [--profile <p>] [--regions "<r1> <r2>"] [--runner-role <name>] [--exec-role <name>] [--allow-production[=<account>]] [--update-runner-trust-policy[=<role>@<account>]] [--force] [--discovery] [--dry-run]`

Assets buckets, CloudFormation execution role and CI runner role in the target account (idempotent).

- `--profile`: the deploy account's AWS profile
- `--project`: the project name (default: cre-setup.json)
- `--regions`: the regions to make assets buckets in (default: cre-setup.json and every region in cre-stacks.json; the partition's control-plane region always, us-east-1 in `aws`)
- `--provider`: gitlab or github (default: cre-setup.json)
- `--github-repo`: (GitHub) owner/repo the runner role trusts (default: cre-setup.json)
- `--runner-role`: the CI runner role name (default GitLabRunner / CreGitHubDeploy)
- `--exec-role`: the CloudFormation execution role name (default CreCfnExecution)
- `--move-account`: set up an account other than cre-setup.json's accountId (refused without it), and record it. Delete the old account's assets buckets first: S3 names are global
- `--allow-production`: set up in a production account (Isengard IsProduction, or an account it can't classify); `=<account>` approves only that one. Refused (10) without it. Only after the user agrees
- `--update-runner-trust-policy`: add this project to an existing runner role's trust policy, which other projects share (the diff is printed); `=<role>@<account>` approves only that role. Refused (10) without it. Only after the user agrees
- `--force`: make every decision this task takes (the switches above), for any subject; only after the user agrees to each
- `--discovery`: change nothing: print one `DECISION=<json>` line per decision the run needs (field, switch, what, change, and `approve`: the subject to pass as `<switch>=<approve>`), and one `CHECK=<json>` line per safety check, a change or not (`status`: change, in-place, create or not-needed), read from the accounts as they are now
- `--dry-run`: print the plan, change nothing

Assets buckets, the execution role, the CI runner role (idempotent). A runner role setup created keeps its `cre-runner` policy's grants and only gains this project's (shared by every project); put extra permissions in a policy of their own. Ask the user first.

### `setup:dns`

`npx projen setup:dns [--zone-id <Z...> | --supernova people --alias <a> | --supernova cre --zone-name <n>] [--domain <fqdn>] [--dns-profile <p>] [--allow-production[=<account>]] [--update-dns-role-trust-policy[=CreDnsDeploy@<account>]] [--force] [--discovery] [--dry-run]`

External DNS: check the zone, or create the cross-account DNS roles / SuperNova prerequisites.

- `--profile`: the deploy account's profile
- `--dns-profile`: the zone account's profile, when the zone is in another account
- `--project`: the project (default: cre-setup.json)
- `--regions`: regions that get certificate-validator trust (default: cre-setup.json)
- `--domain`: the project's domain (default <project>.<zone>)
- `--zone-id`: an existing hosted zone
- `--supernova`: no zone yet: `people` or `cre`; creates the zone and the SuperNova role (exit 13 until delegation completes)
- `--alias`: with `--supernova people`: your alias
- `--zone-name`: with `--supernova cre`: the zone name
- `--runner-role`: the CI runner role (default: the provider's)
- `--move-account`: the deploy account isn't cre-setup.json's accountId on purpose (refused without it)
- `--provider`: gitlab or github
- `--bindle`: with `--supernova`: the bindle named in the SuperNova request text (default: cre-setup.json's bindleId, else $BINDLE_ID, else the aws-cre bindle)
- `--allow-production`: set up in a production account (Isengard IsProduction, or an account it can't classify); `=<account>` approves only that one. Refused (10) without it. Only after the user agrees
- `--update-dns-role-trust-policy`: add this project's runner role to the existing CreDnsDeploy role's trust policy in the DNS account, which other projects share (the diff is printed); `=CreDnsDeploy@<account>` approves only that one. Refused (10) without it. Only after the user agrees
- `--force`: make every decision this task takes (the switches above), for any subject; only after the user agrees to each
- `--discovery`: change nothing: print one `DECISION=<json>` line per decision the run needs (field, switch, what, change, and `approve`: the subject to pass as `<switch>=<approve>`), and one `CHECK=<json>` line per safety check, a change or not (`status`: change, in-place, create or not-needed), read from the accounts as they are now
- `--dry-run`: print the plan, change nothing

Never deletes a hosted zone. Ask the user first.

### `setup:repo`

`npx projen setup:repo prepare|finalize [--var KEY=VALUE]... [--secret KEY=VALUE]... [--repo <owner/name>] [--app-slug <s>]`

Create/adopt the GitHub repo and set CI variables (prepare | finalize).

- `--var`: (GitHub) a repository variable (plain values; hidden ones are `--secret`). `KEY=@<file>` reads the value from a file, `KEY` alone from the environment
- `--secret`: (GitHub) an Actions secret (role ARNs, anything hidden); `KEY=@<file>` / `KEY` as for --var
- `--repo`: (GitHub) owner/repo (default: cre-setup.json)
- `--app-slug`: (GitHub) your automation App (default: cre-setup.json)
- `--app-id`: (GitHub) the App id (default: looked up from the slug)
- `--app-key-file`: (GitHub) the App's private key (default ~/.cre/<slug>.pem)
- `--wait-install`: (GitHub) wait up to N seconds for the App to be installed
- `--dir`: the project directory (default: here): `finalize` pushes it; `prepare` reads its history to check the repo is this project's
- `--no-create`: prepare: refuse (10) when the repo doesn't exist, instead of creating it. The default once this clone has pushed (new_project.py passes it too)
- `--recreate`: prepare: create the repo even though this clone pushed to it before (it was deleted since). Without it prepare refuses (10) once the clone has pushed, so a retired project never comes back by a routine re-run. Ask the user first
- `--message`: `finalize`: the first commit's message
- `--adopt-existing-repo`: `prepare`: set up a repo whose history setup didn't make (work started before the template): it gets the CI variables and role trust. `=<group/project>` or `=<owner/repo>` for only that one. Refused (10) without it. Only after the user agrees. The adoption itself (the scaffold into its main, through an MR/PR) is `new_project.py`'s (/cre-demo-init); `finalize` pushes only into an empty repo
- `--force`: `prepare`: make the decision above; only after the user agrees
- `--discovery`: `prepare`: change nothing; print a `DECISION=<json>` line when the repo has history to adopt (what it holds is the `change`)
- `--dry-run`: print the plan, change nothing

prepare: create or adopt the repo, set variables. finalize: the first push into an empty repo (one with commits: 10), protect main, and on GitLab the weekly upgrade's token and schedule (13 with NEEDS=UPGRADE_TOKEN when GitLab refuses project access tokens; everything else is done). Ask the user first.
