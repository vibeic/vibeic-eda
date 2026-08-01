#!/usr/bin/env bash
# Point this clone's hooks at tools/git-hooks/ so they are versioned.
#
# `core.hooksPath` rather than copying into .git/hooks: a copy silently goes
# stale, and a stale hook that still exits 0 is the exact shape this repo keeps
# finding — a check that reports clean because it is no longer the check.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
git -C "$ROOT" config core.hooksPath tools/git-hooks
echo "core.hooksPath = tools/git-hooks"
echo "installed: $(ls "$ROOT/tools/git-hooks")"
echo
echo "This is one machine's word — it does not travel with a clone and"
echo "--no-verify bypasses it. The 05:30 tick remains the unconditional answer."
