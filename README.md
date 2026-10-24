# Shared agents, one project

A small, model-independent coordination layer for two people using **T3 Code, Claude Code, and Codex**. It uses GitHub issues for the backlog, Git branches for shared reservations, and one isolated workspace per worker. No model API keys or additional orchestration subscription are required.

The application stack is undecided. This repository currently contains the coordination tooling and its integration tests.

## First-time setup

Requirements on **both** people's machines: Git, Python 3.10+, GitHub CLI (`gh`), and collaborator write access to this repository. Sign in to `gh` on your own machine. Keep each person's credentials separate. In Codex cloud, reuse injected credentials and test them before configuring anything new.

If bootstrapping a new, empty copy of this repository, publish the initial baseline once. Skip this step when `main` already contains these files:

```bash
git add .
git commit -m "Bootstrap shared agent coordination"
git push --force-with-lease=refs/heads/main: origin HEAD:refs/heads/main
```

Both people then use their own clone. Never share a clone directory across computers or sync a live `.git` directory through cloud storage.

```bash
git clone https://github.com/bri-yin/hackxrpl.git
cd hackxrpl
python3 scripts/agents.py doctor
python3 -m unittest discover -s tests -v
bash scripts/bootstrap-labels.sh
```

Run the label script once per repository; running it again is safe. The issue form uses `agent:ready` for tasks that can be dispatched. Only mark an issue ready after its scope, dependencies, and acceptance criteria are clear.

In GitHub repository settings:

- Enable Issues, GitHub Actions, and squash merging.
- Protect `main`: require a PR, the **`coordination`** status check, and an up-to-date branch. Do not allow force pushes or direct worker pushes to `main`.
- To allow orchestrator merges, do not require a human approval unless you want that extra gate. Existing protection rules always take precedence.
- Leave `coordination/*` available for collaborator Git pushes. These branches contain reservation metadata and must never be merged into `main` or deleted by generic branch cleanup tools.

The `coordination` check becomes selectable after its first workflow run. Update `coordination.json` and the CI workflow with real application checks when you choose your stack. Passing coordination tests alone does not validate an application.

## Assign a task yourself

Create an issue using **Agent task**. For example:

```markdown
### Goal
Add the home page.

### Allowed paths
web/src/pages/home/
web/tests/home/

### Depends on
none

### Acceptance criteria
The page renders the approved content and has an accessibility test.
```

Use explicit files or directory prefixes, one per line. `.` reserves the entire repository. Globs are intentionally unsupported. Shared manifests, lockfiles, schemas, generated files, and configuration must be included if the task needs to change them. Overlapping scopes cannot run at the same time.

From your control clone, reserve an issue and create a worktree:

```bash
python3 scripts/agents.py start 12 --worker alice-codex-1 --worktree ../hackxrpl-worktrees/issue-12
```

Your friend can reserve another issue from their clone:

```bash
python3 scripts/agents.py start 13 --worker bob-claude-1 --worktree ../hackxrpl-worktrees/issue-13
```

Open **only the assigned worktree** in the chosen tool. Paste the generated assignment prompt into its agent session. For a terminal-based session, first `cd` to that directory. If T3 Code or another client offers automatic worktree creation, use the existing assigned worktree and avoid creating a second one. This workflow does not depend on tool-specific worktree or agent APIs.

Codex cloud tasks already have isolated checkouts. Use `--here` there instead of adding another worktree:

```bash
python3 scripts/agents.py start 12 --worker alice-codex-cloud-1 --here
```

After implementation, **inside the assigned workspace**:

```bash
python3 scripts/agents.py check 12
git add <the-files-you-changed>
git commit -m "Implement issue 12"
python3 scripts/agents.py check 12
python3 scripts/agents.py pr 12 --ready --summary 'Describe the result and any limitations.'
```

`check` verifies the reserved paths and runs configured checks. `pr` requires a successful check of the current clean commit. Omit `--ready` to open a draft PR. Keep the reservation until merge; a PR still in review owns its scope.

## Let an orchestrator assign work

Use one existing Codex, Claude Code, or T3 Code session as the orchestrator. Give it [docs/orchestrator.md](docs/orchestrator.md), the shared goal, and the available worker names. It can create issues with `gh issue create`, inspect the backlog, and assign the next nonconflicting task:

```bash
python3 scripts/agents.py issues
python3 scripts/agents.py status
python3 scripts/agents.py dispatch --worker alice-codex-1 --worktree ../hackxrpl-worktrees
```

Dispatch reserves one eligible issue and prints a worker prompt. **It does not launch a model session, spend API tokens, or control your friend's machine.** Each person starts a session in the assigned workspace. A terminal-capable orchestrator can invoke installed worker CLIs on its own machine using those tools' documented commands and your approved concurrency limit. Cross-machine dispatch uses the same shared Git reservations; no local daemon is required.

When GitHub CI passes, an orchestrator can merge without a human approval:

```bash
python3 scripts/agents.py merge 12 --pr 24
```

This checks the PR's reserved branch and base, required CI, draft/review state, exact head commit, and that the branch includes the current base. It uses a normal squash merge and respects GitHub protection. It then releases the issue reservation. It never uses `--admin`. An empty, pending, failed, or skipped required check cannot pass this gate.

For task PRs, CI also verifies the active reservation and every changed path against its saved scope. It rejects old reassigned branches and PRs from a different repository. Both collaborators should push worker branches to this shared repository, rather than forks.

If someone merges through GitHub instead, clear the reservation with:

```bash
python3 scripts/agents.py finish 12 --pr 24
```

## Coordination guarantees and limits

Reservations live in `coordination/issue-N` remote branches. A short-lived `coordination/scheduler` reservation serializes scope checks and claim writes, so two different issues cannot simultaneously reserve overlapping paths. Git's expected-old-value checks reject stale claims and stale deletions. This works across separate clones and computers, rather than relying on local lock files or racing GitHub comments.

These are cooperative coordination rules, **not a security boundary**. A collaborator can edit files outside the tool, bypass the CLI, or delete reservation refs. Worktrees isolate working files; they do not guarantee independent tasks have no logical conflicts. Scope checks, real application tests, an up-to-date base requirement, and protected merges are all needed. Keep architectural changes and shared dependency updates in serial tasks.

Issue dependencies must be closed before dispatch. Close completed task issues through their merged PRs; manually closing an unfinished dependency can incorrectly unblock a task. The initial implementation considers up to 100 `agent:ready` issues per dispatch, oldest issue number first; there is no token telemetry or autonomous background scheduler.

## Budget and recovery

Start with one worker per person, then increase to two if tasks are independent and review keeps up. Use a cheaper model for triage/docs/simple tasks and a stronger model for architecture, integration, and difficult bugs. Pick models in each tool; this repository does not pool subscriptions, transfer quotas, or bypass provider limits. See [docs/orchestrator.md](docs/orchestrator.md) for a concise coordination prompt and handoff format.

Reservations do not silently expire. For an abandoned task, stop its worker first, preserve or push its work, then run `release N` in the owning clone. This retains every worktree and branch. Do not release active work just to make dispatch proceed.

If the owning machine is unavailable, inspect `status`, confirm the old worker has stopped with its operator, then explicitly recover using the exact current lock OID:

```bash
python3 scripts/agents.py recover --issue 12 --expected <oid-from-status> --reason 'Worker stopped; WIP preserved on its branch'
# Only for a crashed scheduler, not a currently running command:
python3 scripts/agents.py recover --scheduler --expected <scheduler-oid> --reason 'Confirmed coordinator process stopped'
```

Recovery never deletes code. Stop old sessions before reassignment. A recovered worker's old local reservation file remains stale in its clone; archive that issue's file under the Git common directory's `agent-coordination/` folder after confirming recovery, rather than treating the old assignment as valid. Only remove finished worktrees after checking they have no uncommitted or unpushed work.

See [docs/protocol.md](docs/protocol.md) for the state machine, failure handling, and test coverage.
