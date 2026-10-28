import * as fs from 'fs';
import * as path from 'path';
import { TextFile } from 'projen';
import type { Project } from 'projen';
import { PipelinesDetailFile, TaskReferenceFile } from './task-reference';

/**
 * Steering every project type gets. Only the core is `inclusion: always` (the type file, tooling,
 * the generated task reference, project structure, the pipeline essentials); the rest is
 * `inclusion: auto`, loaded when a request matches its description, so a session starts with the
 * few files every change needs (review 10c F17).
 */
export const ALWAYS_STEERING = ['project-structure.md', 'tooling.md', 'pipelines.md'];
export const ON_DEMAND_STEERING = ['pipelines-detail.md', 'constructs.md', 'failure-injection.md', 'load-generation.md', 'fidelity.md'];
export const BASE_STEERING = [...ALWAYS_STEERING, ...ON_DEMAND_STEERING];

/**
 * Emit the package's steering files as projen-managed, read-only files at
 * `.kiro/steering/cre-<name>.md`, so `npx projen update-template` refreshes them and nobody edits
 * them by hand. Demo-specific steering stays as ordinary files next to them.
 *
 * The source markdown is read from `../../steering/` at synth time, i.e. the package's own
 * `steering/` dir (projenrc/cre/steering in a demo).
 */
export function createSteering(project: Project, typeSteering: string, layoutDoc: string, provider: 'gitlab' | 'github'): void {
  const dir = path.join(__dirname, '..', '..', 'steering');
  for (const file of [...BASE_STEERING, typeSteering]) {
    let body = forProvider(fs.readFileSync(path.join(dir, file), 'utf-8'), provider);
    if (file === 'project-structure.md') {
      body = body.replace('{{LAYOUT}}', layoutDoc);
    }
    if (file === 'pipelines-detail.md') {
      // With the setup tasks' entries, from the project's tasks at synth (review 10f decision 5).
      new PipelinesDetailFile(project, provider, body);
      continue;
    }
    new TextFile(project, `.kiro/steering/cre-${file}`, {
      lines: body.replace(/\n$/, '').split('\n'),
      readonly: true,
    });
  }
  // Every task and its switches, from the project's own tasks (written at synth, so never stale).
  new TaskReferenceFile(project, provider);
}

/**
 * A steering line ending in `<!-- github -->` or `<!-- gitlab -->` is about that provider only: kept
 * (without the marker) in its projects, dropped in the other's, so a GitLab project's steering never
 * names a task it doesn't have (review 10c A17).
 */
export function forProvider(markdown: string, provider: 'gitlab' | 'github'): string {
  const marker = / <!-- (github|gitlab) -->$/;
  return markdown.split('\n')
    .filter((line) => { const m = marker.exec(line); return !m || m[1] === provider; })
    .map((line) => line.replace(marker, ''))
    .join('\n');
}
