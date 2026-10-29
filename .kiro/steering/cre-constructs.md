---
inclusion: auto
name: cre-constructs
description: The @cre/constructs library installed in this project: every construct family (networking, observability, auth, load generation, CloudFront Signer edge, DNS), the standard stacks, when to use each and how to import it. Read before writing or changing a CDK stack or construct, or when choosing how to build a feature.
---
# CRE construct library (`@cre/constructs`): what exists, when to use it, how to import it

Every construct below is already installed in this project as the package `@cre/constructs` (the
`cre-constructs/` directory, a `link:` dependency). Before writing a construct or a stack, check
this list. Use one when it fits; write your own only when none does, and say why in the design.

## How to use it

```ts
import { Aws } from 'aws-cdk-lib';
import { CreApp, CreCfnParameter, CreStack, RegionalNetwork, LoadGenerator, CreEdgeStack } from '@cre/constructs';
```

- Import from the package name only, never from a path into `cre-constructs/`.
- Treat it like a package in `node_modules`: **never edit `cre-constructs/`**. `npx projen test`
  fails if it differs from what the template installed. A change goes to
  `cre-demo-template/constructs/` via an MR there; `npx projen update-template` then installs the
  new version here (new constructs arrive, removed ones go).
- The project's own constructs and stacks live in `src/` (FailureScenario: `src/clean` or
  `src/deploy`) and import `@cre/constructs` like any other code.
- Props, examples and **mandatory gotchas** for each construct are in `cre-constructs/README.md`
  (installed with the library; sections named below), and in the TSDoc on each class.
- Stacks: subclass or instantiate the `Cre*Stack` classes. `app.ts` creates a `CreApp`; every
  top-level stack extends `CreStack`, is named `app.stackId('<name>')`, and gets its deploy `region`
  as a prop. Every parameter the pipeline passes is a `CreCfnParameter` (`cre-pipelines.md`): the
  pipeline is generated from the app.
- **No `env`, no region or account literals** (CreStack refuses `env`; the `region` prop says
  where it deploys). Inside a stack, only `Aws.REGION` / `ACCOUNT_ID` / `PARTITION` / `URL_SUFFIX`:
  `cre-pipelines.md` Rule 3, the one home of that rule. Not every construct exists in every
  partition (the edge is `aws` only; each family says), and `npx projen` refuses a stack in a
  region its kind can't use.

## The deploy shape these fit into

Every deploy uploads its assets to a new prefix (`<project>/<pipeline id>/`), passed to every stack
as `AssetsBucketPrefix`, so every stack is updated on every deploy, even when nothing in it changed.
Anything that names an asset by that prefix changes each time: a Lambda's code location is
harmless, but an EC2 launch template whose user data contains `${AssetsBucketPrefix}bin/tool`
gets a new version on every deploy, and an Auto Scaling group that rolls on launch-template
changes replaces its instances. Keep such paths out of what triggers replacement (fetch the
tool through a stable SSM parameter the stack writes, or don't roll on launch-template changes).

The pipeline is one deploy job per `CreStack`, in dependency order (`cre-pipelines.md`). The stacks
below already wire themselves: give them the objects they take values from (`CreEdgeStack` the
routes' `CreEdgeOrigin`s, `CreDashboardStack` the `edge`, `CrePeeringStack` the `networks`,
`addPrivateApi` the regions' `PrivateApiCustomDomain`s) and their `CreCfnParameter`s add the outputs
there and the deploy order.

## Example: two stacks wired by a parameter

The rules it follows (names, `region`, parameters, no exports) are `cre-pipelines.md`'s, "Adding a
top-level stack". A test compiles this example against `@cre/constructs`.

```ts
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { CreApp, CreCfnParameter, CreStack, CreStackProps } from '@cre/constructs';

const app = new CreApp();

class StorageStack extends CreStack {
  public readonly bucket: s3.Bucket;
  constructor(scope: Construct, id: string, props: CreStackProps) {
    super(scope, id, props);
    this.bucket = new s3.Bucket(this, 'Data');
  }
}

interface ApiStackProps extends CreStackProps { readonly storage: StorageStack }
class ApiStack extends CreStack {
  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);
    // Another stack's value: adds the output to StorageStack, this parameter here; the pipeline
    // passes one to the other and deploys ApiStack after StorageStack.
    const bucketName = new CreCfnParameter(this, 'BucketName', {
      from: { stack: props.storage, value: props.storage.bucket.bucketName },
    });
    // A deploy variable (GitLab CI/CD variable, GitHub secret, or local env).
    const domain = new CreCfnParameter(this, 'Domain', { fromVariable: 'DOMAIN' });
    new lambda.Function(this, 'Api', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context):\n    return {"statusCode": 200}\n'),
      environment: { BUCKET: bucketName.valueAsString, DOMAIN: domain.valueAsString },
    });
  }
}

const storage = new StorageStack(app, app.stackId('storage'), { region: 'us-east-1' });
new ApiStack(app, app.stackId('api-us-east-1'), { region: 'us-east-1', storage });
app.synth();
```

## Families

### core
`CreApp` (the app: strips all CDK metadata from every template, records the stacks the pipeline is
generated from, `stackId()`, and fails the synth on a top-level stack that isn't a CreStack or a
parameter without a default or a source), `CreStack` (base class for every top-level stack: its
`region`, `dependsOn`, `assumeRole`, `executionRole`; stack name = id; the deploy's S3-prefix
synthesizer; the `AssetsBucketName`/`AssetsBucketPrefix` parameters every deploy passes; no `env`),
`CreCfnParameter` (a parameter with its source: `from` another stack's value, or `fromVariable`),
`creSynthesizer()`, `requireDeployRegion()` (a deploy-time Rule that the stack is in one of the given
regions), and `CreIamStack` (the shared IAM stack, refused outside its partition's IAM control-plane
region, us-east-1 in `aws`). **When:** every project: one `CreApp`, every stack a `CreStack`, every
passed parameter a `CreCfnParameter`. Roles shared by stacks in several regions go in a
`CreIamStack`; the stacks that use them take the ARNs with `CreCfnParameter({ from })`.

### networking
`RegionalNetwork` (the VPC; `vpcCidr` is a required prop, which the network stacks below fill from their `VpcCidr` parameter; `ipMode` `ipv4` | `dualstack` |
`ipv6`; S3 and DynamoDB gateway endpoints always), with two standard shapes:
`RegionalNetwork.privateNetwork()` (isolated subnets plus an attached, unrouted internet gateway;
no internet) and `RegionalNetwork.egressOnly()` (outbound internet through a NAT gateway per AZ for
IPv4 and an egress-only internet gateway for IPv6). As stacks: `CrePrivateNetworkStack`,
`CreEgressOnlyNetworkStack` (base `CreNetworkStack`), which own the `VpcCidr` parameter (default the
`vpcCidr` prop) and output what peering needs. Also `ParameterizedVpc` (the building block), `PeeringMesh` and
`CrePeeringStack` (cross-region peering, IPv4 only). **When:** always for the VPC. Default to the
private network (Locust tasks and internal ALBs behind the edge live there; add the interface
endpoints the workload needs, e.g. ECR, ECR_DOCKER, CLOUDWATCH_LOGS for Fargate). Use egress-only
only when the workload calls the internet (NAT bills per hour per AZ and per GB). Choose `ipv6`
only after checking every service supports IPv6-only subnets (README §1). Multi-region means a
network stack per region (`app.stackId('network-<region>')`, each with its `region` and `vpcCidr`)
and a `CrePeeringStack` given them as `networks`, which takes each one's VPC id, CIDR, route tables
and region. README §1.

### observability
`DemoObservability` (dashboard header, a client-availability graph over `ClientSuccess`/
`ClientError`, one `NOT_BREACHING` alarm, a dashboard-URL output), `CreDashboardStack` (that as the
`$PROJECT_NAME-dashboard` stack, the dashboard and alarm named after the stack; given `edge`, its
header links the edge and Locust UI, and it deploys after the edge; `links` replaces those two, so list
them too to keep them: `[...DEFAULT_DASHBOARD_LINKS, { label, output, path }]`, where `path` goes under the output's URL), `defaultMetricNamespace()` (the project's metric
namespace, `CRE/<project>`: the dashboard and `LoadGenerator` default to it). Metrics are
Embedded Metric Format lines on stdout (`LoadGenerator`'s image ships `cre_emf.py`: an overlay locustfile calls `cre_emf.emit` once per request).
**The dashboard reads metrics in the region they land in** (the EMF log group's: the Locust task's
region): deploy the dashboard stack there (after the edge, whose URLs it links: `cre-constructs/README.md`
§2, "Operator dashboard"), or set `metricsRegion` to it (then without the baseline
alarm, which can read only its own region). A dashboard next to the edge in us-east-1 over a
client in us-east-2 graphs nothing.
**Only `LoadGenerator` sets the namespace on what it runs.** A
Lambda or container of yours that emits EMF must get it from its stack, or its metrics go to
`MyResilienceDemo`, which no dashboard reads, with no warning:
`environment: { [DEMO_METRIC_NAMESPACE_ENV_VAR]: defaultMetricNamespace(this) }` (both exported).
Widgets: `dashboardStack.observability.addMetricRow(title, metrics)` adds a 24-wide graph row;
`observability.metric(name, dimensions?, statistic = 'Sum')` is a metric **in the demo namespace**
(for `AWS/Lambda`, `AWS/ApplicationELB` and other AWS metrics use `new cloudwatch.Metric(...)` or the
construct's own `metricErrors()` etc., taken through a `CreCfnParameter` when the resource is in
another stack: a token from another stack becomes an export). `metricsRegion` (metrics from another region) works for the
dashboard, not the alarm: an alarm reads only its own region, so it is refused there unless
`createBaselineAlarms: false`. **When:** every resilience demo (required). The
scaffold has no stacks, so add one: `new CreDashboardStack(app, app.stackId('dashboard'), { region })`.
On its own it is only a shell: design the widgets the project's story depends on
(`dashboardStack.observability.addMetricRow(...)`). README §2.

### auth/cloudfront-signer — the default front door
`CreEdgeStack` (the `$PROJECT_NAME-edge` stack: ONE CloudFront distribution behind CloudFront
Signer, Midway + bindle, with path routes to internal ALBs through VPC origins; deployed to
us-east-1 whatever the app region; commercial `aws` partition only), `CreEdgeOrigin` (in the ALB's own stack, `new CreEdgeOrigin(this, 'Origin', { name, loadBalancer, vpc, port? })`:
opens the internal ALB to CloudFront and outputs it for the edge stack, by route name), `LOCUST_EDGE_ROUTE`, and
`DistributionSigner` (the signer itself, applied by `CreEdgeStack`). **When:** any UI, operator page
or API a person reaches, including the Locust UI. Steps: a VPC with an internet gateway attached (`RegionalNetwork.privateNetwork()`, or
`includeInternetGateway: true`),
a `CreEdgeOrigin` per ALB (Locust's is automatic: `loadGen.edgeOrigin`), and
`new CreEdgeStack(app, app.stackId('edge'), { region: 'us-east-1', routes: [{ ...LOCUST_EDGE_ROUTE, origin: loadGen.edgeOrigin! }] })`:
each route takes its origin's ALB, and `BindleId` comes from BINDLE_ID (which has a default). A route of your own is
`{ name: 'app', pathPattern: '/app*', origin: appStack.appOrigin }`: `pathPattern` is required, `/*` makes
it the default route, and `name` must equal the origin's `name`. Each origin must serve under its route's path prefix;
CloudFront doesn't rewrite paths. VPC origins are IPv4 or dual-stack (a route's `ipAddressType`),
never IPv6-only. After the first deploy, run `npx projen cfs:onboard`, and again after the edge stack is renamed or recreated (a new distribution and domain) (it
onboards the `EdgeDomainName` output at CloudFrontSigner); until then every request is 403. Pick another auth family only when this doesn't fit, recorded as a
deviation. README §6.

### auth/federate-alb
`FederateAlbAuth`: Midway through Amazon Federate OIDC at an ALB listener, plus
`app-helper/oidc-data.ts` / `app-helper/oidc_data.py` to validate `x-amzn-oidc-data` in the app. **When:** an internal web
app on its own ALB hostname with a certificate, when CloudFront doesn't fit. Needs a Federate
service profile. README §7.

### auth/allowed-cidr
`AllowedCidrSecurityGroup`: ingress gated by an `AllowedCidr` parameter, default deny-all, reset
on every deploy. **When:** a non-HTTP endpoint, or a machine caller that can't do Midway; defense
in depth under another mechanism. README §3.

### auth/cognito
`CognitoAuthConstruct` plus `CognitoAlbAuthConstruct` (ALB login) or `CognitoApiGwAuthConstruct`
(HTTP API JWT). **When:** only when non-Amazon users must log in. README §3.

### dns
`ValidatedCertificate({ projectName, domain, hostedZoneId, dnsRoleArn })`: an ACM certificate for
`*.<domain>` validated with no manual step, whether the zone is in this account or another. **When:**
`cre-setup.json` has a `domain` and something serves HTTPS on a custom hostname. One per project per
region. Its stack declares `CreCfnParameter`s with `fromVariable: 'DOMAIN'`, `'HOSTED_ZONE_ID'` and
`'DNS_ROLE_ARN'` (`optional: true`) and passes their values. Records go in a `CreDnsStack`, always in
the partition's Route 53 control-plane region (us-east-1 in `aws`; refused elsewhere, and its Rule
`DnsControlPlaneRegion` refuses others at deploy), with `assumeRole: '$DNS_ROLE_ARN?',
executionRole: '$DNS_CFN_EXECUTION_ROLE_ARN?'` (deployed into the zone's account when those are set). With
`assumeRole` it must be `app.stackId('dns')` and hold only record sets (setup's roles in the zone account allow only
that stack, `AWS::Route53::RecordSet` and `RecordSetGroup`; synth refuses anything else). `PrivateApiCustomDomain` (in each region's API stack) + `CreDnsStack.addPrivateApi`
(`PrivateApiDnsRecords`) give a PRIVATE REST API your own hostnames (`api-<region>.<domain>`, and
`api.<domain>` weighted across regions) through its execute-api VPC endpoint: give
`addPrivateApi({ domains })` each region's `PrivateApiCustomDomain` and the records stack takes their
endpoints. **When:** a private API that clients in another (peered) VPC or region
call by name, or that fails over by DNS. Don't hand-build it: README §8 lists seven mistakes that
each cost a 403 or a failed deploy (dotted region names, V1 DomainName, no domain policy,
`aws:SourceVpce` on the API policy, zonal alias target, no access association, dash-form URLs).
README §8.

### load-generation
`LoadGenerator`: Locust on Fargate, one EMF line per request into `DEMO_METRIC_NAMESPACE`.
`exposeWebUi: true` runs the Locust UI on an internal ALB under `/locust` and makes its edge origin,
`loadGen.edgeOrigin` (`edgeRoute: false` keeps it internal); put it on the edge with a route,
`{ ...LOCUST_EDGE_ROUTE, origin: loadGen.edgeOrigin! }` in the `CreEdgeStack`'s `routes`. `users` / `spawnRate` set the
run (headless) or the start form's defaults (UI). Scale with `workers: N` (a controller plus N worker
tasks; `processesPerTask` per core), never `desiredCount` with the UI. `addWebPage(path, html)`
adds an operator page at `/locust<path>` (`{{BASE_PATH}}` for links), with `@route` handlers from
`cre_pages` in the locustfile and `addToControllerRolePolicy` for their AWS access (`addToRolePolicy` grants the workers too); no hand-made Flask routes
(Locust's base path doesn't apply to them). Closed loop (users wait for each response) or
open loop (a fixed request rate that holds when the backend slows): `cre-load-generation.md`. **When:** the story needs
traffic (most projects). Set `containerImages: true` in `.projenrc.ts`. FailureScenario: only in
`src/deploy`. Drive it headlessly with the `locust-cfs` skill (`LOCUST_URL` = the `EdgeLocustUrl`
output). README §4.

### Failure injection (no construct)
The library has none: the design picks the mechanism the story needs and builds it, following
`cre-failure-injection.md` (stage variable, error-rate parameter, orchestrated proxy, FIS; the rules
they share; the operator pages on the Locust UI).