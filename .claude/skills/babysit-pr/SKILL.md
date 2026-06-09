---
name: babysit-pr
description: Monitor a PR until merge — checks build status, resolves review comments, waits for merge, then rotates worktree
argument-hint: "[pr_number]"
arguments: [pr_number]
user-invocable: true
---

Monitor PR #$0. Run these checks and take action based on results.

## Checks

1. **Build/CI status**: Run `gh pr checks $0` to see if CI checks are passing, failing, or pending
2. **Review comments**: Run `gh pr view $0 --json reviewThreads --jq '.reviewThreads[] | select(.isResolved == false)'` to find unresolved review threads
3. **Merge status**: Run `gh pr view $0 --json state,merged,mergeable,mergeStateStatus` to check overall PR state

## Actions

### If checks are failing:
- Investigate the failing check output (use `gh pr checks $0` and `gh run view <run-id> --log-failed` for details)
- Fix the issue in the code, commit, and push
- Report what was fixed

### If there are unresolved review comments:
- Read each unresolved comment thread
- Address the feedback with code changes
- Commit and push fixes
- Report what was addressed

### If the PR is merged:
1. Report "PR #$0 merged successfully"
2. Exit the current worktree with `ExitWorktree` (action: "remove")
3. Enter a new fresh worktree with `EnterWorktree`
4. Report "Ready for next task"
5. **Do NOT schedule another loop iteration** — monitoring is complete

### If checks are passing and waiting for review/merge:
- Report current status briefly (e.g., "Checks passing, awaiting review")
- The loop will continue monitoring on the next iteration
