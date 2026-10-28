---
inclusion: always
---
# Project structure

Everything lives under `src/`. In a resilience demo or customer sandbox:

- **`cdk/`** holds all CDK: the project's stacks and its own constructs. The shared construct
  library is not here: it is the dependency `@cre/constructs` (`cre-constructs/`, see
  `cre-constructs.md`).
- **`app/`** holds all application logic: code, Dockerfiles, container build contexts, static
  assets, seed data.

A failure scenario splits both by role: `src/clean/` (the system, `cdk/` and `app/`) and
`src/deploy/` (the overlay that adds the failure), as the layout below shows.

This project's layout:

- CDK entrypoint: `src/cdk/app.ts`
- CDK (stacks, constructs): `src/cdk/`
- Application logic (code, Dockerfiles, container contexts, assets): `src/app/`
- Construct library: `cre-constructs/`, imported as `@cre/constructs` (a dependency; never edit it)

## What not to edit

- **projen-generated files** (`package.json`, `tsconfig*.json`, `.gitignore`, `.eslintrc.json`,
  `cdk.json`, `.projen/*`, `cre-stacks.json`, CI files such as `.gitlab-ci.yml`,
  `.gitlab/ci-templates/*` or `.github/workflows/*`, and `.kiro/steering/cre-*.md`). Change
  `.projenrc.ts` or the CDK app, then run `npx projen`, and commit what it regenerates.
- **`projenrc/cre/`** — the CRE project-type package, and **`cre-constructs/`** — the construct
  library (`@cre/constructs`), both installed from `cre-demo-template`. A change belongs in the
  template (via an MR there), then `npx projen update-template` here. `npx projen test` fails if
  `cre-constructs/` was edited. `projenrc/cre/steering/` holds the template's unfiltered copies of
  every steering file (every project type, both git providers): read `.kiro/steering/cre-*.md`, the ones
  written for this project.