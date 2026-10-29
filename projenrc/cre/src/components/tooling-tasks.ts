import type { awscdk } from 'projen';
import type { GitProvider } from './git-tasks';
import { CRE_SCRIPTS } from '../paths';

/** What the setup tasks pass to the cresetup scripts: the project's own values, from .projenrc.ts. */
export interface SetupInput {
  readonly name: string;
  readonly provider: GitProvider;
  readonly repository: string;
  /** @default 'aws' */
  readonly partition?: string;
  /** GitHub: a public repo's deploy jobs run in this environment (its OIDC subject is trusted, its variables theirs). */
  readonly githubEnvironment?: string;
  /** GitHub: the builder's automation App. */
  readonly githubAppSlug?: string;
}

// No quote, space or $, and never a leading - (it would read as a switch).
const SAFE = /^[A-Za-z0-9.][A-Za-z0-9._/:-]*$/;

/** `repository`'s path: the GitLab group and project, or the GitHub owner and repo. */
function repositoryPath(repository: string): string[] {
  let parts: string[];
  try {
    parts = new URL(repository).pathname.replace(/\.git$/, '').split('/').filter(Boolean);
  } catch {
    throw new Error(`repository '${repository}' isn't a URL (https://<host>/<group or owner>/<name>)`);
  }
  if (parts.length < 2) throw new Error(`repository '${repository}' needs a group or owner and a name`);
  return parts;
}

/** Switches and values, each single-quoted into a task's shell line: no value may end the quote. */
function quoted(task: string, args: string[]): string {
  for (const v of args.filter((_, i) => i % 2 === 1)) {
    if (!SAFE.test(v)) throw new Error(`${task}: '${v}' (from .projenrc.ts) has characters a task can't pass`);
  }
  return args.map((v) => `'${v}'`).join(' ');
}

/**
 * account.py's fixed leading switches. The runner role's trust comes from `repository`: on GitLab
 * the group path (everything before the project), on GitHub `<owner>/<repo>`, whose OIDC subject
 * account.py reads from GitHub.
 */
export function setupAwsArgs(input: SetupInput): string[] {
  const parts = repositoryPath(input.repository);
  const trust = input.provider === 'gitlab'
    ? ['--namespace', parts.slice(0, -1).join('/')]
    // GitHub's subject names the owner's and repo's IDs (immutable subjects): account.py reads it from GitHub.
    : ['--github-repo', parts.join('/'),
      ...(input.githubEnvironment ? ['--github-environment', input.githubEnvironment] : [])];
  const args = ['--project', input.name, '--provider', input.provider, '--partition', input.partition ?? 'aws', ...trust];
  quoted('setup:aws', args);
  return args;
}

/** github_public.py's fixed switches: the repo, the project, the App and the partition. */
export function githubPublicArgs(input: SetupInput): string[] {
  if (!input.githubAppSlug) throw new Error('setup:github-public: github.appSlug is required');
  return ['--repo', repositoryPath(input.repository).join('/'), '--project', input.name, '--app-slug', input.githubAppSlug,
    '--partition', input.partition ?? 'aws'];
}

/**
 * setup:repo's steps. GitLab: `gitlab.py setup`, which also protects main and sets the variables
 * given. GitHub: `github.py setup` (repo, App, settings), `github.py protect` (main), then
 * `github.py variables` with the variables given (in the AWS environment for a public repo). Each re-applies what it finds different and nothing else,
 * and never creates a missing repo (--no-create): a retired project doesn't come back by a re-run.
 */
export function setupRepoSteps(input: SetupInput): { exec: string; receiveArgs?: boolean }[] {
  const parts = repositoryPath(input.repository);
  const script = (name: string) => `python3 ${CRE_SCRIPTS}/cresetup/${name}`;
  if (input.provider === 'gitlab') {
    const args = ['--project', parts[parts.length - 1], '--namespace', parts.slice(0, -1).join('/'), '--token-file', '.cre/gitlab-token'];
    return [{ exec: `${script('gitlab.py')} setup ${quoted('setup:repo', args)} --no-create "$@"`, receiveArgs: true }];
  }
  if (!input.githubAppSlug) throw new Error('setup:repo: github.appSlug is required');
  const repo = ['--repo', parts.join('/')];
  return [
    { exec: `${script('github.py')} setup ${quoted('setup:repo', [...repo, '--app-slug', input.githubAppSlug])} --no-create` },
    // main's protection, merged into whatever stronger one is there (a free plan's private repo has none: reported).
    { exec: `${script('github.py')} protect ${quoted('setup:repo', repo)}` },
    {
      exec: `${script('github.py')} variables ${quoted('setup:repo', [...repo,
        ...(input.githubEnvironment ? ['--environment', input.githubEnvironment] : [])])} "$@"`,
      receiveArgs: true,
    },
  ];
}

/**
 * Tasks every project type gets besides build/deploy/git:
 *
 *   cfs:curl [--json] <METHOD> <url|/path> [body]   call anything behind CloudFront Signer (Midway)
 *   cfs:onboard [--domain d] [--status]     onboard the edge distribution at CloudFrontSigner
 *   setup:aws / setup:repo                  re-run or repair the account (and DNS account) / repo
 *                                           setup that /cre-demo-init ran
 */
export function createToolingTasks(project: awscdk.AwsCdkTypeScriptApp, provider: GitProvider, setup: SetupInput): void {
  const python = (script: string) => [{ exec: `python3 ${CRE_SCRIPTS}/${script} "$@"`, receiveArgs: true }];
  project.addTask('cfs:curl', {
    description: 'Call a CloudFront Signer (Midway) protected URL: cfs:curl [--json] [-i] GET https://dxxxx.cloudfront.net/path [body]',
    steps: python('cfs/cfs-curl.py'),
  });
  project.addTask('cfs:onboard', {
    description: 'Onboard the edge distribution at CloudFrontSigner (Midway; after the first edge deploy, and again after the edge is renamed or recreated). --status | --offboard',
    steps: python('cfs/cfs-onboard.py'),
  });
  project.addTask('setup:aws', {
    description: 'The CI runner role and the CloudFormation execution role in the deploy account, and with --dns-profile the '
      + 'DNS roles in the hosted zone\'s account, as CloudFormation stacks (idempotent: an unchanged stack is a no-op).',
    steps: [{
      exec: `python3 ${CRE_SCRIPTS}/cresetup/account.py ${quoted('setup:aws', setupAwsArgs(setup))} "$@"`,
      receiveArgs: true,
    }],
  });
  project.addTask('setup:repo', {
    description: `Re-apply the ${provider === 'gitlab' ? 'GitLab project' : 'GitHub repo'}'s settings and set the CI/CD variables `
      + 'given (idempotent: only what differs changes). A variable that feeds a noEcho parameter is stored hidden.',
    steps: setupRepoSteps(setup),
  });
  if (provider === 'github') {
    project.addTask('setup:github-public', {
      description: 'A private repo made public, end to end (from a clean main): scan the history for credentials (--dry-run stops '
        + 'there), make it public with the AWS environment, move the deploy trust and role secrets to that environment, then '
        + 'set github.visibility in .projenrc.ts and merge the regenerated workflows as a PR.',
      steps: [{ exec: `python3 ${CRE_SCRIPTS}/cresetup/github_public.py ${quoted('setup:github-public', githubPublicArgs(setup))} "$@"`, receiveArgs: true }],
    });
    project.addTask('setup:github-app', {
      description: 'Create (once) or check your CRE automation GitHub App, aws-cre-automation-<alias>, and its key in ~/.cre.',
      steps: python('cresetup/github_app.py'),
    });
  }
}

/** CreFailureScenario checks, run as part of `test`. */
export function createFailureScenarioChecks(
  project: awscdk.AwsCdkTypeScriptApp,
  opts: { forbiddenTerms: string[]; cleanPurityAllow: string[]; groundTruthFile: string },
): void {
  for (const a of opts.cleanPurityAllow) {
    if (!/^\S+ \S/.test(a)) throw new Error(`cleanPurityAllow: '${a}' must be '<path or glob> <pattern>'.`);
  }
  const purity = project.addTask('test:clean-purity', {
    description: 'Assert src/clean reads as an ordinary system (no failure, scenario or provenance tells).',
    env: { CRE_FORBIDDEN_TERMS: opts.forbiddenTerms.join('\n'), CRE_PURITY_ALLOW: opts.cleanPurityAllow.join('\n') },
    steps: [{ exec: `python3 ${CRE_SCRIPTS}/failure-scenario/clean-purity-scan.py` }],
  });
  // The answer key is required once the scenario exists: a fresh scaffold (no stacks yet) passes
  // with a warning; once cre-stacks.json lists a stack, a missing key fails the build.
  const gt = project.addTask('test:ground-truth', {
    description: `Validate the answer key (${opts.groundTruthFile}): a gap, a red herring, fixed vocabularies, real paths. Required once the app has stacks.`,
    steps: [{
      exec: `if [ -f "${opts.groundTruthFile}" ]; then python3 ${CRE_SCRIPTS}/failure-scenario/ground-truth-check.py "${opts.groundTruthFile}"; ` +
        'elif python3 -c \'import json,sys; sys.exit(0 if json.load(open("cre-stacks.json")).get("stacks") else 1)\' 2>/dev/null; then ' +
        `echo "❌ ${opts.groundTruthFile} is missing: the scenario has stacks, so its answer key is required (cre-failure-scenario.md, Ground truth)." >&2; exit 1; ` +
        `else echo "⚠  ${opts.groundTruthFile} not written yet (required once the app has stacks)"; fi`,
      shell: ['bash', '-c'],
    }],
  });
  project.testTask.spawn(purity);
  project.testTask.spawn(gt);
}
