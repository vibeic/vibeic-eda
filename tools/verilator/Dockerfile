# eda-tool-verilator — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-verilator:<ref>`. The tag IS the pin: "which
# verilator is in the EDA image" is answered by reading the tag, not by trusting a
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
ARG VERILATOR_REPO=https://github.com/vibeic/verilator.git
ARG VERILATOR_REF=d9f46707510f7d0ad67579aee755c2aabbf1230e  # pinned; branch vibeic/sv-tb-coverage (id 12: pow2-base Pow lowering + $countbits var-ctrl)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git autoconf flex bison ca-certificates help2man perl python3 \
      libfl2 libfl-dev zlib1g zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone "${VERILATOR_REPO}" /verilator \
 && git config --global --add safe.directory /verilator \
 && cd /verilator && git checkout ${VERILATOR_REF} \
 && autoconf && ./configure --prefix=/foss/tools/verilator \
 && make -j"$(nproc)" && make install

# ---------------------------------------------------------------------------
# Stage 7 — RETIRED. The SVRF/Calibre DRC deck is now executed by the NATIVE C++
#   `svrfdrc` buddy compiled in Stage 6 (part of the svrf-native-drc branch), NOT by
#   the old pure-Python `run_svrf_drc.py` interpreter. No separate source stage: the
#   buddy ships inside /klayout/bld and is surfaced on PATH in the runtime stage.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stage 8 — vibeic verification toolchain (cocotb / cocotb-coverage / pyuvm / sby)
#   The professional testbench generator (plugin `professional_tb_gen`, MCP
#   `eda_professional_tb`) emits cocotb TBs + cocotb-coverage covergroups + an SVA
#   bind that RUN on this toolchain — so we OWN it (Bucket-T: fork every OSS EDA tool
#   so we can enhance it, never a "the tool can't do it" excuse). Pure-source fetch
#   here; editable-installed in the runtime so an in-image Python patch is live.
#   sby (SymbiYosys) drives our vibeic/yosys for the SVA-bind / formal path.
#   Cloned from the vibeic org forks (github.com/vibeic/{cocotb,cocotb-coverage,
#   pyuvm,sby}); the pinned SHAs live in those forks (shared upstream history) so
#   a vibeic patch is a branch/commit on top. Keep COCOTB_REF on the stable 2.0.x
#   line the base ships until a real vibeic patch lands.
# ---------------------------------------------------------------------------
# Record every pin this artefact was built from.
RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"verilator","repo":"%s","ref":"%s"}\n' \
      "${VERILATOR_REPO}" "${VERILATOR_REF}" \
      > /vibeic/provenance/verilator.json

# ---------------------------------------------------------------------------
# The published layer carries the tool payload and nothing else — no compiler,
# no source tree, no apt lists. `scratch` enforces that by construction.
#
# The provenance file records what produced this artefact INSIDE it: a tag can
# be moved, a file in the image cannot, so the composing image can assert that
# what it copied is what it asked for.
# ---------------------------------------------------------------------------
FROM scratch
COPY --from=build /foss/tools/verilator /foss/tools/verilator
COPY --from=build /vibeic/provenance/verilator.json /vibeic/provenance/verilator.json
