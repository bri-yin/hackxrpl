# Orchestrator session prompt

Paste this into a terminal-capable agent running in your control clone. Replace the bracketed values before dispatching work.

```text
You coordinate bri-yin/hackxrpl toward this goal: [shared goal].
Read AGENTS.md and README.md. The available workers are [person/tool/session names].
Concurrency limit: [start with 2 total, one per person].
Do not invent an application stack or product requirements that we have not agreed.
If the goal is not defined, help us write a short brief before creating implementation tasks.

Use GitHub issues as the durable backlog. Make each task independently verifiable,
with a concrete goal, explicit Allowed paths, Depends on issue numbers, and acceptance criteria.
Assign shared manifests, lockfiles, schemas, and architectural changes to serial tasks.
Label an issue agent:ready only once it is sufficiently defined. Use agent:blocked for blockers.
Use gh issue create --body-file for multiline bodies. Do not put credentials in issues.

Check scripts/agents.py status before dispatch. Use start for a chosen issue or dispatch
for the next eligible issue. Every worker needs an actual reservation, even if assigned
on GitHub. Supply a distinct person/tool/session worker name and workspace.
Use --here only for an already isolated cloud checkout. Never run parallel workers there.

Print each assignment prompt for its operator to launch in their chosen tool. Do not
claim to have started a friend's session or spent tokens without actually doing so.
If we explicitly authorize local CLI workers, use documented tool commands with the
assigned working directory and limit. Do not assume a tool's flags or headless support.

Ask workers for concise handoffs: issue, PR, result, checks, blocker, next action.
Do not read every file or duplicate all worker reasoning. Review relevant diffs and test
evidence. Escalate cross-cutting decisions to us. Avoid starting duplicate attempts.

When a PR is ready, use scripts/agents.py merge ISSUE --pr PR. It requires successful
CI, the reserved branch and base, and normal GitHub merge permissions/protection.
If checks fail, return a specific failure to the owning worker. Do not bypass checks.
After each merge, update dependent issues and dispatch the next task. Keep the goal
and decisions in GitHub issues/PRs so either person can take over orchestration.
Stop when the goal is done, the budget is exhausted, or an external blocker needs us.
```

## Efficient task sizing

Aim for tasks that one worker can finish with a small diff and a clear test. Use issue descriptions for acceptance criteria and decisions; use the Git reservation for exclusive ownership. Limit simultaneous workers to independent scopes rather than available subscriptions. More concurrent agents can cost more tokens if they repeat context or compete over shared interfaces.

Each tool owns model selection and budget controls. Tell the orchestrator which worker profiles are available, for example `alice-codex-1: difficult implementation`, `bob-claude-1: UI implementation`, and `alice-t3-1: docs/triage`. These are operator-defined roles, not fixed model capability claims. Set per-session limits in the tool if supported. There is no cross-provider budget accounting in this repository.

## Worker handoff

```text
Issue: #12
Workspace / branch: ...
PR: #24
Result: one or two sentences
Checks: exact commands and results
Limitations / decisions: ...
Next action: merge, fix CI, or unblock dependency
```

## No unattended control plane yet

`dispatch` is a one-shot reservation and prompt generator. The orchestrator session decides when to call it. A future background scheduler can build on the same reservation protocol, but would need tool-specific launch adapters, durable process monitoring, budget telemetry, and explicit credentials/authorization for each person's machine. Nothing here shares provider accounts or launches remote sessions automatically.
