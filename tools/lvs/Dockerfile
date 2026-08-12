# eda-tool-lvs — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-lvs:<ref>`. The tag IS the pin: "which
# lvs is in the EDA image" is answered by reading the tag, not by trusting a
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
ARG MAGIC_REPO=https://github.com/vibeic/magic.git
ARG MAGIC_REF=e6e9d9bd0020bc55f066a4379e1ac1886c3674b9  # pinned; branch vibeic/integration is the LVS-fidelity line (gk-merge/2026-07-19) MERGED with vibeic/bridge-tech-multimetal, so the DEF/LVS robustness fixes the image already ran AND the 2026-07-18 batch (#46 foundry layer-map, #47/#37 grid snap, #28 SPEF, #48 NDR, #32 tech-from-LEF, #38) are both present. Neither line was dropped.
ARG NETGEN_REPO=https://github.com/vibeic/netgen.git
ARG NETGEN_REF=91e2b9e29da0d204dbe2851d49687e50aaa6336d  # pinned; branch vibeic/connectivity-match (netgen PR#1: plain-mode lvs finishes + stdout==report, resolves vibe-ic#191 tool layer; report byte-identical so #189 classifier/Step-31 unaffected)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git m4 tcl-dev tk-dev libx11-dev libcairo2-dev libncurses-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone "${MAGIC_REPO}" /magic \
 && cd /magic && git checkout ${MAGIC_REF} \
 && ./configure --prefix=/foss/tools/magic && make -j"$(nproc)" && make install
RUN git clone "${NETGEN_REPO}" /netgen \
 && cd /netgen && git checkout ${NETGEN_REF} \
 && ./configure --prefix=/foss/tools/netgen && make -j"$(nproc)" && make install

# ---------------------------------------------------------------------------
# Stage 5 — vibeic/iverilog (->> nonblocking-event codegen segfault fix + package ordering)
# ---------------------------------------------------------------------------
# Record every pin this artefact was built from.
RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"lvs","repo":"%s","ref":"%s","netgen_repo":"%s","netgen_ref":"%s"}\n' \
      "${MAGIC_REPO}" "${MAGIC_REF}" "${NETGEN_REPO}" "${NETGEN_REF}" \
      > /vibeic/provenance/lvs.json

# ---------------------------------------------------------------------------
# The published layer carries the tool payload and nothing else — no compiler,
# no source tree, no apt lists. `scratch` enforces that by construction.
#
# The provenance file records what produced this artefact INSIDE it: a tag can
# be moved, a file in the image cannot, so the composing image can assert that
# what it copied is what it asked for.
# ---------------------------------------------------------------------------
FROM scratch
COPY --from=build /foss/tools/magic /foss/tools/magic
COPY --from=build /foss/tools/netgen /foss/tools/netgen
COPY --from=build /vibeic/provenance/lvs.json /vibeic/provenance/lvs.json
# The fork's own regression gates travel WITH the tool they test.
#
# vibeic/magic carries ten FAIL->PASS gates under test/ for its LEF/DEF/GDS/
# extraction patches and, until this line, nothing anywhere ran them: not this
# Dockerfile, not FORKS.json, not a workflow (measured 2026-08-12 -- the fork's
# nine workflows have 0 runs, total). A suite nobody invokes is documentation.
#
# They are NOT run in the build stage above, and that is deliberate: this stage
# is bare ubuntu:24.04 with no PDK, and measured there the suite exits 1 with
# def_mfggrid_snap and def_ndr_via_byrule FAILING -- not skipping -- because
# without a foundry tech they fall back to a generated bridge tech whose
# internal unit is 0.001 um instead of 0.0005 um, and the gates' hand-computed
# grid arithmetic stops holding. That is a red gate for a reason that is not a
# defect, which is the failure mode OpenROAD's `find_dup_ids.sh` note in
# FORKS.json already records. So the gates ship as DATA here and are RUN by the
# composing image, which is the only place magic and the PDKs coexist.
COPY --from=build /magic/test /vibeic/tests/magic/test
