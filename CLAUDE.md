# CLAUDE.md

Guidance for Claude Code when working in this generated CRE demo. The rules are in `AGENTS.md`,
imported here, so there is one copy (two copies drift, and the drifted one gets believed):

@AGENTS.md

Then read `docs/agents-local.md` when it exists (this demo's own findings; `AGENTS.md` and this
file are refreshed from the template).

## Steering: read these first

Kiro loads `.kiro/steering/*.md` itself; Claude Code does not. They are generated from the CRE
project type, so they are current. Read these at the start of a session (Kiro always loads them):

- `.kiro/steering/cre-<type>.md`: what kind of project this is (resilience demo, failure scenario,
  or customer sandbox) and what that requires
- `.kiro/steering/cre-tooling.md`: the commands and the change loop. Start here
- `.kiro/steering/cre-tasks.md`: every task and every switch it takes (generated from this project's tasks)
- `.kiro/steering/cre-pipelines.md`: how stacks are defined (`CreStack`, `CreCfnParameter`) and
  how the generated pipeline deploys them
- `.kiro/steering/cre-project-structure.md`
- any demo-specific steering next to them

Read these when the task needs them (Kiro loads them by their description):

- `cre-constructs.md`: before writing or changing a stack or construct
- `cre-pipelines-detail.md`: a stack that must be in a given region, a deploy variable or role, a
  failing deploy job
- `cre-load-generation.md`: a locustfile, the load model, running a test
- `cre-failure-injection.md`: building a failure and its arm/disarm controls
- `cre-fidelity.md`: recreating a system, an incident or a design from a source

Git rules are the `cre-git` skill (`.kiro/skills/cre-git/SKILL.md`).
