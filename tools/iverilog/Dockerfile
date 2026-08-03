# eda-tool-iverilog — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-iverilog:<ref>`. The tag IS the pin: "which
# iverilog is in the EDA image" is answered by reading the tag, not by trusting a
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
ARG IVERILOG_REPO=https://github.com/vibeic/iverilog.git
ARG IVERILOG_REF=0d75e8d22288d4ac9a703c08f1efa5f3fb89292f  # pinned; branch vibeic/sv-tb-coverage (0.2.27: ff-verified descendant of 110cadd5 — no fix dropped; adds vibe-ic#125 $dumpvars/$dumpports forward-ref bind-against-completed-scope + graft guard test, plus SDF INCREMENT/COND/scale/mtm honouring + forward-ref elab)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git autoconf gperf flex bison ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone "${IVERILOG_REPO}" /iverilog \
 && git config --global --add safe.directory /iverilog \
 && cd /iverilog && git checkout ${IVERILOG_REF} \
 && sh autoconf.sh && ./configure --prefix=/foss/tools/iverilog \
 && make -j"$(nproc)" && make install

# ---------------------------------------------------------------------------
# Stage 6 — vibeic/klayout (streamout MANUFACTURINGGRID snap + merge-abutting + foundry
#   layer-map, AND the native SVRF/Calibre DRC engine + `svrfdrc` buddy). Qt-less db-lib
#   + pymod + buddies; shipped parallel as klayout-vibeic so the base 0.30.6 GUI/DRC stays
#   intact. Phase-3 streamout AND commercial sign-off DRC both point here.
#   The svrf-native-drc branch = streamout-fixes + db::SVRFDeck/db::SVRFEngine + the
#   `svrfdrc <deck> <layout> <report>` buddy (native C++, NO Python interpreter). Byte-
#   parity vs the old run_svrf_drc.py proven on a real commercial foundry deck.
# ---------------------------------------------------------------------------
# Record every pin this artefact was built from.
RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"iverilog","repo":"%s","ref":"%s"}\n' \
      "${IVERILOG_REPO}" "${IVERILOG_REF}" \
      > /vibeic/provenance/iverilog.json

# ---------------------------------------------------------------------------
# The published layer carries the tool payload and nothing else — no compiler,
# no source tree, no apt lists. `scratch` enforces that by construction.
#
# The provenance file records what produced this artefact INSIDE it: a tag can
# be moved, a file in the image cannot, so the composing image can assert that
# what it copied is what it asked for.
# ---------------------------------------------------------------------------
FROM scratch
COPY --from=build /foss/tools/iverilog /foss/tools/iverilog
COPY --from=build /vibeic/provenance/iverilog.json /vibeic/provenance/iverilog.json
