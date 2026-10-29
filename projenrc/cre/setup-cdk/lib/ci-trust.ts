import * as iam from 'aws-cdk-lib/aws-iam';
import * as names from './names';
import { SharedResources } from './shared-resources';

/** The GitLab credential vendor's jump role on gitlab.aws.dev (BuilderHub, "Vending AWS credentials to CI/CD jobs"). */
export const GITLAB_JUMP_ROLE = 'arn:aws:iam::979517299116:role/gitlab-runners-prod';

export interface CiTrustProps {
  readonly provider: names.Provider;
  readonly project: string;
  /** GitLab: the group path, e.g. aws-cre/resilience-demos. */
  readonly gitlabNamespace?: string;
  /** GitHub: the repo's OIDC subject prefix, e.g. repo:octo/demo. */
  readonly githubSubject?: string;
  /**
   * GitHub: the environment the deploy jobs run in (a public repo's `AWS`). Its subject is then
   * trusted, not main's: only jobs that declare it get credentials, and only on main (its
   * deployment branch policy). @default main's subject
   */
  readonly githubEnvironment?: string;
  /**
   * GitHub, with githubEnvironment: trust main's subject as well, while a repo switches to the
   * environment (setup:github-public). main's old workflow, which declares no environment, keeps
   * deploying until the switch's PR has merged and its runs have finished; then setup narrows the
   * trust to the environment alone. @default false
   */
  readonly githubAlsoTrustMain?: boolean;
}

/**
 * Who may assume a role from the project's CI, directly (no role chaining): GitLab, the credential
 * vendor's jump role for this project's jobs on a protected branch; GitHub, the account's OIDC
 * provider for runs on main of this repo (or, given githubEnvironment, its jobs in that environment). The same in every account a project's CI reaches.
 */
export function ciPrincipal(shared: SharedResources | undefined, props: CiTrustProps): iam.IPrincipal {
  if (props.provider === 'gitlab') {
    if (!props.gitlabNamespace) throw new Error('GitLab needs the group path (gitlabNamespace)');
    return new iam.ArnPrincipal(GITLAB_JUMP_ROLE).withConditions({
      StringEquals: {
        'aws:PrincipalTag/GitLab:Group': props.gitlabNamespace,
        'aws:PrincipalTag/GitLab:Project': props.project,
        'aws:PrincipalTag/GitLab:Project:ProtectedBranch': 'Yes',
      },
    }).withSessionTags();
  }
  if (props.githubEnvironment !== undefined && !/^[A-Za-z0-9_.-]{1,255}$/.test(props.githubEnvironment)) {
    throw new Error(`githubEnvironment '${props.githubEnvironment}': letters, digits, _ . -`);
  }
  if (!props.githubSubject?.startsWith('repo:')) throw new Error('GitHub needs the OIDC subject prefix (githubSubject, repo:<owner>/<repo>)');
  if (!shared) throw new Error('GitHub CI trust needs the account\'s shared resources (its OIDC provider)');
  if (props.githubAlsoTrustMain && !props.githubEnvironment) throw new Error('githubAlsoTrustMain needs githubEnvironment');
  const main = `${props.githubSubject}:ref:refs/heads/main`;
  const subjects = props.githubEnvironment
    ? [`${props.githubSubject}:environment:${props.githubEnvironment}`, ...(props.githubAlsoTrustMain ? [main] : [])]
    : [main];
  return new iam.WebIdentityPrincipal(shared.oidcProvider(), {
    StringEquals: {
      [`${names.GITHUB_OIDC_HOST}:aud`]: 'sts.amazonaws.com',
      [`${names.GITHUB_OIDC_HOST}:sub`]: subjects.length === 1 ? subjects[0] : subjects,
    },
  });
}
