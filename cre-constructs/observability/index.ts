/**
 * Observability pattern barrel.
 *
 * Exports the always-on `DemoObservability` construct, its props, and the shared
 * `DEMO_METRIC_NAMESPACE` constant (the single source of truth consumed by the construct,
 * and the load-gen container's env var).
 */
export * from './metric-namespace.js';
export * from './demo-observability.js';
export * from './dashboard-stack.js';
