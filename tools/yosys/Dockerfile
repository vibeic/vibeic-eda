# eda-tool-yosys — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-yosys:<ref>`. The tag IS the pin: "which
# yosys is in the EDA image" is answered by reading the tag, not by trusting a
# comment beside an ARG.
#
# Split out of the 605-line monolith on 2026-07-29. That file had eight builder
# stages and `release.yml` invoked build-push-action with no `cache-from` and no
# `cache-to` (measured: 0 occurrences), so moving one character of one pin
# rebuilt every tool from source. Per-tool artefacts make a rebuild cost exactly
# the tool that changed.
#
# The build body below is UNCHANGED from the stage it came from — this split is
# structural, and a behaviour change smuggled into it would be indistinguishable
# from a migration bug.
#
# Source is `github.com/vibeic/*` and nothing else (owner rule 2026-07-29);
# `tools/check_fork_only.py` fails the build on anything else.

FROM ubuntu:24.04 AS build
# THE PIN IS THE DELIVERY. Landing a commit on the fork changes nothing here:
# this ref is what gets built, so a fork merge that does not move it ships the
# old compiler while reading as delivered. That is exactly what happened between
# 0.2.30 and 0.2.31 — vibeic/yosys#2 landed on satfix-integration and the image
# kept building baf3472497, seven commits behind, so every SVA run in the
# toolchain still used the unfixed frontend. Move this ref in the same change
# that lands the fork commit, or the fork commit is inert.
# Pinned to branch satfix-integration.
#   0.2.31  edb458ab8b — adds vibeic/yosys#2: procedural named properties and
#           cover vacuity in the SVA subset, diagnostics anchored on the
#           property rather than the module header, typedef lookup kept on the
#           non-formal fallback of the new SVA keywords.
#   0.2.30  baf3472497 — vibe-ic#354: 0.67+26, i.e. the 0.2.22
#           synth-fixes-integration line 330b3eb19 plus the ExtCdclSat
#           `sat -select-solver` (kissat/cadical) forward-port — the
#           DT1/DT2/DT3 external-CDCL fix v1.5.71 wired and 0.2.29 was
#           supposed to deliver.
# The history above is on its own comment lines because `ARG` splits the rest of
# the line into `name[=value]` tokens: a trailing `#` comment survives that (it
# is read as an ARG name and ignored, which is why 15 other pins here carry one)
# but a token that is exactly `=` yields a blank name and the build dies with
# "ARG names can not be blank". Bisected — a `foo = bar` inside the comment is
# fatal, `foo=bar` is not.
ARG YOSYS_REPO=https://github.com/vibeic/yosys.git
ARG YOSYS_REF=6fd92ced3c75db60b785b143082dde842b794ae0
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake git bison flex gawk pkg-config \
      libreadline-dev tcl-dev libffi-dev zlib1g-dev python3 \
      libboost-system-dev libboost-python-dev libboost-filesystem-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone "${YOSYS_REPO}" /yosys \
 && cd /yosys && git checkout ${YOSYS_REF} && git submodule update --init --recursive \
 && cmake -S /yosys -B /yosys/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/foss/tools/yosys \
 && cmake --build /yosys/build -j"$(nproc)" \
 && cmake --install /yosys/build

# ---------------------------------------------------------------------------
# Stage 2b — external CDCL SAT solvers (kissat + cadical) for the fork yosys
# `sat -select-solver` backend (vibe-ic#354 / 0.2.30; plugin probes them
# SELF-VALIDATINGLY in transition_fault_atpg_run.py and falls back to the
# built-in ezMiniSAT when absent, so their absence never flips a grade).
# Upstream Armin Biere releases, pinned by tag SHA; NOT forks — no vibeic
# delta, so they live outside the fork-gatekeeper ledger.
# ---------------------------------------------------------------------------
# Record every pin this artefact was built from.
RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"yosys","repo":"%s","ref":"%s"}\n' \
      "${YOSYS_REPO}" "${YOSYS_REF}" \
      > /vibeic/provenance/yosys.json

# ---------------------------------------------------------------------------
# The published layer carries the tool payload and nothing else — no compiler,
# no source tree, no apt lists. `scratch` enforces that by construction.
#
# The provenance file records what produced this artefact INSIDE it: a tag can
# be moved, a file in the image cannot, so the composing image can assert that
# what it copied is what it asked for.
# ---------------------------------------------------------------------------
FROM scratch
COPY --from=build /foss/tools/yosys /foss/tools/yosys
COPY --from=build /vibeic/provenance/yosys.json /vibeic/provenance/yosys.json
