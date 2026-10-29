import { createHash } from 'crypto';

/** Every name setup gives an AWS resource, from the project's name. A re-run finds them by name. */

const PROJECT = /^[a-z0-9][a-z0-9-]{1,40}[a-z0-9]$/;

/** 63 (S3's limit) minus `-<12-digit account>-<14-character region>-an`, the longest suffix. */
export const BUCKET_PREFIX_MAX = 63 - (1 + 12 + 1 + 14 + 3);

export const EXEC_ROLE = 'CreCfnExecution';
export const RUNNER_POLICY = 'cre-runner';
export const GITHUB_OIDC_HOST = 'token.actions.githubusercontent.com';
export const TAG_MANAGED = 'cre:managed-by';
export const TAG_PROJECT = 'cre:project';
export const MANAGED_BY = 'cre-setup';

export type Provider = 'gitlab' | 'github';

export function checkProject(project: string): string {
  if (!PROJECT.test(project)) {
    throw new Error(`project name '${project}': 3-42 lowercase letters, digits and hyphens, starting and ending with a letter or digit`);
  }
  return project;
}

/** The project name, or for a long one its first 23 characters and 8 of its SHA-256. */
export function bucketPrefix(project: string): string {
  checkProject(project);
  if (project.length <= BUCKET_PREFIX_MAX) return project;
  return `${project.slice(0, 23).replace(/-+$/, '')}-${createHash('sha256').update(project).digest('hex').slice(0, 8)}`;
}

/** A region's assets bucket, in the S3 account regional namespace (scripts/build/assets.py, the same rule). */
export function bucket(project: string, account: string, region: string): string {
  return `${bucketPrefix(project)}-${account}-${region}-an`;
}

export const runnerRole = (provider: Provider, project: string) =>
  `${provider === 'gitlab' ? 'GitLabRunner' : 'GitHubRunner'}-${checkProject(project)}`;
export const ecrRepository = (project: string) => `cre/${checkProject(project)}`;
// Capitalised, so no project's runner (allowed `stack/<project>-*`, project names are lowercase) can reach them.
export const iamStack = (project: string) => `CreSetup-${checkProject(project)}`;
/** A region's assets bucket stack, made by the deploy (scripts/build/assets.py STACK_PREFIX). */
export const assetsStack = (project: string) => `CreAssets-${checkProject(project)}`;

// The DNS account (a zone in an account other than the app's): one role the deploy and the
// certificate validators assume, one CloudFormation runs as, in one stack per project.
export const dnsStack = (project: string) => `CreSetupDns-${checkProject(project)}`;
export const dnsDeployRole = (project: string) => `CreDnsDeploy-${checkProject(project)}`;
export const dnsExecRole = (project: string) => `CreDnsExec-${checkProject(project)}`;
/** The project's own DNS stack (constructs' app.stackId('dns')), the one stack the DNS roles may deploy. */
export const projectDnsStack = (project: string) => `${checkProject(project)}-dns`;
/** The certificate validator's role in the app account (constructs' certValidatorRoleName). */
export const certValidatorRole = (project: string, region: string) => `${checkProject(project)}-acm-${region}`;
