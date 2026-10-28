import type { IConstruct } from 'constructs';

/**
 * The single source of truth for the EMF metric
 * namespace shared across the observability construct and the load-gen container env var.
 *
 * MUST match the namespace the workload's EMF lines carry. Consumed as the default for
 * {@link DemoObservabilityProps.metricNamespace}. The Locust image's `cre_emf.py` reads the
 * value from the `DEMO_METRIC_NAMESPACE` env var (C3 — that exact env var name), which the CDK
 * stack injects onto the load-gen container so the running value is authoritative and the
 * literals only matter as defaults.
 *
 * One constant, so the dashboard and the emitter can't drift onto different namespaces.
 */
export const DEMO_METRIC_NAMESPACE = 'MyResilienceDemo';

/**
 * The namespace a project's metrics go to by default: `CRE/<project name>` (from the CreApp's
 * `cre:projectName` context, which projen writes), so two projects in one account never mix their
 * metrics on each other's dashboards and alarms. {@link DEMO_METRIC_NAMESPACE} outside a CreApp.
 * The load generator injects it as `DEMO_METRIC_NAMESPACE`, the env var the EMF helper reads.
 */
export function defaultMetricNamespace(scope: IConstruct): string {
  const project = scope.node.tryGetContext('cre:projectName');
  return typeof project === 'string' && project ? `CRE/${project}` : DEMO_METRIC_NAMESPACE;
}

/**
 * The env var name the CDK stack injects onto the load-gen / app container and the Python
 * EMF helper reads. Standardized as `DEMO_METRIC_NAMESPACE`
 * (NOT `EMF_NAMESPACE`) — observability owns the namespace concept.
 */
/**
 * Set to `true` on a construct whose tasks emit the client metrics (`LoadGenerator`), so
 * `CreDashboardStack` can check it reads the region they land in. A global symbol, so it holds
 * across copies of this module.
 */
export const CLIENT_METRICS_SOURCE: unique symbol = Symbol.for('@cre/observability.clientMetricsSource') as any;

export const DEMO_METRIC_NAMESPACE_ENV_VAR = 'DEMO_METRIC_NAMESPACE';
