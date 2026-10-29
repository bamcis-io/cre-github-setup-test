import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import * as names from './names';
import { ciPrincipal, CiTrustProps } from './ci-trust';
import { CFN } from './iam-stack';
import { SharedResources } from './shared-resources';

export interface DnsStackProps extends cdk.StackProps, CiTrustProps {
  /** The partition's control-plane region: the only region the project's `-dns` stack can be in. */
  readonly home: string;
}

/**
 * Deployed into the DNS account, the separate account where the project's hosted zones live (an
 * account for DNS only). Two roles, both this project's:
 *   CreDnsDeploy-<project>  assumed directly by the project's CI (as its runner role is, no role
 *                           chaining) and by its certificate validators in the app account: deploys
 *                           the project's `<project>-dns` stack and writes records
 *   CreDnsExec-<project>    what CloudFormation runs as there: any Route 53 change but deleting a zone
 * The app account is a parameter (AppAccountId), so synth needs no account.
 */
export class DnsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: DnsStackProps) {
    super(scope, id, props);
    const { project, provider } = props;
    cdk.Tags.of(this).add(names.TAG_MANAGED, names.MANAGED_BY);
    cdk.Tags.of(this).add(names.TAG_PROJECT, project);
    const app = new cdk.CfnParameter(this, 'AppAccountId', {
      type: 'String', allowedPattern: '^\\d{12}$', description: 'The account the project deploys its app to',
    }).valueAsString;
    const appRole = (role: string) => `arn:${Aws.PARTITION}:iam::${app}:role/${role}`;

    const exec = new iam.Role(this, 'Exec', {
      roleName: names.dnsExecRole(project),
      assumedBy: new iam.ServicePrincipal(CFN),
      description: `CloudFormation in the DNS account for CRE project ${project}`,
      inlinePolicies: {
        route53: new iam.PolicyDocument({ statements: [
          new iam.PolicyStatement({ sid: 'Route53', actions: ['route53:*'], resources: ['*'] }),
          // A delegated zone deleted by hand leaves the domain open to sniping: never through a stack.
          // Nor turn off a signed zone's DNSSEC or its keys: with the DS record still at the parent,
          // that takes the domain down for every validating resolver.
          new iam.PolicyStatement({
            sid: 'NeverDeleteAZoneOrItsSigning', effect: iam.Effect.DENY, resources: ['*'],
            actions: ['route53:DeleteHostedZone', 'route53:DisableHostedZoneDNSSEC', 'route53:DeactivateKeySigningKey',
              'route53:DeleteKeySigningKey'],
          }),
        ] }),
      },
    });

    const s = (sid: string, actions: string[], resources: string[], conditions?: Record<string, unknown>) =>
      new iam.PolicyStatement({ sid, actions, resources, conditions });
    // GitHub's CI trust needs the OIDC provider in this account too: the shared custom resource makes it.
    const shared = provider === 'github' ? new SharedResources(this, ['OidcProvider']) : undefined;
    const deploy = new iam.Role(this, 'Deploy', {
      roleName: names.dnsDeployRole(project),
      assumedBy: ciPrincipal(shared, props),
      // As the runner's: GitHub's direct sessions may last 2 h. GitLab's vendor is role chaining, capped at 1 h.
      maxSessionDuration: provider === 'github' ? cdk.Duration.hours(2) : undefined,
      description: `Deploys CRE project ${project}'s DNS stack and writes its records`,
      inlinePolicies: {
        dns: new iam.PolicyDocument({ statements: [
          s('Identity', ['sts:GetCallerIdentity'], ['*']),
          s('StacksRead', ['cloudformation:ListStacks', 'cloudformation:ValidateTemplate', 'cloudformation:GetTemplateSummary'], ['*']),
          s('Stack', ['cloudformation:DescribeStacks', 'cloudformation:DescribeStackEvents', 'cloudformation:DescribeStackResources',
            'cloudformation:GetTemplate', 'cloudformation:CreateChangeSet', 'cloudformation:DescribeChangeSet',
            'cloudformation:ExecuteChangeSet', 'cloudformation:DeleteChangeSet', 'cloudformation:ListChangeSets',
            'cloudformation:DeleteStack', 'cloudformation:ContinueUpdateRollback'],
          [`arn:${Aws.PARTITION}:cloudformation:${props.home}:${Aws.ACCOUNT_ID}:stack/${names.projectDnsStack(project)}/*`]),
          // The stack's template is read from the project's assets bucket in that region, in the app account.
          s('Templates', ['s3:GetObject'], [`arn:${Aws.PARTITION}:s3:::${names.bucket(project, app, props.home)}/*`]),
          s('PassExec', ['iam:PassRole'], [exec.roleArn], { StringEquals: { 'iam:PassedToService': CFN } }),
          // Certificate validation records, written directly by the validators.
          s('Records', ['route53:ChangeResourceRecordSets', 'route53:ListResourceRecordSets', 'route53:GetChange',
            'route53:GetHostedZone', 'route53:ListHostedZones', 'route53:ListHostedZonesByName'], ['*']),
        ] }),
      },
    });

    // The certificate validators (Lambda functions in the app account, not CI jobs), by ARN in a
    // condition: IAM refuses a principal that doesn't exist yet, and the project's own stacks make them.
    deploy.assumeRolePolicy!.addStatements(new iam.PolicyStatement({
      sid: 'CertificateValidators',
      actions: ['sts:AssumeRole'],
      principals: [new iam.AccountPrincipal(app)],
      // Any region's validator: a stack may add a region without setup running again.
      conditions: { ArnLike: { 'aws:PrincipalArn': appRole(names.certValidatorRole(project, '*')) } },
    }));
    new cdk.CfnOutput(this, 'DnsDeployRoleArn', { value: deploy.roleArn });
    new cdk.CfnOutput(this, 'DnsExecRoleArn', { value: exec.roleArn });
  }
}
