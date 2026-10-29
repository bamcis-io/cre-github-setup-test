import { createHash } from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as names from './names';

const HANDLER = fs.readFileSync(path.join(__dirname, 'shared-resource.py'), 'utf8');

export function oidcProviderArn(): string {
  return `arn:${Aws.PARTITION}:iam::${Aws.ACCOUNT_ID}:oidc-provider/${names.GITHUB_OIDC_HOST}`;
}

/**
 * The account-wide resources every CRE project in an account shares, through one custom resource
 * function: made if missing, never deleted (shared-resource.py). CloudFormation runs a custom
 * resource only when its properties change: CodeHash changes with the handler's code, so a fix to the
 * handler reaches every account, while a deploy with nothing changed runs nothing.
 */
export type SharedKind = 'ExecutionRole' | 'OidcProvider';

export class SharedResources {
  private readonly fn: lambda.Function;

  /**
   * The function is made directly in `stack` as `SharedResources`, and each resource as its Kind: a
   * custom resource's service token can never change, so these logical IDs must not either. The
   * function may change only the `kinds` this stack uses.
   */
  constructor(private readonly stack: cdk.Stack, private readonly kinds: SharedKind[]) {
    this.fn = new lambda.Function(stack, 'SharedResources', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromInline(HANDLER),
      timeout: cdk.Duration.minutes(1),
      description: 'Makes the account-wide CRE resources if missing; never deletes them',
    });
    if (kinds.includes('ExecutionRole')) {
      this.fn.addToRolePolicy(new iam.PolicyStatement({
        actions: ['iam:GetRole', 'iam:CreateRole', 'iam:TagRole', 'iam:UpdateAssumeRolePolicy', 'iam:AttachRolePolicy'],
        resources: [`arn:${Aws.PARTITION}:iam::${Aws.ACCOUNT_ID}:role/${names.EXEC_ROLE}`],
      }));
    }
    if (kinds.includes('OidcProvider')) {
      this.fn.addToRolePolicy(new iam.PolicyStatement({
        actions: ['iam:CreateOpenIDConnectProvider', 'iam:TagOpenIDConnectProvider', 'iam:AddClientIDToOpenIDConnectProvider'],
        resources: [oidcProviderArn()],
      }));
    }
  }

  /** CreCfnExecution: trusts only CloudFormation, AdministratorAccess. Its ARN. */
  public executionRole(): string {
    return this.resource('ExecutionRole');
  }

  /** The account's GitHub OIDC provider, with the sts.amazonaws.com audience. Its ARN. */
  public oidcProvider(): string {
    return this.resource('OidcProvider');
  }

  private resource(kind: SharedKind): string {
    if (!this.kinds.includes(kind)) throw new Error(`SharedResources: ${kind} isn't one of this stack's kinds (${this.kinds.join(', ')})`);
    return new cdk.CustomResource(this.stack, kind, {
      serviceToken: this.fn.functionArn,
      serviceTimeout: cdk.Duration.minutes(2),
      properties: { Kind: kind, CodeHash: createHash('sha256').update(HANDLER).digest('hex').slice(0, 16) },
    }).getAttString('Arn');
  }
}
