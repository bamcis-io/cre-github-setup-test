# CRE Demo Constructs (canonical library)

This is the **canonical, source-of-truth** CDK construct library for CRE resilience
demos. It is a normal standalone TypeScript source tree (its own `package.json`,
`tsconfig.json`, `jest.config.js`) so each construct can be compiled and unit-tested
in isolation — it is **not** projen-managed.

Every project gets the **whole** library, installed like an npm package: `src/` is mirrored into
the project's `cre-constructs/` with a `package.json` naming it `@cre/constructs`, and the project
type depends on it as `link:./cre-constructs`. Projects import it by name
(`import { RegionalNetwork } from '@cre/constructs'`) and never edit it; `npx projen
update-template` reinstalls it, and a hash manifest fails a project's build if the copy was edited.
This standalone `package.json` is *not* copied; the generated demo gets its other dependencies from
its own projen `.projenrc.ts`. Each project's generated `.kiro/steering/cre-constructs.md` lists
every construct with when to use it (a test keeps that list complete). The recipe (`recipe/recipe.md`) is the single source of
truth for *how* a demo is scaffolded and which families are vendored — this README
documents *what each family is, when to pull it in, and the gotchas* surfaced building
and deploying them (M0–M8).

## Layout

```
src/
  core/                # CreStack, CreIamStack, CreCfnParameter, names, partitions
  networking/          # RegionalNetwork, ParameterizedVpc, PeeringMesh      (on-demand)
  observability/       # DemoObservability, DEMO_METRIC_NAMESPACE              (on-demand)
  auth/
    allowed-cidr/      # AllowedCidrSecurityGroup                              (on-demand)
    cloudfront-signer/ # DistributionSigner — Midway + bindle at CloudFront    (on-demand, the default)
    federate-alb/      # FederateAlbAuth — Midway via Amazon Federate at an ALB (on-demand)
    cognito/           # Cognito user pool + ALB / HTTP API variants            (on-demand)
  dns/                 # ValidatedCertificate — same- or cross-account zone    (on-demand)
  load-generation/     # LoadGenerator + base container                        (on-demand)
  index.ts             # top-level barrel re-exporting every family
test/
  *.test.ts          # synth/assertion tests per construct
```

## Build & test

```sh
npm install
npm run build   # tsc --build  →  lib/
npm test        # jest
```

Pinned versions (reproducible templates, NFR3): `aws-cdk-lib@2.248.0`,
`constructs@10.5.0`, `typescript@~5.6.3`. Do not loosen these pins.

## Every family is on-demand (the contract)

A freshly scaffolded project has **no stacks**. Its `app.ts` is only `const app = new CreApp();`
and `app.synth();`, the build is green with zero edits, and the first deploy uploads nothing. A
project may be serverless with no network at all, so its design (PDD) decides every stack,
including whether there is a VPC. No family is instantiated by the scaffold; PDD adds each one.

What PDD typically adds first: an app stack (a `CrePrivateNetworkStack` subclass if the design
needs a VPC: the workload lives in its region's network stack) and, for a `CreResilienceDemo`
(which requires one), a dashboard:

```ts
import { Construct } from 'constructs';
import {
  CreApp, CreDashboardStack, CrePrivateNetworkStack, CreStandardNetworkStackProps,
} from '@cre/constructs';

const app = new CreApp(); // no CDK metadata; every top-level stack a CreStack; records the stacks for the pipeline

// Only if the design needs a VPC (a stack with none extends CreStack instead). The network stack
// owns the VpcCidr parameter and the outputs CrePeeringStack takes, so going multi-region later is
// one more of these per region and a CrePeeringStack, with no restructuring.
class AppStack extends CrePrivateNetworkStack {   // stack name = id; synthesizer + AssetsBucket* params; no env
  constructor(scope: Construct, id: string, props: CreStandardNetworkStackProps) {
    super(scope, id, props);
    // The workload goes in this.network.vpc.
  }
}
new AppStack(app, app.stackId(), { region: 'us-east-2' });
new CreDashboardStack(app, app.stackId('dashboard'), { region: 'us-east-2' });

app.synth();
```

**App and stack classes.** `CreApp` (core) is the app: it strips CDK metadata from every template,
writes `cre-stacks.json` (the stacks the pipeline is generated from) next to the templates, and fails
the synth on a top-level stack that isn't a `CreStack` or a parameter without a default or a source.
`CreStack` is the base of every top-level stack: its deploy `region` (required), `dependsOn`,
`assumeRole` and `executionRole`; the asset-bucket synthesizer and parameters; named by its id; no
`env` (refused). `CreCfnParameter` is every parameter the pipeline passes: `from` another stack's
value (it adds the output there) or `fromVariable`.
The library also ships the standard stacks: `CreDashboardStack` (observability), `CreEdgeStack`
(auth/cloudfront-signer), `CreDnsStack` (dns), `CrePrivateNetworkStack`,
`CreEgressOnlyNetworkStack`, their base `CreNetworkStack`, and `CrePeeringStack` (networking).

**Where stacks are defined.** In the CDK app, nowhere else. `npx projen` synthesizes it (no
bundling, no asset staging), reads what `CreApp` recorded, and generates the committed
`cre-stacks.json` and one deploy job per stack, in dependency order. `check:stack-plan` fails the
build when the app has changed since. The steering file `cre-pipelines.md` is the full guide.

**No environment inside a stack.** Code refers to where a stack is deployed only through
`Aws.REGION`, `Aws.ACCOUNT_ID`, `Aws.PARTITION` and `Aws.URL_SUFFIX` (`import { Aws } from
'aws-cdk-lib'`) in template strings, never `cdk.Aws.*`, `Fn.sub`, `Stack.of(this).region`, a region
string, `arn:aws:` or an `amazonaws.com` endpoint (service principals excepted), so one template
deploys to any region and partition (commercial, GovCloud, China, EUSC). A stack that must
be in one region uses `requireDeployRegion` (a CloudFormation Rule on `AWS::Region`).

Every family (networking, observability, every auth sub-family including the default
`DistributionSigner`, `LoadGenerator`, `dns`) is **on-demand**:
the scaffold instantiates none of them, and PDD imports and wires each one the design needs.
Every on-demand construct is designed to be **inert/no-op** in its default or unconfigured state
so it never breaks the green gate.

> **PDD handoff:** the recipe's [STEP 4 — Handoff to PDD](../recipe/recipe.md#step-4--handoff-to-pdd)
> and its **on-demand pattern pull-in reference** list the constructs to use and the
> wiring (e.g. `containerImages: true`, the stacks and their `CreCfnParameter`s) for each on-demand family.
> This README is the construct catalogue; the recipe is the procedure.

---

## 1. Networking — `RegionalNetwork`, `ParameterizedVpc`, `PeeringMesh`

**What it is.** A deploy-time-CIDR VPC (`ParameterizedVpc` builds `CfnVPC`/`CfnSubnet`
directly via `Fn::Cidr` + `Fn::GetAZs`, so ONE template deploys to N regions with
different `VpcCidr` parameter values) wrapped by `RegionalNetwork`, plus the optional
cross-region `PeeringMesh`.

**On-demand:** `RegionalNetwork` (single-region isolated VPC by default) is the default way to add
a VPC when the design needs one (or `CrePrivateNetworkStack` / `CreEgressOnlyNetworkStack`, below).
`PeeringMesh`/multi-region is also **on-demand** (degrades to a no-op below two regions).

**Single-region usage:**

```ts
import { RegionalNetwork } from '@cre/constructs';

const network = new RegionalNetwork(this, 'Network', {
  vpcCidr: vpcCidr.valueAsString, // CfnParameter token; default single PRIVATE_ISOLATED tier, 3 AZs
});
// network.vpc: ec2.IVpc   network.peeringRouteTableIds: string[] (consumed by PeeringMesh)
```

Key optional `RegionalNetworkProps`: `subnets` (default one `PRIVATE_ISOLATED` tier),
`azCount` (default 3), `ipMode` (`ipv4` default, `dualstack`, `ipv6`),
`natGatewayPerAz` (one NAT gateway per AZ, in that AZ's public subnet), `includeInternetGateway`, `interfaceEndpoints` (default `[]` — each costs
money, opt in), `scopedIngress` (token-safe `CfnSecurityGroupIngress` to scope an interface
endpoint's SG to an IPv4 `allowCidr` and/or IPv6 `allowCidrIpv6`). S3 and DynamoDB gateway
endpoints are always added (free; they keep that traffic off NAT and work from isolated subnets).

**The two standard networks** (factories on `RegionalNetwork`, and as stacks):

| | `privateNetwork()` / `CrePrivateNetworkStack` | `egressOnly()` / `CreEgressOnlyNetworkStack` |
|---|---|---|
| Subnets | isolated tier only | `ipv4`/`dualstack`: public + private-with-egress; `ipv6`: private-with-egress only |
| Internet gateway | attached, never routed to (CloudFront VPC origins need the attachment) | attached; public tier routes to it |
| IPv4 egress | none | one NAT gateway per AZ; each private subnet routes to its own AZ's NAT |
| IPv6 egress | none | egress-only internet gateway (`::/0`) |
| Use for | Locust tasks, internal ALBs behind the edge, anything that needs no internet | workloads that call the internet |

**IP modes.** `ipv4`: IPv4 only. `dualstack`: an Amazon-provided IPv6 /56 on the VPC, a /64 per
subnet, both families on every ENI. `ipv6`: IPv6-native subnets (no IPv4 on any subnet, though the
VPC still has an IPv4 CIDR because AWS requires one), resource-name hostnames. Gotchas:

- **`ipv6` has no NAT64**, so IPv4-only internet destinations are unreachable; AWS endpoints that
  offer IPv6 work.
- **Interface endpoints:** in `ipv6` mode they are created IPv6-only (AAAA records) and their SG
  admits the VPC's IPv6 /56, so a service whose endpoint lacks IPv6 support fails the deploy. In
  `dualstack` they stay IPv4, which every service supports.
- **Not everything runs IPv6-only.** Check each service before choosing `ipv6` (e.g. Fargate needs
  the account's `dualStackIPv6` ECS setting for IPv6; ALBs and CloudFront VPC origins need IPv4 or
  dual-stack subnets). CloudFront VPC origins support `ipv4` and `dualstack` only.
- **Peering is IPv4-only.** `PeeringMesh` adds IPv4 routes, which do nothing for an `ipv6` network.

**As a stack.** `new CrePrivateNetworkStack(app, app.stackId('network-us-east-1'), { region:
'us-east-1', vpcCidr: '10.1.0.0/16', ipMode, azCount, interfaceEndpoints })` (or
`CreEgressOnlyNetworkStack`) owns the `VpcCidr` parameter and outputs `VpcId`, `VpcCidr`,
`RouteTableIds`, `Region` (plus `VpcIpv6Cidr` with IPv6). Put the workload on
`stack.network.vpc` in a subclass or on the instance. `CreNetworkStack` takes full `network` props
for a custom topology. Multi-region: one network stack per region, then
`new CrePeeringStack(app, app.stackId('peering'), { region, networks: [east, west] })`, which takes
each network's outputs. Give each network its own `vpcCidr` (the default is `10.0.0.0/16` for all):
peering refuses overlapping CIDRs, and `CrePeeringStack` refuses them at synth.

**On-demand multi-region usage** (`PeeringMesh`):

```ts
import { PeeringMesh } from '@cre/constructs';

new PeeringMesh(this, 'Mesh', {
  peers, // PeerDescriptor[]: { region, vpcId, vpcCidr, routeTableIds, role? } — usually CfnParameter tokens
  // managedTag default { key: 'CreDemo', value: 'managed' }; lambdaTimeout default 5 min
});
```

**When to pull it in.** When the design needs a VPC (a serverless design may need none). Pull in
the multi-region branch
when a demo needs **cross-region VPC peering** (e.g. a client region driving traffic to
app regions, or a multi-AZ/multi-region failover story).

### Gotchas (MANDATORY)

- **Multi-region is opt-in, not automatic.** A single-region network is the usual starting point. To go
  multi-region, add one network stack per region to `app.ts` (`app.stackId('network-<region>')`,
  each with its `region`) and a `CrePeeringStack` given them as `networks`.
- **`PeeringMesh` no-ops below 2 peers.** With `peers.length < 2` it creates NOTHING — no
  Lambda, no IAM role, no custom resource. A single-region demo can safely instantiate it.
- **Deploy mechanism is outputs → parameters, NOT `crossRegionReferences`.** The peering
  stack's `R<i>*` parameters are CreCfnParameters `from` each network stack's outputs, and the
  pipeline passes exactly those between the deploy jobs
  (`check:stack-wiring` fails the build on exports and `crossRegionReferences`). (`crossRegionReferences`
  shares *values*; it cannot perform the cross-region peering *accept* and peer-side route
  writes — that is why `PeeringMesh` uses a custom-resource Lambda with regional boto3 clients.)
- **`PeeringMesh` IAM uses `*` resources** (the peer VPCs/route tables live in other regions
  with synth-unknown ARNs). cdk-nag flags `AwsSolutions-IAM5`; a demo running cdk-nag should
  add a documented suppression.
- **Deleting a peering does not delete its routes** — EC2 leaves them as blackholes, and a
  blackhole still blocks `CreateRoute` for that CIDR. The configurator therefore removes the
  routes that target a peering before deleting it, *replaces* (not skips) an existing route on
  Create/Update, accepts a peering a previous run left `pending-acceptance`, and on Update tears
  down pairs the new `peers` no longer contain. Its logic is tested against a stateful fake EC2
  (`test/networking/peering_configurator_logic_test.py`, run from jest).
- **A peering belongs to one stack.** Each is tagged `CreDemoOwner=<StackId>` by the stack that
  created or last adopted it, and Delete/Update remove only their own. Renaming a
  `CrePeeringStack` is safe: the new stack adopts the live peering, so the old stack's delete
  (`teardown --orphans`) leaves it and its routes alone.
- **Private DNS IS enabled** on interface endpoints (`privateDnsEnabled: true`). (The stale
  five-nines AGENTS.md claimed it was disabled; the code enables it — there is no
  "private DNS disabled" here.) Equally: **no Secrets Manager replication and no Route 53
  private hosted zone** are part of this design — do not assert those carried-forward
  five-nines claims.

---

## 2. Observability — `DemoObservability` + `DEMO_METRIC_NAMESPACE`

**What it is.** A hand-rolled CloudWatch baseline over GA `aws-cdk-lib/aws-cloudwatch`
ONLY (deliberately NOT `@cdklabs/multi-az-observability`): a named dashboard (header +
client-perceived-availability graph), exactly ONE baseline alarm
(`treatMissingData: NOT_BREACHING`, no SNS action), and a dashboard-URL CfnOutput. Ships
with the shared `DEMO_METRIC_NAMESPACE` TS constant. The dashboard charts `ClientSuccess` /
`ClientError` / `ClientLatency`, which `LoadGenerator`'s locustfile emits as Embedded Metric Format
(one JSON line per request on stdout; CloudWatch extracts the metrics from the log group, so no
`PutMetricData` and no agent).

**On-demand.** Required for a `CreResilienceDemo`, which PDD gives a `CreDashboardStack`
(`new CreDashboardStack(app, app.stackId('dashboard'), { region })`).

**Usage:**

```ts
import { DemoObservability } from '@cre/constructs';

const obs = new DemoObservability(this, 'Observability', {
  demoName: app.stackId('obs'), // names the dashboard (global in the account) and the alarm: unique per DemoObservability
  // metricNamespace? default defaultMetricNamespace(this) = CRE/<project>; metricsRegion? default the stack region;
  // period? default 1 min; availabilityAlarmThreshold? default 99; createBaselineAlarms? default true
});
obs.addMetricRow('My Series', [obs.metric('SomeMetric', { Region: 'us-east-1' })]); // extend the dashboard
```

A workload of your own that emits metrics prints the same kind of line (the
[EMF specification](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html));
the Locust image's `cre_emf.py` (`client_emf`, `emit`) is a working example.

**When to pull it in.** Every resilience demo (through `CreDashboardStack`), and any other project
whose story needs a dashboard or alarms. Extend it (`addMetricRow`, `metric`,
`availabilityExpr`) when a demo has its own metrics to chart.

### Operator dashboard: link the Locust UI, deploy LAST, use dynamic labels

Fed back from `request-router-metadata-app`. When a demo has a **Locust operator UI behind
CloudFront** (load-generation + edge/CFS), the single operator dashboard should deep-link that UI
and its control routes from a header text widget:

```ts
new CreDashboardStack(app, app.stackId('dashboard'), {
  region: 'us-east-2',
  edge,
  links: [
    { label: 'Locust UI', output: 'EdgeLocustUrl' },
    { label: 'Arm / Disarm failure', output: 'EdgeLocustUrl', path: 'inject' },
    { label: 'Scale fleet', output: 'EdgeLocustUrl', path: 'scale' },
  ],
});
```

`EdgeLocustUrl` (`https://<distribution>/locust/`) is an output of the edge stack, which only
exists **after the edge stack deploys**. A link's `path` goes under it without a leading slash, so
the URL has one slash (`…/locust/inject`). The dashboard and its alarm are named after the stack
(`<project>-dashboard`), so a second dashboard stack, one per region, gets its own. So:

- **Deploy the dashboard as its own stack, LAST — after edge.** It cannot link the operator UI until
  CloudFront exists. Thread the edge stack's `EdgeLocustUrl` output → the dashboard stack's
  `CfnParameter` (outputs→params, never `Fn::ImportValue`): give `CreDashboardStack` the `edge` and
  its CreCfnParameters take `EdgeUrl` / `EdgeLocustUrl`, so it deploys after the edge. This is why `DemoObservability` supports
  `createDashboard: false` (the app stack keeps only the availability **gate alarm**; the visual
  dashboard is the post-edge stack). After the edge in order, not next to it in us-east-1: deploy it
  in the LoadGenerator's region, or set `metricsRegion`, since a dashboard reads metrics only in one
  region (`cre-constructs.md`, "The dashboard reads metrics in the region they land in").
- **Dynamic labels on every visible metric line.** Append the min/avg/max of the shown datapoints to
  each metric's legend so operators read the range without hovering
  ([Graph dynamic labels](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/graph-dynamic-labels.html)):

  ```ts
  // `${MIN}` etc. are CloudWatch tokens — escape as \${...} in a TS template literal so they pass
  // through literally and are NOT resolved as CDK tokens. Apply to VISIBLE series only; leave
  // hidden (visible:false) math inputs, y-axis titles, and annotations plain.
  const dyn = (base: string) => `${base}  [min: \${MIN} | avg: \${AVG} | max: \${MAX} ]`;
  // …then: { label: dyn('p99'), … }
  ```

### Gotchas (MANDATORY)

- **A Locust-linking dashboard MUST deploy after edge, or the CloudFront URL doesn't exist yet.**
  `EdgeLocustUrl` is an **edge-stack** output; a dashboard that deep-links it has to be a separate
  post-edge stack with it threaded output→param (`CreDashboardStack` with `edge` does this). Building the header from a not-yet-created
  distribution URL fails the deploy contract (a `CfnParameter` with no supplier; `CreCfnParameter({ from })` is the supplier).
- **Dynamic-label tokens (`${MIN}`/`${AVG}`/`${MAX}`) must be escaped in TS template literals**
  (`\${MIN}`) so CDK emits them literally instead of trying to resolve them as tokens; and they only
  render on **visible** series (a `visible:false` math input has no legend).

- **Dashboard region MUST equal the EMF log-group region.** Every metric and every widget
  is stamped with `metricsRegion` (default: the stack region), and the dashboard SHOULD be
  deployed into that same region. EMF metrics land in the region of the log group that
  emits them; if the dashboard's region differs, the math expressions resolve against the
  wrong region and the graph shows nothing. Keep `metricsRegion` == the workload's
  log-group region.
- **The namespace is `CRE/<project>`, and only `LoadGenerator` sets it for you.** Inside a CreApp,
  `DemoObservability.metricNamespace` and `LoadGenerator` default to `defaultMetricNamespace(this)`
  (`CRE/<project>`, so projects sharing an account never mix metrics); outside one, to the
  `DEMO_METRIC_NAMESPACE` constant `'MyResilienceDemo'`. The locustfile reads the env var
  **`DEMO_METRIC_NAMESPACE`** (NOT `EMF_NAMESPACE`) and falls back to `'MyResilienceDemo'`.
  `LoadGenerator` injects it; **your own Lambda or container must set it**, or its metrics land
  where no dashboard reads:
  `environment: { [DEMO_METRIC_NAMESPACE_ENV_VAR]: defaultMetricNamespace(this) }`.
- **EMF double-count footgun.** Client-level metrics (`ClientSuccess`/`ClientError`/`ClientLatency`)
  go on EXACTLY one line per logical request. A request that fans out to several regions may emit a
  per-region line for each, but only one of them may carry the client metrics, or the dashboard's
  availability counts the request twice.

---

## 3. Authentication — four sub-families

None is used by the scaffold (it has no stacks). **Default: CloudFront Signer**, through
`CreEdgeStack` (§6), for any endpoint a person or the operator reaches; pick another only when it
doesn't fit, and say why in the design:

| Sub-family | Construct | Gates by | Use when |
|---|---|---|---|
| `auth/cloudfront-signer` | `DistributionSigner` (§6) | Midway + bindle, at CloudFront | **the default**: any UI, operator page or HTTP endpoint people reach |
| `auth/allowed-cidr` | `AllowedCidrSecurityGroup` | network source CIDR | a non-HTTP endpoint, or a machine caller that can't do Midway; defense in depth |
| `auth/federate-alb` | `FederateAlbAuth` (§7) | Midway via Amazon Federate OIDC, at an ALB | an internal web app on an ALB with its own hostname |
| `auth/cognito` | Cognito constructs (below) | Cognito user pool | demos that need non-Amazon users |

**What it is (allowed-cidr + Cognito).** Two layers. **`AllowedCidrSecurityGroup`** (a
security group whose ingress is gated by a console-rotatable `AllowedCidr` CFN parameter,
default deny-all `0.0.0.0/32`). And the on-demand **Cognito** family: a shared
`CognitoAuthConstruct` (user pool + app client + optional hosted-UI domain + a documented
OIDC/Midway no-op seam) consumed by two front-end variants — `CognitoAlbAuthConstruct`
(ALB `authenticate-cognito`) and `CognitoApiGwAuthConstruct` (HTTP API JWT authorizer +
`/amplify-config`).

Both are **on-demand**.

**Usage:**

```ts
import { AllowedCidrSecurityGroup } from '@cre/constructs';

const sg = new AllowedCidrSecurityGroup(this, 'BaselineSg', {
  vpc: network.vpc, // ONLY prop required; construct OWNS the AllowedCidr param (C1 — do NOT declare your own)
  // ports? default [443]; defaultCidr? default '0.0.0.0/32'; allowAllOutbound? default false
});
// sg.securityGroup — attach to the ALB / endpoint you want to gate
```

Rotate the allowlist without redeploy: CFN console → Update → "Use existing template" →
edit `AllowedCidr` → Update.

**On-demand Cognito usage:**

```ts
import {
  CognitoAuthConstruct, CognitoAlbAuthConstruct, CognitoApiGwAuthConstruct,
} from '@cre/constructs';

// ALB variant — confidential client + hosted UI
const albAuth = new CognitoAuthConstruct(this, 'Auth', {
  generateClientSecret: true,
  hostedUiDomainPrefix: 'my-demo-login-7f3a',           // globally unique per region
  callbackUrls: [`https://${alb.loadBalancerDnsName}/oauth2/idpresponse`], // see CALLBACK CASE gotcha
});
new CognitoAlbAuthConstruct(this, 'AlbAuth', {
  auth: albAuth, httpsListener, nextAction: elbv2.ListenerAction.forward([targetGroup]),
});

// API GW variant — public SPA client + JWT authorizer
const apiAuth = new CognitoAuthConstruct(this, 'Auth', { generateClientSecret: false });
const c = new CognitoApiGwAuthConstruct(this, 'ApiAuth', { auth: apiAuth, httpApi });
httpApi.addRoutes({ path: '/api/x', methods: [...], integration, authorizer: c.authorizer });
```

**When to pull it in.** Prefer CloudFront Signer. Add `AllowedCidrSecurityGroup` for a non-HTTP
endpoint or a caller that can't do Midway. Add Cognito when a
demo needs **user authentication** in front of an ALB (browser login, no app-side auth
code) or an HTTP API (SPA sends `Authorization: Bearer <jwt>`). Keep the
`AllowedCidrSecurityGroup` on as defense-in-depth even with Cognito — Cognito
authenticates *users*; the SG limits *network reachability*.

### Gotchas (MANDATORY)

- **ALB `authenticate-cognito` CALLBACK CASE gotcha.** The ALB sends Cognito a
  `redirect_uri` of `https://<alb-host>/oauth2/idpresponse` with the host **lowercased by
  the browser**. An auto-generated ALB name is MIXED-CASE, so `loadBalancerDnsName` is
  mixed-case and the registered callback won't match the lowercase runtime `redirect_uri`
  → Cognito returns **"Client is not enabled for OAuth2.0 flows"**. CDK cannot
  `.toLowerCase()` the DNS token, so **pin an explicit all-lowercase `loadBalancerName` on
  the ALB** (recommended) — or register both cases in `callbackUrls`/`logoutUrls`. Then
  build the callback from that lowercase DNS. (Found in the M6 live login test.)
- **HTTP API routes are public unless they pass `authorizer`.** An HTTP API has no API-wide
  auth after construction, and `CognitoApiGwAuthConstruct` receives an already-built `httpApi`, so
  it can't set `defaultAuthorizer` for you. Instead it **warns at synth** for every route without
  an authorizer (except `/amplify-config`). Treat that warning as a bug unless the route is meant
  to be public.
- **The ALB variant fails fast on a client that can't work.** It throws unless the
  `CognitoAuthConstruct` has `generateClientSecret: true` (ALB `authenticate-cognito` rejects a
  public client) and explicit `callbackUrls` (otherwise CDK registers `https://example.com` and
  every login bounces).
- **`/amplify-config` is intentionally PUBLIC.** The `CognitoApiGwAuthConstruct`
  `/amplify-config` route has no authorizer by design — it returns only **non-secret,
  client-discoverable IDs** (`region`, `userPoolId`, `appClientId`) so the static SPA
  bundle discovers them at runtime instead of baking per-deploy IDs into the build. This is
  not a leak.
- **The self-signed cert in the integ app is integ-only.** The M6 integration app used a
  self-signed cert to satisfy the ALB HTTPS listener; **production needs a real ACM/DNS
  cert**. Do not ship the self-signed cert as the demo's TLS story.
- **The Midway `oidcFederation` seam is a documented v1 no-op.** Passing
  `CognitoAuthProps.oidcFederation` has NO effect in v1 — no `UserPoolIdentityProviderOidc`
  is created and the client's `supportedIdentityProviders` stays `[COGNITO]`. The seam is
  designed so federation can be turned on later with zero rework to either front-end
  variant; the exact code that flips on is documented in `oidc-federation-props.ts`.
- **Load-gen against a Cognito-protected target** must wire its own auth: the
  `LoadGenerator` dropped the five-nines `execute-api:Invoke` default, so a demo load-testing
  a Cognito-protected endpoint must add the task-role grants + an auth-token env var itself
  (see the load-gen section / recipe).

---

## 4. Load generation — `LoadGenerator` + base container

**What it is.** An on-demand Locust-on-Fargate load generator (ARM64 by default). ONE
`DockerImageAsset` is the single source of truth for both CI providers' container-build
paths. Ships a generic, env-driven base container (`container/locustfile.py` emits one EMF
line per request) that a demo overlays via `workloadDirectory`.

**On-demand** (M7) — off by default; NEVER in the green skeleton.

**Usage:**

```ts
import { LoadGenerator } from '@cre/constructs';

new LoadGenerator(this, 'LoadGen', {
  vpc: network.vpc,
  targetUrl: api.url,                  // WITH scheme (`http://${alb.loadBalancerDnsName}`, not the bare DNS); token OK
  // targetPath? default '/'; httpMethod? default 'GET'
  // emfNamespace? default defaultMetricNamespace(this) = CRE/<project> (injected as env var DEMO_METRIC_NAMESPACE)
  // architecture? default ARM64 (drives BOTH image platform AND task runtimePlatform — can't drift)
  // users/spawnRate (run size + ramp headless; the UI start form's defaults with exposeWebUi)
  // min-maxWaitSeconds/cpu/memoryLimitMiB/desiredCount; exposeWebUi? default false (headless)
  // classPicker? (with exposeWebUi) lets the start form and /swarm's user_classes pick classes
  // workers? default 0; workerCpu/workerMemoryLimitMiB; processesPerTask; nofileUlimit
  // taskSubnets? default CDK's choice: private-with-egress, else isolated (the private network: needs the ECR, ECR_DOCKER, LOGS endpoints)
  // workloadDirectory? a dir OVERLAID on the base container — usually just locustfile.py + helpers
});
```

**A target behind its own security group** (an internal ALB whose listener is `open: false`, an
NLB target, a database) refuses the generator until it is allowed in:
`alb.connections.allowFrom(loadGen.service, ec2.Port.tcp(80))`, and with `workers` also
`loadGen.workerService!` (the workers send the requests; the controller then sends none).

**Scaling: workers.** `workers: N` makes the main task the Locust controller (coordinator and web UI,
no users of its own) and runs N worker tasks in a second Fargate service. Workers find the controller
by Cloud Map (`controller.<stack>-<id>.loadgen.internal`) and connect to its port 5557, which only the
workers' security group can reach; they are not behind the ALB. The users are spread across the
workers. Scale load by adding workers; give each more cores with `workerCpu` and
`processesPerTask` (Locust runs one core per process). Headless, the controller waits for all N workers
before starting.

**Pages and handlers on the Locust UI.** `loadGen.addWebPage('/status', html)` serves `html` at
`/locust/status` (behind the same CloudFront Signer auth as the UI). `{{BASE_PATH}}` in the HTML
becomes the UI base path, so write links and `fetch()` calls as `{{BASE_PATH}}/api/flush`. For
a button that does something, put the handler in the overlay's locustfile:

```python
from cre_pages import route

@route("/api/flush", methods=["POST"])   # served at /locust/api/flush
def flush():
    boto3.client("ssm").put_parameter(...)
    return {"ok": True}
```

and grant it access with `loadGen.addToControllerRolePolicy(...)`, which the workers never get
(`addToRolePolicy` grants every task; `addEnvironment(...)` passes config).
Handlers run in the controller (or standalone) task. There is no index page; link pages from each
other or from the dashboard.

**Steady request rate (open loop).** The base locustfile is closed-loop: each user waits for a
response before its next request, so when the target slows the request rate falls and the
failure looks smaller than it is. To hold the rate steady, overlay an open-loop locustfile, as the
request-router demo does. The pattern, a runnable example (it emits through `cre_emf`) and what
each piece is for are in the project's `cre-load-generation.md` steering, the one copy; it also
needs `nofileUlimit` (e.g. 65536) and enough `workers`.

**When to pull it in.** When a demo needs **synthetic traffic** to make a resilience story
visible on the dashboard (e.g. drive an availability dip during a failure injection).

### Gotchas (MANDATORY)

- **`containerImages` gates BOTH tasks AND CI jobs — together, never one without the other.**
  The single `containerImages` project option (in `.projenrc.ts`; the project type passes it to
  the build and deploy tasks and the provider workflow) gates the container lifecycle
  tasks (`build:container-plan`, `build:docker`, `deploy:docker`) AND the provider's
  container-build CI jobs. With it OFF (the green default) ZERO container work is emitted.
  Flipping it on lights up the tasks and jobs in lockstep — never emit a job with no asset
  or vice-versa.
- **One container path on both providers.** The build makes the images and the deploy pushes
  them; the deploy never builds:
  - `build:container-plan` lists every `DockerImageAsset` in `cdk.out`, and `build:docker` builds
    each into `assets/containers/<hash>.tar.gz`, which rides in `dist/content.zip`: with
    `docker build` (`projenrc/cre/scripts/build/build-docker.py`) on GitHub runners and your
    laptop, and with **kaniko** (`kaniko-build.sh`) in GitLab's `build:docker` job, because the
    shared GitLab runners block Docker-in-Docker and user namespaces.
  - `deploy:docker` (inside `deploy:upload`) pushes the tarballs with **crane** to the CDK
    asset repository in **every region of a stack that uses the image** (from the stacks' `region`).
  - On GitHub this keeps split trust: the privileged deploy job pushes pre-built tarballs and
    never runs a Dockerfile from a PR.
- **A local `npx projen build` builds the images with a container daemon** once `containerImages`
  is on. Without one, `build:docker` warns that the images aren't built locally and the build goes
  on (the synth and tests still ran; CI builds the images, and `npx projen deploy` then stops at the
  missing tarball). `CRE_REQUIRE_IMAGES=1` makes a missing daemon an error, as it is in CI. Docker,
  or podman with a `docker` shim on PATH, works.
- **In the private network the tasks run in isolated subnets** (CDK picks them when the VPC has
  no egress subnets) and reach AWS only through endpoints: synth fails unless the VPC has the
  **ECR, ECR_DOCKER and CLOUDWATCH_LOGS** interface endpoints (`RegionalNetwork.interfaceEndpoints`).
  Egress subnets reach ECR and Logs through NAT. A VPC defined outside the app (imported from
  parameters into a client stack, or `fromLookup`) can't be checked: synth warns and lists the
  endpoints it must have.
- **`workloadDirectory` is an overlay.** Its files are merged over the base container at
  synth (`.venv`, `.git`, `__pycache__` skipped), so a demo ships only what it changes; a
  file it provides — even the Dockerfile — replaces the base one.
- **Packages: `requirements-extra.txt`, never `requirements.txt`.** The base image has `locust`,
  `requests` and `boto3` (pinned), so `/inject` handlers can call SSM or FIS with the task role.
  The overlay's `requirements-extra.txt` is installed on top. An overlay `requirements.txt`
  replaces the base one (the Locust pin with it) and draws a synth warning. Import `boto3` from
  the locustfile or the modules it imports: Locust's gevent patching must run first (a bare
  `import boto3, locust` fails with `RecursionError`).
- **Every service has the ECS deployment circuit breaker with rollback.** A crashing image or a
  task that can't pull fails the deploy in minutes and rolls back, instead of CloudFormation
  waiting hours for a steady state.
- **Web UI mode is one task.** `exposeWebUi` with `desiredCount > 1` throws: each task is an
  independent Locust, and the ALB would spread `/swarm`/`/stop`/stats across them. Scale with
  `workers`.
- **Locust's `--web-base-path` does not prefix custom routes.** Routes added to `web_ui.app`
  directly are served at the root, outside the edge route. Use `addWebPage` / `cre_pages.route`,
  which add the base path. Paths on Locust's own routes (`/swarm`, `/stats/...`) are refused.
- **Pages are baked into the image** at prepare time, so `addWebPage` must be called before
  anything reads `loadGen.image`. Changing a page rebuilds the image.
- **IPv6-only subnets can't hold the UI's ALB** (it needs IPv4 or dual-stack subnets). Put a
  `LoadGenerator` with `exposeWebUi` in an `ipv4` or `dualstack` network.
- **It emits the client contract** `DemoObservability` reads: `ClientSuccess`, `ClientError` and
  `ClientLatency` with no dimensions (the availability graph, `ClientAvailabilityAlarm`, the FIS
  stop condition), and the same per `Target`, through `cre_emf.py` in the base image. A
  `workloadDirectory` locustfile replaces the base one, so it emits nothing unless it calls
  `from cre_emf import emit` once per request; without it the dashboard is empty and the alarm
  can't fire.
- **Choosing user classes needs `classPicker: true`** (with `exposeWebUi`; Locust's
  `--class-picker`). Without it the start form and `/swarm`'s `user_classes` are ignored and every
  class in the locustfile runs at its weight.
- **Only 2xx/3xx count as ClientSuccess.** The base locustfile records 4xx (403 auth, 404 wrong
  path, 429 throttling) as `ClientError`, so a misconfigured target shows red, not green.
- **Cognito-protected target:** see the auth section — `LoadGenerator` has no
  `execute-api:Invoke` default; wire task-role grants + an auth-token env var yourself.

---

## 5. Failure injection — steering, not a construct

The library has no failure-injection construct. How a project breaks its system depends on the
story (a dependency's answer flipped by an API Gateway stage variable, an error-rate parameter the
code reads, an orchestrated proxy, an AWS FIS experiment, ...), so each project builds the one it
needs. The approaches, the rules they share (off at deploy, arm/disarm/status, scoped IAM, never
unauthenticated, bounded) and how to put the controls on the Locust UI are in the generated
steering, `.kiro/steering/cre-failure-injection.md` (source:
`packages/cre-projen/steering/failure-injection.md` in the template).

---

## 6. CloudFront Signer — `CreEdgeStack`, `CreEdgeOrigin`, `DistributionSigner` (`auth/cloudfront-signer`)

**The edge, bundled.** One `$PROJECT_NAME-edge` stack holds ONE CloudFront distribution behind
CloudFront Signer, with path-based routes to internal ALBs in the app stacks through (cross-region)
VPC origins. It deploys to us-east-1 (its `region`), after the stacks holding its origins, so the
signer's secret is in a region CloudFront Signer reads, whatever the app region.

```ts
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import {
  CreEdgeOrigin, CreEdgeStack, CrePrivateNetworkStack, LOCUST_EDGE_ROUTE, LoadGenerator,
} from '@cre/constructs';

// App stack (any region): its region's private network, whose attached internet gateway CloudFront
// VPC origins need. Fargate in isolated subnets needs the ECR, ECR_DOCKER and LOGS endpoints.
const appStack = new CrePrivateNetworkStack(app, app.stackId('app-us-east-2'), {
  region: 'us-east-2',
  interfaceEndpoints: [ec2.InterfaceVpcEndpointAwsService.ECR, ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
    ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS],
});
const vpc = appStack.network.vpc;
const loadGen = new LoadGenerator(appStack, 'LoadGen', { vpc, targetUrl, exposeWebUi: true }); // Locust at /locust, origin 'locust' made for you
const appOrigin = new CreEdgeOrigin(appStack, 'AppOrigin', { name: 'app', loadBalancer: appAlb, vpc }); // any other internal ALB

// The edge (app.ts): each route takes its origin's ALB; BindleId comes from BINDLE_ID.
new CreEdgeStack(app, app.stackId('edge'), { region: 'us-east-1', routes: [
  { ...LOCUST_EDGE_ROUTE, origin: loadGen.edgeOrigin! },
  { name: 'app', pathPattern: '/*', origin: appOrigin },
] });
```

- `CreEdgeOrigin` adds one security group per CloudFront origin-facing prefix list (IPv4, IPv6;
  looked up at deploy time) to the ALB, and outputs `Edge<Name>AlbArn` / `Edge<Name>AlbDnsName`.
  `CreEdgeStack` takes them as CreCfnParameters of the same names, plus `BindleId` from the
  BINDLE_ID deploy variable, so it deploys after the origins' stacks.
- The `/*` route is the default behaviour; with none, the first route is. CloudFront doesn't
  rewrite paths: each origin must serve under its prefix (Locust uses `--web-base-path`).
- Outputs: `EdgeUrl`, `Edge<Name>Url` per route, `EdgeDistributionId`, `EdgeDomainName` (onboarded
  after the first deploy by `npx projen cfs:onboard`, and again for a renamed or recreated edge), `CfsSecretArn`. `CreDashboardStack` links
  `EdgeUrl` and `EdgeLocustUrl`.
- Proven pattern: request-router-metadata-app's edge and client stacks.

`DistributionSigner` below is what `CreEdgeStack` applies; use it directly only for a distribution
the edge stack can't express.

**What it is.** A one-call construct — `DistributionSigner.apply(distribution, { authorizerBindleId })`
— that puts a CloudFront distribution behind **CloudFrontSigner** (Midway auth + Bindle
authorization) without the internal Brazil/BATS `@amzn/cloudfront-signer` dependency. It
provisions everything the flow needs and wires it onto an existing `cloudfront.Distribution`:

- a rotated KMS key (CFS service principal granted `Decrypt` on it),
- an RSA signing key pair minted **inside** a Python custom-resource Lambda (the private key
  only ever lands in Secrets Manager — us-east-1),
- a CloudFront public key + **trusted key group** on **every** behavior of the distribution —
  default and additional, including behaviors added after `apply()` — except the path patterns
  in `denyListBehaviors`,
- a private S3 origin serving `/error/403.html` + `/js/cfs-handler.js` as **unrestricted**
  behaviors, and a `403 → /error/403.html` custom error response (your other error responses
  are kept).

It mirrors the API of the internal `@amzn/cloudfront-signer` `DistributionSigner` so a demo
can later swap to the real package with no call-site change.

**On-demand** (M9) — NEVER in the green skeleton. Vendor it only when a demo needs a
publicly reachable page gated by Midway (e.g. a Locust operator UI) instead of a WAF/CIDR
allow-list. The distribution and all signer infra must live in a region where CFS can read the
signing secret — **us-east-1** recommended (see the region gotcha below).

**Usage:**

```ts
import { DistributionSigner } from '@cre/constructs';

const distribution = new cloudfront.Distribution(this, 'UiDistribution', {
  defaultBehavior: { origin: /* your VPC/ALB/HTTP origin */ },
});

// BINDLE_ID is supplied per-demo (e.g. the aws-cre team bindle) via a CfnParameter/CI var.
const signer = DistributionSigner.apply(distribution, {
  authorizerBindleId: bindleId.valueAsString,
});

new cdk.CfnOutput(this, 'CfsSecretArn', { value: signer.secretArn });
```

After deploy, onboard the distribution's `*.cloudfront.net` domain: `npx projen cfs:onboard` (again after the edge stack is renamed or recreated, which makes a new distribution and domain)
(or by hand at <https://cloudfrontsigner.ninjas.security.a2z.com/>).

**This construct ships the hardened V2 flow (the CFS Auth Hardening campaign).** The bundled
`static/cfs/js/cfs-handler.js` is the **V2** handler, not the sunset `/sign` (V1) one, so any
demo built from this template is out of scope for the campaign by construction:

- redirects to **`/v2/authorize`**, never the legacy `/sign`;
- signed cookies are returned in the **URL fragment (`#`)**, not query parameters (they never
  hit access logs);
- cookies carry a **15-minute TTL** (matching the Midway SSO token), not 24 hours;
- a **nonce/RFP handshake** (`cfsNonce` out, `CloudFront-RFP` cookie + SHA-384 validation
  back) defeats request forgery;
- cookies default to **`SameSite=Lax`**.

### Gotchas (MANDATORY)

- **VPC origins are IPv4 or dual-stack, never IPv6-only.** A route's `ipAddressType: 'dualstack'`
  sets the VPC origin's `IpAddressType` (CDK has no prop yet; it's an L1 override) and needs a
  dual-stack ALB in dual-stack subnets. An `ipv6` network can't be a VPC origin.
- **Supported secret regions only — use us-east-1.** CFS reads the signing secret from Secrets
  Manager in a fixed set of regions (`CFS_SUPPORTED_SECRET_REGIONS`: us-east-1, us-west-2,
  eu-central-1, eu-west-1, ap-northeast-1, ap-southeast-2, plus the two China regions — from CFS's
  `MultiRegionSecretsManagerConfig`, linked from the onboarding wiki). Onboarding a distribution
  whose secret lives elsewhere fails with "not in a supported region for CloudFrontSigner", so the
  construct throws at synth for any other resolved region, and warns when the stack is
  environment-agnostic and it can't tell. us-east-1 is the tested path; cross-region VPC origins
  let it front an ALB in another region.
- **The secret name must start with `CFSSigningKey`** (onboarding wiki §1.2.3, "otherwise
  onboarding will fail"). It is `CFSSigningKey-<path id>-<stack UUID>`: the UUID changes on every
  destroy + redeploy, so the name can't collide with a predecessor still in its recovery window.
  The key group is named the same way, because CloudFront key-group names are unique per account.
- **The KMS key policy is the wiki's statement verbatim.** `cloudfrontsigner.aws.internal` gets
  Encrypt/Decrypt/ReEncrypt\*/GenerateDataKey\*/DescribeKey with no `aws:SourceAccount`
  condition. Don't trim or condition it: CFS documents neither, and a condition its requests don't
  carry breaks every secret read.
- **One signer per distribution, several per stack.** `apply()` names the signer after the
  distribution (`DistributionSigner<distId>`), so an app UI and a Locust UI can both be signed in
  one stack. Each gets its own key, secret and key group.
- **Redeploys invalidate the CFS files.** The deployment of `/error/403.html` and
  `/js/cfs-handler.js` invalidates both paths, so a stale cached 403 page can't keep pointing CFS
  at a previous secret ARN.
- **Do not downgrade the handler to V1.** The whole construct exists to keep demos on the
  hardened V2 flow. `test/auth.cloudfront-signer.test.ts` pins the V2 invariants (fragment,
  15-min TTL, nonce/RFP, `SameSite=Lax`, `/v2/authorize`); if you edit `cfs-handler.js`, keep
  those green. Only set a cookie to `SameSite=None` if a demo genuinely needs cross-site
  cookies, and do it deliberately.
- **The key provisioner self-heals on redeploy.** On every Create/Update it regenerates the
  pair and re-writes the secret bound to the **current** stack's CMK, and the KMS key is
  `RemovalPolicy.DESTROY` (7-day pending window). This fixes the old "Access to KMS is not
  allowed" drift where a teardown+redeploy left the retained secret bound to an orphaned CMK.
  Bump `KeyPairVersion` (custom-resource prop) to force an in-place key rotation.
- **`/js/cfs-handler.js` and `/error/*` MUST stay unrestricted** (no trusted key group) — they
  are the redirect machinery served *before* the viewer has signed cookies. The construct adds
  them for you; don't attach the key group to them.
- **Every 403 becomes the auth redirect — including your origin's own 403s.** CloudFront custom
  error responses can't tell a missing/expired signed cookie apart from a 403 the origin returned,
  so both serve `/error/403.html`. An origin that answers a signed-in viewer with 403 sends them
  through Midway again, and gets 403 again: a redirect loop. Have the origin use another status
  (e.g. 404) for "forbidden" behind this construct. A 403 entry of your own in `errorResponses` is
  replaced (with a synth warning).
- **Opt paths out with `denyListBehaviors`, and keep the patterns exact.** Matching is by exact
  `pathPattern` string; an entry that matches no behavior produces a synth warning and the path
  stays restricted.
- **Upgrading a demo that vendored an earlier edge version (none was ever in a tagged release).**
  The signer's construct id changed (`DistributionSigner` → `DistributionSigner<distId>`) and the
  secret is renamed, so the next deploy **replaces** the key, secret, key group and files bucket:
  a new secret ARN, picked up through the redeployed (and invalidated) 403 page. The `*.cloudfront.net`
  domain does not change, so onboarding stays valid. The first edge version (`4afc8ab`) also created
  `CFSSigningKey-<id>` itself and never deleted it; it no longer collides, but is left behind unused.
  Delete it by hand once the upgrade is deployed:
  `aws secretsmanager delete-secret --region us-east-1 --secret-id CFSSigningKey-<id>`.
- **Rollback mints a new key pair.** Each Create/Update of the key provisioner generates a fresh
  pair, including the Update CloudFormation sends when rolling back a failed stack update. After a
  rollback the secret and the key group can disagree and viewers get 403s; a successful redeploy
  fixes it. A rotation also has a brief window, between the secret update and the key group update,
  where new cookies are signed with a key CloudFront doesn't trust yet.
- **The 403 page injects your Bindle id and secret ARN** at synth time. Onboarding at the CFS
  portal (once, per domain) is a manual step the construct cannot do for you.

---

## 7. Federate ALB auth — `FederateAlbAuth` (`auth/federate-alb`)

**What it is.** Midway authentication at an Application Load Balancer through Amazon Federate
OIDC (per the Federate team's [ALB integration page](https://w.amazon.com/bin/view/AmazonFederate/help/ALB/)).
The listener's default action becomes `authenticate-oidc` against `idp.federate.amazon.com`, then
your `next` action. Ships two app-side helpers that validate the `x-amzn-oidc-data` header:
`app-helper/oidc-data.ts` (Node 18+, no dependencies) and `app-helper/oidc_data.py` (needs
`cryptography`).

```ts
import { FederateAlbAuth } from '@cre/constructs';

const auth = new FederateAlbAuth(this, 'Auth', {
  listener: httpsListener, loadBalancer: alb,
  hostname: 'demo.alias.people.aws.dev',            // lowercase, under an allowlisted domain
  next: elbv2.ListenerAction.forward([targetGroup]),
  clientId: federateClientId,                         // from the Federate service profile
  clientSecret: secretsmanager.Secret.fromSecretNameV2(this, 'S', 'demo/federate-client'),
});
auth.allowTargetsFromLoadBalancer(ec2.Port.tcp(8080), service);
```

### Gotchas (MANDATORY)

- **The app must validate `x-amzn-oidc-data`** (signature against the ALB's regional key, `signer` =
  the ALB ARN, `exp`). Use the helper; the Federate page calls this mandatory. And give targets no
  ingress except from the ALB (`allowTargetsFromLoadBalancer`), or the header can be forged.
- **Federate profile:** OIDC, from the *Midway-Protected Internal Service* template (auto-approved),
  redirect URI = the `FederateRedirectUri` output, **PKCE disabled** (ALB doesn't support it).
- **Hostname:** the auto-approved template only accepts domains on Midway's allowlist (`aws.dev`,
  `amazon.dev`, …); `*.elb.amazonaws.com` is not. Use `<alias>.people.aws.dev` or the `cre` org via
  SuperNova (`npx projen setup:dns`), plus a certificate (§8). A hostname elsewhere synthesizes
  with a warning and needs a security review.
- **HTTPS only, lowercase hostname**: both are synth errors.
- **Behind CloudFront**, forward all headers, cookies and query strings, or login loops.

---

## 8. DNS — `ValidatedCertificate` (`dns`)

**What it is.** An ACM certificate DNS-validated with no manual step, whether the hosted zone is
in the deploy account or another one. A CloudFormation Condition on the `dnsRoleArn` value picks
the path at deploy time: empty → native `DomainValidationOptions.HostedZoneId`; set → a custom
resource, created in parallel with the certificate, assumes that role and UPSERTs the validation
CNAMEs while CloudFormation waits for issuance.

```ts
import { ValidatedCertificate } from '@cre/constructs';

const domain = new CreCfnParameter(this, 'Domain', { fromVariable: 'DOMAIN' });
const zone = new CreCfnParameter(this, 'HostedZoneId', { fromVariable: 'HOSTED_ZONE_ID' });
const dnsRole = new CreCfnParameter(this, 'DnsRoleArn', { fromVariable: 'DNS_ROLE_ARN', optional: true });
const cert = new ValidatedCertificate(this, 'Cert', {
  projectName: 'my-demo',                    // the project name; names the validator role <project>-acm-<region>
  domain: domain.valueAsString,              // certificate is *.<domain> unless `names` overrides it
  hostedZoneId: zone.valueAsString, dnsRoleArn: dnsRole.valueAsString,
});
```

### Gotchas (MANDATORY)

- **The default certificate is `*.<domain>`.** Put each hostname directly under the domain
  (`app.<domain>`); the wildcard covers neither `<domain>` itself nor `a.b.<domain>`. Pass
  `names` to add the apex or other names.
- **The DNS role trusts one validator role per region, by exact ARN.** The cross-account validator
  Lambda runs as `<project>-acm-<region>` (a fixed name, so the deploy needs `CAPABILITY_NAMED_IAM`,
  which `deploy-stack.py` passes). `setup/dns.py` (`npx projen setup:dns`) makes `CreDnsDeploy` trust only the runner role
  and those ARNs, one per region passed to `setup:dns`, so no other role the project creates can write to the
  zone. Consequences: **one** ValidatedCertificate per project per region (a second is refused at
  synth; put extra names in `subjectAlternativeNames`), a region added later needs `setup:dns`
  re-run, and `projectName` must keep the role name within 64 characters.
- **Records the demo publishes** go in a `$PROJECT_NAME-dns` stack, always in the partition's
  Route 53 control-plane region, us-east-1 in `aws` (refused at synth elsewhere, and the
  `DnsControlPlaneRegion` Rule fails a deploy anywhere else):
  `new CreDnsStack(app, app.stackId('dns'), { region: 'us-east-1', assumeRole: '$DNS_ROLE_ARN?',
  executionRole: '$DNS_CFN_EXECUTION_ROLE_ARN?' })` takes `Domain` and `HostedZoneId` from DOMAIN and
  HOSTED_ZONE_ID; add `route53.CfnRecordSet`s whose targets are CreCfnParameters `from` the stacks
  that own them, or `addPrivateApi(...)`. The deploy assumes `DNS_ROLE_ARN` when the zone is in
  another account (and hands CloudFormation `DNS_CFN_EXECUTION_ROLE_ARN`, falling back to
  `CFN_EXECUTION_ROLE_ARN`).
- **Delete leaves the validation CNAME** (harmless; removing it could break another certificate's
  renewal). Never delete a SuperNova-delegated zone by hand.

### Private API Gateway custom domains — `PrivateApiCustomDomain` + `PrivateApiDnsRecords`

**What it is.** Your own hostnames, with TLS, for a PRIVATE REST API reached through its
execute-api VPC endpoint, from the same VPC or a peered one (no private hosted zone: public DNS
returns the endpoint's private IPs). For `name: 'api'`: `api-<region>.<domain>` per region, and
`api.<domain>`, which every region serves, weighted across them for failover.

Two halves, because they live in different stacks (often different accounts). Each region's stack
outputs `PrivateApi<Name>VpceDnsName` / `...VpceHostedZoneId`; the records stack takes them as its
`PrivateApi<Name><Region>VpceDnsName` / `...VpceHostedZoneId` parameters (CreCfnParameters):

```ts
// In each region's API stack (environment-agnostic; the hostname uses AWS::Region):
const cert = new ValidatedCertificate(this, 'Cert', { projectName: 'my-demo', domain, hostedZoneId, dnsRoleArn });
const vpce = network.vpc.addInterfaceEndpoint('ExecuteApi', { service: ec2.InterfaceVpcEndpointAwsService.APIGATEWAY });
const api = new apigateway.RestApi(this, 'Api', {
  endpointConfiguration: { types: [apigateway.EndpointType.PRIVATE], vpcEndpoints: [vpce] },
  policy: /* allow execute-api:Invoke to '*'; NO aws:SourceVpce condition */,
});
this.apiDomain = new PrivateApiCustomDomain(this, 'ApiDomain', { name: 'api', domain, certificateArn: cert.certificateArn, restApi: api, vpcEndpoint: vpce });

// The -dns stack (us-east-1, with the DNS role variables), given each region's domain:
const dns = new CreDnsStack(app, app.stackId('dns'), { region: 'us-east-1', assumeRole: '$DNS_ROLE_ARN?', executionRole: '$DNS_CFN_EXECUTION_ROLE_ARN?' });
dns.addPrivateApi({ domains: [apiUsEast1.apiDomain, apiUsWest2.apiDomain] /* weights?, healthCheckIds?, recordTypes? */ });
```

The `-dns` stack always deploys in us-east-1, so it can't infer where the API is: it takes each
domain's stack `region` prop. The API stacks stay environment-agnostic. The
shared name's set identifiers are the region names. The client's VPC needs a route to the endpoint (peering) and the endpoint's security
group must admit the client CIDR (`scopedIngress`).

#### Gotchas (MANDATORY): the seven mistakes, each already handled or refused

1. **A dotted region name** (`api.us-east-1.<domain>`): the wildcard `*.<domain>` covers one
   label, so API Gateway says the name is not covered by the certificate. The construct uses
   `api-<region>`.
2. **V1 `AWS::ApiGateway::DomainName`** rejects PRIVATE ("Private Endpoint types are not
   supported"). The construct uses `DomainNameV2`.
3. **No policy on the custom domain name** makes API Gateway require SigV4: plain HTTPS gets 403.
   The construct sets an allow-all policy; the access associations limit who can reach it.
4. **An `aws:SourceVpce` condition on the REST API's own policy** rejects calls through the custom
   domain (403). Leave it off; the access association does that job. (Not checked: it's your API.)
5. **A zonal endpoint DNS name as the alias target** fails with "alias target name does not lie
   within the target zone". The construct outputs the regional name (the first `DnsEntries` entry).
6. **No `DomainNameAccessAssociation`** for the endpoint: every call is 403. The construct creates
   one per name, for the endpoint in the same region.
7. **The dash-form URL** (`<apiId>-<vpceId>.execute-api.<region>.amazonaws.com`) works without
   any of this, but presents `*.execute-api...`, so it can't carry your hostname (TLS mismatch)
   or Route 53 routing. Use it only when you need no custom name; the order is `<apiId>-<vpceId>`
   (the reverse is NXDOMAIN).

Also: records are `A` by default; add `'AAAA'` to `recordTypes` for an endpoint in a dual-stack or
IPv6 network. The shared name defaults to the first region at weight 100 and the rest at 0, which
Route 53 answers only when every weighted record above 0 is unhealthy, so give `healthCheckIds`
(e.g. ARC Region switch's) for failover.
