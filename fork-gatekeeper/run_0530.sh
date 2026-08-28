#!/usr/bin/env bash
# The 05:30 entry point on 8HD-d. ONE cron line, two pipelines, in order.
#
#   1. daily_0530.py --apply  — the six steps over every fork: upstream into
#      our line, our branches into our line, the merge/cherry-pick conflicts
#      handed to THIS HOST'S GATEKEEPER AI (step 2b), then prune.
#   2. run_tick.sh            — the pre-existing plugin-repo gatekeeper tick.
#      Kept, not replaced: it answers a different question (the plugin repo's
#      version gate), and it was already the only thing on this cron line.
#
# Exit codes are captured DIRECTLY, never through a pipe. `cmd | tee; echo $?`
# reports tee's status, which has already caused two false "clean" reports on
# this campaign.
set -uo pipefail
export PATH="${HOME}/.local/bin:/home/reyerchu/.nvm/versions/node/v22.22.0/bin:/usr/local/bin:/usr/bin:/bin"
export HOME="${HOME:-/home/reyerchu}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export GK_STATE_DIR="${GK_STATE_DIR:-${HOME}/.cache/eda-fork-gatekeeper}"
export GK_PRODUCTION_WRITER=1
LOG="${GK_STATE_DIR}/daily_0530.log"
mkdir -p "${GK_STATE_DIR}"

LOCK="${GK_STATE_DIR}/daily_0530.lock"
exec 9>"${LOCK}"
if ! flock -n 9; then
    echo "[$(date -Is)] [skip] a 05:30 round is already running" >> "${LOG}"
    exit 0
fi

ROUND_AT="$(date -Is)"
export GK_CURRENT_ROUND_AT="${ROUND_AT}"
{
  echo "════════ ${ROUND_AT} 05:30 round ════════"
} >> "${LOG}"

# A release writes its pins BEFORE building and commits them only once the image
# is in a registry, so a build that cannot publish leaves them uncommitted — and
# the page-publication tests check the real repository, so a dirty tree turns 5
# of them red, the semantic pytest gate goes RED, run_tick is NOT CHECKED, and
# the release that would retry never runs. Round 12 (2026-08-21) died exactly
# there after round 11 lost its upload to a network timeout. Recovering that
# state is the FIRST thing a round does, so nothing downstream inherits it.
# The recovery refuses anything that is not a pin-shaped unstaged edit, and a
# refusal does not stop the round: the round then reports the dirty tree the
# way it always has, rather than this quietly deciding a human's edit is ours.
if ! python3 "${DIR}/recover_pin_only_dirty_tree.py" >> "${LOG}" 2>&1; then
    echo "[$(date -Is)] pin-only dirty-tree recovery REFUSED — the tree carries work this round cannot own; see above" >> "${LOG}"
fi

# Give buildkit a concurrency ceiling before anything builds. Tool artefacts now
# build two at a time (GK_BUILD_PARALLEL), which bounds how many TOOLS overlap
# but not how many steps buildkit runs inside them; `--oci-max-parallelism` is
# the daemon's own ceiling and can only be set when the builder is created.
#
# Here, at the start, because that is the one moment in a round when no bake is
# in flight. It is a no-op once the ceiling is set, it carries the builder's
# existing config across by READING it off the live builder, and it never removes
# a builder without --keep-state — the state volume held 98.89 GB of cache when
# this landed, and dropping it would rebuild every artefact from scratch.
#
# A refusal does not stop the round: building without the ceiling is what every
# round did until now, so it is a worse round, not a broken one.
if ! python3 "${DIR}/ensure_builder_ceiling.py" >> "${LOG}" 2>&1; then
    echo "[$(date -Is)] buildkit concurrency ceiling NOT SET — see above; the round builds without it" >> "${LOG}"
fi

# Step 1-4 + the AI half.  The gatekeeper may read real code across several
# forks, so elapsed runtime is not a verdict.  This is a ZERO-PROGRESS grace:
# stream-json/tool activity renews it, and a progressing turn runs to completion.
# A crash before daily_0530 writes its terminal round must leave NO record for
# downstream GitHub convergence. Otherwise yesterday's merge evidence can be
# read under today's checker timestamp and impersonate a current measurement.
INPUT_CLEAR=0
if ! rm -f "${GK_STATE_DIR}/daily_0530.json"; then
    INPUT_CLEAR=1
    echo "[$(date -Is)] could not clear daily_0530.json — downstream provenance must reject any surviving record" >> "${LOG}"
fi
python3 "${DIR}/daily_0530.py" --apply \
        --ai-stall-grace "${GK_AI_STALL_GRACE:-5400}" \
        --json "${GK_STATE_DIR}/daily_0530.json" >> "${LOG}" 2>&1
SIX=$?
echo "[$(date -Is)] daily_0530 exit ${SIX}" >> "${LOG}"

# A process exit is not the release authorization for AI-resolved conflicts.
# `daily_0530` can return 1 after a conflict was successfully delivered, and an
# AI process can return 0 while its source tip is still absent from the published
# mainline.  Read this round's retained postconditions and fail closed on both a
# measured rejection and NORECORD.  The normalized sidecar is also the exact
# evidence the learning loop retains later in this script.
AI_AUTH_JSON="${GK_STATE_DIR}/ai-release-authorization.json"
AI_RUN_ID=""
if ! rm -f "${AI_AUTH_JSON}"; then
    INPUT_CLEAR=1
    echo "[$(date -Is)] could not clear ${AI_AUTH_JSON} — stale AI authorization must not permit release" >> "${LOG}"
fi
if [ -f "${DIR}/check_ai_release_authorization.py" ]; then
    python3 "${DIR}/check_ai_release_authorization.py" \
        --round-json "${GK_STATE_DIR}/daily_0530.json" \
        --forks-json "${DIR}/FORKS.json" \
        --round-at "${ROUND_AT}" --json "${AI_AUTH_JSON}" >> "${LOG}" 2>&1
    AI_AUTH=$?
else
    AI_AUTH=2
    echo "[$(date -Is)] AI release authorization NOT CHECKED — checker missing" >> "${LOG}"
fi
echo "[$(date -Is)] check_ai_release_authorization exit ${AI_AUTH} (0=authorized, 1=blocked, 2=NORECORD)" >> "${LOG}"
if [ "${AI_AUTH}" -eq 0 ]; then
    AI_RUN_ID="$(python3 - "${AI_AUTH_JSON}" <<'PY'
import json
import re
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
meta = value.get("round") if isinstance(value, dict) else None
run_id = meta.get("run_id") if isinstance(meta, dict) else None
if (value.get("status") != "authorized" or not isinstance(run_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", run_id)):
    raise SystemExit("authorization sidecar has no valid authorized run_id")
print(run_id)
PY
)"
    if [ "$?" -ne 0 ]; then
        AI_AUTH=2
        AI_RUN_ID=""
        echo "[$(date -Is)] AI release authorization sidecar could not supply its exact round nonce — forced NORECORD" >> "${LOG}"
    fi
fi

# Is this round even running the code that was landed for it?
#
# Nothing here or in run_tick.sh updates this checkout, so a fix landed in
# fork-gatekeeper/ stays invisible to the thing that runs it. Measured 2026-08-03:
# five programs behind origin/main, including discover_forks.py and build_page.py —
# the two that produce the published numbers — and among the missed commits was the
# one that changed HOW they are computed, after the old reference was shown to both
# overcount and undercount in one sweep. The round would have put today's date on a
# number produced by a method already known to be wrong.
#
# Placed before the publish and deliberately NOT gating the MERGE half: bringing
# upstream into our line is useful even from slightly older code, and blocking it
# would trade a reporting problem for a synchronisation one.
python3 "${DIR}/check_round_code_is_current.py" >> "${LOG}" 2>&1
CODE=$?
echo "[$(date -Is)] check_round_code_is_current exit ${CODE}" >> "${LOG}"

# ...and does that code still pass its own tests? NOTHING RAN THEM.
#
# The check above asks whether this checkout is CURRENT. It cannot ask whether
# the code is CORRECT, and until this line the answer was nobody's job: the only
# `pytest` invocations anywhere in the repo were inside the test files
# themselves. Measured 2026-08-04: five tests had been failing for at least seven
# releases, and 0.2.63 published with all five red. Among them was a NEGATIVE
# CONTROL — `an_unreproducible_fingerprint_is_a_finding` — which meant the
# release-record checker had genuinely stopped detecting an unreproducible
# record, and the disclosure was sitting in a red suite nobody ran.
#
# A suite that can be red for seven releases without anything noticing is not a
# suite, it is a directory of files. This is what makes it one.
#
# GATES THE PUBLISH HALF, NOT THE MERGE HALF — the same split, and for the same
# reason, as `check_round_code_is_current` above: bringing upstream into our line
# is useful even from code whose tests are red, and blocking it would trade a
# correctness-reporting problem for a synchronisation one. What it does block is
# putting today's date on numbers produced by logic that fails its own tests.
PYTEST_STAMP="$(date +%Y-%m-%dT%H%M%S%z)"
PYTEST_RETAINED="${GK_STATE_DIR}/rounds/${PYTEST_STAMP}/semantic_pytest.json"
mkdir -p "$(dirname "${PYTEST_RETAINED}")"
GK_CURRENT_WATCH_RESET_JSON="${GK_STATE_DIR}/daily_0530.json" \
GK_CURRENT_WATCH_RESET_ROUND_AT="${ROUND_AT}" \
python3 "${DIR}/semantic_pytest_gate.py" \
    --junit "${GK_STATE_DIR}/fork-gatekeeper.junit.xml" \
    --json "${GK_STATE_DIR}/semantic_pytest.json" \
    --json "${PYTEST_RETAINED}" \
    --cwd "${DIR}/.." \
    --stall-grace "${GK_PYTEST_STALL_GRACE:-300}" \
    -- python3 -m pytest -q "${DIR}" >> "${LOG}" 2>&1
TESTS=$?
echo "[$(date -Is)] fork-gatekeeper tests exit ${TESTS}" >> "${LOG}"

# Step 5 — build + verify + publish, and the plugin-repo gatekeeper tick.
#
# A red suite and NORECORD are different evidence, but both mean there is no
# green record authorising a publish. Likewise, code already proved stale must
# not ship. Keep the merge half above useful; fail closed only at this release
# boundary. TICK=2 means NOT CHECKED — it is never forged into a successful
# run_tick result.
RELPIN_JSON="${GK_STATE_DIR}/release-pins-committed.json"
if ! rm -f -- "${RELPIN_JSON}"; then
    INPUT_CLEAR=1
    echo "[$(date -Is)] could not clear ${RELPIN_JSON} — stale release-pin evidence must not describe this round" >> "${LOG}"
fi
TICK_STATE="ran"
if [ "${TESTS}" -ne 0 ]; then
    TICK=2
    if [ "${TESTS}" -eq 1 ]; then
        TICK_STATE="not_checked_pytest_red"
        TEST_KIND="RED"
    else
        TICK_STATE="not_checked_pytest_norecord"
        TEST_OUTCOME="$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("outcome") or "unknown")
except Exception:
    print("missing")
' "${PYTEST_RETAINED}" 2>/dev/null)"
        TEST_KIND="NORECORD (outcome=${TEST_OUTCOME})"
    fi
    # NAME THE TREE THE TESTS RAN ON, when it is not the tree that would publish.
    #
    # `check_round_code_is_current` has already run and its verdict is in
    # ${CODE}. When that verdict is non-zero the suite just measured a checkout
    # that is NOT the code this round would report, so "semantic pytest RED"
    # points a reader at a test defect when the actual situation is a checkout
    # left on something else.
    #
    # MEASURED 2026-08-29. The 05:30 round logged, in this order:
    #     check_round_code_is_current exit 1
    #     fork-gatekeeper tests exit 1
    #     run_tick NOT CHECKED — semantic pytest RED
    # and cron.log carried only the third line. The checkout was parked on an
    # unmerged feature branch; the one red test guarded a `COPY` of a file that
    # exists ONLY on that branch. A reader following cron.log — which is the
    # channel a human actually reads — went looking for a test bug, and began
    # fixing `.dockerignore` on main, where the file the test names does not
    # exist. The information needed to stop that was already computed, one
    # variable away, and simply was not written down.
    #
    # This does not change what is gated: the pytest result still decides, and
    # a red suite still blocks the publish. It changes only what the refusal
    # SAYS, so the first line a human reads names the tree as well as the tests.
    CHECKOUT_NOTE=""
    if [ "${CODE}" -ne 0 ]; then
        CHECKOUT_NOTE=" — and this checkout is NOT the current round code (check_round_code_is_current exit ${CODE}), so the suite measured a tree that would not have published; fix the checkout before reading the test as a defect"
    fi
    echo "[$(date -Is)] run_tick NOT CHECKED (TICK=2) — semantic pytest ${TEST_KIND}${CHECKOUT_NOTE}; no release was authorised" >> "${LOG}"
    echo "[$(date -Is)] run_tick NOT CHECKED — semantic pytest ${TEST_KIND}${CHECKOUT_NOTE}; see ${PYTEST_RETAINED}" >> "${GK_STATE_DIR}/cron.log"
elif [ "${CODE}" -ne 0 ]; then
    TICK=2
    if [ "${CODE}" -eq 1 ]; then
        TICK_STATE="not_checked_stale_round_code"
        CODE_KIND="this checkout is superseded"
    else
        TICK_STATE="not_checked_round_code_norecord"
        CODE_KIND="code currency produced NORECORD (rc=${CODE})"
    fi
    echo "[$(date -Is)] run_tick NOT CHECKED (TICK=2) — ${CODE_KIND}; no release was authorised" >> "${LOG}"
    echo "[$(date -Is)] run_tick NOT CHECKED — ${CODE_KIND}" >> "${GK_STATE_DIR}/cron.log"
elif [ "${AI_AUTH}" -ne 0 ]; then
    TICK=2
    if [ "${AI_AUTH}" -eq 1 ]; then
        TICK_STATE="not_checked_ai_unverified"
        AI_KIND="an AI conflict lacks a verified published-mainline postcondition"
    else
        TICK_STATE="not_checked_ai_norecord"
        AI_KIND="AI release authorization produced NORECORD (rc=${AI_AUTH})"
    fi
    echo "[$(date -Is)] run_tick NOT CHECKED (TICK=2) — ${AI_KIND}; no release was authorised" >> "${LOG}"
    echo "[$(date -Is)] run_tick NOT CHECKED — ${AI_KIND}; see ${AI_AUTH_JSON}" >> "${GK_STATE_DIR}/cron.log"
else
    AI_CAPABILITY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    AI_HANDOFF="${GK_STATE_DIR}/ai-release-handoff.${AI_CAPABILITY}.json"
    if [ -z "${AI_CAPABILITY}" ] || ! (umask 077; python3 - \
            "${AI_HANDOFF}" "${AI_CAPABILITY}" "$$" \
            "${GK_STATE_DIR}/daily_0530.json" "${DIR}/FORKS.json" \
            "${ROUND_AT}" "${AI_RUN_ID}" <<'PY'
import json
import os
import sys

path = sys.argv[1]
record = {
    "schema_version": 1,
    "capability": sys.argv[2],
    "parent_pid": int(sys.argv[3]),
    "round_json": sys.argv[4],
    "forks_json": sys.argv[5],
    "round_at": sys.argv[6],
    "run_id": sys.argv[7],
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(record, stream, sort_keys=True)
    stream.write("\n")
PY
    ); then
        AI_AUTH=2
        TICK=2
        TICK_STATE="not_checked_ai_norecord"
        echo "[$(date -Is)] run_tick NOT CHECKED (TICK=2) — could not create one-use AI release capability; no release was authorised" >> "${LOG}"
        echo "[$(date -Is)] run_tick NOT CHECKED — AI release capability produced NORECORD" >> "${GK_STATE_DIR}/cron.log"
        rm -f -- "${AI_HANDOFF}"
    else
        "${DIR}/run_tick.sh" \
            --ai-round-json "${GK_STATE_DIR}/daily_0530.json" \
            --ai-forks-json "${DIR}/FORKS.json" \
            --ai-round-at "${ROUND_AT}" \
            --ai-run-id "${AI_RUN_ID}" \
            --ai-capability "${AI_CAPABILITY}" \
            >> "${GK_STATE_DIR}/cron.log" 2>&1
        TICK=$?
        rm -f -- "${AI_HANDOFF}"
        if [ "${TICK}" -eq 9 ]; then
            # The inner release boundary consumed or rejected its one-use
            # context.  It intentionally leaves the prior terminal release
            # record untouched; this state tells LL not to read that old file.
            AI_AUTH=2
            TICK_STATE="not_checked_ai_norecord"
            echo "[$(date -Is)] run_tick refused current-round AI authorization (TICK=9) — forced NORECORD; no release was authorised" >> "${LOG}"
        fi
        echo "[$(date -Is)] run_tick exit ${TICK}" >> "${LOG}"
    fi
fi

# Say so on the day a release FAILS. Until this existed the only signal was
# `check_publish_silence`, which measures the PAGE going stale — a consequence,
# counted in DAYS. 2026-08-22: the build died on a klayout version assertion,
# build_page was skipped, the round finished normally and NOTHING was raised,
# because the page had published the day before and the streak was under the
# threshold. A build failing identically every round could have sat most of a
# week first.
#
# It stays quiet on a successful release and on a round that never authorised
# one, and it never stops the round: an alarm that cannot be raised must not
# also cost the reporting steps that follow it.
if [ -x "${DIR}/check_release_failed.py" ] || [ -f "${DIR}/check_release_failed.py" ]; then
    python3 "${DIR}/check_release_failed.py" \
        --tick-exit "${TICK}" --tick-state "${TICK_STATE}" \
        --round-at "${ROUND_AT}" >> "${LOG}" 2>&1
    echo "[$(date -Is)] check_release_failed exit $? (0=nothing to raise, 1=raised, 2=NORECORD)" >> "${LOG}"
else
    echo "[$(date -Is)] check_release_failed NOT CHECKED — checker missing" >> "${LOG}"
fi

# RELEASE BOUNDARY: run_tick has already cut (or refused) the image.  Nothing
# after it may advance a fork tip.  This used to call the selective-PR lander;
# on 2026-08-26 that made OpenROAD#8 land after image 0.3.32 was published,
# creating the very past-pin work the convergence check below then rejected.
# The release path's only fork mutation authority is daily_merge.py inside
# run_tick, before selftests and daily_release.  Gatekeeper's late pass is forced
# to assessment-only there, so there is deliberately no post-release lander.

# Step 6 — refresh the fork ledger.  Publication is deliberately later, after
# every current-round delivery/convergence source has produced its verdict.
#
# This step did not exist. The cron merged, built, verified and published every
# morning, and never touched https://vibeic.ai/eda-forks.html — so the page only
# ever changed when a human regenerated it by hand, and silently described an
# older world in between. A daily pipeline whose public output is updated
# manually is not a daily pipeline.
#
# Discovery runs AFTER run_tick so the ledger reflects the release that just
# shipped. Exit status is captured directly, per the note at the top of this
# file.
python3 "${DIR}/discover_forks.py" >> "${LOG}" 2>&1
DISC=$?
echo "[$(date -Is)] discover_forks exit ${DISC}" >> "${LOG}"

# THE ROUND MUST NOTICE ITS OWN SILENCE (vibeic-eda#58). Three consecutive days
# published nothing and raised no alert: the round exited 1 into a log, and the
# only visible symptom was a public page that did not move — noticed by a person,
# on day three. Every step above reports ITS OWN exit; none of them answers "did
# today's numbers actually reach the ledger?", which is the question a reader of
# the page is really asking.
#
# It runs LAST and UNCONDITIONALLY — after a failed publish is exactly when it
# matters, so it must not sit behind any of the exits above.
# THE TWO DAILY NUMBERS ARE MEASURED BY ONE PROGRAM, ALWAYS THE SAME WAY.
# They were measured by hand on 2026-08-02 and the hand got them wrong twice in
# one evening: a missing pin fell back to the clone's HEAD, which makes the gap
# identically 0, and "behind" was read as one number when it is two (SYNC lag vs
# RELEASE lag, which need opposite fixes). Both mistakes produced a reassuring
# answer. A measurement that has to be remembered is not a measurement.
GAP_JSON="${GK_STATE_DIR}/fork_gap.json"
# As with daily_0530.json above, a producer crash must expose MISSING, not leave
# yesterday's valid sidecar available to today's LL and sync verdict.
if ! rm -f "${GAP_JSON}"; then
    INPUT_CLEAR=1
    echo "[$(date -Is)] could not clear ${GAP_JSON} — forced dirty-input failure" >> "${LOG}"
fi
python3 "${DIR}/fork_gap_report.py" \
    --merge-evidence "${GK_STATE_DIR}/daily_0530.json" \
    --round-at "${ROUND_AT}" \
    --json "${GAP_JSON}" >> "${LOG}" 2>&1
GAP=$?
echo "[$(date -Is)] fork_gap_report exit ${GAP}" >> "${LOG}"

# GitHub convergence is a separate delivery boundary from commit/image lag:
# every fork issue and PR that remains open after this round is unfinished work,
# while a failed API read is NORECORD, never an invented zero. Clear the live
# sidecar first so a checker that cannot start cannot leave yesterday's answer
# available to the LL capture. Keep the exact round artefact beside semantic
# pytest evidence for post-mortem audit.
FORK_GH_JSON="${GK_STATE_DIR}/fork-github-convergence.json"
if ! rm -f "${FORK_GH_JSON}"; then
    INPUT_CLEAR=1
    echo "[$(date -Is)] could not clear ${FORK_GH_JSON} — forced dirty-input failure" >> "${LOG}"
fi
python3 "${DIR}/fork_github_convergence.py" \
    --forks-json "${DIR}/FORKS.json" \
    --round-json "${GK_STATE_DIR}/daily_0530.json" \
    --round-at "${ROUND_AT}" \
    --expected-actor "${GK_GITHUB_ACTOR:-reyerchu}" \
    --json "${FORK_GH_JSON}" >> "${LOG}" 2>&1
FORK_GH=$?
echo "[$(date -Is)] fork_github_convergence exit ${FORK_GH} (0=exact-zero, 1=open work, 2=NORECORD)" >> "${LOG}"
FORK_GH_RETAINED="${GK_STATE_DIR}/rounds/${PYTEST_STAMP}/fork-github-convergence.json"
FORK_GH_RETAIN_TMP="${FORK_GH_RETAINED}.tmp.$$"
if [ -f "${FORK_GH_JSON}" ]; then
    # Copy to a same-directory temporary, then rename: readers see the old full
    # record or the new full record, never a partial cp. Retention is evidence,
    # so failure overrides even an otherwise-clean checker result to NORECORD.
    if ! cp "${FORK_GH_JSON}" "${FORK_GH_RETAIN_TMP}" \
       || ! mv "${FORK_GH_RETAIN_TMP}" "${FORK_GH_RETAINED}"; then
        if ! rm -f "${FORK_GH_RETAIN_TMP}"; then
            echo "[$(date -Is)] fork_github_convergence retention temp cleanup failed: ${FORK_GH_RETAIN_TMP}" >> "${LOG}"
        fi
        FORK_GH=2
        echo "[$(date -Is)] fork_github_convergence retention failed — forced NORECORD" >> "${LOG}"
    fi
else
    FORK_GH=2
    echo "[$(date -Is)] fork_github_convergence wrote no JSON — forced NORECORD" >> "${LOG}"
fi

# CAPTURE THE LEARNING LOOP, NOT ONLY THE LOG.  A release failure used to leave
# yesterday's successful daily-release.json in place and the text log was the
# only way to reconstruct that the image compose had died on DNS.  This join is
# deliberately after fork_gap_report: an error message says what broke, while
# the measured gap says what consequence survived the round.  Every morning —
# success, self-healed retry, deterministic refusal, or missing evidence — gets
# one retained row under release-ll/ for later policy and dashboard audits.
python3 "${DIR}/capture_release_ll.py" \
    --state-dir "${GK_STATE_DIR}" \
    --eda-root "${DIR}/.." \
    --release-json "${GK_STATE_DIR}/daily-release.json" \
    --release-pins-json "${RELPIN_JSON}" \
    --gap-json "${GAP_JSON}" \
    --fork-github-json "${FORK_GH_JSON}" \
    --forks-json "${DIR}/FORKS.json" \
    --expected-github-actor "${GK_GITHUB_ACTOR:-reyerchu}" \
    --ai-authorization-json "${AI_AUTH_JSON}" \
    --watch-reset-json "${GK_STATE_DIR}/daily_0530.json" \
    --semantic-pytest-json "${PYTEST_RETAINED}" \
    --round-at "${ROUND_AT}" --tick-exit "${TICK}" \
    --tick-state "${TICK_STATE}" >> "${LOG}" 2>&1
LL=$?
echo "[$(date -Is)] capture_release_ll exit ${LL}" >> "${LOG}"

python3 "${DIR}/check_ledger_is_fresh.py" \
    --ledger "${GK_STATE_DIR}/ledger" \
    --json "${GK_STATE_DIR}/ledger_freshness.json" >> "${LOG}" 2>&1
FRESH=$?
echo "[$(date -Is)] check_ledger_is_fresh exit ${FRESH}" >> "${LOG}"

# DID THE ROUND ACTUALLY CONVERGE? (vibeic-eda#104)
#
# `fork_gap_report` runs above, measures the gap, writes it to JSON -- and its
# exit code was NOT in the condition below. So the round measured that our forks
# were behind upstream, said so in a file, and reported success. Measured
# 2026-08-06: `daily_0530 exit 1` on five consecutive mornings (08-02 .. 08-06)
# while the fleet sat 12 commits behind upstream, and nothing escalated, because
# the one number that says whether the round did its job was the one number the
# round did not look at.
#
# SYNC LAG IS FATAL. "Merge upstream into our line" is this round's primary duty;
# if the round finishes and a fork is still behind upstream, the round did not do
# it, whatever else went right. That is a different claim from "something needed
# a human" (SIX=1), which is information: a conflict handed to the gatekeeper is
# the system working.
#
# RELEASE LAG IS NAMED, NOT FATAL. A pin trailing our own fork tip closes with a
# rebuild, and `daily_release` DELIBERATELY declines to move a pure mirror's pin
# on its own -- ORFS and slang carry none of our commits, so advancing them is a
# gatekeeper decision, not an automatic one. Failing the round on a decision that
# is deliberately reserved for a human would make it permanently red, and a
# permanently red round is one people route around.
SYNC_LAG=$(python3 -c "
import json,sys
try:
    d=json.load(open('${GAP_JSON}'))
    v=d.get('q1_sync_lag_at_merge')
    if v is None: v=d.get('q1_sync_lag')
    print(v if v is not None else 'unmeasured')
except Exception:
    print('unmeasured')
" 2>/dev/null)
STRANDED=$(python3 -c "
import json
try:
    d=json.load(open('${GAP_JSON}'))
    v=d.get('q2_our_commits_stranded')
    print(v if isinstance(v, int) and not isinstance(v, bool) else 'unmeasured')
except Exception:
    print('unmeasured')
" 2>/dev/null)
PAST_PIN_SUBSTANTIVE=$(python3 -c "
import json
try:
    d=json.load(open('${GAP_JSON}'))
    v=d.get('q2_ours_past_the_pin_substantive')
    print(v if isinstance(v, int) and not isinstance(v, bool) else 'unmeasured')
except Exception:
    print('unmeasured')
" 2>/dev/null)
if [ "${GAP}" -ge 2 ]; then
    echo "[$(date -Is)] CONVERGENCE: fork_gap_report produced NORECORD/incomplete evidence (exit ${GAP}) — a numeric partial sum is not an authoritative zero" >> "${LOG}"
    GAP_FATAL=1
elif [ "${SYNC_LAG}" = "unmeasured" ]; then
    echo "[$(date -Is)] CONVERGENCE: sync lag COULD NOT BE MEASURED (fork_gap_report exit ${GAP}) — that is not zero, and the round is not clean" >> "${LOG}"
    GAP_FATAL=1
elif [ "${STRANDED}" = "unmeasured" ] || [ "${PAST_PIN_SUBSTANTIVE}" = "unmeasured" ]; then
    echo "[$(date -Is)] CONVERGENCE: fork delivery state COULD NOT BE MEASURED — unknown stranded/past-pin work is not zero" >> "${LOG}"
    GAP_FATAL=1
elif [ "${STRANDED}" -ne 0 ] 2>/dev/null; then
    echo "[$(date -Is)] CONVERGENCE: FAILED — ${STRANDED} commit(s) are stranded in patched forks the image cannot build from" >> "${LOG}"
    GAP_FATAL=1
elif [ "${PAST_PIN_SUBSTANTIVE}" -ne 0 ] 2>/dev/null; then
    echo "[$(date -Is)] CONVERGENCE: FAILED — ${PAST_PIN_SUBSTANTIVE} substantive own commit(s) remain past the image pin" >> "${LOG}"
    GAP_FATAL=1
elif [ "${SYNC_LAG}" -ne 0 ] 2>/dev/null; then
    echo "[$(date -Is)] CONVERGENCE: FAILED — ${SYNC_LAG} commit(s) this round SAW upstream and did not take. Merging upstream into our line is this round's primary duty; it did not happen. (Measured against each fork's upstream_evidence.tip_seen — the tip its own fetch resolved before merging — NOT a fresh fetch, which moves during the ~1h release phase. The live drift is q1_sync_lag in fork_gap.json and is reported there, not here.)" >> "${LOG}"
    GAP_FATAL=1
else
    echo "[$(date -Is)] CONVERGENCE: sync lag 0 — every fork took everything this round saw upstream" >> "${LOG}"
    GAP_FATAL=0
fi

# PUBLISH LAST, FROM THIS ROUND'S CERTIFICATE (BLOCKING).
#
# The old placement published immediately after discovery. Gap, GitHub, LL and
# freshness all ran later, so each could report a failure after today's page had
# already replaced yesterday's good one. Worse, build_page read the live
# daily-release.json directly; when AI skipped the release tick that file was
# necessarily yesterday's terminal record. The page therefore attributed an old
# release to a new round.
#
# Every authority below is a required current-round zero. A measured open GitHub
# inventory (rc=1), NORECORD (rc=2), a malformed/stale source rejected by LL, or
# any release/tick refusal leaves the last good page byte-for-byte untouched.
# build_page independently revalidates the strict gap/GitHub/LL JSON, run nonce,
# timestamps and fleet denominator before its atomic rename.
PAGE=0
if [ "${TESTS}" -eq 0 ] && [ "${CODE}" -eq 0 ] \
   && [ "${AI_AUTH}" -eq 0 ] && [ "${TICK}" -eq 0 ] \
   && [ "${DISC}" -eq 0 ] && [ "${GAP}" -eq 0 ] \
   && [ "${FORK_GH}" -eq 0 ] && [ "${LL}" -eq 0 ] \
   && [ "${FRESH}" -eq 0 ] && [ "${INPUT_CLEAR}" -eq 0 ] \
   && [ "${GAP_FATAL}" -eq 0 ]; then
    python3 "${DIR}/build_page.py" \
        --out /home/reyerchu/vibeic.ai/eda-forks.html \
        --round-at "${ROUND_AT}" \
        --run-id "${AI_RUN_ID}" \
        --forks-json "${DIR}/FORKS.json" \
        --gap-json "${GAP_JSON}" \
        --fork-github-json "${FORK_GH_JSON}" \
        --release-ll-json "${GK_STATE_DIR}/release-ll/latest.json" \
        --expected-github-actor "${GK_GITHUB_ACTOR:-reyerchu}" \
        >> "${LOG}" 2>&1
    PAGE=$?
    echo "[$(date -Is)] build_page exit ${PAGE}" >> "${LOG}"
    if [ "${PAGE}" -eq 0 ]; then
        PAGE_PUBLISHED=1
    else
        PAGE_PUBLISHED=0
        PAGE_SKIP="build_page ran and failed (rc=${PAGE})"
    fi
else
    PAGE_PUBLISHED=0
    # PAGE remains zero because the source gate already owns the round failure.
    # Do not write a fake `build_page exit 0`: a step that did not run says so.
    if [ "${TESTS}" -ne 0 ]; then
        PAGE_SKIP="semantic pytest is not a natural green record (rc=${TESTS})"
    elif [ "${CODE}" -ne 0 ]; then
        PAGE_SKIP="round code currency is stale or NORECORD (rc=${CODE})"
    elif [ "${AI_AUTH}" -ne 0 ]; then
        PAGE_SKIP="AI release authorization is blocked or NORECORD (rc=${AI_AUTH})"
    elif [ "${TICK}" -ne 0 ]; then
        PAGE_SKIP="release tick is not authorized/current (rc=${TICK})"
    elif [ "${DISC}" -ne 0 ]; then
        PAGE_SKIP="fork discovery/ledger refresh is nonzero (rc=${DISC})"
    elif [ "${GAP}" -ne 0 ]; then
        PAGE_SKIP="current-round fork gap is open or NORECORD (rc=${GAP})"
    elif [ "${FORK_GH}" -ne 0 ]; then
        PAGE_SKIP="current-round GitHub delivery is open or NORECORD (rc=${FORK_GH})"
    elif [ "${LL}" -ne 0 ]; then
        PAGE_SKIP="current-round release LL is invalid or nonzero (rc=${LL})"
    elif [ "${FRESH}" -ne 0 ]; then
        PAGE_SKIP="ledger freshness is stale or NORECORD (rc=${FRESH})"
    elif [ "${INPUT_CLEAR}" -ne 0 ]; then
        PAGE_SKIP="a stale round input could not be cleared"
    else
        PAGE_SKIP="whole-round fork convergence is not zero"
    fi
    echo "[$(date -Is)] build_page SKIPPED — ${PAGE_SKIP}; last good page preserved" >> "${LOG}"
fi

# THE SILENCE MUST BE AUDIBLE, NOT MERELY MEASURED (vibeic-eda#58, again).
#
# #58 added the measurement after three silent days were found by a person. On
# 2026-08-20 three more silent days were found the SAME WAY — by a person opening
# https://vibeic.ai/eda-forks.html and seeing numbers that had not moved since
# 08-18T07:48. Every one of those rounds DID notice: each wrote `build_page
# SKIPPED — <reason>` right here. It wrote it into daily_0530.log, which nobody
# opens, so the detection existed and the alert did not.
#
# This runs LAST and UNCONDITIONALLY, like the convergence report above and for
# the same reason: after a failed publish is exactly when it matters, so it must
# not sit behind any of the exits. It owns the publish history end to end, and
# when the page has been silent for the threshold it opens a PR on vibe-ic —
# a PR on THIS repo — vibe-ic is not in fork_github_convergence's denominator
# for a reason, and neither is this alarm allowed to be.
#
# Its rc=2 (NORECORD) is a round failure below, and deliberately so: an alerter
# that cannot deliver but reports "nothing to report" is worse than no alerter,
# because the pipeline then looks healthy in exactly the case it is not.
SILENCE_JSON="${GK_STATE_DIR}/publish-silence.json"
python3 "${DIR}/check_publish_silence.py" \
    --history "${GK_STATE_DIR}/publish_history.jsonl" \
    --round-at "${ROUND_AT}" \
    --published "${PAGE_PUBLISHED:-0}" \
    --reason "${PAGE_SKIP:-unstated}" \
    --threshold "${GK_PUBLISH_SILENCE_THRESHOLD:-2}" \
    --json "${SILENCE_JSON}" >> "${LOG}" 2>&1
SILENCE=$?
echo "[$(date -Is)] check_publish_silence exit ${SILENCE} (0=published or below threshold, 1=silent and escalated, 2=NORECORD)" >> "${LOG}"
if [ "${SILENCE}" -ne 0 ]; then
    # cron.log is the file a person actually opens. The log above is for audit.
    echo "[$(date -Is)] PUBLISH SILENCE rc=${SILENCE} — see ${SILENCE_JSON}" >> "${GK_STATE_DIR}/cron.log"
fi

# 0 only when BOTH are clean: the delivery pipeline and every
# evidence/convergence boundary. The six steps' own 1 means "a case still needs
# a human", which is information, not noise.
if [ "${SIX}" -ne 0 ] || [ "${TICK}" -ne 0 ] || [ "${DISC}" -ne 0 ] \
   || [ "${PAGE}" -ne 0 ] || [ "${FRESH}" -ne 0 ] || [ "${CODE}" -ne 0 ] \
   || [ "${TESTS}" -ne 0 ] || [ "${INPUT_CLEAR}" -ne 0 ] \
   || [ "${SILENCE}" -eq 2 ] \
   || [ "${AI_AUTH}" -ne 0 ] \
   || [ "${GAP_FATAL}" -ne 0 ] \
   || [ "${FORK_GH}" -ne 0 ] || [ "${LL}" -ne 0 ]; then exit 1; fi
exit 0
