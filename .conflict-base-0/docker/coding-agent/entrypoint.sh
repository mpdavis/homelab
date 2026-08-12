#!/usr/bin/env bash
# Idempotent home-dir bootstrap, then hand off to the CloudCLI server.
# HOME is a PVC — everything set up here survives pod restarts.
set -euo pipefail

git config --global user.name "Michael Davis (coding-agent)"
git config --global user.email "michael@mpdavis.com"
git config --global init.defaultBranch main

# GH_TOKEN (fine-grained PAT from the ExternalSecret) authenticates both gh and
# git-over-https pushes via gh's credential helper.
if [ -n "${GH_TOKEN:-}" ]; then
  gh auth setup-git || echo "WARN: gh auth setup-git failed; git pushes will not authenticate" >&2
fi

REPO_DIR="$HOME/homelab"
REPO_URL="${GIT_REPO_URL:-https://github.com/mpdavis/homelab}"
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone "$REPO_URL" "$REPO_DIR" \
    || echo "WARN: initial clone failed; clone manually from a session" >&2
else
  git -C "$REPO_DIR" fetch origin || true
fi

exec cloudcli
