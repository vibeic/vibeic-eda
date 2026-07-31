# eda-tool-fastercap — 3D capacitance extraction, built from OUR fork.
#
# Until now the shipped image's FasterCap came from the BASE image's build
# (/foss/tools/rftoolkit/bin/FasterCap, 469 624 bytes, dated Jun 2), while the
# registry recorded FasterCap as a fork we track. A fork we never build is a fork
# only on paper: an upstream fix we merge would never reach a user, and a patch of
# our own would live in a repo nobody compiles. `check_fork_only` cannot see this
# shape, because the tool IS present and IS the right version string — it is just
# somebody else's binary.
FROM ubuntu:24.04 AS build

# THREE repositories, because upstream splits the program across three and the
# CMakeLists says so:
#
#     include_directories ("../LinAlgebra")
#     add_subdirectory("../LinAlgebra" LinAlgebra)
#     include_directories ("../Geometry")
#     add_subdirectory("../Geometry" Geometry)
#
# `../` — SIBLINGS of the checkout, not subdirectories of it. We had forked only
# FasterCap, so `cmake -S .` died at `add_subdirectory given source
# "../Geometry" which is not an existing directory`, and the image had no choice
# but to keep consuming the base image's binary. Nobody noticed, because nobody
# had tried to build it from our side.
#
# LinAlgebra and Geometry were mirrored 2026-08-01 and added to FORKS.json, so
# the daily tick now tracks upstream fixes to them too. Forking one third of a
# program and calling the program forked is the same shape as the rest of this
# work: the registry said we had it, and what we had could not build.
ARG FASTERCAP_REPO=https://github.com/vibeic/FasterCap.git
ARG FASTERCAP_REF=595ea4348196b4f9a2b51c3d4768f4edb1746fb5
ARG LINALGEBRA_REPO=https://github.com/vibeic/LinAlgebra.git
ARG LINALGEBRA_REF=627132d70bfd7eadd727f930286938a5a01d9914
ARG GEOMETRY_REPO=https://github.com/vibeic/Geometry.git
ARG GEOMETRY_REF=de03ffebfd5013b96102bd60f71c8fe8b73870e2

# wxWidgets is REQUIRED, not optional. The first build here omitted it and cmake
# stopped at `wxWidgets requested but not found` — and the tempting fix was to
# turn the GUI off and ship a smaller binary. Measured against the base image's
# build first: `ldd /foss/tools/bin/FasterCap` resolves a wx library, so the
# binary users have today IS the wx-linked one. Building without it would have
# replaced a tool with a quieter tool while every version string still matched.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git cmake ca-certificates \
      libwxgtk3.2-dev \
 && rm -rf /var/lib/apt/lists/*

# Laid out as siblings under /src, which is the layout the CMakeLists assumes.
RUN mkdir -p /src \
 && git clone --filter=blob:none "${FASTERCAP_REPO}"  /src/FasterCap \
 && git clone --filter=blob:none "${LINALGEBRA_REPO}" /src/LinAlgebra \
 && git clone --filter=blob:none "${GEOMETRY_REPO}"   /src/Geometry \
 && git -C /src/FasterCap  checkout ${FASTERCAP_REF} \
 && git -C /src/LinAlgebra checkout ${LINALGEBRA_REF} \
 && git -C /src/Geometry   checkout ${GEOMETRY_REF} \
 && cd /src/FasterCap \
 # The top-level `find_package(wxWidgets ...)` sets include dirs for THIS
 # directory; `add_subdirectory("../LinAlgebra")` and `../Geometry` bring in
 # trees whose own CMakeLists never ask for wx, and both include <wx/debug.h>.
 # cmake configured cleanly and the compile then died on a missing header in the
 # SUBPROJECTS. Push wx's flags in globally so every target sees them.
 #
 # `--version=3.0` is what the CMakeLists requests; Ubuntu 24.04 ships 3.2, so
 # the config query is made without pinning a version rather than by editing
 # upstream's file — changing the recipe and the source in one step would leave
 # no way to attribute a later failure to either. (The ONE source change we do
 # carry is `vibeic/wx32-compat`, which drops `wxTE_AUTO_SCROLL`; wx deleted it
 # in 3.2 and it had been a no-op since 3.0.)
 #
 # LIBS, not just cxxflags. With the flags pushed in but the libraries left to
 # the top-level target alone, every translation unit compiled wx's assert
 # machinery in and the final link died on `undefined reference to
 # wxTrapInAssert` — a symbol verified present in libwx_baseu-3.2. Half the
 # toolchain settings reaching every target and the other half reaching one is
 # what produced that; both halves go in globally.
 #
 # STANDARD_LIBRARIES, not EXE_LINKER_FLAGS. The latter is emitted BEFORE the
 # object files, and ld resolves left-to-right: the libraries were on the line
 # but had already been passed over by the time the objects that needed them
 # were read, so the link still failed — on a DIFFERENT set of symbols
 # (wxNewId, ~wxArrayString), which is what a position bug looks like as
 # opposed to a missing-library one. CMAKE_CXX_STANDARD_LIBRARIES appends to
 # the END of every link line, which is where they have to be.
 && WXFLAGS="$(wx-config --cxxflags)" \
 && WXLIBS="$(wx-config --libs)" \
 && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_CXX_FLAGS="${WXFLAGS}" \
      -DCMAKE_CXX_STANDARD_LIBRARIES="${WXLIBS}" \
      -DCMAKE_INSTALL_PREFIX=/foss/tools/fastercap \
 && cmake --build build -j"$(nproc)" \
 # Upstream's CMakeLists declares `add_executable(FasterCap ...)` and NO
 # install() rule anywhere across the three repos, so `cmake --install build`
 # exits 0, prints `-- Install configuration: "Release"`, and copies nothing.
 # A zero exit that delivers no artifact is the same shape as the rest of this
 # work; the install is done explicitly and the binary is required to be there.
 # INSTALLED WHERE THE BASE IMAGE'S BINARY ACTUALLY LIVES. The base ships
 # /foss/tools/bin/FasterCap as a SYMLINK to /foss/tools/rftoolkit/bin/FasterCap
 # (469 624 bytes, dated Jun 2). An overlay under /foss/tools/fastercap/ would
 # sit BESIDE that and change nothing: the symlink still resolves to the base's
 # build, every version string still matches, and the image would report a tool
 # we build while users run a tool we don't. Both paths are written, so whichever
 # one the consumer resolves reaches OUR binary.
 && install -d /foss/tools/fastercap/bin /foss/tools/rftoolkit/bin \
 && install -m 0755 build/FasterCap /foss/tools/fastercap/bin/FasterCap \
 && install -m 0755 build/FasterCap /foss/tools/rftoolkit/bin/FasterCap \
 && printf 'fastercap %s\nlinalgebra %s\ngeometry %s\n' \
      "${FASTERCAP_REF}" "${LINALGEBRA_REF}" "${GEOMETRY_REF}" \
      > /foss/tools/fastercap/SOURCES

# Capability, not presence: the binary must RUN, must be wx-linked like the build
# it replaces, and must be present at BOTH paths — the one the base image's
# symlink resolves to is the one that decides which binary users actually get,
# so an assertion that only checks our own overlay path would pass on exactly
# the failure it exists to catch.
RUN set -e; \
    for BIN in /foss/tools/fastercap/bin/FasterCap \
               /foss/tools/rftoolkit/bin/FasterCap; do \
      [ -x "$BIN" ] || { echo "FAIL: no FasterCap at $BIN"; \
                         find /foss/tools -name 'FasterCap*' | head -20; exit 1; }; \
      "$BIN" -v 2>&1 | head -1; \
      ldd "$BIN" | grep -qi wx \
        || { echo "FAIL: $BIN is not linked against wxWidgets -- the base image's"; \
             echo "      build is, and dropping it would ship a quieter tool under"; \
             echo "      the same name"; ldd "$BIN" | head -20; exit 1; }; \
    done; \
    cmp -s /foss/tools/fastercap/bin/FasterCap /foss/tools/rftoolkit/bin/FasterCap \
      || { echo "FAIL: the two install paths hold DIFFERENT binaries"; exit 1; }; \
    echo "fastercap: runs at both paths, wx-linked, identical"

RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"fastercap","repo":"%s","ref":"%s"}\n' \
      "${FASTERCAP_REPO}" "${FASTERCAP_REF}" > /vibeic/provenance/fastercap.json

FROM scratch
COPY --from=build /foss/tools/fastercap /foss/tools/fastercap
COPY --from=build /foss/tools/rftoolkit/bin/FasterCap /foss/tools/rftoolkit/bin/FasterCap
COPY --from=build /vibeic/provenance/fastercap.json /vibeic/provenance/fastercap.json
