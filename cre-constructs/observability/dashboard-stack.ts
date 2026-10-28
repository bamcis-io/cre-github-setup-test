import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { CreCfnParameter } from '../core/cre-cfn-parameter.js';
import { CreStack, CreStackProps } from '../core/cre-stack.js';
import { DemoObservability, DemoObservabilityProps } from './demo-observability.js';
import { CLIENT_METRICS_SOURCE } from './metric-namespace.js';

export interface CreDashboardLink {
  readonly label: string;
  /** The edge stack output holding the URL (e.g. `EdgeUrl`). One the edge doesn't have throws (a default link is dropped instead). */
  readonly output: string;
  /**
   * A path under that URL, without a leading slash (`'inject'` for a Locust UI page:
   * `{ label: 'Arm / Disarm failure', output: 'EdgeLocustUrl', path: 'inject' }`). @default the URL itself
   */
  readonly path?: string;
}

/** Links every dashboard gets: the edge, and Locust when the edge has a `locust` route. */
export const DEFAULT_DASHBOARD_LINKS: CreDashboardLink[] = [
  { label: 'Locust UI (CloudFront Signer)', output: 'EdgeLocustUrl' },
  { label: 'Edge (CloudFront Signer)', output: 'EdgeUrl' },
];

export interface CreDashboardStackProps extends CreStackProps, Omit<DemoObservabilityProps, 'demoName' | 'headerMarkdown' | 'createDashboard'> {
  /**
   * The project name: the header's title. The dashboard and its alarm are named after this stack
   * (`<project>-dashboard`), so a second dashboard stack (one per region) or a renamed one never
   * takes another's global dashboard name. @default the CreApp's project name
   */
  readonly projectName?: string;
  /**
   * The edge stack whose URLs the header links. The dashboard then deploys after it. Without one,
   * the header has no links.
   */
  readonly edge?: CreStack & { readonly outputValues: Record<string, string> };
  /** Header links, from the edge's outputs. @default {@link DEFAULT_DASHBOARD_LINKS} */
  readonly links?: CreDashboardLink[];
  /** Extra markdown under the links. */
  readonly headerMarkdown?: string;
}

/**
 * The project's dashboard, as its own `$PROJECT_NAME-dashboard` stack. Given the `edge`, its header
 * links the edge and Locust URLs (CreCfnParameters taking the edge's outputs), so it deploys after it.
 *
 * It is a {@link DemoObservability}: a header, a client-availability graph and one alarm. That is
 * only the scaffold. Every project adds the widgets its story depends on with
 * `observability.addMetricRow(...)`, or by adding widgets to `observability.dashboard`.
 */
export class CreDashboardStack extends CreStack {
  public readonly observability: DemoObservability;

  constructor(scope: Construct, id: string, props: CreDashboardStackProps) {
    super(scope, id, props);
    const projectName = props.projectName ?? this.node.tryGetContext('cre:projectName');
    if (!projectName) throw new Error(`CreDashboardStack ${id}: no projectName, and the app has no project name (cre:projectName).`);
    const parts: string[] = [];
    const edge = props.edge;
    // One parameter per edge output, shared by every link on it: several pages under the Locust
    // URL are the point of `path` (review 10d A2: a second link on one output reused the id).
    const urls = new Map<string, CreCfnParameter>();
    for (const link of edge ? props.links ?? DEFAULT_DASHBOARD_LINKS : []) {
      const value = edge!.outputValues[link.output];
      if (value === undefined) {
        // A default link the edge doesn't have (no Locust route) is left out; a link the caller
        // named is a typo or a missing route, and a dashboard without it would look fine (review 10e F3).
        if (!props.links) continue;
        throw new Error(`CreDashboardStack ${id}: link '${link.label}' names the edge output '${link.output}', which ` +
          `${edge!.stackName} doesn't have. Its outputs: ${Object.keys(edge!.outputValues).sort().join(', ')}.`);
      }
      let url = urls.get(link.output);
      if (!url) {
        url = new CreCfnParameter(this, link.output, {
          from: { stack: edge!, value },
          description: `URL of the edge's ${link.output} output (${edge!.stackName}), for the dashboard's links.`,
        });
        urls.set(link.output, url);
      }
      // EdgeUrl has no trailing slash; every Edge<Route>Url ends with one (CreEdgeStack).
      const path = (link.path ?? '').replace(/^\/+/, '');
      const sep = path && link.output === 'EdgeUrl' ? '/' : '';
      parts.push(`- [${link.label}](${url.valueAsString}${sep}${path})\n`);
    }
    if (props.headerMarkdown) parts.push(`\n${props.headerMarkdown}`);
    // The dashboard graphs metrics of one region (its own, or metricsRegion). The client metrics are
    // in the LoadGenerator's region, and a dashboard placed elsewhere draws nothing, with no error
    // (review 10d A7). Checked once the app is built, when every LoadGenerator exists.
    this.node.addValidation({
      validate: () => {
        const reads = props.metricsRegion ?? this.deployRegion;
        const loadRegions = [...new Set(this.node.root.node.findAll()
          .filter((c) => (c as unknown as Record<symbol, unknown>)[CLIENT_METRICS_SOURCE] === true)
          .map((c) => (cdk.Stack.of(c) as Partial<CreStack>).deployRegion)
          .filter((r): r is string => !!r))];
        if (loadRegions.length > 0 && !loadRegions.includes(reads)) {
          cdk.Annotations.of(this).addWarningV2('@cre/observability:dashboard-region',
            `CreDashboardStack ${this.node.id} reads metrics in ${reads}, but the LoadGenerator's client metrics land in ` +
            `${loadRegions.join(', ')}: its availability graph and alarm stay empty. Deploy the dashboard in that region, or set ` +
            `metricsRegion: '${loadRegions[0]}'.`);
        }
        return [];
      },
    });
    this.observability = new DemoObservability(this, 'Observability', {
      demoName: this.stackName,
      title: projectName,
      metricNamespace: props.metricNamespace,
      metricsRegion: props.metricsRegion,
      period: props.period,
      availabilityAlarmThreshold: props.availabilityAlarmThreshold,
      createBaselineAlarms: props.createBaselineAlarms,
      headerMarkdown: parts.length > 0 ? cdk.Fn.join('', parts) : undefined,
    });
  }
}
