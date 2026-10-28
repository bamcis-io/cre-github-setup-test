import type { awscdk } from 'projen';
import type { GitProvider } from './git-tasks';
import { CRE_SCRIPTS } from '../paths';

/**
 * Tasks every project type gets besides build/deploy/git:
 *
 *   cfs:curl [--json] <METHOD> <url|/path> [body]   call anything behind CloudFront Signer (Midway)
 *   cfs:onboard [--domain d] [--status]     onboard the edge distribution at CloudFrontSigner
 *   setup:aws / setup:repo / setup:dns      re-run or repair the account / repo / DNS setup that
 *                                           /cre-demo-init ran (same scripts, same flags)
 */
export function createToolingTasks(project: awscdk.AwsCdkTypeScriptApp, provider: GitProvider): void {
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
    description: 'Assets buckets, CloudFormation execution role and CI runner role in the target account (idempotent).',
    steps: python('setup/aws_account.py'),
  });
  project.addTask('setup:repo', {
    description: `Create/adopt the ${provider === 'gitlab' ? 'GitLab project' : 'GitHub repo'} and set CI variables (prepare | finalize).`,
    steps: python(`setup/repo_${provider}.py`),
  });
  if (provider === 'github') {
    project.addTask('setup:github-public', {
      description: 'A private repo made public, end to end (from a clean main): scan the history for credentials, show what becomes readable (--dry-run stops there), then make it public, protect main, regenerate the workflows for GitHub auto-merge, push them as a PR and wait for it to merge.',
      steps: [{ exec: `python3 ${CRE_SCRIPTS}/setup/repo_github.py publish "$@"`, receiveArgs: true }],
    });
    project.addTask('setup:github-app', {
      description: 'Create (once) or find your CRE automation GitHub App, aws-cre-automation-<alias>, and its key in ~/.cre.',
      steps: python('setup/github_app.py'),
    });
  }
  project.addTask('setup:dns', {
    description: 'External DNS: check the zone, or create the cross-account DNS roles / SuperNova prerequisites.',
    steps: python('setup/dns.py'),
  });
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
