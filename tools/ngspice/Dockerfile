# eda-tool-ngspice — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-ngspice:<NGSPICE_REF>`. The tag IS the pin:
# "which ngspice is in the EDA image" is answered by reading the tag, not by
# trusting a comment next to an ARG.
#
# The composing image does `COPY --from=ghcr.io/vibeic/eda-tool-ngspice:<sha>`,
# so a release is a set of COPYs from immutable digests rather than a rebuild of
# everything. Before this split, `release.yml` ran `docker/build-push-action`
# with no `cache-from` and no `cache-to` (measured: 0 occurrences), so moving one
# character of one pin rebuilt all eight tools from source.
#
# Source is `github.com/vibeic/ngspice` and nothing else. Per the owner rule of
# 2026-07-29 the image consumes only our forks; a build arg pointing anywhere
# else is a policy violation, and `tools/check_fork_only.py` fails the build on
# one rather than leaving it to review.

FROM ubuntu:24.04 AS build

# Pinned to a commit, never a branch: a branch would make the image's contents
# depend on when it was built.
ARG NGSPICE_REPO=https://github.com/vibeic/ngspice.git
ARG NGSPICE_REF=eccb8e6ecb399abfcdf241db636d5671a8861d9c  # branch master -- vibeic/batch-honesty-integration is merged into master, and master tip IS this commit (0 SHA diff, measured).
# AC (5.5x), #68 hardened DSPF, w4/w5. `make check` 54/54 at this SHA.

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git autoconf automake libtool bison flex \
      libx11-dev libxaw7-dev libreadline-dev libncurses-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN git clone "${NGSPICE_REPO}" /ngspice \
 && cd /ngspice && git checkout ${NGSPICE_REF} \
 && ./autogen.sh \
 && ./configure --prefix=/foss/tools/ngspice --with-ngshared=no --enable-xspice --enable-openmp --disable-debug \
 && make -j"$(nproc)" && make install

# Record what produced this artefact INSIDE the artefact. A tag can be moved; a
# file in the image cannot be, so the composing image can assert that what it
# copied is what it asked for.
RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"ngspice","repo":"%s","ref":"%s"}\n' "${NGSPICE_REPO}" "${NGSPICE_REF}" \
      > /vibeic/provenance/ngspice.json

# ---------------------------------------------------------------------------
# The published layer carries the tool directory and nothing else — no compiler,
# no source tree, no apt lists. `scratch` keeps it that way by construction: an
# artefact that inherited a base could quietly grow a second copy of something
# the composing image already has.
# ---------------------------------------------------------------------------
FROM scratch
COPY --from=build /foss/tools/ngspice /foss/tools/ngspice
COPY --from=build /vibeic/provenance/ngspice.json /vibeic/provenance/ngspice.json
