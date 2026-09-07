#!/usr/bin/env bash
# The forks daily_merge could not publish -- ALL of them, not the five whose
# state names happen to contain a word somebody wrote down once.
#
# run_tick.sh's [merge] block logs `head -1` (daily_merge's summary line, which
# gives a COUNT of forks needing a human) and then
# `grep -E "CONFLICT|NEEDS_HUMAN|FAILED|NO_UPSTREAM"` to name them. That grep is
# a hand-written list of state names, and daily_merge has grown well past it.
#
# MEASURED against daily_merge.py, by walking its AST for every literal it can put
# in a row's `state`: 26 states, 3 of them clean, so 23 mean "this fork was not
# published" -- and the grep names only 8 of the 23. These FIFTEEN are invisible in
# tick.log, which is the only append-only channel a human reads:
#
#   BRANCH_IDENTITY_CHANGED         POST_MERGE_CHECK_STALLED
#   BRANCH_IDENTITY_UNMEASURABLE    POST_MERGE_CHECK_UNMEASURABLE
#   BRANCH_LOOKUP_UNKNOWN           PUSH_CANDIDATE_NOT_FAST_FORWARD
#   CLONE_STALLED                   PUSH_CANDIDATE_UNMEASURABLE
#   FETCH_STALLED                   PUSH_CAS_REJECTED
#   MERGE_STALLED                   PUSH_CAS_UNCONFIRMED
#   NO_BRANCH_AT_PIN                UPSTREAM_DEFAULT_UNMEASURABLE
#                                   WORKTREE_STALLED
#
# So a round that exits 4 because of any of those ten logs "1 need a human" and
# never says which fork. That is what the 2026-08-25, 2026-09-07 and 2026-09-08
# rounds left behind, and it is why not even someone with a shell on the build
# host could answer "the same fork every morning, or a different one?".
#
# Note the shape of the gap: every one of the fifteen is a STALLED, UNMEASURABLE,
# UNKNOWN or REJECTED outcome -- the states that mean "the question was not
# answered". The pattern was written from the failures somebody had already seen,
# so the ones it cannot see are precisely the ones nobody had seen yet.
#
# INVERTED, not extended. The three CLEAN states are a closed set daily_merge
# defines and rarely changes; the unclean set grows every time a new way to fail
# is discovered, which is exactly the list a fixed pattern cannot keep up with. A
# state this script does not recognise is therefore REPORTED, never dropped -- the
# safe direction, and the opposite of the one the grep failed in.
#
# Usage: merge_findings.sh <path-to-daily-merge.txt>
# Prints: daily_merge's own [NEEDS HUMAN] / [POST-MERGE CHECK] lines, every fork
# row whose state is not MERGED/ALREADY_CURRENT/WOULD_MERGE, the indented
# conflict/check lines belonging to those rows, and any traceback line -- a crash
# is a merge that did not happen and must not read as silence.
# Prints nothing, exit 0, on a clean round.
set -uo pipefail

out="${1:?usage: merge_findings.sh <daily-merge.txt>}"
[ -f "${out}" ] || { echo "MISSING: ${out} — daily_merge left no output to read"; exit 0; }

awk '
  /^\[(NEEDS HUMAN|POST-MERGE CHECK)\]/ { print; next }
  /^Traceback/                              { crash = 1; print; next }
  /^[A-Za-z_.]*(Error|Exception):/           { crash = 0; print; next }
  crash && /^  /                             { print; next }
  /^  [^ ]/ {
      keep = ($2 != "MERGED" && $2 != "ALREADY_CURRENT" && $2 != "WOULD_MERGE")
      if (keep) print
      next
  }
  keep
' "${out}"
