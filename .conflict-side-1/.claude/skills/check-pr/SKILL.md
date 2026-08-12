---
name: check-pr
description: Check if a PR is mergeable — no merge conflicts, required checks passing
argument-hint: "[pr_number]"
arguments: [pr_number]
user-invocable: true
allowed-tools: Bash(gh *)
---

Check PR #$0 for mergeability. Run these checks and report results:

1. **Merge conflicts**: Run `gh pr view $0 --json mergeable,mergeStateStatus` to check for conflicts
2. **Required checks**: Run `gh pr checks $0` to see if any required status checks are failing or pending
3. **Review status**: Run `gh pr view $0 --json reviewDecision` to check if reviews are approved

If there are merge conflicts, fix them by merging the base branch into the PR branch and resolving conflicts.

If checks are failing, investigate and report what's wrong.

Report a short summary of the PR's merge readiness.
