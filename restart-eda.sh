#!/usr/bin/env bash
#
# restart-eda.sh — recreate the `vibeic-eda` MCP-EDA container on a chosen image
# tag, faithfully preserving the existing container's mounts / cmd / user /
# workdir. Swaps ONLY the image; everything else is carried over verbatim.
#
# Why this exists: the eda-tools MCP server (.mcp.json) binds the container by
# NAME (`vibeic-eda`), not by image tag — so "use the newest image" means
# `docker rm -f` the old container and `docker run` a new one on the desired
# tag. A `docker run` container is pinned to the image ID that the tag resolved
# to AT CREATION, so moving `latest` to a new build does NOT update a running
# container — you must recreate. This script is that recreate, done safely.
#
# The MCP server process itself needs NO restart: it drives the container via
# `docker exec <name> ...` on every call, so it re-attaches to the new
# same-named container automatically.
#
# Usage:
#   ./restart-eda.sh                      # recreate on the PINNED vibeic/vibeic-eda:$(cat VERSION)
#   ./restart-eda.sh 0.2.11               # bare tag  -> vibeic/vibeic-eda:0.2.11
#   ./restart-eda.sh vibeic/vibeic-eda:latest   # full ref honored as-is (explicit floating opt-in)
#   FORCE=1 ./restart-eda.sh              # recreate even if an EDA job is running
#
# Env overrides:
#   NAME=vibeic-eda            container name to manage
#   IMAGE_REPO=vibeic/vibeic-eda   repo prepended to a bare tag argument
#   DESIGNS_DIR=/path/to/your/designs   existing designs dir mounted at /foss/designs (fresh-container fallback only; must already exist)
#   RESTART_EDA_PRINT_IMAGE=1  print the resolved image ref and exit (no docker)
#
# After a successful recreate, confirm the toolchain from Claude Code with the
# MCP tool `eda_doctor` (skip_versions=false) — expect "14/14 checks passed".
#
set -euo pipefail

NAME="${NAME:-vibeic-eda}"
IMAGE_REPO="${IMAGE_REPO:-vibeic/vibeic-eda}"

# EDA tool process names used for the in-flight-job guard.
EDA_PROCS='openroad|yosys|magic|netgen|klayout|iverilog|verilator|ngspice|fault|tclsh'

die() { echo "restart-eda: $*" >&2; exit "${2:-1}"; }

# --- resolve requested image ref -------------------------------------------
# The no-arg default is the PINNED version from the VERSION file next to this
# script (the image's single source of truth), never a floating `latest`: a
# stale local `latest` would silently recreate the container on an outdated
# toolchain. Floating tags stay available by passing them explicitly.
if [[ $# -ge 1 && -n "${1:-}" ]]; then
  arg="$1"
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [[ -f "${SCRIPT_DIR}/VERSION" ]] || die \
    "no tag argument and no VERSION file at ${SCRIPT_DIR}/VERSION — pass a tag explicitly"
  arg="$(tr -d '[:space:]' < "${SCRIPT_DIR}/VERSION")"
  [[ -n "$arg" ]] || die "VERSION file ${SCRIPT_DIR}/VERSION is empty — pass a tag explicitly"
fi
if [[ "$arg" == *:* || "$arg" == */* ]]; then
  IMAGE="$arg"                    # a full ref (repo[:tag] or repo/path) — honor as-is
else
  IMAGE="${IMAGE_REPO}:${arg}"    # a bare tag — prepend the repo
fi
if [[ "${RESTART_EDA_PRINT_IMAGE:-0}" == "1" ]]; then
  echo "$IMAGE"; exit 0           # resolution-only mode (used by the regression tests)
fi
echo "== target image : ${IMAGE}"

command -v docker >/dev/null 2>&1 || die "docker CLI not found on PATH"

# --- the image must exist locally (never silently pull) --------------------
docker image inspect "$IMAGE" >/dev/null 2>&1 || die \
  "image '${IMAGE}' not found locally. Build or pull it first, e.g.:
       docker pull ${IMAGE}
   (available local tags:)
$(docker images "${IMAGE_REPO}" --format '       {{.Repository}}:{{.Tag}} {{.ID}}' 2>/dev/null | sort -u)" 1
TARGET_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
echo "   image id     : ${TARGET_ID}"

# --- capture existing container config (or fall back to canonical defaults) --
declare -a BINDS=() CMD=()
USER_SPEC="" WORKDIR="" OLD_MEMORY=0

if docker container inspect "$NAME" >/dev/null 2>&1; then
  OLD_IMG="$(docker inspect "$NAME" --format '{{.Config.Image}}')"
  echo "== existing container '${NAME}' found (image: ${OLD_IMG}) — cloning its config"

  # in-flight EDA job guard (skip idle sleep/startup/VNC).
  if docker top "$NAME" -o args 2>/dev/null | grep -iqE "$EDA_PROCS"; then
    if [[ "${FORCE:-0}" != "1" ]]; then
      echo "-- an EDA tool process is running inside '${NAME}':" >&2
      docker top "$NAME" -o pid,args 2>/dev/null | grep -iE "$EDA_PROCS" >&2 || true
      die "refusing to recreate mid-job. Re-run with FORCE=1 to override." 2
    fi
    echo "-- FORCE=1: recreating despite a running EDA job."
  fi

  while IFS= read -r b; do [[ -n "$b" ]] && BINDS+=( -v "$b" ); done \
    < <(docker inspect "$NAME" --format '{{range .HostConfig.Binds}}{{println .}}{{end}}')
  USER_SPEC="$(docker inspect "$NAME" --format '{{.Config.User}}')"
  OLD_MEMORY="$(docker inspect "$NAME" --format '{{.HostConfig.Memory}}' 2>/dev/null || echo 0)"
  WORKDIR="$(docker inspect "$NAME"  --format '{{.Config.WorkingDir}}')"
  while IFS= read -r c; do [[ -n "$c" ]] && CMD+=( "$c" ); done \
    < <(docker inspect "$NAME" --format '{{range .Config.Cmd}}{{println .}}{{end}}')
else
  echo "== no existing container '${NAME}' — using canonical vibeic-eda defaults"
  # Path-portability: NEVER default a designs dir under $HOME — docker would
  # create a missing bind-mount source root-owned (the phantom-directory bug).
  # With no existing container to preserve, require the user to name an EXISTING
  # directory via DESIGNS_DIR (or VIBEIC_DESIGNS); refuse rather than invent one.
  DESIGNS_DIR="${DESIGNS_DIR:-${VIBEIC_DESIGNS:-}}"
  [[ -n "$DESIGNS_DIR" ]] || die \
    "no existing '${NAME}' container to preserve, and neither DESIGNS_DIR nor VIBEIC_DESIGNS is set — point one at your existing designs dir, e.g.  DESIGNS_DIR=/path/to/your/designs ${0##*/} ${1:-<tag>}"
  [[ "$DESIGNS_DIR" == /* ]] || die \
    "DESIGNS_DIR must be an absolute path (got '${DESIGNS_DIR}') — a relative path would become a docker named volume, not a bind mount"
  [[ -d "$DESIGNS_DIR" ]] || die \
    "DESIGNS_DIR '${DESIGNS_DIR}' does not exist — create it deliberately first (the installer never creates a workspace for you)"
  BINDS=( -v "${DESIGNS_DIR}:${DESIGNS_DIR}" -v "${DESIGNS_DIR}:/foss/designs" )
  USER_SPEC="$(id -u)"
  WORKDIR="/foss/designs"
  CMD=( --skip sleep infinity )
fi

echo "   binds        : ${BINDS[*]:-<none>}"
echo "   user/workdir : ${USER_SPEC:-<image default>} / ${WORKDIR:-<image default>}"
echo "   cmd          : ${CMD[*]:-<image default>}   (entrypoint stays image-baked)"

# --- memory ceiling --------------------------------------------------------
# MEASURED 2026-08-19: a yosys inside this container grew to 113 GB on a 125 GB
# host that carries 2 GB of swap, and the machine went down — GNOME was OOM-
# killed and the session restarted, which from the seat is indistinguishable
# from a reboot. Nothing in the stack could have stopped it:
#
#   * the container was created with HostConfig.Memory == 0, i.e. NO cgroup
#     limit, so "the container" and "the host" are the same 125 GB pool;
#   * `ulimit -v` inside the container is unlimited, so yosys never gets an
#     allocation failure to report — it just keeps growing;
#   * the MCP server drives every tool through `docker exec` with a wall-clock
#     timeout, and that timeout kills the exec CLIENT, not the process in the
#     container. Measured here: kill the client, and the in-container process
#     is still running. So past the timeout the tool keeps allocating with
#     nothing left watching it.
#
# The host OOM killer then picks its victim by score, which is why a synthesis
# run took down the desktop session rather than only itself. A cgroup limit
# makes that failure local: the kernel kills the runaway INSIDE the container,
# the tool's own log ends in "Killed", and the host keeps its cushion.
# --memory-swap pinned to the same value denies the container the host's swap
# too — the minutes of "frozen" that preceded the crash were swap thrash on a
# 2 GB swapfile, not the kill itself.
#
#   EDA_MEMORY=64g          explicit ceiling (any docker size string)
#   EDA_MEMORY=0            opt out entirely (restores the pre-fix behaviour)
#   EDA_MEMORY_FRACTION=70  percent of host RAM used when EDA_MEMORY is unset
#
# An existing container's own NON-ZERO ceiling is preserved across a recreate,
# like the binds and the cmd are. A zero one is deliberately NOT preserved:
# "unlimited" is the bug this section exists to remove, so cloning it forward
# would make the fix un-adoptable by exactly the containers that need it.
MEM_FRACTION="${EDA_MEMORY_FRACTION:-70}"
[[ "$MEM_FRACTION" =~ ^[0-9]+$ ]] || die \
  "EDA_MEMORY_FRACTION must be an integer percent (got '${MEM_FRACTION}')"
(( MEM_FRACTION >= 1 && MEM_FRACTION <= 100 )) || die \
  "EDA_MEMORY_FRACTION must be between 1 and 100 (got '${MEM_FRACTION}')"
MEM_FLOOR=$(( 2 * 1024 * 1024 * 1024 ))   # under 2 GiB a ceiling only breaks tools

if [[ -n "${EDA_MEMORY:-}" ]]; then
  case "${EDA_MEMORY}" in
    0|unlimited|none) MEMORY="" ;;
    *)                MEMORY="${EDA_MEMORY}" ;;
  esac
elif [[ "${OLD_MEMORY:-0}" =~ ^[0-9]+$ ]] && (( OLD_MEMORY > 0 )); then
  MEMORY="${OLD_MEMORY}"
else
  # Read MemTotal in the kB awk was handed, and do the arithmetic in bash.
  #
  # MEASURED 2026-08-19, and it is why this comment exists: the first version
  # of this asked awk for the byte count directly, `print $2 * 1024`. On one
  # host that prints 134973464576; on five others the same expression printed
  # `1.34974e+11`, because awk falls back to OFMT once the value outgrows the
  # precision it will print as an integer, and the two boxes disagree about
  # where that is. The scientific-notation string then failed the ^[0-9]+$
  # check and every one of those five hosts silently created the unbounded
  # container this whole section exists to prevent. `$2` on its own is the
  # integer awk was given, so nothing is left for it to reformat.
  HOST_MEM_KB="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || true)"
  if [[ "${HOST_MEM_KB:-}" =~ ^[0-9]+$ ]] && (( HOST_MEM_KB > 0 )); then
    HOST_MEM=$(( HOST_MEM_KB * 1024 ))
    MEMORY=$(( HOST_MEM * MEM_FRACTION / 100 ))
    (( MEMORY >= MEM_FLOOR )) || MEMORY="$MEM_FLOOR"
    (( MEMORY <= HOST_MEM  )) || MEMORY="$HOST_MEM"
  else
    # REFUSE rather than fall back to unbounded. A safety ceiling whose failure
    # mode is "no ceiling" is worse than none at all: it reports success while
    # leaving the exact configuration that took a host down, which is precisely
    # what the awk bug above did on five machines at once. Opting out stays
    # available, but it has to be asked for.
    die "could not read MemTotal from /proc/meminfo (got '${HOST_MEM_KB:-<nothing>}'),
   so no memory ceiling could be computed. Refusing to create '${NAME}' unbounded
   — a single runaway tool would then be able to exhaust the host. Pass
   EDA_MEMORY=<size> to set one explicitly, or EDA_MEMORY=0 to opt out on purpose."
  fi
fi
if [[ -n "$MEMORY" ]]; then
  echo "   memory       : ${MEMORY} (--memory / --memory-swap; swap denied)"
else
  echo "   memory       : <unlimited — opted out>"
fi

# --- recreate --------------------------------------------------------------
# `--init` because the container's command is `sleep infinity`, and PID 1 owns
# reaping. `sleep` never calls wait(), so every process reparented to it after
# its own parent exits stays a zombie for the life of the container. Measured on
# a worker after a day of runs: 11 defunct `yosys`, and — the part that costs
# real time — a zombie reports its LIFETIME-AVERAGE %CPU, so `ps` showed 96.5,
# 89.1, 16.5 … on a completely idle host. Every "is this machine busy?" check
# reads that as work in progress. docker-init (tini) execs the command as its
# child and reaps, so the same question gets a true answer.
declare -a RUN=( docker run -d --init --name "$NAME" )
[[ -n "$USER_SPEC" ]] && RUN+=( -u "$USER_SPEC" )
[[ -n "$WORKDIR"  ]] && RUN+=( -w "$WORKDIR" )
[[ -n "$MEMORY"   ]] && RUN+=( --memory "$MEMORY" --memory-swap "$MEMORY" )
RUN+=( "${BINDS[@]}" "$IMAGE" )
[[ ${#CMD[@]} -gt 0 ]] && RUN+=( "${CMD[@]}" )

# Resolution-only mode: print the argv we WOULD run and stop, before anything
# destructive. The old container is still there afterwards. This is what the
# regression test reads, so the ceiling is asserted on the command that is
# actually issued rather than on a re-derivation of it.
if [[ "${RESTART_EDA_PRINT_RUN:-0}" == "1" ]]; then
  printf '%s\n' "${RUN[@]}"
  exit 0
fi

echo "== removing old container (if any)"
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "== ${RUN[*]}"
"${RUN[@]}" >/dev/null

# --- verify ----------------------------------------------------------------
NEW_ID="$(docker inspect "$NAME" --format '{{.Image}}')"
echo
docker ps --filter "name=^/${NAME}$" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
if [[ "$NEW_ID" == "$TARGET_ID" ]]; then
  echo "== OK: container image id matches ${IMAGE}"
else
  die "container image id ${NEW_ID} != target ${TARGET_ID}" 3
fi
echo
echo "Next: in Claude Code run the MCP tool  eda_doctor (skip_versions=false)"
echo "      — expect '14/14 checks passed' before driving the flow."
