#!/usr/bin/env bash
# run_tick.sh — the release-capable inner half of the daily EDA-fork round.
#
# Mirrors the awesome-open-ic enrich cron: flock so rounds never overlap, a fixed
# headless PATH/HOME, and gh OAuth resolved to GH_TOKEN (the vibeic org rejects
# long-lived fine-grained PATs; the gho_ OAuth token works headless). Then runs the
# gatekeeper tick, which re-seeds ledgers and writes the daily assessment/report.
# The outer 05:30 wrapper alone regenerates the public monitor page after it has
# assembled and verified the current round's convergence certificate.
#
# Invoked by run_0530.sh after that wrapper has produced this round's merge and
# AI-delivery evidence.  It deliberately refuses a direct/manual invocation:
# releasing without the wrapper's one-use, current-round authorization context
# would let yesterday's green record bypass today's unresolved AI conflict.
set -uo pipefail

AI_ROUND_JSON=""
AI_FORKS_JSON=""
AI_ROUND_AT=""
AI_RUN_ID=""
AI_CAPABILITY=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --ai-round-json|--ai-forks-json|--ai-round-at|--ai-run-id|--ai-capability)
            if [ "$#" -lt 2 ]; then
                echo "[fatal] $1 requires a value" >&2
                exit 2
            fi
            case "$1" in
                --ai-round-json) AI_ROUND_JSON="$2" ;;
                --ai-forks-json) AI_FORKS_JSON="$2" ;;
                --ai-round-at) AI_ROUND_AT="$2" ;;
                --ai-run-id) AI_RUN_ID="$2" ;;
                --ai-capability) AI_CAPABILITY="$2" ;;
            esac
            shift 2
            ;;
        *)
            echo "[fatal] unknown run_tick argument: $1" >&2
            exit 2
            ;;
    esac
done

export PATH="${HOME}/.local/bin:/home/reyerchu/.nvm/versions/node/v22.22.0/bin:/usr/local/bin:/usr/bin:/bin"
export HOME="${HOME:-/home/reyerchu}"

# SOURCE (version-controlled, canonical) is this script's own directory — so the cron
# runs whatever is checked into vibeic-eda/fork-gatekeeper/, no separate deployed copy
# to drift. RUNTIME STATE (ledgers, reports, locks, logs) lives in the cache dir,
# OUTSIDE the source tree, so running in place never dirties the repo.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export GK_STATE_DIR="${GK_STATE_DIR:-${HOME}/.cache/eda-fork-gatekeeper}"
# THIS is the production runner (vibeic/vibeic-eda#12). GK_STATE_DIR says WHERE the state
# is; it does not say who is entitled to overwrite it, and before #12 nothing did — any
# checkout that imported assess_release and called assess() wrote the cache this tick
# reads. The declaration lives here, in the cron entrypoint, and nowhere else: a run that
# is not this script writes its own state (point GK_STATE_DIR elsewhere) or asks for the
# shared one on purpose. Exported, so the integration harness this tick spawns inherits it.
export GK_PRODUCTION_WRITER=1
LOG_DIR="${GK_STATE_DIR}"
LOCK="${LOG_DIR}/tick.lock"
LOG="${LOG_DIR}/tick.log"
mkdir -p "${LOG_DIR}"

log() { echo "[$(date -Is)] $*" | tee -a "${LOG}"; }

exec 9>"${LOCK}"
if ! flock -n 9; then
    log "[skip] another gatekeeper round is running"
    exit 0
fi

# --- CURRENT-ROUND AI RELEASE CAPABILITY -------------------------------------
#
# This is repeated here, at the process that can actually invoke daily_release.
# The outer wrapper's check is useful for deciding whether to start this tick,
# but cannot protect this entrypoint from a direct invocation or a second stale
# cron line.  The wrapper therefore creates an unguessable, one-use handoff bound
# to its exact round path, registry, timestamp, run nonce, and parent PID.  Claim
# it atomically, validate that context, then independently re-run the canonical
# checker.  Missing/replayed context and every nonzero checker result stop before
# GitHub auth, daily_merge, or any release mutation.
CANONICAL_AI_ROUND_JSON="${GK_STATE_DIR}/daily_0530.json"
CANONICAL_AI_FORKS_JSON="${DIR}/FORKS.json"
AI_AUTH_JSON="${GK_STATE_DIR}/ai-release-authorization.json"
AI_HANDOFF=""
AI_HANDOFF_CLAIM=""
AI_RECHECK_JSON=""

cleanup_ai_release_context() {
    [ -n "${AI_HANDOFF_CLAIM}" ] && rm -f -- "${AI_HANDOFF_CLAIM}"
    [ -n "${AI_RECHECK_JSON}" ] && rm -f -- "${AI_RECHECK_JSON}"
}
trap cleanup_ai_release_context EXIT

if [ "${AI_ROUND_JSON}" != "${CANONICAL_AI_ROUND_JSON}" ] \
   || [ "${AI_FORKS_JSON}" != "${CANONICAL_AI_FORKS_JSON}" ] \
   || [ -z "${AI_ROUND_AT}" ] \
   || [[ ! "${AI_RUN_ID}" =~ ^[A-Za-z0-9_-]{32,256}$ ]] \
   || [[ ! "${AI_CAPABILITY}" =~ ^[0-9a-f]{64}$ ]]; then
    log "[fatal] no valid current-round AI release context; invoke run_0530.sh, not run_tick.sh directly"
    exit 2
fi
if [ ! -f "${DIR}/check_ai_release_authorization.py" ]; then
    log "[fatal] AI release authorization checker is missing; daily_release is blocked"
    exit 9
fi

AI_HANDOFF="${GK_STATE_DIR}/ai-release-handoff.${AI_CAPABILITY}.json"
AI_HANDOFF_CLAIM="${AI_HANDOFF}.claimed.$$"
if [ -e "${AI_HANDOFF_CLAIM}" ] \
   || ! mv -- "${AI_HANDOFF}" "${AI_HANDOFF_CLAIM}"; then
    log "[fatal] current-round AI release capability is missing or already consumed; daily_release is blocked"
    exit 9
fi

if ! python3 - "${AI_HANDOFF_CLAIM}" "${AI_CAPABILITY}" "${PPID}" \
        "${AI_ROUND_JSON}" "${AI_FORKS_JSON}" "${AI_ROUND_AT}" \
        "${AI_RUN_ID}" <<'PY'
import json
import sys
from pathlib import Path


def strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def reject_constant(value):
    raise ValueError(f"non-finite JSON number {value!r}")


path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"),
                   object_pairs_hook=strict_object,
                   parse_constant=reject_constant)
expected = {
    "schema_version": 1,
    "capability": sys.argv[2],
    "parent_pid": int(sys.argv[3]),
    "round_json": sys.argv[4],
    "forks_json": sys.argv[5],
    "round_at": sys.argv[6],
    "run_id": sys.argv[7],
}
if value != expected:
    raise SystemExit("AI release handoff does not exactly match this process")

round_record = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"),
                          object_pairs_hook=strict_object,
                          parse_constant=reject_constant)
meta = round_record.get("_round") if isinstance(round_record, dict) else None
if not isinstance(meta, dict) or meta.get("run_id") != sys.argv[7]:
    raise SystemExit("AI release handoff run_id does not match daily_0530.json")
PY
then
    log "[fatal] current-round AI release capability is malformed, stale, or for another parent; daily_release is blocked"
    exit 9
fi

# The capability has now done its one job.  Remove the claimed file before the
# expensive work; the EXIT trap is defence in depth for every earlier return.
rm -f -- "${AI_HANDOFF_CLAIM}"
AI_HANDOFF_CLAIM=""
AI_RECHECK_JSON="${AI_AUTH_JSON}.tick-recheck.$$"
rm -f -- "${AI_RECHECK_JSON}"
python3 "${DIR}/check_ai_release_authorization.py" \
    --round-json "${AI_ROUND_JSON}" \
    --forks-json "${AI_FORKS_JSON}" \
    --round-at "${AI_ROUND_AT}" --json "${AI_RECHECK_JSON}" \
    >> "${LOG}" 2>&1
AI_AUTH_RC=$?

# A zero process status alone is not enough: retain and inspect the exact record
# produced by this re-check, including the wrapper's nonce.  Replacement is a
# same-directory rename, so LL readers see the earlier complete record or this
# complete record, never a partially-written authorization.
AI_RECHECK_OK=0
if [ -f "${AI_RECHECK_JSON}" ]; then
    if [ "${AI_AUTH_RC}" -eq 0 ]; then
        python3 - "${AI_RECHECK_JSON}" "${AI_ROUND_JSON}" \
            "${AI_FORKS_JSON}" "${AI_ROUND_AT}" "${AI_RUN_ID}" <<'PY'
import json
import sys
from pathlib import Path


def strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def reject_constant(value):
    raise ValueError(f"non-finite JSON number {value!r}")


value = json.loads(
    Path(sys.argv[1]).read_text(encoding="utf-8"),
    object_pairs_hook=strict_object, parse_constant=reject_constant)
round_meta = value.get("round") if isinstance(value, dict) else None
registry = value.get("registry") if isinstance(value, dict) else None
if not (
    value.get("program") == "check_ai_release_authorization"
    and value.get("status") == "authorized"
    and value.get("source") == sys.argv[2]
    and isinstance(registry, dict) and registry.get("source") == sys.argv[3]
    and value.get("round_at") == sys.argv[4]
    and isinstance(round_meta, dict) and round_meta.get("run_id") == sys.argv[5]
):
    raise SystemExit("AI authorization record contradicts current handoff")
PY
        AI_RECHECK_OK=$?
    fi
    if mv -f -- "${AI_RECHECK_JSON}" "${AI_AUTH_JSON}"; then
        AI_RECHECK_JSON=""
    else
        AI_RECHECK_OK=1
    fi
else
    # Do not leave the wrapper's earlier green record behind after an inner
    # checker that formed no record at all.
    rm -f -- "${AI_AUTH_JSON}"
    AI_RECHECK_OK=1
fi
if [ "${AI_AUTH_RC}" -ne 0 ] || [ "${AI_RECHECK_OK}" -ne 0 ]; then
    log "[fatal] current-round AI release authorization refused (checker rc=${AI_AUTH_RC}); daily_release is blocked"
    exit 9
fi
log "[blocking] current-round AI release authorization re-verified (${AI_RUN_ID})"
# --- END CURRENT-ROUND AI RELEASE CAPABILITY ---------------------------------

# gh OAuth token → GH_TOKEN (headless), like the enrich cron.
TOKEN="$(gh auth token 2>/dev/null || true)"
[ -z "${TOKEN}" ] && TOKEN="$(cat "${HOME}/.config/github/token" 2>/dev/null || true)"
if [ -z "${TOKEN}" ]; then
    log "[fatal] no GitHub token (gh auth / token file both empty)"
    exit 2
fi
export GH_TOKEN="${TOKEN}"

# The fork-only rule, enforced HERE because GitHub Actions does not run on this
# repo. Measured 2026-07-29: `actions/runs` -> total_count 0, for every workflow
# including the pre-existing image-version-sync, while `actions/permissions`
# reports enabled. So `.github/workflows/fork-only.yml` is registered and never
# fires, and a rule enforced only there would be enforced nowhere.
#
# This tick already runs from the checked-out repo on a schedule, so it is the
# one place the checks reliably execute. It does NOT stop upstream tracking:
# that remains useful even when a pin disagrees with itself. It DOES participate
# in the explicit [blocking] release authorization below, after the merge survey
# has completed, so a bad source declaration can never reach daily_release.
GUARD_OUT="${LOG_DIR}/source-guards.txt"
: > "${GUARD_OUT}"
guard_rc=0
# check_doc_counts.py gets `--online` HERE and nowhere else. Two of the README's
# counts are properties of the GitHub org (its fork count, and how many distinct
# upstreams those forks have) — no checkout can verify them, and they are the two
# most likely to rot, since forking one repo changes both. This tick already holds
# a `gh` token and already runs daily, so it is the only place that can notice.
# The PR workflow runs the same checker offline, where those rows report as
# unverified rather than passing.
for g in check_fork_only.py check_pins_agree.py check_dockerfile_layer_budget.py check_doc_counts.py check_fork_presence_claims.py; do
    case "${g}" in
        check_doc_counts.py) extra=(--online) ;;
        *)                   extra=() ;;
    esac
    if [ -f "${DIR}/../tools/${g}" ]; then
        log "[guard] ${g} ${extra[*]}"
        if ! python3 "${DIR}/../tools/${g}" "${extra[@]}" >>"${GUARD_OUT}" 2>&1; then
            guard_rc=1
        fi
    else
        # A missing guard is not a passing guard. If tools/ moves or is renamed,
        # this must say so rather than quietly checking nothing.
        echo "MISSING: tools/${g} — nothing was checked" >> "${GUARD_OUT}"
        guard_rc=1
    fi
done
if [ "${guard_rc}" != "0" ]; then
    log "[guard] FAILED — the image would build from a source we do not control,"
    log "[guard] or a pin disagrees with itself. Details:"
    sed 's/^/[guard]   /' "${GUARD_OUT}" | tee -a "${LOG}"
else
    log "[guard] source guards clean"
    grep -E "pending|DATED" "${GUARD_OUT}" | sed 's/^/[guard]   /' | tee -a "${LOG}" || true
fi

# vibe-ic#553 — the OTHER direction. Everything below asks "should we adopt this
# upstream commit?"; this asks "is upstream carrying a fix for a bug we still
# ship?" Those are different questions and only the first was wired. Surfaced by
# vibe-ic#551: an rsz::stitchTrees segfault we were about to file upstream had
# been fixed there on 2026-07-13, 772 commits before our pin.
#
# Reporting only — it never adopts, and it does NOT gate the tick, for the same
# reason the source guards do not: a survey that cannot run must not stop the
# fork monitoring.
# ---------------------------------------------------------------------------
# THE DAILY DELIVERY CHAIN (owner rulings, 2026-07-29)
#
#   "daily merge all new commit from upstream for forked tools" / "每天自動合併、
#    不等人"  and  "只要有任何一個工具有新版，我們的 image 也就重新再出一版 …
#    最晚會在隔天早上的 5 點半，提供一個 daily 的新版 updated docker image"
#
# Four hops, and every one of them has been the place the work stopped:
#
#   upstream -> fork branch   daily_merge.py       merge everything, no PR
#   fork branch -> pin        daily_release.py     move the pin at all 3 sites
#   pin -> tool artefact      daily_release.py     rebuild what changed
#   artefact -> image         daily_release.py     recompose + cut a version
#
# On 2026-07-29 `vibeic/yosys#2` was merged and every run that day still used the
# pre-merge yosys, because hop 2 never happened and nothing looked. Running these
# in the tick is what makes the rulings take effect; until this block existed
# they were a policy nobody executed.
#
# NEITHER GATES THE TICK. A fork that conflicts is left at its previous tip and
# reported; upstream tracking and the gatekeeper round still run. What must never
# happen is a SILENT skip, so both write their own log and a non-zero exit is
# carried into the tick's exit code rather than discarded.
MERGE_OUT="${LOG_DIR}/daily-merge.txt"
merge_rc=0
# Reuse below is permitted only from evidence produced by THIS invocation. A
# crash or missing program must expose absence, not yesterday's green row.
rm -f "${LOG_DIR}/daily-merge.json"
if [ -f "${DIR}/daily_merge.py" ]; then
    log "[merge] merging all upstream commits into every fork"
    python3 "${DIR}/daily_merge.py" --json "${LOG_DIR}/daily-merge.json" \
        > "${MERGE_OUT}" 2>&1
    merge_rc=$?
    head -1 "${MERGE_OUT}" | sed 's/^/[merge]   /' | tee -a "${LOG}"
    grep -E "CONFLICT|NEEDS_HUMAN|FAILED|NO_UPSTREAM" "${MERGE_OUT}" \
        | sed 's/^/[merge]   /' | tee -a "${LOG}" || true
else
    echo "MISSING: fork-gatekeeper/daily_merge.py — nothing was merged" > "${MERGE_OUT}"
    log "[merge] MISSING — nothing was merged, which is not a clean result"
    merge_rc=1
fi

# --- PRE-RELEASE BLOCKING GATE ------------------------------------------------
#
# Run the forks' declared checks ONCE, after daily_merge has updated the clone
# tips and before daily_release is even invoked.  These are the same declarations
# and runner used by daily_merge's merge-triggered checks; this daily pass covers
# ALREADY_CURRENT forks and our own patches without introducing a second suite.
#
# THREE STATES. rc=2 is COULD-NOT-CHECK (including a stalled semantic pytest
# progress lease), not a pass.  Both red and no-record block release.  The source
# guards above are the other input to this authorization.  Neither condition
# suppresses merge/reporting work: it suppresses only the irreversible delivery
# path. A non-clean merge round participates too: a partial fleet update is
# useful evidence for the report, but it is not authorization to cut an image.
#
# Do not run an expensive declaration twice.  A MERGED daily_merge row means
# every declared check completed on the exact merged worktree and the push then
# succeeded. Reuse that evidence; ask check_fork_selftests only about the tools
# daily_merge could not cover (chiefly ALREADY_CURRENT forks). Any malformed or
# incomplete reuse record fails open toward *running* the checks, never toward
# passing them.
SELFTEST_ARGS=()
selftest_plan_rc=1
selftest_reused_checks=0
selftest_reused_tools=""
if [ -f "${DIR}/FORKS.json" ] && [ -s "${LOG_DIR}/daily-merge.json" ]; then
    SELFTEST_PLAN="$(python3 - "${DIR}/FORKS.json" \
        "${LOG_DIR}/daily-merge.json" <<'PY'
import json
import sys

registry = json.load(open(sys.argv[1], encoding="utf-8"))
merge = json.load(open(sys.argv[2], encoding="utf-8"))
if merge.get("program") != "daily_merge" or not isinstance(merge.get("forks"), list):
    raise ValueError("not a complete daily_merge record")

declared = {}
for fork in registry.get("forks", []):
    checks = fork.get("post_merge_check")
    if checks:
        if not isinstance(checks, list) or not fork.get("tool"):
            raise ValueError("malformed post_merge_check declaration")
        declared[fork["tool"]] = checks
if not declared:
    raise ValueError("no declared fork checks")

rows = {}
for row in merge["forks"]:
    repo = row.get("repo") if isinstance(row, dict) else None
    if not repo or repo in rows:
        raise ValueError("missing or duplicate daily_merge repo row")
    rows[repo] = row

needed = []
reused_tools = []
reused_checks = 0
for tool, declarations in declared.items():
    row = rows.get(tool, {})
    observed = row.get("checks") if isinstance(row, dict) else None
    declared_names = [str(c.get("name") or c.get("path") or "unnamed")
                      for c in declarations if isinstance(c, dict)]
    observed_names = [str(c.get("name")) for c in observed] \
        if isinstance(observed, list) else []
    reusable = (
        row.get("state") == "MERGED"
        and len(declared_names) == len(declarations)
        and observed_names == declared_names
        and all(c.get("ok") is True and c.get("outcome") == "natural"
                for c in observed)
    )
    if reusable:
        reused_tools.append(tool)
        reused_checks += len(observed)
    else:
        needed.append(tool)

print(f"REUSED_CHECKS\t{reused_checks}")
print("REUSED_TOOLS\t" + ",".join(reused_tools))
for tool in needed:
    print("TOOL\t" + tool)
PY
)"
    selftest_plan_rc=$?
    if [ "${selftest_plan_rc}" = "0" ]; then
        while IFS=$'\t' read -r kind value; do
            case "${kind}" in
                REUSED_CHECKS) selftest_reused_checks="${value:-0}" ;;
                REUSED_TOOLS)  selftest_reused_tools="${value:-}" ;;
                TOOL)          SELFTEST_ARGS+=(--tool "${value}") ;;
            esac
        done <<< "${SELFTEST_PLAN}"
    fi
fi

SELFTEST_OUT="${LOG_DIR}/fork-selftests.txt"
selftest_rc=0
# Same freshness rule: red/no-record today must not leave yesterday's complete
# green sidecar for the LL/report consumer to mistake for current evidence.
rm -f "${LOG_DIR}/fork-selftests.json"
if [ -f "${DIR}/check_fork_selftests.py" ]; then
    if [ "${selftest_plan_rc}" = "0" ] && [ "${#SELFTEST_ARGS[@]}" = "0" ]; then
        printf '  PASS            reused %s daily_merge check(s)\n\nfork self-checks: %s declared, %s pass, 0 FAIL, 0 COULD-NOT-CHECK\n' \
            "${selftest_reused_checks}" "${selftest_reused_checks}" \
            "${selftest_reused_checks}" > "${SELFTEST_OUT}"
        python3 - "${LOG_DIR}/fork-selftests.json" \
            "${selftest_reused_checks}" "${selftest_reused_tools}" <<'PY'
import json
import sys
from pathlib import Path

count = int(sys.argv[2])
Path(sys.argv[1]).write_text(json.dumps({
    "declared": count,
    "failed": 0,
    "could_not_check": 0,
    "results": [],
    "reused_from_daily_merge": {
        "checks": count,
        "tools": [x for x in sys.argv[3].split(",") if x],
    },
}, indent=2) + "\n", encoding="utf-8")
PY
        selftest_rc=$?
    else
        SELFTEST_CMD=(python3 "${DIR}/check_fork_selftests.py"
            --json "${LOG_DIR}/fork-selftests.json"
            --log-dir "${LOG_DIR}")
        if [ "${selftest_plan_rc}" = "0" ]; then
            SELFTEST_CMD+=("${SELFTEST_ARGS[@]}")
        fi
        "${SELFTEST_CMD[@]}" > "${SELFTEST_OUT}" 2>&1
        selftest_rc=$?
    fi
    if [ "${selftest_reused_checks}" != "0" ]; then
        log "[selftest] reused ${selftest_reused_checks} green check(s) from daily_merge candidate worktrees (${selftest_reused_tools})"
        # Keep the machine-readable denominator honest when this run combined
        # fresh daily checks with reused candidate-worktree checks. The reused
        # rows remain in daily-merge.json; this sidecar names that provenance
        # rather than copying and subtly changing their schema.
        if [ "${#SELFTEST_ARGS[@]}" != "0" ]; then
            python3 - "${LOG_DIR}/fork-selftests.json" \
                "${selftest_reused_checks}" "${selftest_reused_tools}" <<'PY' || \
                log "[selftest] could not annotate reused evidence in fork-selftests.json"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
record = json.loads(path.read_text(encoding="utf-8"))
count = int(sys.argv[2])
record["declared"] = int(record.get("declared", 0)) + count
record["reused_from_daily_merge"] = {
    "checks": count,
    "tools": [x for x in sys.argv[3].split(",") if x],
    "record": "daily-merge.json",
}
path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
PY
        fi
    fi
    grep -E "^  (PASS|FAIL|COULD-NOT-CHECK) |^      -> |^fork self-checks:" "${SELFTEST_OUT}" \
        | sed 's/^/[selftest] /' | tee -a "${LOG}" || true
    [ "${selftest_rc}" = "1" ] && log "[selftest] a fork's own check is RED; daily_release is blocked (vibe-ic#813)"
    [ "${selftest_rc}" = "2" ] && log "[selftest] a registered fork check formed NO RECORD; daily_release is blocked (vibe-ic#813)"
else
    echo "MISSING: ${DIR}/check_fork_selftests.py — the forks' own checks were NOT run" > "${SELFTEST_OUT}"
    log "[selftest] nothing was checked; daily_release is blocked"
    selftest_rc=2
fi
# --- END PRE-RELEASE SELFTEST -------------------------------------------------

blocking_rc=0
[ "${guard_rc}" != "0" ] && blocking_rc=1
[ "${merge_rc}" != "0" ] && blocking_rc=1
[ "${selftest_rc}" != "0" ] && blocking_rc=1

RELEASE_OUT="${LOG_DIR}/daily-release.txt"
if [ "${blocking_rc}" != "0" ]; then
    # Do not leave yesterday's successful sidecar looking like evidence from a
    # release that was never authorized today.
    rm -f "${LOG_DIR}/daily-release.json"
    echo "BLOCKED: source guards, daily merge, or fork self-checks are not green; daily_release was not invoked" \
        > "${RELEASE_OUT}"
    log "[blocking] REFUSE daily_release — source guards, daily merge, and fork self-checks must all be green"
    # This is not a daily_release failure: no release was attempted.  The
    # originating guard/selftest rc is folded into the final tick status below,
    # preserving the useful diagnosis instead of replacing it with rc=5.
    release_rc=0
elif [ -f "${DIR}/daily_release.py" ]; then
    log "[blocking] PASS — source guards, daily merge, and fork self-checks authorize candidate construction"
    # The check_no_capability_lost block later in this file observes a released
    # image. Moving it here would test yesterday's tag and falsely call that
    # candidate evidence. The exact candidate's authority remains inside
    # daily_release: depth and every tool smoke must pass before docker push.
    log "[release] moving pins to their fork tips, rebuilding what changed"
    python3 "${DIR}/daily_release.py" --json "${LOG_DIR}/daily-release.json" \
        > "${RELEASE_OUT}" 2>&1
    release_rc=$?
    head -1 "${RELEASE_OUT}" | sed 's/^/[release]   /' | tee -a "${LOG}"
    grep -E "VERSION|building|composing|UNRESOLVED|FAIL" "${RELEASE_OUT}" \
        | sed 's/^/[release]   /' | tee -a "${LOG}" || true
    if [ "${release_rc}" != "0" ]; then
        # vibeic-eda#96: head -1 above is right for a PASSING stage and wrong
        # for a FAILING one -- on 2026-07-31 and 2026-08-05 it showed only a
        # summary line truncated to a bare colon, while the actual
        # "ERROR: failed to solve" sat ~130 lines into the same file, unread.
        "${DIR}/release_failure_summary.sh" "${RELEASE_OUT}" \
            | sed 's/^/[release]   /' | tee -a "${LOG}" || true
        # Adjacent, same defect family: daily-release.txt is overwritten by
        # the NEXT round, so a failure's full evidence survives only until
        # tomorrow's 05:30 unless copied out now. Bounded retention (last 5)
        # -- release failures are rare and cluster in bursts, not unbounded.
        FAIL_ARCHIVE_DIR="${LOG_DIR}/failed-releases"
        mkdir -p "${FAIL_ARCHIVE_DIR}"
        cp "${RELEASE_OUT}" "${FAIL_ARCHIVE_DIR}/$(date -u +%Y-%m-%dT%H%M%SZ).txt"
        ls -1t "${FAIL_ARCHIVE_DIR}"/*.txt 2>/dev/null | tail -n +6 | xargs -r rm -f
        log "[release] full output archived to ${FAIL_ARCHIVE_DIR} (the next round overwrites daily-release.txt)"
    fi
else
    echo "MISSING: fork-gatekeeper/daily_release.py — nothing was released" \
        > "${RELEASE_OUT}"
    log "[release] MISSING — no image was cut, which is not a clean result"
    release_rc=1
fi

# Does the tool the flow INVOKES come from the artefact we built? Every other
# gate stops at "the image contains our tool", which is a different claim and was
# true for months while verilator ran the base image's April build (#18) and
# klayout's LEF/DEF plugin was loaded by nothing but svrfdrc (#17).
#
# REPORT-ONLY on purpose. #17 and #18 are open and would fail it; a gate whose
# first act is to stop every daily release is a gate someone switches off. It
# takes --strict once they are closed.
# RE-RUN THE PIN GUARD, because `daily_release` is the thing that writes pins.
# The guards at the top of this tick read the tree as it was BEFORE the release
# moved anything; a disagreement introduced by the release itself would go
# unnoticed until the next morning. That is not hypothetical — `check_pins_agree`
# went red on main today because the artefact tag gained a RECIPE component and
# the checker still composed the old two-part tag, and it was a PR that found it,
# not this tick.
#
# Report-only: the image is already built and published by this point, so failing
# here would not un-publish it. What it buys is knowing the same day.
POSTPIN_OUT="${LOG_DIR}/pins-after-release.txt"
if [ -f "${DIR}/../tools/check_pins_agree.py" ]; then
    if python3 "${DIR}/../tools/check_pins_agree.py" > "${POSTPIN_OUT}" 2>&1; then
        log "[pins-after] $(tail -1 "${POSTPIN_OUT}")"
    else
        log "[pins-after] DISAGREEMENT introduced by this release — details in ${POSTPIN_OUT}"
        grep -E "disagrees|pulls|pins " "${POSTPIN_OUT}" | head -6 | sed 's/^/[pins-after]   /' | tee -a "${LOG}" || true
    fi
else
    echo "MISSING: tools/check_pins_agree.py — nothing was re-checked" > "${POSTPIN_OUT}"
    log "[pins-after] MISSING — nothing was re-checked, which is not a clean result"
fi

# --- IS THE LAST RELEASE'S PIN MOVE COMMITTED? (vibeic-eda#99) ---
#
# `daily_release.write_released_record` measures `RELEASED.json`'s pins from
# the WORKING TREE, after `rewrite_pin` has already edited it there.
# `commit_release_record` then commits exactly `VERSION` / `RELEASED.json` /
# `README.md` — never the pin files, on purpose (#71's revert dda4b8c,
# `test_only_the_two_record_files_are_committed` is LOAD-BEARING and stays: a
# release commit sweeping in an unreviewed pin move is what `git add -A` is
# banned in this org to prevent). So the ORDINARY shape of a release leaves
# `RELEASED.json` naming a pin the working tree holds and HEAD does not —
# and every check above this line, including `[pins-after]`, reads the
# working tree, so all of them read clean through the whole gap.
#
# Observed directly (#99's own report): after 0.2.65 published, HEAD kept
# stating `OPENROAD_REF=f396ce8ee` while the image had been built from
# `b64a496b9`, and nothing anywhere said so.
#
# THIS reads RELEASED.json's recorded pins against the pins actually
# COMMITTED at HEAD (`git show HEAD:<path>`, never the filesystem) — the one
# comparison an uncommitted edit cannot fool, because it never reads the copy
# the edit lives in.
#
# NON-FATAL, on the same footing as [claims]/[oracle]/[ci-ran]: closing the
# gap is the gatekeeper's review to make by committing the pin file(s) by
# explicit path, and no tick can do that FOR itself without reintroducing the
# exact defect #71's revert protects against — see the note on scope in #99.
# Loud in the log and the JSON either way, so the outstanding review cannot go
# unseen the way the #71 gap did.
RELPIN_OUT="${LOG_DIR}/release-pins-committed.txt"
RELPIN_JSON="${LOG_DIR}/release-pins-committed.json"
relpin_rc=0
if ! rm -f -- "${RELPIN_JSON}"; then
    echo "COULD-NOT-TELL: stale ${RELPIN_JSON} could not be cleared" > "${RELPIN_OUT}"
    log "[release-pins] stale sidecar could not be cleared, which is not a clean result"
    relpin_rc=2
elif [ -f "${DIR}/check_release_pins_committed.py" ]; then
    python3 "${DIR}/check_release_pins_committed.py" \
        --round-at "${AI_ROUND_AT}" --run-id "${AI_RUN_ID}" \
        --json "${RELPIN_JSON}" > "${RELPIN_OUT}" 2>&1
    relpin_rc=$?
    grep -E "^\[(PASS|FAIL)\]|^  FAIL " "${RELPIN_OUT}" | sed 's/^/[release-pins]   /' | tee -a "${LOG}" || true
    [ "${relpin_rc}" = "1" ] && log "[release-pins] a released pin is uncommitted at HEAD — the gatekeeper's review is outstanding (vibeic-eda#99)"
    [ "${relpin_rc}" = "2" ] && log "[release-pins] could not tell — RELEASED.json is absent or unparseable, which is NOT a pass (vibeic-eda#99)"
else
    echo "MISSING: ${DIR}/check_release_pins_committed.py — whether the last release's pins are committed was NOT checked" > "${RELPIN_OUT}"
    log "[release-pins] nothing was checked, which is not a clean result"
    relpin_rc=2
fi

# Derived ONCE, before every consumer. It used to be computed inside the
# `if [ -f fork_reaches_flow_check.py ]` branch while two later checks read it —
# so a missing checker left REACH_IMG unset, and under this script's `set -u`
# that is not a silent skip but an unbound-variable abort of the whole tick.
# A variable that three checks depend on does not belong inside one of them.
REL_VER="$(python3 -c 'import json;print(json.load(open("'"${DIR}"'/../RELEASED.json"))["version"])' 2>/dev/null || true)"
REACH_IMG=""
[ -n "${REL_VER}" ] && REACH_IMG="ghcr.io/vibeic/vibeic-eda:${REL_VER}"

REACH_OUT="${LOG_DIR}/fork-reaches-flow.txt"
if [ -f "${DIR}/fork_reaches_flow_check.py" ]; then
    # The image RELEASED.json says we last published — not `:local`, which was
    # `eda-local`'s tag and stopped being produced when the compose started
    # tagging by version (0.2.33). The stale `:local` from before that was still
    # on this host, so the check did not report a missing image: it quietly
    # inspected one from hours earlier and reported it as current. `--json` and
    # the provenance comparison are only meaningful against a FRESH image, which
    # is precisely what that made them not be.
    :  # REACH_IMG is derived above, before any of its consumers
    if [ -n "${REACH_IMG}" ] && docker image inspect "${REACH_IMG}" >/dev/null 2>&1; then
        log "[reach] checking the composed image runs OUR builds"
        python3 "${DIR}/fork_reaches_flow_check.py" "${REACH_IMG}" \
            --json "${LOG_DIR}/fork-reaches-flow.json" > "${REACH_OUT}" 2>&1 || true
        head -1 "${REACH_OUT}" | sed 's/^/[reach]   /' | tee -a "${LOG}"
        grep -E "resolves outside|not on PATH" "${REACH_OUT}" \
            | sed 's/^/[reach]   /' | tee -a "${LOG}" || true
    else
        # No composed image is not a pass. Say which image was missing.
        echo "MISSING IMAGE: ${REACH_IMG} — nothing was checked" > "${REACH_OUT}"
        log "[reach] ${REACH_IMG} absent — nothing was checked, not a clean result"
    fi
else
    echo "MISSING: fork-gatekeeper/fork_reaches_flow_check.py" > "${REACH_OUT}"
    log "[reach] MISSING — nothing was checked, which is not a clean result"
fi

INBOUND_OUT="${LOG_DIR}/inbound-survey.txt"
if [ -f "${DIR}/inbound_survey.py" ]; then
    log "[inbound] surveying upstream fixes our pins lack"
    python3 "${DIR}/inbound_survey.py" \
        --json "${LOG_DIR}/inbound-survey.json" > "${INBOUND_OUT}" 2>&1
    inbound_rc=$?
    # `inbound_survey.py` already distinguishes RC_PARTIAL (1 — some forks could
    # not be surveyed, the REST were) from RC_NOTHING (2). This branch treated
    # both as failure and printed only "survey FAILED", discarding the report it
    # had just written to disk.
    #
    # MEASURED on the 2026-07-30 05:51 tick: two forks lack an upstream parent
    # (cadical, kissat), so rc was 1 — and a complete 44-line survey of the other
    # 14 tools went unread. It named 4 pins behind upstream including 20 slang
    # fixes, among them a parser crash on invalid checker port connections, and a
    # memory-safety fix in xschem. Nothing was hidden; the one line a reader sees
    # said the whole thing failed, so nobody looked at the file (vibeic-eda#34).
    #
    # A partial result is a result. What must not be lost is WHICH part failed,
    # so the errors are printed too rather than the report replacing them.
    if [ "${inbound_rc}" != "2" ]; then
        # THE HEADLINE BY NAME, not `head -1` (vibeic-eda#101). This file is
        # `> out 2>&1`, and stderr is unbuffered while stdout is block-buffered, so
        # the first LINE of it is routinely an error row — measured on the
        # 2026-08-06 tick, where all three stderr rows precede the headline. The
        # daily log has therefore been showing a per-fork error where it says it is
        # showing the summary, which is the same shape as everything else this
        # issue is about: a value that answers the question NEXT TO the one asked.
        grep -m1 "^inbound_survey:" "${INBOUND_OUT}" | sed 's/^/[inbound]   /' \
            | tee -a "${LOG}" || true
        # `UNMEASURABLE:` (with the colon — the STDOUT summary line, not the
        # per-fork stderr rows, which the PARTIAL branch below prints) joins the
        # pattern: the survey now states the count of forks it could not ask about
        # beside the number they are not part of, and a summary line nobody greps
        # for is a summary line nobody sees.
        grep -E "SAMPLED|NOTE:|UNMEASURABLE:|behind +[1-9]" "${INBOUND_OUT}" \
            | sed 's/^/[inbound]   /' | tee -a "${LOG}" || true
    fi
    if [ "${inbound_rc}" = "1" ]; then
        log "[inbound] survey PARTIAL — the forks below are UNMEASURABLE, not zero; every other tool IS reported above"
        grep -E "UNMEASURABLE \(ERROR\)" "${INBOUND_OUT}" | head -5 | sed 's/^/[inbound]   /' \
            | tee -a "${LOG}" || true
    elif [ "${inbound_rc}" != "0" ]; then
        log "[inbound] survey produced NOTHING (rc=${inbound_rc}) — details in ${INBOUND_OUT}"
    fi
else
    # Same rule as the guards above: a missing survey is not an empty gap.
    echo "MISSING: fork-gatekeeper/inbound_survey.py — nothing was surveyed" \
        > "${INBOUND_OUT}"
    log "[inbound] MISSING — nothing was surveyed, which is not a clean result"
fi

# `tools/check_image_provenance.py` is wired ONLY into .github/workflows/release.yml,
# and that workflow has never run — `actions/runs` reports total_count 0 for this
# repo, with all three workflows `state=active` (vibe-ic#550). A guard that
# shipped, is documented, behaves correctly, and executes nowhere is this repo's
# own failure applied to a gate. The tick is the one thing that reliably runs,
# which is the same reasoning that moved the source guards here.
# Did we take something AWAY? Every other check here asks what we added — that
# sources are ours, that a pin is coherent, that the flow runs our build. Three
# times this week a replacement removed a co-tenant instead: eqy/mcy and sby with
# the yosys prefix (#19), the yices solvers with the same rm -rf (#25), and the
# slang plugin left behind by a yosys upgrade (#24). Nothing was looking.
#
# BLOCKING (vibeic-eda#88). This ran with a trailing `|| true` until 2026-08-05,
# so it printed `LOST:` lines and the round carried on — a check that LOOKS like
# a gate and is not one, which is worse than no gate because the log reads clean
# to anyone scanning for a failure. The shape copied here is `capability_gate.py`,
# which is wired correctly: regression.json -> image_build.cmd -> gatekeeper.py
# -> build_and_regress.sh, where rc!=0 emits `built_red` and nothing is promoted.
#
# rc=2 BLOCKS EXACTLY LIKE rc=1, in both the program and this call site. rc=2 is
# "the image could not be probed" — nothing was compared, and measuring nothing
# proves nothing. The same rule already governs [ship] (rc 6) and the source
# guards (rc 3) in this file; the `|| true` here was the outlier.
#
# The status is captured on the SAME line as the command, before `head`/`grep`
# can overwrite $?. Writing `python3 ... ; cap_rc=$?` on the next line after a
# pipe is how build_and_regress.sh's first draft read the status of the wrong
# process, and this file's own `[ship]` block spells the rule out.
CAP_OUT="${LOG_DIR}/capability-lost.txt"
cap_rc=0
if [ -f "${DIR}/check_no_capability_lost.py" ] && [ -n "${REACH_IMG}" ]; then
    log "[capability] ${REACH_IMG}"
    python3 "${DIR}/check_no_capability_lost.py" "${REACH_IMG}" \
        --json "${LOG_DIR}/capability-lost.json" > "${CAP_OUT}" 2>&1; cap_rc=$?
    head -1 "${CAP_OUT}" | sed 's/^/[capability]   /' | tee -a "${LOG}"
    grep "LOST:" "${CAP_OUT}" | sed 's/^/[capability]   /' | tee -a "${LOG}" || true
    [ "${cap_rc}" != "0" ] && log "[capability] rc=${cap_rc} — a command the base image provided no longer resolves in ours, or the image could not be probed at all (vibeic-eda#88)"
else
    echo "MISSING: check_no_capability_lost.py or no released image — nothing was checked" \
        > "${CAP_OUT}"
    log "[capability] nothing was checked, which is not a clean result"
    # Same rule as the branch above: a checker that is not there, or a release
    # with no image to probe, compared nothing. That is rc=2's meaning, so it
    # gets rc=2 rather than the silence it got before.
    cap_rc=2
fi

PROV_OUT="${LOG_DIR}/image-provenance.txt"
if [ -f "${DIR}/../tools/check_image_provenance.py" ] && [ -n "${REACH_IMG}" ]; then
    log "[provenance] ${REACH_IMG}"
    python3 "${DIR}/../tools/check_image_provenance.py" "${REACH_IMG}" \
        > "${PROV_OUT}" 2>&1 || true
    tail -1 "${PROV_OUT}" | sed 's/^/[provenance]   /' | tee -a "${LOG}"
else
    echo "MISSING: tools/check_image_provenance.py or no released image — nothing was checked" \
        > "${PROV_OUT}"
    log "[provenance] nothing was checked, which is not a clean result"
fi

# --- NOTHING UNTRACKED AND UNIGNORED (2026-07-30) ---
# Sixteen files sat here untracked-and-unignored for eight to eleven days —
# `.bak` copies of the Dockerfile and the fork ledgers, build logs from a failed
# 0.2.27 attempt, an ALIGN dump. That is the state one `git add -A` turns into a
# commit, which is why this repo forbids `-A`; the prohibition is a rule
# everyone must remember, and this is the rule nobody has to.
#
# `.gitignore` already carried `*.log` and did not match
# `build_0.2.27.log.attempt1` — `.log` is not at the end. A rule written for the
# case its author met, which is why the guard checks the OUTCOME.
#
# Non-fatal: the tick's job is upstream tracking, and a stray file must not stop
# it. Loud in the log; the guard names paths and never deletes.
UNTRACKED_OUT="${LOG_DIR}/untracked-artefacts.txt"
if [ -f "${DIR}/untracked_artefact_guard.py" ]; then
    python3 "${DIR}/untracked_artefact_guard.py" > "${UNTRACKED_OUT}" 2>&1
    untracked_rc=$?
    tail -1 "${UNTRACKED_OUT}" | sed 's/^/[untracked]   /' | tee -a "${LOG}"
    [ "${untracked_rc}" != "0" ] && log "[untracked] rc=${untracked_rc} — ignore them or remove them; the guard will not choose"
else
    echo "MISSING: fork-gatekeeper/untracked_artefact_guard.py — nothing was checked" \
        > "${UNTRACKED_OUT}"
    log "[untracked] nothing was checked, which is not a clean result"
fi

# --- WHAT THE IMAGE SAYS ABOUT ITSELF (vibeic-eda#28) ---
# PDKS.json declares every PDK the image ships and where its version can be read
# back OUT of the image. This is the only guard with jurisdiction over them: every
# other one checks what we CLONE, and the PDKs arrive pre-installed in the base,
# so they are invisible to all of them by construction. open_pdks produces the
# libs.tech trees every DRC and LVS verdict is computed against.
#
# I LANDED THIS CHECK AND DID NOT WIRE IT. It has existed since 6f04ab6 and has
# never run — a gate that exists and is not called is the same as no gate, which
# is the defect it was written to prevent, one level up. Found by sweeping every
# program this session added for whether anything actually invokes it.
#
# Non-fatal like its neighbours: the tick's job is upstream tracking, and one
# undeclared PDK must not stop that. Loud in the log, and a missing checker
# reports rather than passes.
CLAIMS_OUT="${LOG_DIR}/image-claims.txt"
if [ -f "${DIR}/check_image_claims.py" ] && [ -n "${REACH_IMG}" ]; then
    log "[claims] ${REACH_IMG}"
    python3 "${DIR}/check_image_claims.py" "${REACH_IMG}" \
        > "${CLAIMS_OUT}" 2>&1
    claims_rc=$?
    tail -1 "${CLAIMS_OUT}" | sed 's/^/[claims]   /' | tee -a "${LOG}"
    [ "${claims_rc}" != "0" ] && log "[claims] rc=${claims_rc} — a PDK the image ships is undeclared, or a declared one is gone"
else
    echo "MISSING: fork-gatekeeper/check_image_claims.py or no released image — nothing was checked" \
        > "${CLAIMS_OUT}"
    log "[claims] nothing was checked, which is not a clean result"
fi

# --- DID CI RUN AT ALL? (vibe-ic#550, extended to this repo 2026-07-29) ---
# #550 was filed against vibe-ic: Actions disabled at the ACCOUNT level, 561
# commits landed with no CI, and nothing noticed for nine days because
# `gh run list` prints nothing for "never ran" and nothing for "no match".
#
# Measured here, same account, same day:
#
#     vibeic/vibe-ic      2 runs   (both Dependency Graph, none CI)
#     vibeic/vibeic-eda   0 runs   3 workflows, all `active`
#     gh workflow run fork-only.yml -> HTTP 422:
#         "Actions has been disabled for this user."
#
# So this repo is under the same block, has three workflows that have never
# executed, and — unlike vibe-ic, which gained `ci_ran_at_all_check` in v1.8.2 —
# had NO gate that could notice. The fork-only workflow landed earlier today is
# in exactly that position: present, active, and never once run.
#
# The checker is REUSED from the plugin rather than reimplemented. A second
# implementation of "did CI run" is how two programs come to disagree about the
# same question, which is the defect vibeic-eda#29 was about.
#
# Non-fatal here BY DESIGN: the block is account-level and no tick can clear it,
# so failing the tick would turn a standing owner-action into daily noise. It is
# logged loudly, and rc 2 (could not look) stays distinct from rc 1 (no run).
# Defined BEFORE the branch that reads it. This script runs under `set -u`,
# and line 200 already records what that costs: a variable assigned only
# inside a conditional and read outside aborts the whole tick rather than
# skipping a step. Same shape, same file, so it gets the same treatment.
CI_OUT="${LOG_DIR}/ci-ran-at-all.txt"
CI_CHECK="${VIBE_IC_PROGRAMS:-/home/reyerchu/vibe-ic/vibe-ic-marketplace/plugins/vibe-ic/programs}/ci_ran_at_all_check.py"
if [ -f "${CI_CHECK}" ]; then
    python3 "${CI_CHECK}" "${DIR}/.." > "${CI_OUT}" 2>&1
    ci_rc=$?
    tail -1 "${CI_OUT}" | sed 's/^/[ci-ran]   /' | tee -a "${LOG}"
    [ "${ci_rc}" != "0" ] && log "[ci-ran] rc=${ci_rc} — see vibe-ic#550; the tick does not fail on it because no tick can re-enable Actions"
else
    echo "MISSING: ${CI_CHECK} — whether CI ran was NOT checked" > "${CI_OUT}"
    log "[ci-ran] nothing was checked, which is not a clean result"
fi

# vibeic-eda#35 — is the image the union of the forks' masters?
#
# `check_our_commits_ship.py` answers exactly the question that issue asks, it
# has been correct since it was written, and NOTHING CALLED IT. It reported
# nothing for as long as it existed; the 36-stranded state it was built to
# detect went unnoticed for precisely that reason. A check that exists, is
# right, and is never invoked is indistinguishable from one that was never
# written — the same "absence rendering as a pass" this tick keeps finding
# elsewhere.
#
# It compares by PATCH EQUIVALENCE (`git cherry`), not by SHA reachability: the
# same fix cherry-picked onto a dozen branches is one fix, and counting SHAs
# overcounted it 136-fold when this was first measured.
#
# FAILS the tick (rc 6), unlike the CI block above. The distinction is who can
# act: an account-level Actions block is a standing owner action no tick can
# clear, so failing on it would be daily noise. A commit of ours that no build
# branch reaches IS this tick's job — the merge step it runs is what puts it
# there — so a non-zero here means the round did not finish its own work.
SHIP_OUT="${LOG_DIR}/our-commits-ship.txt"
ship_rc=0
if [ -f "${DIR}/check_our_commits_ship.py" ]; then
    python3 "${DIR}/check_our_commits_ship.py" \
        --json "${LOG_DIR}/our-commits-ship.json" > "${SHIP_OUT}" 2>&1
    ship_rc=$?
    tail -1 "${SHIP_OUT}" | sed 's/^/[ship]   /' | tee -a "${LOG}"
    grep -E "STRANDED|NOT reachable" "${SHIP_OUT}" | sed 's/^/[ship]   /' | tee -a "${LOG}" || true
    [ "${ship_rc}" != "0" ] && log "[ship] rc=${ship_rc} — a commit of ours is not reachable from the branch its pin names (vibeic-eda#35)"
else
    echo "MISSING: ${DIR}/check_our_commits_ship.py — whether our commits ship was NOT checked" > "${SHIP_OUT}"
    log "[ship] nothing was checked, which is not a clean result"
    ship_rc=2
fi

# vibeic-eda#60 — the mirror image of the check above. `check_our_commits_ship`
# asks whether a commit of ours is reachable from the branch its PIN names;
# this asks whether a fork carrying commits of ours is pinned AT ALL.
#
# Latent today: the four unpinned forks (ciel, open_pdks, sv2v, IHP-Open-PDK)
# carry zero of our commits, so the condition is unreachable. It activates on
# the first patch, when the ledger reports ahead=1 on a fork the image does not
# build from — and a number going UP is the last place anyone looks for a
# failure. Added while it is free.
#
# Placed AFTER the block above, not inside the comment paragraph that describes
# it: the first attempt anchored on the earliest mention of
# `check_our_commits_ship.py` in this file, which is prose, and `bash -n`
# accepted the result.
UNSHIP_OUT="${LOG_DIR}/unshippable-patches.txt"
unship_rc=0
if [ -f "${DIR}/check_unshippable_patches.py" ]; then
    python3 "${DIR}/check_unshippable_patches.py" > "${UNSHIP_OUT}" 2>&1
    unship_rc=$?
    tail -1 "${UNSHIP_OUT}" | sed 's/^/[unship] /' | tee -a "${LOG}"
    [ "${unship_rc}" = "1" ] && log "[unship] a fork carries patches that cannot reach the image (vibeic-eda#60)"
else
    echo "MISSING: ${DIR}/check_unshippable_patches.py — nothing asked" > "${UNSHIP_OUT}"
    log "[unship] nothing was checked, which is not a clean result"
    unship_rc=2
fi

# vibeic-eda#93 — the patch->oracle map, asking BOTH of its questions.
#
# The hand-built map asked "does this commit SHIP a test?" and nothing else, so
# `ee778e7ced` (494 adds) was filed NO-ORACLE while it was in fact the CAUSE of
# a dead one: it changed the routed DEF that upstream's `drt:top_level_term2`
# diffs against, the golden was never regenerated, and the test has been
# permanently red since. Ship-a-test and break-a-test are opposite conditions
# with opposite remedies, and one question cannot separate them.
#
# DOES NOT FAIL THE TICK, and the reason is the same one that makes the CI block
# above non-fatal: who can act. A stale golden is cleared by a source change a
# human lands; no tick can regenerate it, so failing here would be daily noise
# on a standing state. What it must not do is stay invisible — hence the log line
# and the JSON, on every round.
#
# COULD-NOT-MEASURE is the expected majority answer and is NOT a defect of this
# check: the verdict ledgers cover the modules that were actually run. An absent
# verdict is reported as absent, never as a pass.
ORACLE_OUT="${LOG_DIR}/oracle-map.txt"
oracle_rc=0
if [ -f "${DIR}/oracle_map.py" ]; then
    OR_DIR="/home/reyerchu/vibe-ic-forks/OpenROAD"
    LEDGERS=""
    for L in "${DIR}"/ORACLE_VERDICTS.OpenROAD.*.json; do
        [ -f "${L}" ] && LEDGERS="${LEDGERS} --verdicts ${L}"
    done
    # shellcheck disable=SC2086
    python3 "${DIR}/oracle_map.py" --repo "${OR_DIR}" --head HEAD \
        --json "${LOG_DIR}/oracle-map.json" ${LEDGERS} > "${ORACLE_OUT}" 2>&1
    oracle_rc=$?
    grep -E "BROKE-EXISTING-ORACLE ·|counting:" "${ORACLE_OUT}" \
        | sed 's/^/[oracle] /' | tee -a "${LOG}" || true
    [ "${oracle_rc}" = "1" ] && log "[oracle] a commit of ours BROKE a pre-existing test — the remedy is to regenerate the golden, not to write a test (vibeic-eda#93)"
    [ "${oracle_rc}" = "2" ] && log "[oracle] the map could not be built — nothing was measured, which is not a clean result"
else
    echo "MISSING: ${DIR}/oracle_map.py — whether our commits broke an existing test was NOT asked" > "${ORACLE_OUT}"
    log "[oracle] nothing was checked, which is not a clean result"
    oracle_rc=2
fi

# --- IS A TEST WE WROTE REACHABLE BY ANY BUILD? (2026-08-12 fleet audit) ------
# The audit found the SAME defect in five forks independently, each by hand:
# magic 9, netgen 13, ngspice 12, klayout 25, OpenSTA 4 test entry points that no
# build system can reach — and sby's 33, which are now wired in (2c2f04e) and are
# the worked example. `make test` returning 0 on an inert suite is not a neutral
# non-signal, it is a green one, so a post-merge check spelled `make test` would
# report health on a fork whose entire regression suite runs nothing.
#
# Fleet-wide rather than per-fork BY DESIGN: one shape found six times is a
# program, not a thing each fork must remember. It is static and needs no build,
# which is what makes it affordable here every day.
#
# Non-fatal to the tick on its own — the known backlog is waived BY NAME in
# TEST_REACH_WAIVERS.json, so rc=1 means a NEW orphan appeared or a waiver
# outlived its truth, and both of those are worth a loud line rather than a
# silent one.
REACH_OUT="${LOG_DIR}/tests-reachable.txt"
reach_rc=0
if [ -f "${DIR}/check_tests_are_reachable.py" ]; then
    python3 "${DIR}/check_tests_are_reachable.py" \
        --json "${LOG_DIR}/tests-reachable.json" > "${REACH_OUT}" 2>&1
    reach_rc=$?
    grep -E "^test reachability:|^  (FAIL|COULD-NOT-CHECK) |^      [a-zA-Z]" "${REACH_OUT}" \
        | head -40 | sed 's/^/[reach] /' | tee -a "${LOG}" || true
    [ "${reach_rc}" = "1" ] && log "[reach] a test of ours is reachable by NO build, and is not waived by name"
    [ "${reach_rc}" = "2" ] && log "[reach] the question could not be put for at least one fork — not a pass"
else
    echo "MISSING: ${DIR}/check_tests_are_reachable.py" > "${REACH_OUT}"
    log "[reach] nothing was checked, which is not a clean result"
    reach_rc=2
fi

# The owner ruling implemented by daily_merge.py supersedes selective adoption
# in this release path: it merges the complete upstream tip, checks the merged
# candidate, and does so BEFORE daily_release builds the image. This call is
# after daily_release so its reports describe the image just published. It may
# refresh local assessment state, but it may neither move a fork nor replace the
# public page with an uncertified render. Leaving GK_MERGE_PR inheritable allowed
# the 2026-08-26 round to publish 0.3.32 and then open+land OpenROAD#8, creating
# two commits past its own image pin. Force both historical fork-write paths off;
# standalone gatekeeper runs may still opt into them explicitly. run_0530.sh
# publishes the page later, and only from the current-round LL.
export GK_MERGE_PR=0
unset GK_RUN_HARNESS
export GK_DEFER_PUBLIC_PAGE=1
log "[start] eda-fork gatekeeper assessment (fork-writes=off, public-page=deferred)"
cd "${DIR}" || exit 2
python3 gatekeeper.py >>"${LOG}" 2>&1
rc=$?
# A guard failure must not be erased by a successful tick.
[ "${guard_rc}" != "0" ] && [ "${rc}" = "0" ] && rc=3
# Nor by a merge or a release that needed a human. A tick that merged nothing and
# cut no image because a fork conflicted has not had a clean day.
[ "${merge_rc:-0}" != "0" ] && [ "${rc}" = "0" ] && rc=4
[ "${release_rc:-0}" != "0" ] && [ "${rc}" = "0" ] && rc=5
# Nor by our own commits failing to reach the branches the image builds from.
[ "${ship_rc:-0}" != "0" ] && [ "${rc}" = "0" ] && rc=6
# Nor by a capability the base image gave us and ours no longer resolves
# (vibeic-eda#88). rc=2 from that program means it could not probe the image at
# all, and it lands here for the same reason rc=1 does: nothing was compared.
[ "${cap_rc:-0}" != "0" ] && [ "${rc}" = "0" ] && rc=7
# Nor by a fork's own declared check being red — or never having run at all
# (vibe-ic#813). A tick that published an image built from a tree whose own
# regression suite is unwired has not had a clean day, and reporting 0 would say
# it did. rc=8: 7 was taken by the capability gate while this was in flight.
[ "${selftest_rc:-0}" != "0" ] && [ "${rc}" = "0" ] && rc=8
log "[done] gatekeeper tick exit ${rc}"
exit ${rc}
