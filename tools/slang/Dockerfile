# eda-tool-slang — one tool, one image, keyed on the commit that built it.
#
# See tools/gtkwave/Dockerfile for why these four moved. slang is the load-bearing
# one of the four: it is the SystemVerilog front end, so an elaboration it rejects
# is an RTL our synthesis cannot read. Shipping the base image's build of a
# front end we had forked meant the one tool we most needed to be able to patch
# was the one we were not building.
#
# NOTE ON SCOPE, because the name collides. This is `MikePopoloski/slang`, the
# standalone front end at `/foss/tools/slang`. It is NOT the yosys plugin — that
# is `povik/sv-elab` (renamed from `povik/yosys-slang`), which the base ships at
# `/foss/tools/slang-yosys-plugin` and which yosys vendors separately. Building
# one does not build the other; the plugin remains on the base's build and is
# recorded as such rather than quietly implied to be fixed here.
#
# Boost is fetched as a source tarball and used HEADER-ONLY, which is upstream's
# choice and worth keeping: it avoids pinning the image to whatever libboost-dev
# the distro happens to carry, and slang needs only the headers.
#
# The pin is what the base image ACTUALLY ships (`/foss/tools/slang/SOURCES` ->
# 24809c8e), newer than the commit in upstream's current Dockerfile.

FROM ubuntu:24.04 AS build

ARG SLANG_REPO=https://github.com/vibeic/slang.git
ARG SLANG_REF=d69e89069834a07e0fe9518bd33e343b514e7007
ARG BOOST_VERSION=1.88.0

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git cmake ninja-build python3 wget ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Headers only — `b2 headers` stages them, nothing is compiled or linked.
RUN cd /tmp \
 && BOOST_DIR="boost_$(echo ${BOOST_VERSION} | tr . _)" \
 && wget -q "https://archives.boost.io/release/${BOOST_VERSION}/source/${BOOST_DIR}.tar.gz" \
 && tar xf "${BOOST_DIR}.tar.gz" \
 && cd "${BOOST_DIR}/tools/build" && ./bootstrap.sh gcc \
 && cd ../.. && tools/build/b2 -j"$(nproc)" headers \
 && echo "${BOOST_DIR}" > /tmp/boost_dir

RUN git clone --filter=blob:none "${SLANG_REPO}" /slang \
 && cd /slang && git checkout ${SLANG_REF} \
 && B="/tmp/$(cat /tmp/boost_dir)" \
 && cmake -B build -DSLANG_INCLUDE_TESTS=OFF \
      -DBoost_ROOT="${B}" -DBoost_NO_SYSTEM_PATHS=ON \
      -DBOOST_INCLUDEDIR="${B}" -DCMAKE_CXX_FLAGS="-isystem ${B}" \
 && cmake --build build -j"$(nproc)" \
 && cmake --install build --strip --prefix=/foss/tools/slang \
 && echo "slang ${SLANG_REF}" > /foss/tools/slang/SOURCES

RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"slang","repo":"%s","ref":"%s"}\n' \
      "${SLANG_REPO}" "${SLANG_REF}" \
      > /vibeic/provenance/slang.json

FROM scratch
COPY --from=build /foss/tools/slang /foss/tools/slang
COPY --from=build /vibeic/provenance/slang.json /vibeic/provenance/slang.json
