# eda-tool-xyce — one tool, one image, keyed on the commit that built it.
#
# See tools/gtkwave/Dockerfile for why these four moved: forked, and the image
# still consumed the base image's build, which `check_fork_only` is blind to.
#
# THE HONEST CAVEAT, stated at the top because it is the one thing a reader must
# not miss: **Xyce builds from our fork, Trilinos does not.** Xyce cannot be
# built without Trilinos, `vibeic/Trilinos` does not exist, and creating it is
# blocked — `gh repo fork trilinos/Trilinos` still returns HTTP 403 "You cannot
# fork this repository at this time", and at 853 MB it is also the size where
# the mirror route used for the six sources in 9861b90 was reported to fail on
# large files. So this file moves Xyce itself to our fork and leaves its largest
# dependency upstream. That is a real improvement and an incomplete one, and
# `tools/PENDING_FORKS.json` carries Trilinos with a date rather than this file
# carrying a silence.
#
# The reconfigure scripts under `scripts/` are upstream IIC-OSIC-TOOLS' own,
# copied unmodified. They are not tidied or "improved": changing build flags in
# the same commit that changes the source would alter both the input and the
# output, leaving no way to attribute a behavioural difference to either.
#
# BUILD COST: Trilinos plus Xyce is the longest build in this repo by a wide
# margin — hours, not minutes. That cost is exactly what the per-tool split was
# for. Before it, this would have been paid on every release; now it is paid
# once per commit of `vibeic/Xyce`, and every release that does not move the pin
# copies the artefact instead of rebuilding it.

# TRILINOS IS ITS OWN STAGE, and the reason is the same argument as the per-tool
# split one level down: it has its own upstream and its own pin, it is by far the
# longest part of this build, and nothing about Xyce's own dependencies should
# force it to recompile.
#
# That is not hypothetical tidiness — it was paid for twice. Adding `libfl-dev`
# and then `adms` to a single shared apt layer invalidated Trilinos both times
# and cost a full rebuild each, to fix a failure that happened minutes from the
# end. With the split, a missing Xyce dependency costs the Xyce layer only.
FROM ubuntu:24.04 AS trilinos

ARG TRILINOS_REPO=https://github.com/vibeic/Trilinos.git
ARG TRILINOS_REF=89b8c7f016c247568f7c9c1f32d250c8d2683de0  # branch vibeic/xyce-trilinos-12.12 -- Trilinos 12.12.1. NOT master: the development line removed AztecOO, Amesos, Ifpack, EpetraExt and Isorropia, and trilinos.reconfigure.sh enables all five, so a master pin fails configure. Xyce here is built with autotools, whose minimum is NOT the cmake path 13.5. Branch created 2026-07-31 so the pin hangs off a branch instead of being an orphan.

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git cmake gfortran ca-certificates \
      libopenmpi-dev openmpi-bin \
      libblas-dev liblapack-dev libsuitesparse-dev \
 && rm -rf /var/lib/apt/lists/*

ENV XYCE_NAME=xyce
COPY scripts/ /images/xyce/scripts/

RUN mkdir -p /tmp/xyce \
 && cd /tmp/xyce \
 && git clone --filter=blob:none "${TRILINOS_REPO}" trilinos \
 && cd trilinos && git checkout ${TRILINOS_REF} \
 && mkdir -p parallel_build && cd parallel_build \
 && cp /images/xyce/scripts/trilinos.reconfigure.sh ./reconfigure.sh \
 && chmod +x reconfigure.sh && ./reconfigure.sh \
 && make -j"$(nproc)" && make install \
 && rm -rf /tmp/xyce/trilinos

FROM ubuntu:24.04 AS build

ARG XYCE_REPO=https://github.com/vibeic/Xyce.git
ARG XYCE_REF=d72b5846a0397ddf852a49305cb6f395457685ca
ARG TRILINOS_REPO=https://github.com/vibeic/Trilinos.git
ARG TRILINOS_REF=89b8c7f016c247568f7c9c1f32d250c8d2683de0  # branch vibeic/xyce-trilinos-12.12 -- Trilinos 12.12.1. NOT master: the development line removed AztecOO, Amesos, Ifpack, EpetraExt and Isorropia, and trilinos.reconfigure.sh enables all five, so a master pin fails configure. Xyce here is built with autotools, whose minimum is NOT the cmake path 13.5. Branch created 2026-07-31 so the pin hangs off a branch instead of being an orphan.
# one-at-a-time discovery:
#   libfl-dev  FlexLexer.h — the flex C++ header ships separately from the flex
#              binary, and Xyce's ReactionNetwork includes it.
#   adms       admsXml, which `--enable-user-plugin` invokes to generate the ADMS
#              device models. Xyce itself links without it; only the plugin
#              directory needs it, so the build fails at the very end.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git cmake gfortran ca-certificates \
      autoconf automake libtool libtool-bin flex bison libfl-dev adms bc \
      libopenmpi-dev openmpi-bin \
      libblas-dev liblapack-dev libfftw3-dev libsuitesparse-dev \
 && rm -rf /var/lib/apt/lists/*

ENV XYCE_NAME=xyce
COPY scripts/ /images/xyce/scripts/
# Staged outside /tmp/xyce because git will not clone into a non-empty directory,
# then moved into place — the reconfigure script hard-codes
# ARCHDIR=/tmp/$XYCE_NAME/xycelibs/parallel and that script is kept unmodified.
COPY --from=trilinos /tmp/xyce/xycelibs /xycelibs

RUN git clone --filter=blob:none "${XYCE_REPO}" /tmp/xyce \
 && mv /xycelibs /tmp/xyce/xycelibs \
 && cd /tmp/xyce && git checkout ${XYCE_REF} \
 && ./bootstrap

RUN cd /tmp/xyce \
 && mkdir -p parallel_build && cd parallel_build \
 && cp /images/xyce/scripts/xyce.reconfigure.sh ./reconfigure.sh \
 && chmod +x reconfigure.sh \
 && TOOLS=/foss/tools ./reconfigure.sh \
 && cd src/DeviceModelPKG/ADMS && make -j"$(nproc)" \
 && cd ../../.. && make -j"$(nproc)" && make install \
 && find /foss/tools/xyce -type f -name "*.a" -delete \
 && printf 'xyce %s\ntrilinos %s\n' "${XYCE_REF}" "${TRILINOS_REF}" \
      > /foss/tools/xyce/SOURCES

# `trilinos_repo`/`trilinos_ref` are recorded even though they are not ours, so
# `check_image_provenance` reports the shipped image honestly instead of
# implying every input was.
RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"xyce","repo":"%s","ref":"%s","trilinos_repo":"%s","trilinos_ref":"%s"}\n' \
      "${XYCE_REPO}" "${XYCE_REF}" "${TRILINOS_REPO}" "${TRILINOS_REF}" \
      > /vibeic/provenance/xyce.json

FROM scratch
COPY --from=build /foss/tools/xyce /foss/tools/xyce
COPY --from=build /vibeic/provenance/xyce.json /vibeic/provenance/xyce.json
