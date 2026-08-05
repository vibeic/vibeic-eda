#!/usr/bin/env bash
# Rewrite /foss/pdks/versions.txt so it names the versions THIS image runs.
#
# The manifest belongs to the base image. We replace several of the tools it
# names and never touched it, so our image shipped a file telling users the
# base's versions -- xschem 3.4.6 against an actual 3.4.8, klayout 0.30.5
# against 0.30.10, magic 8.3.589 against 8.3.679, ngspice 43 against 46.
#
# The fix has to be a PROBE, not a table of numbers. A recorded version is a
# claim that goes stale the next time a pin moves, which is exactly how the
# original defect arose; a probe cannot be stale because it asks the binary.
#
# Runs at compose time, after every tool replacement, inside the image.
#
# THREE STATES, and the first draft of this script got all three wrong, which
# is why they are spelled out:
#
#   0  every tool in the table that the manifest names was probed, and the
#      manifest now agrees with the binary.
#   1  a tool could not be probed, OR the manifest could not be written. Both
#      mean the manifest is NOT known to be correct, which is not a pass.
#
# The first draft printed "4 claim(s) corrected" and exited 0 while every single
# write had failed with EACCES -- it counted intents, not writes. And its
# "could not probe" arm never fired, because a regex run over the output of a
# nonexistent command still matched a digit in the error text and recorded
# `magic 1`. Both were found by RUNNING it against the shipped image, neither by
# reading it.
set -uo pipefail

MANIFEST=/foss/pdks/versions.txt
TABLE="${1:?usage: refresh_versions_manifest.sh <tool_version_probes.tsv>}"

if [ ! -f "$MANIFEST" ]; then
    echo "[FAIL] $MANIFEST does not exist in this image -- nothing was" >&2
    echo "       compared or rewritten, which is a gap, not a pass." >&2
    exit 1
fi
if [ ! -w "$MANIFEST" ]; then
    echo "[FAIL] $MANIFEST is not writable by $(id -un) -- the claims cannot" >&2
    echo "       be corrected. Refusing to report success for writes that" >&2
    echo "       would silently fail." >&2
    exit 1
fi

rc=0
changed=0
while IFS=$'\t' read -r tool cmd regex; do
    case "${tool:-}" in ''|'#'*) continue ;; esac

    # Only tools the manifest actually names. A tool we ship that the base never
    # listed has no claim to correct, and inventing a line would make the
    # manifest say something the base image never said.
    grep -qE "^${tool}[[:space:]]" "$MANIFEST" || continue

    # The command must EXIST and SUCCEED. Matching a regex against whatever a
    # failed command printed is how the first draft turned a missing binary into
    # `magic 1` -- the error text contains digits.
    bin="${cmd%% *}"
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "[FAIL] ${tool}: \`${bin}\` is not on PATH, so the manifest's" >&2
        echo "       claim for it cannot be checked or corrected." >&2
        rc=1
        continue
    fi

    # `bash -lc` is required for the base's PATH, and its profile.d prints
    # `[INFO] Final PATH variable: ...` to stdout on every login shell -- which
    # is itself full of digits. Strip the banner before matching.
    out="$(bash -lc "$cmd" 2>&1 | grep -v '^\[INFO\]' | head -3)"
    probe_rc=$?
    actual="$(printf '%s' "$out" | grep -oE "$regex" | head -1)"
    actual="${actual#ngspice-}"

    if [ -z "$actual" ]; then
        echo "[FAIL] ${tool}: \`${cmd}\` (rc=${probe_rc}) printed no version to" >&2
        echo "       record. The manifest still carries the base's claim." >&2
        rc=1
        continue
    fi

    claimed="$(grep -E "^${tool}[[:space:]]" "$MANIFEST" | head -1 | awk '{print $2}')"
    [ "$claimed" = "$actual" ] && continue

    # Rewrite only the version field, so any trailing columns the base keeps in
    # that line survive. Whole-line rewrites are how a previous bumper ate the
    # trailing comments in the pin files.
    #
    # The write is VERIFIED, not assumed: `cat > file` can fail after awk has
    # already succeeded, and that is precisely what happened on the first run.
    tmp="$(mktemp)"
    if ! awk -v t="$tool" -v v="$actual" '$1 == t { $2 = v } { print }' \
            OFS=' ' "$MANIFEST" > "$tmp"; then
        echo "[FAIL] ${tool}: could not build the rewritten manifest." >&2
        rm -f "$tmp"; rc=1; continue
    fi
    if ! cat "$tmp" > "$MANIFEST" 2>/dev/null; then
        echo "[FAIL] ${tool}: could not WRITE ${MANIFEST}." >&2
        rm -f "$tmp"; rc=1; continue
    fi
    rm -f "$tmp"

    # Read it back. The claim of this script is that the file now says the right
    # thing, so that is what gets asserted -- not that a write call returned.
    wrote="$(grep -E "^${tool}[[:space:]]" "$MANIFEST" | head -1 | awk '{print $2}')"
    if [ "$wrote" != "$actual" ]; then
        echo "[FAIL] ${tool}: after writing, the manifest still says" >&2
        echo "       '${wrote}', not '${actual}'." >&2
        rc=1; continue
    fi

    echo "  versions.txt ${tool}: ${claimed} -> ${actual}"
    changed=$((changed + 1))
done < "$TABLE"

if [ "$rc" -ne 0 ]; then
    echo "[FAIL] refresh_versions_manifest: at least one tool could not be" >&2
    echo "       probed or written; the manifest is NOT known to be correct." >&2
    exit 1
fi

echo "refresh_versions_manifest: ${changed} claim(s) corrected against the" \
     "binaries this image actually ships"
