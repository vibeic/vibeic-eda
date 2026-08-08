#!/usr/bin/env bash
# vibeic-eda#96: what a human reading the cron log needs when the release stage
# FAILED. run_tick.sh's [release] block uses `head -1` for the common, PASSING
# case -- right there, wrong here.
#
# daily_release.py's compose runs with `_sh(stream=True)`, which deliberately
# does NOT capture buildx's output (vibeic-eda#26: capturing hid a real wedge
# behind a silent buffer). Streamed output inherits daily_release.py's own
# stdout/stderr, which run_tick.sh has already redirected into the same file
# this script reads -- so the true cause IS in that file, just not on line 1
# and not matched by run_tick.sh's existing `VERSION|building|composing|
# UNRESOLVED|FAIL` grep: docker buildx's own failure vocabulary is
# "ERROR: failed to solve: ...", and lowercase "failed" does not match the
# uppercase "FAIL" in that pattern. Measured 2026-07-31 (0.2.47) and
# 2026-08-05 (0.2.64): both failed, both rendered as a single line ending in a
# colon, and in both cases the real "ERROR: failed to solve" sat roughly 130
# lines into the same file, unread.
#
# Position-independent by construction (grep over the whole file, not a
# window near either end) -- deliberately, since a buildx failure can land
# anywhere depending on which of several parallel stages failed first.
#
# Usage: release_failure_summary.sh <path-to-daily-release.txt>
# Prints: every "ERROR:" line found, each with 2 lines of trailing context
# (buildx's own next lines are the "failed to resolve source metadata" detail
# and the "Dockerfile:NNN FROM ..." stage that named the broken reference).
# Prints nothing, exit 0, if no such line exists -- the caller decides what
# "nothing found on a failing round" means; this script only reports what it sees.
set -euo pipefail

out="${1:?usage: release_failure_summary.sh <daily-release.txt>}"
grep -A2 -E "^ERROR:" "${out}" || true
