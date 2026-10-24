# Reservation protocol

All workers must use the same origin remote and `coordination/*` namespace. Local state lives under the Git common directory at `agent-coordination/issue-N.json`; it is shared by worktrees in one clone and never committed. GitHub issue text defines the scope when claimed. The reservation stores that scope immutably until release.

## State transitions

1. Fetch the configured base branch and verify it exists. Read the open issue and require every declared dependency to be closed.
2. Acquire `refs/heads/coordination/scheduler` with a push whose expected previous value is empty. Only one cooperating caller can succeed.
3. Read current issue reservations. Reject the same issue, repository-wide scopes, or overlapping file/directory prefixes. Directory boundaries matter: `app` conflicts with `app/x` but not `apple`.
4. Create a unique metadata commit and atomically create `refs/heads/coordination/issue-N`. Save its exact OID locally. Release the scheduler with an expected-OID deletion.
5. Create the worker branch from the fetched base and either a new worktree or switch the existing isolated checkout. If workspace creation fails, attempt reservation release. Never delete user files as rollback.
6. Validate the task's changed paths and configured commands. Record the validated commit and whether the checkout is clean. PR creation requires the same clean commit and pushes normally.
7. Merge under the scheduler reservation after validating the matching PR, CI checks, current PR head, and that the current base is an ancestor of the head. GitHub's `--match-head-commit` check prevents merging a replacement head. GitHub protection remains authoritative, including a strict up-to-date rule to cover base updates outside this coordinator.
8. After the matching PR is confirmed merged, remove the issue reservation using its exact OID. Worktrees and code branches are retained. `finish` supports merges performed outside the CLI.

Reservation commits are independent metadata trees containing `claim.json`, not application commits. They have no reason to be merged into `main`. Coordination branches may be visible in GitHub branch lists. Exclude them from stale-branch deletion automation. The scheduler serializes brief coordination/merge commands, not the hours of worker implementation.

## Failure behavior

- The protocol uses `git push --force-with-lease=REF:EXPECTED`, including an empty expected value for creation. It never uses unconditional force. A stale owner cannot delete a replacement claim.
- A crash can leave a scheduler or issue reservation behind. Reservations have no automatic timeout: a slow worker is not proof it died. Explicit recovery uses an exact OID after the operator confirms shutdown.
- A transport failure can have an uncertain outcome. Inspect `status` and remote refs before retrying or recovering. A failure while writing local state can leave a valid remote reservation requiring recovery.
- Claims remain reserved when CI, PR creation, or merge fails. Resume the original worker instead of assigning duplicate work.
- `status` is observational and can race a simultaneous release. Retry it after the other command completes if a metadata fetch reports a missing ref.
- An orchestrator can finish a merged task from another clone. The original clone's local state then becomes stale; it does not grant permission to continue the old task. Archive that one local state file before reusing the issue in that clone.
- A newly merged `main` can invalidate another worker's assumptions. Update the task branch using your normal merge/rebase policy, resolve only its changes, rerun `check`, and let GitHub enforce up-to-date checks. Do not rebase a branch while its worker is actively editing it.

Scope checks include committed changes since the merge base, staged/unstaged changes, and untracked nonignored files. Renames are treated as removal plus addition. They do not inspect ignored files or submodule internals, sandbox a malicious agent, or prove semantic independence. This repository currently has no submodules; add explicit handling before assigning submodule work.

## Tests

`python3 -m unittest discover -s tests -v` uses temporary bare Git remotes and independent clones. It exercises concurrent claims, overlapping/disjoint scopes, stale leases, ownership, crash recovery, dependencies, worktree isolation, path validation, stale validation, and merge gating. GitHub CLI calls in tests are mocked; real Git pushes and worktree operations are not. Live GitHub issue/PR/merge behavior requires authenticated GitHub API access and a published baseline.

The CI check runs on pushes to `main` and pull requests targeting `main`. For PRs it reads reservation metadata through the GitHub contents API, verifies the issue/branch/base identity, and checks changed paths including both sides of renames. Both collaborators must use branches in this shared repository, not forks. Its token has read-only contents permission and it does not execute commands from issue descriptions. Add application-specific build and test jobs before treating green CI as application readiness.
