#!/usr/bin/env bash
# build_and_regress.sh — the option-B image_build.cmd (owner-approved).
#
# For each candidate tool (a fork with a NEW upstream release), try to integrate it:
#   1. rebase our vibeic/* branch onto the new release tag (in a scratch worktree).
#      - tag missing        -> status "tag_missing"  (defer)
#      - rebase CONFLICT     -> abort, status "rebase_conflict" + the file (defer; our
#                               patch overlaps the upstream change — needs human review)
#      - clean               -> status "rebased_clean", new sha recorded
#   2. for the clean set: temp-push the rebased sha (so the build can fetch it), bump the
#      vibeic-eda Dockerfile ARG(s) in a SCRATCH build tree, `docker build` the image,
#      and run a smoke regression against it.
#      - build/regress green -> status "built_green"
#      - red                 -> status "built_red"   (defer)
#   3. The legacy GK_MODE=promote arm is retired. It could publish version/latest
#      but could not atomically publish RELEASED.json and read all three names
#      back. Candidate verification remains; daily_release.py is the sole final
#      image publication authority.
#
# Input : env VIBEIC_CANDIDATES = JSON [{tool,arg,branch,release}]  (set by gatekeeper.py)
# Output: writes per-candidate results JSON to $GK_RESULT (default result.json), which
#         the gatekeeper reads to set each tool's verdict.
set -uo pipefail

# Where an ARG pin lives. Before the per-tool split it was always the root
# Dockerfile; each tool now owns `tools/<tool>/Dockerfile`, and the composing
# Dockerfile carries an `IMG_<TOOL>` tag derived from that pin.
#
# This searches rather than assumes, and the callers below REFUSE to build when
# it finds nothing. `sed` reports success when it matches nothing, so an
# unchanged pin would otherwise build the OLD sha and be reported "built_green
# at <new sha>" -- a green result for a build that never contained the commit it
# names. That is the one failure mode this whole function exists to prevent.
_pin_file() {   # _pin_file <tree> <ARG name>  ->  path, or empty
    local tree="$1" arg="$2" f
    for f in "${tree}/Dockerfile" "${tree}"/tools/*/Dockerfile; do
        [ -f "$f" ] && grep -qE "^ARG ${arg}=" "$f" && { echo "$f"; return 0; }
    done
    return 1
}

# Move a pin everywhere it is written, or fail. Keeping the three sites in step
# is what `tools/check_pins_agree.py` asserts; doing it here by hand and getting
# it half-right would produce exactly the drift that check exists to catch.
_bump_pin() {   # _bump_pin <tree> <ARG name> <sha>  -> 0 moved, 1 could not
    local tree="$1" arg="$2" sha="$3" f short
    f="$(_pin_file "$tree" "$arg")" || return 1
    sed -i -E "s/^(ARG ${arg}=)[^[:space:]]+/\1${sha}/" "$f"
    grep -qE "^ARG ${arg}=${sha}" "$f" || return 1
    short="${sha:0:7}"
    # docker-bake.hcl: the variable that decides the artefact tag.
    [ -f "${tree}/docker-bake.hcl" ] && \
        sed -i -E "s/^(variable \"${arg}\"[^\"]*\")[0-9a-f]+(\")/\1${sha}\2/" \
            "${tree}/docker-bake.hcl"
    # Dockerfile: the IMG_<TOOL> tag that gets pulled. Multi-source artefacts are
    # tagged <short1>-<short2>, so replace only the half this ARG owns.
    if [ -f "${tree}/tools/check_pins_agree.py" ]; then
        python3 - "$tree" "$arg" "$sha" <<'PYBUMP'
import re, sys, pathlib
tree, arg, sha = sys.argv[1], sys.argv[2], sys.argv[3]
TOOLS = {"OPENROAD_REF": ("openroad", 0), "YOSYS_REF": ("yosys", 0),
         "KISSAT_REF": ("sat-solvers", 0), "CADICAL_REF": ("sat-solvers", 1),
         "NGSPICE_REF": ("ngspice", 0), "MAGIC_REF": ("lvs", 0),
         "NETGEN_REF": ("lvs", 1), "IVERILOG_REF": ("iverilog", 0),
         "KLAYOUT_REF": ("klayout", 0), "VERILATOR_REF": ("verilator", 0)}
if arg not in TOOLS:
    sys.exit(0)
tool, idx = TOOLS[arg]
p = pathlib.Path(tree) / "Dockerfile"
var = "IMG_" + tool.upper().replace("-", "_")
m = re.search(r"^ARG %s=(\S+)$" % var, p.read_text(), re.M)
if not m:
    sys.exit(1)
repo, _, tag = m.group(1).rpartition(":")
parts = tag.split("-")
if idx >= len(parts):
    sys.exit(1)
parts[idx] = sha[:7]
s = p.read_text().replace(m.group(0), "ARG %s=%s:%s" % (var, repo, "-".join(parts)))
p.write_text(s)
PYBUMP
        [ "$?" != "0" ] && return 1
    fi
    return 0
}


FORKS_DIR="${GK_FORKS_DIR:-/home/reyerchu/vibe-ic-forks}"
EDA_CLONE="${GK_EDA_CLONE:-/home/reyerchu/vibeic-eda}"
GATEKEEPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GK_MODE="${GK_MODE:-verify}"                    # verify | promote
GK_STATE_DIR="${GK_STATE_DIR:-${HOME}/.cache/eda-fork-gatekeeper}"
GK_RESULT="${GK_RESULT:-${GK_STATE_DIR}/last_build_result.json}"
STAMP="$(date +%Y%m%d)"
SCRATCH="/tmp/gk-build-${STAMP}"
IMG="vibeic/vibeic-eda:gkcandidate-${STAMP}"
RESULTS="[]"

emit() { python3 - "$RESULTS" "$1" "$2" "$3" "$4" <<'PY'
import json,sys
arr=json.loads(sys.argv[1]); arr.append({"tool":sys.argv[2],"status":sys.argv[3],"detail":sys.argv[4][:300],"sha":sys.argv[5] if len(sys.argv)>5 else ""})
print(json.dumps(arr))
PY
}

# a lenient "the tool actually runs" check inside the freshly built image, so a
# promoted tool is exercised (not just the yosys/openroad baseline). Presence fallback
# for tools without a stable version flag — build success is the strong signal.
_tool_smoke() {
    case "$1" in
        yosys)           echo "yosys -V" ;;
        OpenROAD)        echo "openroad -version" ;;
        klayout)         echo "klayout -v" ;;
        magic)           echo "magic --version || command -v magic" ;;
        netgen)          echo "command -v netgen" ;;
        iverilog)        echo "iverilog -V >/dev/null" ;;
        # verilator is NOT `verilator --version`, deliberately. vibeic-eda#87: a
        # version banner passes on an image where --trace-fst, --sc and constrained
        # randomize() are all dead -- measured, that was 0.2.63. This drives the real
        # entry point end to end (verilate -> g++ -> run -> SMT solver) and asserts a
        # VALUE: the design prints VLSMOKE_OK only when randomize() returns a number
        # that actually satisfies `x>100 && x<110`. Verified to DISCRIMINATE: rc=1 on
        # 0.2.63 as shipped, rc=0 on the same image once VERILATOR_SOLVER is set.
        # The SystemVerilog is base64 so no quoting of it can survive into this chain.
        verilator)       echo "cd /tmp && echo 'Y2xhc3MgQzsgcmFuZCBiaXQgWzc6MF0geDsgY29uc3RyYWludCBjMSB7IHggPiAxMDA7IHggPCAxMTA7IH0gZW5kY2xhc3MKbW9kdWxlIHQ7CiAgaW5pdGlhbCBiZWdpbgogICAgQyBjID0gbmV3OwogICAgaWYgKGMucmFuZG9taXplKCkgJiYgYy54ID4gMTAwICYmIGMueCA8IDExMCkgJGRpc3BsYXkoIlZMU01PS0VfT0sgeD0lMGQiLCBjLngpOwogICAgZWxzZSAkZGlzcGxheSgiVkxTTU9LRV9CQUQgeD0lMGQiLCBjLngpOwogICAgJGZpbmlzaDsKICBlbmQKZW5kbW9kdWxlCg==' | base64 -d > vsm.sv && verilator --binary --timing -Wno-fatal --Mdir /tmp/vsmd vsm.sv -o vsm >/dev/null 2>&1 && /tmp/vsmd/vsm 2>/dev/null | grep -q VLSMOKE_OK" ;;
        ngspice)         echo "ngspice --version >/dev/null 2>&1 || command -v ngspice" ;;
        sby)             echo "sby --help >/dev/null 2>&1 || command -v sby" ;;
        cocotb)          echo "python3 -c 'import cocotb'" ;;
        cocotb-coverage) echo "python3 -c 'import cocotb_coverage'" ;;
        pyuvm)           echo "python3 -c 'import pyuvm'" ;;
        *)               echo "command -v $1" ;;
    esac
}

# mark every clean candidate promote_failed (→ the gatekeeper maps that to DEFERRED with
# the real reason) WITHOUT touching the working tree. Used by the step-0 PRESERVATION
# guards (dirty tree / ahead-of-origin / fetch fail) — those must NEVER discard human
# work. Mutates the global RESULTS, so it must run in the caller's shell (no subshell).
_pf_mark() {
    local tool
    for tool in "${!CLEAN_SHA[@]}"; do
        RESULTS="$(emit "$tool" "promote_failed" "$1" "${CLEAN_SHA[$tool]}")"
    done
    echo "[promote] ABORT — $1 (tools stay DEFERRED)"
}
# GK_MODE=promote remains accepted only so old schedulers get an explicit,
# per-candidate DEFERRED reason instead of falling through to a second publisher.
promote_all() {
    # Candidate verification remains useful, but final publication belongs
    # exclusively to daily_release.py's immutable/latest/record/readback
    # transaction.  A second writer cannot manufacture equivalent evidence
    # merely by sharing its host lock.
    _pf_mark "legacy GK_MODE=promote is retired — use daily_release.py for canonical publication"
    return 1
}

CANDS="$(python3 -c 'import json,os;print("\n".join("%s\t%s\t%s\t%s\t%s"%(c["tool"],c.get("arg") or "",c.get("branch") or "",c.get("release") or "",c.get("upstream") or "") for c in json.loads(os.environ.get("VIBEIC_CANDIDATES","[]"))))')"
[ -z "${CANDS}" ] && { echo "[]" > "${GK_RESULT}"; echo "no candidates"; exit 0; }

declare -A CLEAN_SHA CLEAN_ARG CLEAN_FORKREMOTE CLEAN_BRANCH
CLEAN_COUNT=0
while IFS=$'\t' read -r tool arg branch release upstream; do
    [ -z "$tool" ] && continue
    dir="${FORKS_DIR}/${tool}"
    if [ ! -d "${dir}/.git" ]; then RESULTS="$(emit "$tool" "no_clone" "no fork clone at ${dir}" "")"; continue; fi
    # Remote conventions vary per clone (yosys: origin=upstream; magic: origin=fork,
    # no upstream). Resolve BOTH by URL — fetch release tags from UPSTREAM, push the
    # rebased candidate to the FORK — adding a remote if the clone lacks one.
    up_url="https://github.com/${upstream}.git"
    fork_url="https://github.com/vibeic/${tool}.git"
    up_remote="$(git -C "$dir" remote -v | awk -v u="$up_url" '$2==u && /\(fetch\)/{print $1; exit}')"
    [ -z "$up_remote" ] && { git -C "$dir" remote remove gk_up 2>/dev/null; git -C "$dir" remote add gk_up "$up_url"; up_remote=gk_up; }
    fork_remote="$(git -C "$dir" remote -v | awk -v f="$fork_url" '$2==f && /\(fetch\)/{print $1; exit}')"
    [ -z "$fork_remote" ] && { git -C "$dir" remote remove gk_fork 2>/dev/null; git -C "$dir" remote add gk_fork "$fork_url"; fork_remote=gk_fork; }
    git -C "$dir" fetch "$up_remote" --tags -q 2>/dev/null
    git -C "$dir" fetch "$fork_remote" -q 2>/dev/null
    if ! git -C "$dir" rev-parse "refs/tags/${release}" >/dev/null 2>&1; then
        RESULTS="$(emit "$tool" "tag_missing" "no git tag ${release} on ${upstream} (checked remote ${up_remote})" "")"
        continue
    fi
    if [ -z "$branch" ]; then
        RESULTS="$(emit "$tool" "no_vibeic_branch" "no branch pin on ARG in vibeic-eda/Dockerfile — add a '# … branch <name>' comment to auto-integrate ${tool}" "")"
        continue
    fi
    # resolve the vibeic branch: prefer the fork remote-tracking ref, else a local branch
    br_ref="$branch"
    git -C "$dir" rev-parse --verify "refs/remotes/${fork_remote}/${branch}" >/dev/null 2>&1 && br_ref="refs/remotes/${fork_remote}/${branch}"
    wt="/tmp/gk-wt-${tool}"
    git -C "$dir" worktree remove --force "$wt" 2>/dev/null; rm -rf "$wt"
    if ! git -C "$dir" worktree add -q --detach "$wt" "$br_ref" 2>/tmp/gkwt; then
        RESULTS="$(emit "$tool" "worktree_fail" "branch ${branch}: $(tail -1 /tmp/gkwt)" "")"; continue
    fi
    if git -C "$wt" rebase "$release" >/tmp/gkrb 2>&1; then
        sha="$(git -C "$wt" rev-parse HEAD)"
        CLEAN_SHA[$tool]="$sha"; CLEAN_ARG[$tool]="$arg"; CLEAN_FORKREMOTE[$tool]="$fork_remote"; CLEAN_BRANCH[$tool]="$branch"; CLEAN_COUNT=$((CLEAN_COUNT+1))
        RESULTS="$(emit "$tool" "rebased_clean" "rebased ${branch} onto ${release}" "$sha")"
        # temp-push to the FORK so the docker build can fetch the rebased sha
        git -C "$wt" push -f "$fork_remote" "HEAD:refs/heads/gk-candidate/${STAMP}" -q 2>/dev/null \
            || echo "  warn: temp-push failed for ${tool} (build may not find sha)"
    else
        cf="$(grep -m1 -iE 'CONFLICT' /tmp/gkrb | sed 's/^/ /')"
        git -C "$wt" rebase --abort 2>/dev/null
        RESULTS="$(emit "$tool" "rebase_conflict" "our ${branch} patch overlaps upstream ${release}:${cf}" "")"
    fi
    git -C "$dir" worktree remove --force "$wt" 2>/dev/null; rm -rf "$wt"
done <<< "${CANDS}"

# ---- build the image with the clean candidates bumped in -------------------
if [ "${CLEAN_COUNT}" -gt 0 ]; then
    rm -rf "${SCRATCH}"; cp -a "${EDA_CLONE}" "${SCRATCH}"
    BUMP_FAILED=""
    for tool in "${!CLEAN_SHA[@]}"; do
        arg="${CLEAN_ARG[$tool]}"; sha="${CLEAN_SHA[$tool]}"
        if [ -z "$arg" ] || ! _bump_pin "${SCRATCH}" "$arg" "$sha"; then
            BUMP_FAILED="${BUMP_FAILED} ${tool}"
        fi
    done
    if [ -n "${BUMP_FAILED}" ]; then
        # Building anyway would produce an image WITHOUT these commits and report
        # it green at their shas. Refuse, and name what could not be moved.
        for tool in ${BUMP_FAILED}; do
            RESULTS="$(emit "$tool" "pin_not_found" "could not move ARG ${CLEAN_ARG[$tool]} in the build tree (layout changed?) — refusing to build, because an image built with the OLD pin would be reported green at the NEW sha" "")"
        done
        echo "[build] REFUSED: could not bump${BUMP_FAILED}"
        CLEAN_COUNT=0
    fi
fi
if [ "${CLEAN_COUNT}" -gt 0 ]; then
    # Every pin site must agree before we build; that is exactly the drift the
    # bumps above could introduce.
    if [ -f "${SCRATCH}/tools/check_pins_agree.py" ] \
       && ! python3 "${SCRATCH}/tools/check_pins_agree.py" >/tmp/gkpins 2>&1; then
        echo "[build] REFUSED: pins disagree after bump"; cat /tmp/gkpins
        for tool in "${!CLEAN_SHA[@]}"; do
            RESULTS="$(emit "$tool" "pin_drift" "pins disagree after bump: $(tail -1 /tmp/gkpins)" "")"
        done
        CLEAN_COUNT=0
    fi
fi
if [ "${CLEAN_COUNT}" -gt 0 ]; then
    echo "[build] docker build ${IMG} with: ${!CLEAN_SHA[*]}"
    # After the per-tool split the composing Dockerfile COPYs published
    # artefacts, so a bumped pin needs its artefact built first. bake does both
    # in one DAG; without it the build fails on `not found` for the new tag.
    if [ -f "${SCRATCH}/docker-bake.hcl" ]; then
        BUILD_CMD=(docker buildx bake -f "${SCRATCH}/docker-bake.hcl" --load eda-local)
    else
        BUILD_CMD=(docker build -t "${IMG}" "${SCRATCH}")
    fi
    # Stream BuildKit bytes to the parent progress lease and retain the same
    # diagnostic log. `pipefail` makes the build's rc authoritative, not tee's.
    if (cd "${SCRATCH}" && "${BUILD_CMD[@]}") 2>&1 \
            | tee "/tmp/gk-docker-${STAMP}.log"; then
        [ -f "${SCRATCH}/docker-bake.hcl" ] && docker tag ghcr.io/vibeic/vibeic-eda:local "${IMG}"
        # smoke regression: yosys/openroad baseline + a per-candidate-tool check so the
        # tool being shipped is actually exercised in the fresh image, not just assumed.
        SMOKE="yosys -V && openroad -version"
        for tool in "${!CLEAN_SHA[@]}"; do SMOKE="${SMOKE} && { $(_tool_smoke "$tool") ; }"; done
        SMOKE="${SMOKE} && echo SMOKE_OK"
        if docker run --rm --entrypoint bash "${IMG}" -lc "${SMOKE}" >/tmp/gksmoke 2>&1; then
            # CAPABILITY GATE (vibeic-eda#84/#87) — BLOCKING, and it must run before
            # promote_all, not after and not merely printed.
            #
            # Everything above this line asks whether a binary STARTS. On 0.2.63 all
            # twelve of those probes passed while nine capabilities were measurably
            # dead: --trace-fst rc=2, constrained randomize() returning values that
            # violate the constraint, iverilog writing an ASCII VCD named .fst, the
            # DEFAULT PDK unable to simulate a transistor. A release that ships that
            # is a release this script called green.
            #
            # capability_gate drives each capability's REAL entry point and asserts a
            # VALUE; it blocks on BROKEN *and* on INCONCLUSIVE (a probe whose control
            # is also red proves nothing, and "we could not tell" must not promote);
            # and it fails on a STALE waiver, so fixing something forces the waiver
            # line to be deleted rather than left behind as folklore.
            CAPLOG="/tmp/gk-capability-${STAMP}.log"
            # Capture the status into a variable on the SAME line, before any other
            # command can overwrite $?. Writing `else CAP_OK=0; CAP_RC=$?` reads the
            # ASSIGNMENT's status (always 0), which is the same family of mistake as
            # reading an exit code through a pipe — the one capability_smoke's own
            # docstring rule 4 exists for, and which its first draft shipped.
            python3 "$(dirname "${BASH_SOURCE[0]}")/capability_gate.py" "${IMG}" \
                   --json "/tmp/gk-capability-${STAMP}.json" >"${CAPLOG}" 2>&1; CAP_RC=$?
            tail -25 "${CAPLOG}" || true
            if [ "${CAP_RC}" -ne 0 ]; then
                # rc=2 means the image could not be probed at all. That is NOT a pass:
                # measuring nothing proves nothing, so it blocks exactly like rc=1.
                for tool in "${!CLEAN_SHA[@]}"; do
                    RESULTS="$(emit "$tool" "built_red" "capability gate FAILED rc=${CAP_RC} (see ${CAPLOG}): $(grep -m1 -A1 'BLOCKING' "${CAPLOG}" | tail -1 | tr -d '\n')" "")"
                done
                echo "[capability] BLOCKED rc=${CAP_RC} — a capability the image advertises does not run; nothing promoted"
            else
                for tool in "${!CLEAN_SHA[@]}"; do
                    RESULTS="$(emit "$tool" "built_green" "image ${IMG} built + smoke-passed + capability-gate green" "${CLEAN_SHA[$tool]}")"
                done
                if [ "${GK_MODE}" = "promote" ]; then
                    promote_all || echo "[promote] not completed — candidates stay DEFERRED (verified green, not shipped)"
                fi
            fi
        else
            for tool in "${!CLEAN_SHA[@]}"; do RESULTS="$(emit "$tool" "built_red" "smoke regression failed: $(tail -1 /tmp/gksmoke)" "")"; done
        fi
    else
        for tool in "${!CLEAN_SHA[@]}"; do RESULTS="$(emit "$tool" "built_red" "docker build failed (see /tmp/gk-docker-${STAMP}.log)" "")"; done
    fi
    rm -rf "${SCRATCH}"
    # cleanup temp candidate refs
    for tool in "${!CLEAN_SHA[@]}"; do git -C "${FORKS_DIR}/${tool}" push "${CLEAN_FORKREMOTE[$tool]}" --delete "gk-candidate/${STAMP}" -q 2>/dev/null; done
fi

echo "${RESULTS}" > "${GK_RESULT}"
echo "wrote results -> ${GK_RESULT}"
python3 -c 'import json,os;[print("  %-16s %-16s %s"%(r["tool"],r["status"],r["detail"][:70])) for r in json.load(open(os.environ["GK_RESULT"]))]' 2>/dev/null
