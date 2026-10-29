import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { ciPrincipal } from './ci-trust';
import * as names from './names';
import { SharedResources } from './shared-resources';

export { GITLAB_JUMP_ROLE } from './ci-trust';
export const CFN = 'cloudformation.amazonaws.com';

export interface IamStackProps extends cdk.StackProps {
  readonly project: string;
  readonly provider: names.Provider;
  /** GitLab: the group path, e.g. aws-cre/resilience-demos. */
  readonly gitlabNamespace?: string;
  /** GitHub: the repo's OIDC subject prefix, e.g. repo:octo/demo. */
  readonly githubSubject?: string;
  /** GitHub: the deploy jobs' environment (ci-trust.ts). */
  readonly githubEnvironment?: string;
  /** GitHub: main's subject too, while the repo switches to the environment (ci-trust.ts). */
  readonly githubAlsoTrustMain?: boolean;
}

/**
 * A project's IAM, in one stack: its runner role (CI deploys as it), and the account's shared
 * CloudFormation execution role (and on GitHub the
 * OIDC provider) through a custom resource that makes them only if missing and never deletes them.
 */
export class IamStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: IamStackProps) {
    super(scope, id, props);
    const { project, provider } = props;
    cdk.Tags.of(this).add(names.TAG_MANAGED, names.MANAGED_BY);
    cdk.Tags.of(this).add(names.TAG_PROJECT, project);

    const shared = new SharedResources(this, provider === 'github' ? ['ExecutionRole', 'OidcProvider'] : ['ExecutionRole']);
    const execRoleArn = shared.executionRole();
    const runner = new iam.Role(this, 'Runner', {
      roleName: names.runnerRole(provider, project),
      assumedBy: ciPrincipal(shared, props),
      // GitHub only: GitLab reaches the role by role chaining, which AWS caps at one hour.
      maxSessionDuration: provider === 'github' ? cdk.Duration.hours(2) : undefined,
      description: `CI deploy role for CRE project ${project}`,
      inlinePolicies: { [names.RUNNER_POLICY]: runnerPolicy(project, execRoleArn) },
    });

    new cdk.CfnOutput(this, 'RunnerRoleArn', { value: runner.roleArn });
    new cdk.CfnOutput(this, 'CfnExecutionRoleArn', { value: execRoleArn });
  }
}

/**
 * What CI needs, on this project's resources only: its stacks (named `<project>` or
 * `<project>-<suffix>`), its assets buckets and ECR repository in any region, and passing the
 * execution role to CloudFormation. Only actions that take no resource ARN use "*".
 */
export function runnerPolicy(project: string, execRoleArn: string): iam.PolicyDocument {
  const arn = (service: string, region: string, resource: string) =>
    `arn:${Aws.PARTITION}:${service}:${region}:${Aws.ACCOUNT_ID}:${resource}`;
  // Any region: the deploy makes a region's bucket (and repository) the first time a stack goes there.
  const buckets = [`arn:${Aws.PARTITION}:s3:::${names.bucket(project, Aws.ACCOUNT_ID, '*')}`];
  const s = (sid: string, actions: string[], resources: string[], conditions?: Record<string, unknown>) =>
    new iam.PolicyStatement({ sid, actions, resources, conditions });
  return new iam.PolicyDocument({ statements: [
    s('Identity', ['sts:GetCallerIdentity'], ['*']),
    s('StacksRead', ['cloudformation:ListStacks', 'cloudformation:ValidateTemplate', 'cloudformation:GetTemplateSummary'], ['*']),
    s('Stacks', ['cloudformation:DescribeStacks', 'cloudformation:DescribeStackEvents', 'cloudformation:DescribeStackResources',
      'cloudformation:GetTemplate', 'cloudformation:CreateChangeSet', 'cloudformation:DescribeChangeSet',
      'cloudformation:ExecuteChangeSet', 'cloudformation:DeleteChangeSet', 'cloudformation:ListChangeSets',
      'cloudformation:DeleteStack', 'cloudformation:ContinueUpdateRollback'],
    [arn('cloudformation', '*', `stack/${project}/*`), arn('cloudformation', '*', `stack/${project}-*/*`),
      // Its assets bucket stacks, made by the deploy (scripts/build/assets.py).
      arn('cloudformation', '*', `stack/${names.assetsStack(project)}/*`)]),
    s('AssetsBuckets', ['s3:ListBucket', 's3:GetBucketLocation'], buckets),
    s('AssetsObjects', ['s3:GetObject', 's3:PutObject', 's3:DeleteObject'], buckets.map((b) => `${b}/*`)),
    s('EcrLogin', ['ecr:GetAuthorizationToken'], ['*']),
    s('EcrRepository', ['ecr:CreateRepository', 'ecr:DescribeRepositories', 'ecr:DescribeImages',
      'ecr:BatchCheckLayerAvailability', 'ecr:InitiateLayerUpload', 'ecr:UploadLayerPart', 'ecr:CompleteLayerUpload',
      'ecr:PutImage', 'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'],
    [arn('ecr', '*', `repository/${names.ecrRepository(project)}`)]),
    s('PassExecutionRole', ['iam:PassRole'], [execRoleArn], { StringEquals: { 'iam:PassedToService': CFN } }),
  ] });
}
