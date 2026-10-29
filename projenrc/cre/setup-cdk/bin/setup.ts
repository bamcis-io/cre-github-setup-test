#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { DnsStack } from '../lib/dns-stack';
import { IamStack } from '../lib/iam-stack';
import * as names from '../lib/names';

/**
 * The AWS resources a CRE project needs before its pipeline can deploy, synthesized from context:
 *   npx cdk synth -c project=<name> -c provider=gitlab|github -c home=<control-plane region> \
 *     [-c gitlabNamespace=<group>] [-c githubSubject=repo:<owner>/<repo> [-c githubEnvironment=AWS]] [-c dns=true]
 * The IAM stack (in the app account, in the control-plane region) and, with dns=true, the DNS stack
 * (in the separate DNS account). Setup deploys them itself with change sets: nothing is
 * bootstrapped. The assets buckets aren't here: the deploy makes each region's the first time a
 * stack goes there (scripts/build/assets.py).
 */
const app = new cdk.App({ analyticsReporting: false });
const ctx = (key: string) => app.node.tryGetContext(key) as string | undefined;
const project = names.checkProject(ctx('project') ?? '');
const provider = ctx('provider');
if (provider !== 'gitlab' && provider !== 'github') throw new Error("context 'provider' must be gitlab or github");
const home = ctx('home') ?? 'us-east-1';
// The project's hosted zones are in a separate DNS account: a second stack, deployed there.
const dns = ctx('dns') === 'true';

// Templates are deployed directly by setup, never as CDK assets: an asset added by mistake fails at synth.
const synthesizer = () => new cdk.BootstraplessSynthesizer();

new IamStack(app, 'Iam', {
  stackName: names.iamStack(project),
  synthesizer: synthesizer(),
  project,
  provider,
  gitlabNamespace: ctx('gitlabNamespace'),
  githubSubject: ctx('githubSubject'),
  githubEnvironment: ctx('githubEnvironment'),
});
if (dns) {
  new DnsStack(app, 'Dns', {
    stackName: names.dnsStack(project),
    synthesizer: synthesizer(),
    project,
    provider,
    home,
    gitlabNamespace: ctx('gitlabNamespace'),
    githubSubject: ctx('githubSubject'),
    githubEnvironment: ctx('githubEnvironment'),
  });
}
