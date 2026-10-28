---
inclusion: auto
name: cre-failure-injection
description: Injecting a failure on purpose: choosing a mechanism (SSM parameter, proxy, stage variable, AWS FIS), off at deploy, least privilege, bounded, and the arm/disarm controls in the Locust UI (/inject). Read when designing or building a failure, a failure scenario, or its operator controls.
---
# Failure injection: choose the mechanism the story needs

The template ships **no** failure-injection construct, script or helper. A failure is part of the
project's design: what breaks, where, and how it reaches the system decide the mechanism, and the
demos built so far each needed a different one. Pick one from the approaches below (or design
another), wire its controls into the Locust UI, and record the choice in the design.

The examples named below are demos on internal GitLab (ask the user for access if a clone
fails): `request-router-metadata-app`, `five-nines-app` and
`distributed-dns-update-app`, each at `git@ssh.gitlab.aws.dev:aws-cre/resilience-demos/<name>.git`
(web: `https://gitlab.aws.dev/aws-cre/resilience-demos/<name>`). Clone one beside this project to
read the files a section cites.

## Rules every mechanism follows

- **Off at deploy.** A fresh deploy is healthy: the knob's initial value is "disarmed", an FIS
  template does nothing until an experiment starts. The green build and the first deploy never
  inject anything.
- **Arm, disarm and status.** Three operations, each idempotent. Status reads the live state (the
  parameter, the stage variable, the running experiment), never a value cached in the UI. An
  operator page shows the current state before it enables its buttons.
- **Disarm is a recovery, not only a flag.** If the failure leaves damage behind (deleted records,
  a wedged state machine, a scaled-down fleet), the disarm path repairs it, or the design says
  how the system recovers by itself and how long that takes.
- **One small control plane, least privilege.** One Lambda (or SSM Automation document, or FIS
  role) flips the knob, with IAM scoped to exactly that resource, and **both** the read (status)
  and the write (arm/disarm), or status fails with AccessDenied and the page never enables its
  buttons (the demo AGENTS.md's bug class 5):
  - SSM: `ssm:GetParameter` and `ssm:PutParameter` on the parameter ARN.
  - API Gateway: `apigateway:GET` and `apigateway:PATCH` on the one stage ARN.
  - FIS: `fis:StartExperiment` on the template ARNs **and** on
    `arn:<partition>:fis:<region>:<account>:experiment/*` (the action needs both resource types);
    `fis:StopExperiment` and `fis:GetExperiment` on `experiment/*`; `fis:ListExperiments` on `*`.
- **Never unauthenticated.** A control API is private (API Gateway `PRIVATE`, bound to the VPC's
  execute-api endpoint) with an explicit Deny when `aws:sourceVpce` isn't that endpoint, or uses
  IAM auth. The operator reaches it through the Locust UI, which is behind CloudFront Signer.
- **Readers fail safe.** Code that reads a knob decides what a read error means, and says so in a
  comment: falling back to "off" stops a failure flickering on; keeping the last value stops it
  flickering off mid-demo. Cache the read (3–5 s) so the knob isn't read per request.
- **Bounded.** A failure that runs unattended has a limit: an FIS stop-condition alarm, an
  orchestration that always ends by disarming, a duration.
- **One mechanism per project** (PDD, the `/pdd` planning skill that designs the demo: one owner per concern). A second kind of failure is a second
  target on the same control plane, not a second control plane.
- **FailureScenario:** everything here lives in `src/deploy` (`cre-failure-scenario.md`, in a failure-scenario project, "How the
  failure reaches the clean system"). Prefer mechanisms that change nothing in `src/clean`.

## Approaches

### A. A stage variable on an API Gateway endpoint (the dependency answers differently)

The failure is a dependency's *answer*: an error code, a "moved", a slow response. The dependency
is an API Gateway endpoint (often a `MockIntegration` standing in for a real service), and its
mapping template branches on a stage variable:

```ts
// request template on the dependency's mock integration
'#if($stageVariables.failureEnabled == "true" && $input.path(\'$.tableId\') == $stageVariables.badTable)\n' +
'{"statusCode": 421}\n#else\n{"statusCode": 200}\n#end'
// stage: variables { failureEnabled: 'false', badTable: 'tbl-01099' }
```

Arm/disarm is the API Gateway `UpdateStage` call (`patchOperations: [{ op: 'replace', path:
'/variables/failureEnabled', value: 'true' }]`); in IAM that is `apigateway:PATCH` on the stage
(`arn:aws:apigateway:<region>::/restapis/<id>/stages/<stage>`): there is no `apigateway:UpdateStage`
action. It takes effect on the live stage at once, with
no redeploy. Call it either from the Locust controller (`addToControllerRolePolicy`) through a
`com.amazonaws.<region>.apigateway` interface endpoint (private DNS on), or from a Lambda outside
the VPC behind a small private API (`POST /unavailable`, `POST /available`, `GET /` →
`{"mode": ...}`), as request-router-metadata-app does. API Gateway's own AWS-service integration
can't call `apigateway`, so that toggle can't be a plain integration.

*Fits:* the system is unchanged and only sees a dependency misbehave; the failure can be scoped
(one table, one partition) by more stage variables. *Example:* request-router-metadata-app,
`src/deploy/backend/cdk/fault-spec.ts` (the template) and `mode-toggle-api.ts` (the toggle).

### B. An error-rate parameter the system's code reads

An SSM String parameter holds a percentage (`"0"` at deploy), one per target if the story has
several (sync path, async ingest, queue processing). The functions or instances read it (cached)
and fail that fraction of requests: return a 500, raise so the queue redelivers, or decide per
message ID (`hash(id) % 100 < rate`) so a retried message fails the same way.

Arm/disarm is `ssm:PutParameter` (rate 0 disarms), from a Lambda behind a private API
(`POST {"target": "sync", "rate": 25}`, `GET` → every target's rate). One parameter per region
lets the failure hit one region only.

*Fits:* "N% of requests fail here", where the story needs a dial rather than a switch, and the code
that fails is yours. *Costs:* the system's code must read the knob, so in a FailureScenario the
check goes in a wrapper the overlay adds, never in `src/clean`. *Example:* five-nines-app,
`src/cdk/lib/nested-stacks/application-workload-stack.ts` (parameters, `InjectFn`, `/inject`),
`src/lambda/inject/handler.py`, and each handler's reader.

### C. An orchestrated fault: a proxy or Lambda that runs a sequence

The failure is a *sequence* of events (a race, a stale write, a cleanup at the wrong moment), not
a steady error. A deploy-only component sits in the path and decides, request by request, what to
let through. distributed-dns-update-app points the workers' SDKs at a proxy
(`AWS_ENDPOINT_URL_ROUTE_53`, `AWS_ENDPOINT_URL_DYNAMODB`, one URL path per worker) — an internal
ALB in front of a Lambda that re-signs and forwards to the real services. Armed (an SSM `on`/`off`
flag), it pins one worker to a stale plan, holds its lock, lets the fleet advance, then releases
it and the cleaner in the order that reproduces the incident. Its progress lives in a DynamoDB item
with optimistic locking, because Lambda invocations race.

Arm/disarm are SSM Automation documents (`aws ssm start-automation-execution --document-name
<project>-inject-fault | <project>-restore`, or the console), each running as a role scoped to the
flag, the state item, the schedule rule and the proxy. Restore disarms, clears the state, invokes
the proxy's repair action, then lets the fleet re-converge.

*Fits:* the failure only happens when things line up, and the story is that they line up. The
system's code is unchanged (only its endpoints are redirected). *Costs:* the most code; test the
sequence offline. *Example:* distributed-dns-update-app, `src/deploy/cdk/lib/fault-proxy.ts`,
`src/deploy/proxy/handler.py`, `src/deploy/cdk/lib/deploy-stack.ts`.

### D. An AWS FIS experiment

The failure is infrastructure: an AZ's network slows or drops packets, instances stop or burn CPU,
a dependency is unreachable from a subnet. Define `fis.CfnExperimentTemplate`s (there is no L2) in
the overlay; nothing happens until `fis:StartExperiment`. Check the action list in the
[FIS actions reference](https://docs.aws.amazon.com/fis/latest/userguide/fis-actions-reference.html)
for the resource you target; per-AZ templates (a target filter on the AZ) tell an AZ story.

- **A stop condition always**: `stopConditions: [{ source: 'aws:cloudwatch:alarm', value:
  alarm.alarmArn }]` with the project's availability alarm, never `source: 'none'`.
- **The FIS role**: assumed by `fis.amazonaws.com` with `aws:SourceAccount` and an `aws:SourceArn`
  of `arn:<partition>:fis:<region>:<account>:experiment/*`; the AWS-managed
  `service-role/AWSFaultInjectionSimulator*Access` policies for the target types used.
- **Logs**: `logConfiguration.cloudWatchLogsConfiguration` to a log group the role can write to.
  The L1 types it as untyped JSON and passes it verbatim, so write CloudFormation's casing:
  `{ LogGroupArn: logGroup.logGroupArn }`, not `logGroupArn` (the same for `s3Configuration`:
  `BucketName`, `Prefix`).
- **SSM-document network faults** (`aws:ssm:send-command` with `AWSFIS-Run-Network-Latency-Sources`
  / `-Packet-Loss-Sources`) run only on EC2 instances with the SSM agent, on Amazon Linux 2023,
  Ubuntu, RHEL 8/9 or CentOS 9 (ECS tasks: the next bullet). `Sources` and
  `DurationSeconds` are **required**: without them the document fails. `Sources` is a
  comma-separated list of IPv4 addresses, CIDRs, domain names, AZ names or IDs, `ALL`, `DYNAMODB`
  or `S3`. The FIS role also needs `ec2:DescribeInstances` and `ec2:DescribeSubnets`.
- **ECS and Fargate tasks** (`aws:ecs:task-network-latency` / `-packet-loss` / `-blackhole-port`,
  `-cpu-stress`, `-kill-process`) also run SSM documents, so the task definition needs what the
  [ECS task actions page](https://docs.aws.amazon.com/fis/latest/userguide/ecs-task-actions.html)
  lists, or the experiment fails when it starts, after the merge: a non-essential
  `amazon-ssm-agent` sidecar container running AWS's registration script, with
  `MANAGED_INSTANCE_ROLE_NAME` set; a managed-instance role (`AmazonSSMManagedInstanceCore` plus
  `ssm:DeleteActivation`, `ssm:DeregisterManagedInstance`); the task role granted
  `ssm:CreateActivation`, `ssm:AddTagsToResource` and `iam:PassRole` on that role; for the network
  and kill-process actions `pidMode: ecs.PidMode.TASK` (on Fargate with `runtimePlatform` set); for
  Fargate network actions `enableFaultInjection: true` on the task definition and
  `useEcsFaultInjectionEndpoints: 'true'` on the action; ECS Exec off. The FIS role needs
  `ecs:DescribeTasks`, `ssm:SendCommand`, `ssm:ListCommands`, `ssm:CancelCommand`. The three network
  actions can't run at the same time. In isolated subnets AWS's script fails at its `dnf install`
  (no internet): use the FIS non-root agent image (its dependencies are pre-installed; URIs on that
  page), and give the VPC the SSM agent's `ssm`, `ssmmessages` and `ec2messages`
  endpoints.
- **"An AZ loses its network"** is usually simpler without any agent: `aws:network:disrupt-connectivity`
  on `aws:ec2:subnet` targets filtered by AZ swaps in a network ACL that denies traffic for the
  duration (`scope`: `all`, `availability-zone` for traffic to the other AZs, `s3`, `dynamodb`,
  `prefix-list`, `vpc`), whatever runs in the subnets (Lambda, ALB, Fargate). The FIS role needs the
  `ec2:*NetworkAcl*`, `ec2:CreateTags`, `ec2:DescribeSubnets` / `DescribeVpcs` /
  `DescribeManagedPrefixLists` / `GetManagedPrefixListEntries` grants the
  [actions reference](https://docs.aws.amazon.com/fis/latest/userguide/fis-actions-reference.html) lists.
- Arm is `fis:StartExperiment` (template id), disarm `fis:StopExperiment` (experiment id), status
  `fis:GetExperiment` / `ListExperiments`, from a Lambda or the Locust task role, with the grants
  listed under "One small control plane".

*Fits:* the story is an infrastructure fault the system should survive; nothing in the system
changes at all. *Costs:* targets must be FIS-supported resource types; per-experiment limits.

### Other knobs

A value the system already reads as configuration (a timeout, a pool size, a downstream endpoint, a
feature flag) changed by the overlay's control plane; scaling a fleet down; a security-group rule
that cuts a path. Same rules: off at deploy, arm/disarm/status, scoped IAM, bounded.

## Operator controls in the Locust UI

The Locust controller (or standalone) task hosts the operator's pages, behind the same CloudFront
Signer auth as the Locust UI, and reachable from an agent with the `locust-cfs` skill
(`.kiro/skills/locust-cfs/scripts/lc.py GET /inject/current`, from the project root). Build them with `LoadGenerator` (`exposeWebUi: true`):

- **A page**: `loadGenerator.addWebPage('/inject', html)`. A route can't share a page's path, whatever
  its method (refused when the container starts): `/inject` the page, `/inject/arm` the route. Write links and `fetch()` calls as
  `{{BASE_PATH}}/inject/...`; the base path (`/locust` behind the edge) is filled in.
- **Its handlers**: in the `workloadDirectory` locustfile, `from cre_pages import route` and
  `@route('/inject/<mode>', methods=['POST'])`. Paths are relative to the base path. Don't add
  routes to `web_ui.app` directly: Locust's base path doesn't apply to them.
- **What the handlers call**: the control API's URL (a stack output passed in with
  `loadGenerator.addEnvironment('INJECT_URL', url)`) over the VPC's execute-api endpoint, or AWS
  directly (`ssm`, `fis`, `ssm:StartAutomationExecution`) with the permissions granted by
  `loadGenerator.addToControllerRolePolicy(...)` (the handlers run in the controller; workers never
  get the grant). In the private network (isolated subnets) each service called needs its
  interface endpoint on the VPC (`new ec2.InterfaceVpcEndpointAwsService('ssm')`, `'fis'`, ...):
  in a demo, in the network stack's `interfaceEndpoints`; in a failure scenario, added from the
  overlay (`cre-failure-scenario.md`). Synth refuses a grant without one (it only warns for a VPC
  imported from outside the app, whose endpoints it can't see).
  Give every AWS and HTTP call explicit, short timeouts.

**Individual actions.** One route per operation: `GET /inject/current` (status, read live),
`POST /inject/unavailable` (arm), `POST /inject/available` (disarm), plus the story's other levers
(`POST /scale/desired`, `POST /flush`). The page loads the current state first, keeps its buttons
disabled until then, and re-reads after each action. With several targets or regions (approach B),
one row per target with its own rate and region. Examples: request-router-metadata-app
`src/deploy/client/locust/locustfile.py` (`/inject`, `/scale`, `/flush`), five-nines-app
`src/locust/locustfile.py` (`/inject` per target and region).

**An orchestrated run.** One button that runs the whole story: `POST /orchestrate/start` spawns a
`gevent` greenlet in the controller that steps through phases by calling the same local routes
(and Locust's own `/swarm`, `/stop`), `GET /orchestrate/status` reports the phase and timings for
the page to poll, `POST /orchestrate/cancel` sets a flag the greenlet checks between steps. The
request router's run: scale the fleet up → wait until it is ready → ramp users slowly to the
target → hold a healthy steady state → arm → hold → disarm with load still on (does it
self-heal?) → done. Rules:

- **Always clean up**: a `finally` that stops the swarm, disarms and returns the fleet to rest,
  whether the run finished, was cancelled or failed.
- **One run at a time** (a lock; `start` answers 409 while running), and the state behind a lock.
- **Wait for conditions, not just time**: advance when the fleet is healthy or the users reached
  the target, with a cap so a stuck step can't hang the run.
- **Phase durations** as environment variables with defaults, so a rehearsal can run shorter.

Link the pages from the dashboard header: `CreDashboardStack` `links`, with a `path` under the
Locust URL. `links` replaces the default links, so keep them: `links: [...DEFAULT_DASHBOARD_LINKS,
{ label: 'Arm / Disarm failure', output: 'EdgeLocustUrl', path: 'inject' }]`.
