# eda-tool-openroad — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-openroad:<ref>`. The tag IS the pin: "which
# openroad is in the EDA image" is answered by reading the tag, not by trusting a
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

FROM openroad/ubuntu24.04-dev:latest AS build
# Pinned to the F-A2 commit (tapcell -bound_to_placement) shipped in vibeic-eda:0.2.7,
# a fast-forward over the F-A1 repair_antennas -reroute commit (0.2.6).
# HEAD of branch vibeic/post-route-detailed-routing-repair as of 2026-07-09.
ARG OPENROAD_REPO=https://github.com/vibeic/OpenROAD.git
ARG OPENROAD_REF=92b079b47a1c1c470eb9fb0d32613a0b27379ad6  # pinned; branch vibeic/openroad-integration (0.2.22 consolidation: route DR6 + place FP2 + pdn PD2/PD4 + timing/fill/cts/dpl-leak; src/sta=1e21c3f superset fetched from vibeic/OpenSTA fork; commit-msg NDA-purged 2026-07-19)
RUN git clone "${OPENROAD_REPO}" /src \
 && cd /src && git checkout ${OPENROAD_REF} \
 && git submodule update --init --recursive --depth 1 \
 && ./etc/Build.sh -threads=$(nproc) -cmake="-DCMAKE_BUILD_TYPE=Release" \
      -prefix=/vibeic-openroad

# ---------------------------------------------------------------------------
# Stage 2 — vibeic/yosys (tri-state fanin preservation + modern slang SV frontend)
# ---------------------------------------------------------------------------
# Record every pin this artefact was built from.
RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"openroad","repo":"%s","ref":"%s"}\n' \
      "${OPENROAD_REPO}" "${OPENROAD_REF}" \
      > /vibeic/provenance/openroad.json

# ---------------------------------------------------------------------------
# The published layer carries the tool payload and nothing else — no compiler,
# no source tree, no apt lists. `scratch` enforces that by construction.
#
# The provenance file records what produced this artefact INSIDE it: a tag can
# be moved, a file in the image cannot, so the composing image can assert that
# what it copied is what it asked for.
# ---------------------------------------------------------------------------
FROM scratch
COPY --from=build /opt/or-tools /opt/or-tools
# THE ARTEFACT'S OWN PATH, not upstream's build layout. `etc/Build.sh`
# defaults to BAZEL since the 2026-07-29 daily merge and installs to
# /install/OpenROAD; the CMake tree at /src/build/bin no longer exists, and
# the build failed here with `failed to calculate checksum of ref ...
# /src/build/bin/openroad: not found` AFTER compiling all 11,875 targets.
# Published at its FINAL runtime path, matching every other artefact here
# (yosys -> /foss/tools/yosys, iverilog -> /foss/tools/iverilog). An earlier
# attempt used /vibeic/bin/openroad and the compose step failed with `cannot
# copy to non-directory`: /vibeic/ is the shared PROVENANCE namespace every
# artefact writes a file into, so a payload directory cannot also live there.
COPY --from=build /vibeic-openroad/bin/openroad /foss/tools/openroad/bin/openroad
COPY --from=build /vibeic/provenance/openroad.json /vibeic/provenance/openroad.json
