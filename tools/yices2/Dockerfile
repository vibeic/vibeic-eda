# eda-tool-yices2 — one tool, one image, keyed on the commit that built it.
#
# THE FIFTH AND WORST OF THE FORKED-BUT-NOT-BUILT SET, because its inventory row
# read stronger than the others'. `docs/TOOL_INVENTORY.md` Table A listed `yices`
# with origin "added by us", which reads as a stronger provenance claim than the
# "iic-osic-tools base" on gtkwave, slang, xschem and Xyce — while the binaries
# were entirely the base image's. The cause is that `inventory.py` decides origin
# by "is this directory name absent from the base", which is a correct statement
# about DIRECTORIES and a misleading one about PROVENANCE: we created
# /foss/tools/yices, we did not create what is in it.
#
# What the composing Dockerfile did, and what this replaces: upstream builds
# yices2 INSIDE the yosys prefix (its install.sh configures
# `--prefix=${TOOLS}/yosys`), so the binaries arrived at /foss/tools/yosys/bin.
# Our build replaces the yosys prefix wholesale, which is what deleted them and
# left the four dangling symlinks of vibeic-eda#13. The workaround was to `cp -a`
# them out of the base's yosys before the delete — a comment in that step says
# outright "they are not ours to rebuild". They are now.
#
# This matters beyond tidiness: yices is the DEFAULT smtbmc engine for sby, so
# every step-5 formal proof resolves through it. #13 was exactly this binary
# going missing, and the failure surfaced as a failed proof rather than as a
# missing file.

FROM ubuntu:24.04 AS build

ARG YICES2_REPO=https://github.com/vibeic/yices2.git
ARG YICES2_REF=05178c03ddf49c6bba63c5c7153774c11a5da12d

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git autoconf automake libtool gperf ca-certificates \
      libgmp-dev \
 && rm -rf /var/lib/apt/lists/*

# Upstream's own steps (`_build/images/yosys/scripts/install.sh`), with one
# change and only one: the prefix. Upstream installs into the yosys prefix
# because it builds both in one stage; here yices2 is its own artefact, so it
# gets its own directory and stops being a lodger in another tool's tree.
RUN git clone --filter=blob:none "${YICES2_REPO}" /yices2 \
 && cd /yices2 && git checkout ${YICES2_REF} \
 && autoconf \
 && ./configure --prefix=/foss/tools/yices \
 && make -j"$(nproc)" \
 && make install \
 && echo "yices2 ${YICES2_REF}" > /foss/tools/yices/SOURCES

# The four binaries the image's PATH symlinks name. Asserted here rather than
# discovered later: a missing one becomes a dangling /foss/tools/bin link, which
# is #13 again, and `command -v` does not see it because a broken symlink is
# still a directory entry.
RUN for t in yices yices-sat yices-smt yices-smt2; do \
      test -x "/foss/tools/yices/bin/$t" || { echo "MISSING /foss/tools/yices/bin/$t"; exit 1; }; \
    done; \
    /foss/tools/yices/bin/yices-smt2 --version

RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"yices2","repo":"%s","ref":"%s"}\n' \
      "${YICES2_REPO}" "${YICES2_REF}" \
      > /vibeic/provenance/yices2.json

FROM scratch
COPY --from=build /foss/tools/yices /foss/tools/yices
COPY --from=build /vibeic/provenance/yices2.json /vibeic/provenance/yices2.json
