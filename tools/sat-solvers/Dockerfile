# eda-tool-sat-solvers — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-sat-solvers:<ref>`. The tag IS the pin: "which
# sat-solvers is in the EDA image" is answered by reading the tag, not by trusting a
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
ARG KISSAT_REPO=https://github.com/arminbiere/kissat.git
ARG KISSAT_REF=8af8e56f174b778aef3aa45af9f739b2a5f492c2   # tag rel-4.0.4
ARG CADICAL_REPO=https://github.com/arminbiere/cadical.git
ARG CADICAL_REF=c60730422e758ef1cebe7aeddf2dda31c996bf04    # tag rel-3.0.1
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone "${KISSAT_REPO}" /kissat \
 && cd /kissat && git checkout ${KISSAT_REF} \
 && ./configure && make -j"$(nproc)" \
 && install -m 0755 build/kissat /usr/local/bin/kissat
RUN git clone "${CADICAL_REPO}" /cadical \
 && cd /cadical && git checkout ${CADICAL_REF} \
 && ./configure && make -j"$(nproc)" \
 && install -m 0755 build/cadical /usr/local/bin/cadical

# ---------------------------------------------------------------------------
# Stage 3 — vibeic/ngspice (batch-honesty rc + $& scalar + control-mode .param + native MC)
# ---------------------------------------------------------------------------
# Record every pin this artefact was built from.
RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"sat-solvers","repo":"%s","ref":"%s","cadical_repo":"%s","cadical_ref":"%s"}\n' \
      "${KISSAT_REPO}" "${KISSAT_REF}" "${CADICAL_REPO}" "${CADICAL_REF}" \
      > /vibeic/provenance/sat-solvers.json

# ---------------------------------------------------------------------------
# The published layer carries the tool payload and nothing else — no compiler,
# no source tree, no apt lists. `scratch` enforces that by construction.
#
# The provenance file records what produced this artefact INSIDE it: a tag can
# be moved, a file in the image cannot, so the composing image can assert that
# what it copied is what it asked for.
# ---------------------------------------------------------------------------
FROM scratch
COPY --from=build /usr/local/bin/kissat /usr/local/bin/kissat
COPY --from=build /usr/local/bin/cadical /usr/local/bin/cadical
COPY --from=build /vibeic/provenance/sat-solvers.json /vibeic/provenance/sat-solvers.json
