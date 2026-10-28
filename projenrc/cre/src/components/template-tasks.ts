import type { awscdk } from 'projen';
import { CRE_SCRIPTS } from '../paths';

/**
 * `npx projen update-template` — pull the template clone, refresh this package (projenrc/cre/), the
 * construct library and the skill links from it, then re-synth and build. See scripts/template/update-template.py.
 */
export function createTemplateTasks(project: awscdk.AwsCdkTypeScriptApp): void {
  project.addTask('update-template', {
    description:
      // Which clone it uses lives in one place, the --from entry of the task reference (review 10d G3).
      'Pull the cre-demo-template clone, refresh projenrc/cre, cre-constructs and the skill links from it, then synth + build.',
    steps: [{ exec: `python3 ${CRE_SCRIPTS}/template/update-template.py "$@"`, receiveArgs: true }],
  });
}
