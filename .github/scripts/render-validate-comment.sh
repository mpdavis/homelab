#!/usr/bin/env bash
# Post (or update) the single sticky Render & Validate comment on a pull request.
#
# One comment, edited in place on every push, rather than a new one per run — the
# rendered diff changes with each commit and appending would bury the PR. Silence is
# the default: a PR that renders clean and changes no output gets no comment at all,
# unless a previous run left one that now needs correcting.
#
# Reads render.txt / schema.txt / diff.txt from the working directory and
# PR_NUMBER / RENDER_EXIT / SCHEMA_EXIT / DIFF_CHANGED from the environment.
set -euo pipefail

MARKER="<!-- render-validate -->"
PR="${PR_NUMBER}"
REPO="${GITHUB_REPOSITORY}"

# GitHub rejects comment bodies over 65536 characters; leave room for the framing.
BODY_LIMIT=60000

render_exit="${RENDER_EXIT:-0}"
schema_exit="${SCHEMA_EXIT:-0}"
diff_changed="${DIFF_CHANGED:-}"

# A skipped step reports an empty exit code. Only the render step is unconditional,
# so an empty schema_exit means rendering already failed and short-circuited it.
[ -z "$render_exit" ] && render_exit=0
[ -z "$schema_exit" ] && schema_exit=0

# Emit a fenced block from a file, truncated to fit the comment limit.
emit_block() {
  local file="$1" lang="${2:-}"
  printf '```%s\n' "$lang"
  if [ ! -f "$file" ]; then
    # The step that produces it never ran — the job died earlier.
    printf '(no output captured; see the workflow log)\n'
    printf '```\n'
    return
  fi
  if [ "$(wc -c <"$file")" -gt "$BODY_LIMIT" ]; then
    head -c "$BODY_LIMIT" "$file"
    printf '\n\n… truncated. See the workflow log for the full output.\n'
  else
    cat "$file"
  fi
  printf '```\n'
}

{
  printf '%s\n' "$MARKER"
  if [ "$render_exit" != '0' ]; then
    printf '## ❌ Manifests do not render\n\n'
    printf 'Flux could not reconcile this tree offline, so it will not reconcile it in the cluster either.\n\n'
    emit_block render.txt
  elif [ "$schema_exit" != '0' ]; then
    printf '## ❌ Rendered objects failed schema validation\n\n'
    printf 'These render, but the API server would reject them.\n\n'
    emit_block schema.txt
  elif [ "$diff_changed" = 'true' ]; then
    printf '## ✅ Renders clean — this is what changes in the cluster\n\n'
    emit_block diff.txt diff
  else
    printf '## ✅ Renders clean — no change to the rendered output\n\n'
    printf 'Every Kustomization and HelmRelease reconciles, and the rendered manifests are '
    printf 'byte-identical to the base commit.\n'
  fi
} > comment.md

existing=$(gh api "repos/${REPO}/issues/${PR}/comments" --paginate \
  --jq "[.[] | select(.body | startswith(\"${MARKER}\")) | .id] | first // empty")

# Nothing to say and nothing to correct — stay quiet.
if [ -z "$existing" ] && [ "$render_exit" = '0' ] && [ "$schema_exit" = '0' ] \
   && [ "$diff_changed" != 'true' ]; then
  echo "Renders clean with no output change and no existing comment; not commenting."
  exit 0
fi

if [ -n "$existing" ]; then
  gh api -X PATCH "repos/${REPO}/issues/comments/${existing}" -F body=@comment.md >/dev/null
  echo "Updated comment ${existing}."
else
  gh pr comment "$PR" --body-file comment.md
  echo "Created a new comment."
fi
