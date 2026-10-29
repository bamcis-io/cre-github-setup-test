import { FileBase } from 'projen';
import type { IResolver, Project } from 'projen';

/**
 * The reference for every task that takes arguments: its usage and every switch. The generated
 * steering `.kiro/steering/cre-tasks.md` is written from this table plus the project's own tasks
 * (`TaskReferenceFile`), so it lists exactly the tasks the project has. `test/task-reference.test.ts`
 * runs each script's `--help` and fails when a switch is missing here or documented but not
 * accepted, so the reference can't drift from the scripts.
 */
export interface TaskRef {
  /** One line, as typed: `npx projen deploy [--stack <id>]... [--no-build] [--profile <p>]`. */
  readonly usage: string;
  /** The usage per provider, when the switches differ (cre-tasks.md shows the project's own). */
  readonly usageByProvider?: { readonly gitlab: string; readonly github: string };
  /** Each switch (as the script spells it) and what it does. A text starting `(GitLab)` or
   * `(GitHub)` is that provider's only: cre-tasks.md leaves it out for the other. */
  readonly switches: ReadonlyArray<readonly [string, string]>;
  /** The script under scripts/ whose `--help` lists the switches (the test reads it), plus any fixed leading args. */
  readonly script?: readonly string[];
  /** Extra lines (modes, exit codes, environment). */
  readonly notes?: string;
}

export const TASK_REFERENCE: Readonly<Record<string, TaskRef>> = {
  'branch': {
    usage: 'npx projen branch <type>/<description>',
    switches: [['--branch', 'the new branch name (instead of the positional one)']],
    script: ['git/branch.py'],
    notes: 'Creates the branch from origin\'s default branch, just fetched (from a detached HEAD: at HEAD), and carries uncommitted changes onto it. On the default branch, unpushed commits move onto the new branch and the local default branch is reset to origin\'s. Exit 10 refused (a bad name, a name already used, an unknown base), 30 on a conflict, nothing changed; 20 interrupted or an internal error, the repo put back. Through `npx projen` the exit is 1: the code is on the `RESULT` line.',
  },
  'git:status': {
    usage: 'npx projen git:status',
    switches: [],
    script: ['git/status.py'],
    notes: 'Changes nothing in the repo or on the remote (it fetches, and holds the cre-git lock while it reads): classifies the repo and says what `push` would do, including each refusal push would make before changing anything (`Push would: refuse (<STATE>)`, the same check push runs).',
  },
  'commit': {
    usage: 'npx projen commit --message "<type>(<scope>): <description>" [--files "<paths>"] [--branch <new>] [--allow-secret-path <path>]...',
    switches: [
      ['--message', 'the commit message (Conventional Commits; also -m). Required'],
      ['--files', 'stage only these paths (space- or newline-separated; repeatable). Globs as a shell reads them: `src/*.ts` stays in src/, `src/**/*.ts` goes below. Default: every change'],
      ['--branch', 'move to this new branch first when the current one can\'t take the commit (also -b)'],
      ['--allow-secret-path', 'stage a file whose path looks like a credential (e.g. a test fixture named `*.pem`); repeatable'],
    ],
    script: ['git/commit.py'],
    notes: 'Validates (synth + build), stages, commits. Never pushes.',
  },
  'push': {
    usage: 'npx projen push --message "<type>(<scope>): <description>" [--title "<MR/PR title>"] [--branch <new>] [--files "<paths>"] [--allow-secret-path <path>]...',
    switches: [
      ['--message', 'the commit message (also -m). Required'],
      ['--title', 'the MR/PR title (also -t). Default: the message\'s first line'],
      ['--branch', 'the new branch, when the current one can\'t take the change (also -b)'],
      ['--files', 'commit only these paths (repeatable; `*` stays in one directory, `**` crosses them). Default: every change'],
      ['--allow-secret-path', 'allow a credential-looking path; repeatable'],
    ],
    script: ['git/push.py'],
    notes: 'Validate, commit, push, open or update the MR/PR, arm auto-merge. Every commit it would send is checked first: a credential (`SECRET_IN_HISTORY`) or a file over `CRE_MAX_FILE_MB` (`LARGE_FILE_IN_HISTORY`) committed with plain git is refused. Exit 0 done | 10 refused | 20 validation failed | 30 conflict | 40 incomplete (re-run resumes). Another task running in the clone is `LOCKED` (10): wait for it, never remove the lock. Ask the user first.',
  },
  'watch': {
    usage: 'npx projen watch [<n> | --branch <b> | --sha <sha>] [--until checks|merged|deployed] [--timeout <s>] [--interval <s>] [--start-timeout <s>]',
    switches: [
      ['--branch', 'follow the latest MR/PR from this branch (e.g. the weekly upgrade branch)'],
      ['--sha', 'follow only the deploy of this commit on main'],
      ['--until', 'stop after this stage: checks, merged or deployed (default)'],
      ['--timeout', 'give up after this many seconds (default 3600)'],
      ['--interval', 'seconds between polls (default 15)'],
      ['--start-timeout', 'how long to wait for a pipeline/run to appear (default 300)'],
    ],
    script: ['git/watch.py'],
    notes: 'Read-only. `<n>` is MR !n / PR #n; none of them: the current branch\'s MR/PR. Exit 0 reached --until | 10 nothing to watch | 11 credentials | 20 closed unmerged | 30 a job failed | 40 timed out, never started, or the MR/PR couldn\'t be read (re-run).',
  },
  'ci:rerun': {
    usage: 'npx projen ci:rerun [--sha <sha>] [--all]',
    switches: [
      ['--sha', 'main\'s head, given explicitly; any other commit is refused (exit 10): re-running an older deploy would put its templates back'],
      ['--all', 'run the whole deploy again (a redeploy without a commit), not only the failed jobs'],
    ],
    script: ['git/ci.py'],
    notes: 'Then `npx projen watch --sha <sha>`. Exit 0 started | 10 nothing to re-run | 11 credentials | 20 the provider refused, or the deploy couldn\'t be read (re-run). Ask the user first.',
  },
  'deploy': {
    usage: 'npx projen deploy [--stack <id>]... [--no-build] [--profile <p>]',
    switches: [
      ['--stack', 'deploy only this stack (repeatable); the outputs of the stacks it needs are read from AWS'],
      ['--no-build', 'skip the build that runs first'],
      ['--profile', 'the AWS profile (default: the ambient credentials)'],
    ],
    script: ['build/deploy-local.py'],
    notes: 'The pipeline on your machine; `deploy:prune` runs only for a full deploy (not with `--stack`). Variables come from the environment, `.cre/deploy.env`, then a prompt. Exit 0 deployed | 1 failed or bad arguments (no RESULT line). Ask the user first.',
  },
  'stacks': {
    usage: 'npx projen stacks [--profile <p>]',
    switches: [['--profile', 'the AWS profile']],
    script: ['build/stacks.py', 'status'],
    notes: 'Each planned stack\'s deployed status, plus orphans: this project\'s stacks that main\'s committed plan no longer has. A stack this branch adds or removes is marked "not yet on main".',
  },
  'stacks:outputs': {
    usage: 'npx projen stacks:outputs [--stack <id>] [--profile <p>]',
    switches: [['--stack', 'only this stack'], ['--profile', 'the AWS profile']],
    script: ['build/stacks.py', 'outputs'],
    notes: 'Prints `<stack> <Key>=<Value>` per output (EdgeUrl, EdgeLocustUrl, ids).',
  },
  'teardown': {
    usage: 'npx projen teardown [--orphans] [--stack <id>...] [--profile <p>] [--dry-run] [--allow-production[=<account>]]',
    switches: [
      ['--orphans', 'only the orphans (from main\'s committed plan, freshly fetched; refused while main\'s plan can\'t be read, this branch\'s plan differs, or main\'s stacks aren\'t all deployed: merge and deploy first), newest first'],
      ['--stack', 'only this stack (repeatable); refused while a deployed stack still takes its outputs. With --orphans: only this orphan'],
      ['--profile', 'the AWS profile'],
      ['--dry-run', 'print what it would delete, in order, and delete nothing; where a real run would refuse, the list, then why (exit 10)'],
      ['--allow-production', 'delete in a production account (Isengard IsProduction, or one it can\'t classify); `=<account>` for only that one. Refused (10) without it; a dry run needs none. Only after the user agrees'],
    ],
    script: ['build/stacks.py', 'teardown'],
    notes: 'Reverse deploy order. Buckets, setup roles and retained resources stay; a stack in another account is skipped. Running it deletes (it doesn\'t ask): show the user `--dry-run` first, and run it only once they agree.',
  },
  'setup:aws': {
    usage: 'npx projen setup:aws [--profile <p>] [--dns-profile <p>] [--allow-production <account>]...',
    switches: [
      ['--profile', 'the deploy account\'s AWS profile'],
      ['--dns-profile', 'the profile of the separate account the project\'s hosted zones are in: that account gets the stack CreSetupDns-<project> (the roles the DNS stack deploys and validates certificates with). Leave it out when the zone is in the deploy account'],
      ['--allow-production', 'set up an account Isengard calls production, or can\'t classify (repeat for the DNS account). Refused (10) without it, before anything changes. Only after the user agrees'],
      ['--project', 'set by the task (.projenrc.ts `name`); a second value is refused'],
      ['--provider', 'set by the task (the project\'s provider); a second value is refused'],
      ['--partition', 'set by the task (.projenrc.ts `partition`, default aws); a second value is refused'],
      ['--namespace', '(GitLab) set by the task (the group in `repository`); a second value is refused'],
      ['--github-repo', '(GitHub) set by the task (`<owner>/<repo>` from `repository`): the runner role trusts its OIDC subject, read from GitHub (gh\'s login); a second value is refused'],
      ['--github-subject', '(GitHub) the OIDC subject prefix itself, instead of --github-repo (template setup); not with this task'],
      ['--github-also-trust-main', '(GitHub) with --github-environment, main\'s subject too: only while setup:github-public switches the repo (it narrows the trust again)'],
      ['--github-environment', '(GitHub) set by the task for a public repo (`AWS`): the runner role trusts that environment\'s jobs instead of main\'s; a second value is refused'],
    ],
    script: ['cresetup/account.py'],
    notes: 'Deploys CreSetup-<project> (the runner role GitLabRunner-<project> or GitHubRunner-<project>, and the shared CreCfnExecution) in the partition\'s control-plane region with a change set; an unchanged stack is a no-op, so a re-run takes seconds. Prints ACCOUNT_ID, RUNNER_ROLE_ARN, CFN_EXECUTION_ROLE_ARN (and DNS_ACCOUNT_ID, DNS_ROLE_ARN, DNS_CFN_EXECUTION_ROLE_ARN): `setup:repo` puts them in the CI variables. No buckets: the deploy makes each region\'s. Exit 0 | 10 refused, nothing changed | 11 credentials or Midway | 20 a call failed (a re-run continues). Ask the user first.',
  },
  'setup:repo': {
    usage: 'npx projen setup:repo [--deploy-var KEY=VALUE]... [--var KEY=VALUE]... (GitLab) | [--var KEY=VALUE]... [--secret KEY=VALUE]... (GitHub)',
    usageByProvider: {
      gitlab: 'npx projen setup:repo [--deploy-var KEY=VALUE]... [--var KEY=VALUE]...',
      github: 'npx projen setup:repo [--var KEY=VALUE]... [--secret KEY=VALUE]...',
    },
    switches: [
      // One entry per provider, so each project's cre-pipelines-detail.md explains only its own form (review 10d F6).
      ['--deploy-var', '(GitLab) a variable only deploy jobs on main read: protected, scoped to the `AWS` environment. Every stack variable (a role ARN, `DOMAIN`, an API key) is one. `KEY=@<file>` reads the value from a file'],
      ['--var', '(GitLab) a variable every job reads (not protected). No stack variable needs it: use `--deploy-var`. `KEY=@<file>` reads the value from a file'],
      ['--var', '(GitHub) an Actions variable (shown in logs), in the `AWS` environment for a public repo. `KEY=@<file>` reads the value from a file'],
      ['--secret', '(GitHub) an Actions secret (role ARNs, anything hidden), in the `AWS` environment for a public repo. `KEY=@<file>` as for --var'],
      ['--project', '(GitLab) set by the task (the project in `repository`); a second value is refused'],
      ['--namespace', '(GitLab) set by the task (the group in `repository`); a second value is refused'],
      ['--token-file', '(GitLab) set by the task (the project\'s `.cre/gitlab-token` link to your token file); a second value is refused'],
      ['--no-create', '(GitLab) set by the task: a missing project is refused (10), never created again'],
      ['--bindle', '(GitLab) the owning bindle of a project setup creates: refused with --no-create, so never by this task'],
      ['--repo', '(GitHub) set by the task (`<owner>/<repo>` from `repository`); a second value is refused'],
      ['--environment', '(GitHub) set by the task for a public repo (`AWS`); a second value is refused'],
    ],
    notes: 'Idempotent: re-applies the repo\'s settings (main\'s protection, merge settings; GitLab the weekly upgrade token and schedule; GitHub the App and, when public, the `AWS` environment) and writes only the variables that differ. A missing repo is refused, never created. A variable that feeds a `noEcho` stack parameter is stored hidden (masked on GitLab, a secret on GitHub) whichever switch set it, read from `cre-stacks.json` and `cdk.out/` (refused, 10, until `npx projen` has made them). GitLab refuses to mask some values (10, with its message). Exit 0 | 10 refused | 11 credentials or Midway | 20 a call failed (a re-run continues). Ask the user first.',
  },
  'setup:github-app': {
    usage: 'npx projen setup:github-app [--alias <a>] [--key-dir <dir>] [--wait <s>]',
    switches: [
      ['--alias', 'your Amazon alias (default `whoami`); the App is aws-cre-automation-<alias>'],
      ['--key-dir', 'where its key lives (default ~/.cre)'],
      ['--wait', 'seconds to wait for the "Create GitHub App" click (default 600; 0 opens nothing and doesn\'t wait)'],
    ],
    script: ['cresetup/github_app.py'],
    notes: 'One App per builder, for all their repos: it exists and is yours, its key is one of its keys, and it has Contents, Pull requests and Workflows write; otherwise GitHub\'s manifest flow makes it (one browser click). Exit 0 ready | 10 refused (another owner\'s App by that name, no or wrong key, missing permissions, or not created yet: the page\'s address is printed) | 11 gh isn\'t signed in | 20 a call failed.',
  },
  'setup:github-public': {
    usage: 'npx projen setup:github-public --profile <p> [--dns-profile <p>] [--allow-production <account>]... [--dry-run]',
    switches: [
      ['--profile', 'the deploy account\'s AWS profile (the runner role\'s trust moves to the `AWS` environment)'],
      ['--dns-profile', 'the hosted zone account\'s profile, when the project has one: its stack\'s trust moves too'],
      ['--allow-production', 'an account Isengard calls production, or can\'t classify, as for setup:aws. Only after the user agrees'],
      ['--dry-run', 'scan the history and say what becomes readable; change nothing'],
      ['--dir', 'the project\'s clone (default: here)'],
      ['--repo', 'set by the task (`<owner>/<repo>` from `repository`); a second value is refused'],
      ['--project', 'set by the task (.projenrc.ts `name`); a second value is refused'],
      ['--app-slug', 'set by the task (`github.appSlug`): the App approves and merges the switch\'s PR; a second value is refused'],
      ['--partition', 'set by the task (.projenrc.ts `partition`, default aws); a second value is refused'],
    ],
    script: ['cresetup/github_public.py'],
    notes: 'From main with nothing uncommitted. Makes the repo public: running it is the decision, so it doesn\'t ask. Only when the user asks (show them `--dry-run` first). Refused (10) when the history holds something shaped like a credential. The trust takes main\'s subject as well as the environment\'s until the switch has merged and main\'s earlier deploys have finished, so main keeps deploying throughout. Repo-level variables and secrets it didn\'t set are named: one the workflows read is moved with setup:repo (give the value again), one nothing reads is a leftover to delete. Re-running carries on where it stopped (.git/cre-github-public.json). Exit 0 | 10 refused | 11 credentials | 20 a call failed.',
  },
  'cfs:curl': {
    usage: 'npx projen cfs:curl [--json] [-i] <GET|POST|PUT|PATCH|DELETE|HEAD> <url-or-/path> [body]',
    switches: [
      ['--json', 'send the body as application/json (default: a form)'],
      ['--include', 'print the response headers first (also -i)'],
    ],
    script: ['cfs/cfs-curl.py'],
    notes: 'A `/path` needs CFS_URL. GET, HEAD and DELETE take no body. Exit 0 2xx/3xx | 1 4xx/5xx | 2 bad arguments | 11 no Midway cookie, or the CloudFront Signer handshake failed (a CloudFront 403 page is 1, naming both causes) | 20 the request failed. Through npx projen only 0 or 1 reaches the caller and there is no RESULT line (stdout is the response body): the stderr message says which. A POST changes the demo\'s state: only when the user asks.',
  },
  'cfs:onboard': {
    usage: 'npx projen cfs:onboard [--domain <dxxxx.cloudfront.net>] [--profile <p>] [--status | --offboard]',
    switches: [
      ['--domain', 'the distribution (default: the edge stack\'s EdgeDomainName)'],
      ['--profile', 'the profile to read the edge stack with'],
      ['--status', 'only say whether it is onboarded (ONBOARDED=true|false)'],
      ['--offboard', 'undo it (the edge then answers 403)'],
    ],
    script: ['cfs/cfs-onboard.py'],
    notes: 'After the first edge deploy, and again after the edge stack is renamed or recreated (a new distribution and domain). Exit 0 done | 10 bad input or no edge domain | 11 Midway expired | 20 the portal failed. Ask the user first.',
  },
  'update-template': {
    usage: 'npx projen update-template [--from <path>] [--allow-branch]',
    switches: [
      ['--from', 'the template clone (recorded for next time). Without it: $CRE_TEMPLATE_DIR, the clone `.cre/template.json` records (older projects: projenrc/cre/SOURCE.json), then the sibling directory'],
      ['--allow-branch', 'use a clone that is on another branch, as it is (testing a template change)'],
    ],
    script: ['template/update-template.py'],
    notes: 'Exit 0 updated or current | 10 refused, nothing changed | 20 synth/build failed after copying (the restore command is printed). Its last stdout line is `RESULT code=<n> step=update-template`. Ask the user first.',
  },
};

/** Tasks projen adds that CRE projects never run (they bypass the pipeline or projen itself). */
export const NEVER_RUN = new Set(['eject', 'release', 'bump', 'unbump', 'bump:releasable-commits']);
/** Other marks: what the task is for, when it isn't an ordinary command. */
const MARKS: Readonly<Record<string, string>> = {
  'upgrade': ' Only when the user asks: CI opens a weekly upgrade MR/PR (and skips while a person\'s commit is on its branch).',
  'test:watch': ' Interactive (watch mode): not for an agent session.',
  'install': ' Changes the lockfile; CI runs `yarn install --immutable` instead.',
};
/** CI job steps: run by `deploy` / the pipeline in order, never on their own. */
// build:container-plan and build:docker are steps of build and the CI build jobs (review 10c A18).
const CI_STEP = /^(deploy:(upload|s3|stack|prune|docker)|ci:build:.*|build:(container-plan|docker)|pre-compile|post-compile|post-upgrade|bundle|package:.*|synth:silent)$/;

/**
 * Tasks used once or twice in a project's life (account, DNS and repo setup): their entries are in
 * the on-demand `cre-pipelines-detail.md`, not the always-on `cre-tasks.md`, which names them with a
 * pointer (review 10f decision 5: about 1.1k tokens in every session otherwise).
 */
export const SETUP_TASKS: readonly string[] = ['setup:aws', 'setup:repo'];

/** One task's entry: heading, usage, description, this provider's switches, notes. */
function entryLines(t: { name: string; description?: string }, provider: 'gitlab' | 'github'): string[] {
  const r = TASK_REFERENCE[t.name];
  const lines = [`### \`${t.name}\``, '', `\`${r.usageByProvider?.[provider] ?? r.usage}\``, ''];
  if (t.description) lines.push(t.description, '');
  // A switch of the other provider's is noise here (review 10c F8).
  const other = provider === 'gitlab' ? '(GitHub)' : '(GitLab)';
  const switches = r.switches.filter(([, text]) => !text.startsWith(other));
  for (const [flag, text] of switches) lines.push(`- \`${flag}\`: ${text}`);
  if (switches.length) lines.push('');
  if (r.notes) lines.push(r.notes, '');
  return lines;
}

/**
 * `.kiro/steering/cre-pipelines-detail.md`: the steering file's text, then the SETUP_TASKS
 * entries this project has, written at synth from `project.tasks` like cre-tasks.md.
 */
export class PipelinesDetailFile extends FileBase {
  private readonly proj: Project;
  private readonly provider: 'gitlab' | 'github';
  private readonly body: string;

  constructor(project: Project, provider: 'gitlab' | 'github', body: string) {
    super(project, '.kiro/steering/cre-pipelines-detail.md', { readonly: true, marker: false });
    this.proj = project;
    this.provider = provider;
    this.body = body;
  }

  protected synthesizeContent(_resolver: IResolver): string | undefined {
    const setup = SETUP_TASKS.map((n) => this.proj.tasks.tryFind(n)).filter((t): t is NonNullable<typeof t> => !!t);
    const lines = [this.body.replace(/\n+$/, ''), ''];
    if (setup.length) {
      lines.push('## Setup tasks and their switches', '',
        'Generated by `npx projen` from the project\'s tasks (the rest are in `cre-tasks.md`). They change the AWS',
        'account, the DNS zone or the repository: ask the user first.', '');
      for (const t of setup) lines.push(...entryLines(t, this.provider));
    }
    return lines.join('\n').replace(/\n+$/, '') + '\n';
  }
}

/**
 * `.kiro/steering/cre-tasks.md`: every task this project has, with the reference above. Written at
 * synth from `project.tasks`, so it always lists exactly the tasks there are. SETUP_TASKS are
 * named with a pointer to cre-pipelines-detail.md.
 */
export class TaskReferenceFile extends FileBase {
  private readonly proj: Project;
  private readonly provider: 'gitlab' | 'github';

  constructor(project: Project, provider: 'gitlab' | 'github') {
    super(project, '.kiro/steering/cre-tasks.md', { readonly: true, marker: false });
    this.proj = project;
    this.provider = provider;
  }

  protected synthesizeContent(_resolver: IResolver): string | undefined {
    const tasks = [...this.proj.tasks.all].sort((a, b) => a.name.localeCompare(b.name));
    const own = tasks.filter((t) => TASK_REFERENCE[t.name] && !SETUP_TASKS.includes(t.name));
    const setup = tasks.filter((t) => SETUP_TASKS.includes(t.name));
    const rest = tasks.filter((t) => !TASK_REFERENCE[t.name]);
    const lines = [
      '---',
      'inclusion: always',
      '---',
      '# Every task in this project, and its switches',
      '',
      'Generated by `npx projen` from the project\'s tasks: it lists exactly what `npx projen <task>` runs',
      'here. When to use which, what to ask first and the exit codes: `cre-tooling.md`. Each task that',
      'takes arguments also prints its usage with `--help`.',
      '',
      '## Tasks that take arguments',
      '',
    ];
    for (const t of own) lines.push(...entryLines(t, this.provider));
    if (setup.length) {
      lines.push(`**Setup** (${setup.map((t) => `\`${t.name}\``).join(', ')}): account, DNS and repository setup, used once or `
        + 'twice in a project\'s life. They change the AWS account or the repo, so ask the user first. Their usage and switches are '
        + 'in `cre-pipelines-detail.md` ("Setup tasks and their switches").', '');
    }
    // Tasks never run, and CI steps, are one line each: as table rows they were 17 rows saying only
    // "not on its own" in an always-on file (review 10e F4).
    const never = rest.filter((t) => NEVER_RUN.has(t.name));
    const steps = rest.filter((t) => !NEVER_RUN.has(t.name) && !MARKS[t.name] && CI_STEP.test(t.name));
    const table = rest.filter((t) => !never.includes(t) && !steps.includes(t));
    lines.push('## Other tasks', '', '| Task | What it does |', '|---|---|');
    for (const t of table) {
      const mark = MARKS[t.name] ?? '';
      // projen's own descriptions end without a period, which glued the mark on as one sentence
      // ("... lockfile (non-frozen) Changes the lockfile", review 10d F6).
      let desc = (t.description ?? '').trim();
      if (mark && desc && !/[.!?:]$/.test(desc)) desc += '.';
      lines.push(`| \`${t.name}\` | ${desc.replace(/\|/g, '\\|')}${mark} |`);
    }
    const names = (ts: typeof rest) => ts.map((t) => `\`${t.name}\``).join(', ');
    lines.push('');
    if (steps.length) lines.push(`**Steps of \`build\` / \`deploy\` / CI, run by them in order, never on their own:** ${names(steps)}.`, '');
    if (never.length) lines.push(`**Never run** (they bypass the pipeline or projen itself): ${names(never)}.`, '');
    return lines.join('\n').replace(/\n+$/, '') + '\n';
  }
}
