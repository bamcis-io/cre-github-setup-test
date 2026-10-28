---
inclusion: always
---
# This project is a Failure Scenario

**Purpose:** a working system with an intentional failure built in, used as a benchmark to rate
how well a resilience agent finds it. The scenario can come from anywhere: the user's own
description, an AWS COE, a public post-mortem, a blog post. Whatever it is, the fidelity rules apply.

## `src/clean` — the system under review

The review agent is told to review **only `src/clean`**, and is not allowed to look outside it.

- It must read as an ordinary, working system. No identifiers, comments, file or directory names,
  docs, or test names that point at the failure, the incident or source it came from, or the fact
  that this is a scenario.
- It keeps only the monitoring the recreated system would really have had.
- `npx projen test` runs **`test:clean-purity`** over it: a generic list of tells (scenario words,
  failure-injection tooling, the CRE edge, dashboard and load generator, "demo" names, provenance,
  editorializing like "metastable") plus this scenario's own terms, and it fails if `src/clean`
  imports anything from `src/deploy`. Put the source system's names, class names, incident ids and
  commit SHAs in the `forbiddenTerms` option in `.projenrc.ts` as soon as you know them (literal
  text, case-insensitive).
- A generic pattern that catches an ordinary word (a real `incident` table, a `benchmark` module):
  allow it for that file in `.projenrc.ts`, `cleanPurityAllow: ['<path> <pattern>']`, the pattern
  exactly as `clean-purity-scan.py` lists it. Never reword real domain code to dodge the scan, and
  never allow a scenario term.
- Import constructs from `@cre/constructs` in either tree. Failure injection, load generation, the
  edge and the dashboard are imported only in `src/deploy`.

### How the failure reaches the clean system

The overlay may import the system; the system never knows the overlay exists. So the failure is
triggered through something an ordinary system already has. The mechanisms themselves (stage
variable, error-rate parameter, orchestrated proxy, FIS) are in `cre-failure-injection.md`; here is
how each reaches a clean system:

- **Infrastructure the overlay changes:** an FIS experiment, or a value the system already reads as
  configuration (a timeout, a pool size, a downstream endpoint, a feature flag) that the overlay's
  tooling changes. Prefer this: nothing in `src/clean` changes.
- **A dependency that answers differently:** the overlay overrides the behaviour of something the
  system calls (a mock integration's response template with a stage variable, as
  request-router-metadata-app does), or redirects the system's SDK endpoints to a proxy
  (`AWS_ENDPOINT_URL_<SERVICE>` environment variables, as distributed-dns-update-app does). The
  system's code is unchanged. If `src/clean` must accept the override, the hook reads as ordinary
  configuration, named for what it is (`dependencyResponseTemplate`, not `faultBehavior`), with
  a healthy default; only the overlay passes the failing one. request-router's `FAULT_BEHAVIOR`
  is a demo, where naming the fault is fine; in a scenario it would point the reviewer at it.
- **An error-rate parameter** needs code that reads it, so it can't go in `src/clean`: the overlay
  wraps the system's entrypoint instead, and the clean handler is untouched. A Lambda's handler
  resolves inside its code asset, which is the clean tree's, so the wrapper reaches the function as
  a **layer** (Python puts `/opt/python` on the import path):
  1. `src/deploy/app/layer/python/wrapper.py` reads the parameter (cached; its name in an
     environment variable), fails the configured fraction of calls, and otherwise imports and calls
     the clean handler named in its `CLEAN_HANDLER` environment variable.
  2. In `src/deploy/cdk`, on the function `createSystem` returned:
     `fn.addLayers(new lambda.LayerVersion(fn.stack, 'Overlay', { code: lambda.Code.fromAsset('src/deploy/app/layer') }))`,
     `fn.addEnvironment('CLEAN_HANDLER', '<module.function it had>')`, the parameter's name in
     another variable, `param.grantRead(fn)`, and the handler override
     `(fn.node.defaultChild as lambda.CfnFunction).handler = 'wrapper.handler'`.
  A container: the overlay builds an image `FROM` the clean one with the wrapper as its entrypoint.

## `src/deploy` — the overlay

Everything that injects, triggers, drives or observes the failure lives here:

- `src/deploy/cdk/app.ts` is the CDK entrypoint. It calls **`createSystem(app)`** from
  `src/clean/cdk/system.ts`, then adds the overlay's stacks. The system's stacks *and their
  topology* (regions, which stack takes which value from which) are created in `createSystem`, so
  the reviewer sees the whole system. `createSystem` returns the stacks the overlay wires to, each
  exposing what it needs as public fields (the ALB and VPC behind a UI, a function to wrap, the
  resources the dashboard watches); give `System` those stack classes (`{ api: ApiStack; data:
  DataStack }`) rather than `Record<string, CreStack>`. Never create a system stack in `src/deploy`.
- **The edge origin is created by the overlay, in the system's stack:** `CreEdgeOrigin` is a CRE
  tell, so it never appears in `src/clean`. In `src/deploy/cdk`:
  `const origin = new CreEdgeOrigin(system.api, 'EdgeOrigin', { name: 'app', loadBalancer: system.api.alb, vpc: system.api.vpc })`,
  then the route `{ name: 'app', pathPattern: '/app*', origin }` in the `CreEdgeStack`. The
  construct lands in the system stack's template, but its source is in the overlay.
- **Metrics without CRE tells:** `src/clean` can't use `DEMO_METRIC_NAMESPACE`
  (the scan refuses it). The overlay's dashboard charts what the system really emits: AWS
  service metrics (`AWS/Lambda`, `AWS/ApplicationELB`, ... as `new cloudwatch.Metric(...)` or the
  construct's `metric*()` methods), the system's own metrics in its own namespace (a realistic name,
  written in `src/clean`), and the load generator's client metrics (the demo namespace,
  `observability.metric()`).
- **The load generator, and the endpoints only the overlay needs, are added by the overlay to a
  system stack**, like the edge origin: never in `src/clean`'s props, where an `fis` or `ssm`
  endpoint would hint at the answer. In isolated subnets it needs ECR, ECR_DOCKER and
  CLOUDWATCH_LOGS, plus one per service the `/inject` handlers call; synth checks every endpoint on
  the VPC, wherever it was added:
  ```ts
  const loadGen = new LoadGenerator(system.api, 'LoadGen', { vpc: system.api.vpc, targetUrl, exposeWebUi: true });
  for (const svc of [ec2.InterfaceVpcEndpointAwsService.ECR, ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
    ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS, new ec2.InterfaceVpcEndpointAwsService('ssm')]) {
    system.api.vpc.addInterfaceEndpoint(`Overlay${svc.shortName}`, { service: svc });
  }
  ```
  A clean system that already has an endpoint the overlay needs (an ordinary ECR endpoint) keeps
  it, and the overlay doesn't add it again.
- **Dashboards are required, and they live here**: the overlay that injects the failure also makes
  it observable. (This is steering, not a build check.)

## Ground truth (required)

The answer key is `ground-truth/<project-name>.jsonl` (the `groundTruthFile` option), outside
`src/clean`. `npx projen test` runs **`test:ground-truth`**: it validates the key, and fails when
there is none once the app has stacks (a fresh scaffold passes). JSON Lines, one object per
scenario: `id` (unique), `category`, `notes`, `findings[]`. `category` is always `ngrh_real`:
`ngrh` is Next Gen Resilience Hub, whose findings the benchmark compares, and `_real` marks the
scenario as a real finding rather than a non-issue.

Every finding has `finding_id` (unique), `type`, `severity`, `location_hint` and `description`.

- **Gaps** (`type: gap`): the actual failure, one or more. `severity` `high` | `medium` | `low`;
  `raf_category`, the failure category from AWS's Resilience Analysis Framework:
  `single_point_of_failure`, `excessive_load`, `excessive_latency`, `misconfiguration_and_bugs` or
  `shared_fate`; `rel_category`, the Well-Architected Reliability pillar question it falls under,
  `REL-01` … `REL-13` (REL 1 service quotas … REL 13 disaster recovery); `expected_recommendation`.
- **Red herrings** (`type: non-issue`): things a reviewer might plausibly flag but should not.
  `severity: non-issue`, and a `rationale` explaining why it is not a finding.
- `location_hint` is `<path> (symbol or note)`, several joined with ` + `. The path is
  repo-relative (`src/clean/app/router.rs`) or relative to `src/clean` (`app/router.rs`); a bare
  file name after ` + ` means the same directory as the previous path. Every path must exist under
  `src/clean`.

The key needs at least one gap and at least one red herring.

## Fidelity

`docs/fidelity.md` (scaffolded) records the scenario's sources and every deviation from them
(`cre-fidelity.md`). It is outside `src/clean`.
