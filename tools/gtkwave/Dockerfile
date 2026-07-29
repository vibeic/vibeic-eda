# eda-tool-gtkwave — one tool, one image, keyed on the commit that built it.
#
# WHY THIS FILE EXISTS. We forked `gtkwave/gtkwave` on 2026-07-28, and the image
# went on shipping the BASE image's build of it — so the fork existed and nothing
# consumed it. `tools/check_fork_only.py` could not see that: it checks what we
# BUILD and what we COPY, and says in its own docstring that it is blind to the
# ~35 tools the base supplies. A tool we have forked but do not build from is
# exactly the shape that blindness hides.
#
# The build steps are upstream IIC-OSIC-TOOLS' own recipe
# (`_build/images/gtkwave/scripts/install.sh`), reproduced here rather than
# reinvented, and pointed at our fork. Keeping their steps is the point: this
# change is meant to alter WHERE the source comes from and nothing else.
#
# The pin is the commit the base image ACTUALLY ships, read out of the image
# (`/foss/tools/gtkwave/SOURCES` -> 7d7b4db9), not the one in upstream's current
# Dockerfile. Those differ — the base was built from an older tree — and pinning
# to the recipe rather than to the artefact would have silently changed the
# version while claiming only to change the source.

FROM ubuntu:24.04 AS build

ARG GTKWAVE_REPO=https://github.com/vibeic/gtkwave.git
ARG GTKWAVE_REF=7d7b4db9e2f5485afe2aeeab0ad112f5b6a9b94b

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git meson ninja-build pkg-config gperf flex bison patchelf \
      desktop-file-utils dpkg-dev ca-certificates \
      libgtk-4-dev libbz2-dev liblzma-dev libjudy-dev zlib1g-dev \
      libgirepository1.0-dev gobject-introspection \
 && rm -rf /var/lib/apt/lists/*

# `mkdir build/include` between setup and compile is upstream's, not a typo:
# the generated headers land there and meson does not create it.
RUN git clone --filter=blob:none "${GTKWAVE_REPO}" /gtkwave \
 && cd /gtkwave && git checkout ${GTKWAVE_REF} \
 && meson setup build --prefix=/foss/tools/gtkwave \
 && mkdir -p build/include \
 && meson compile -C build \
 && meson install -C build

# rpath so the binaries find their own libgtkwave rather than whatever the
# composing image happens to have. Upstream does this and dropping it would move
# the failure to run time, in a container where it is hardest to diagnose.
RUN LIBDIR="/foss/tools/gtkwave/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH)" \
 && patchelf --set-rpath "${LIBDIR}" "${LIBDIR}/libgtkwave.so" \
 && patchelf --set-rpath "${LIBDIR}" /foss/tools/gtkwave/bin/rtlbrowse \
 && echo "gtkwave ${GTKWAVE_REF}" > /foss/tools/gtkwave/SOURCES

RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"gtkwave","repo":"%s","ref":"%s"}\n' \
      "${GTKWAVE_REPO}" "${GTKWAVE_REF}" \
      > /vibeic/provenance/gtkwave.json

FROM scratch
COPY --from=build /foss/tools/gtkwave /foss/tools/gtkwave
COPY --from=build /vibeic/provenance/gtkwave.json /vibeic/provenance/gtkwave.json
