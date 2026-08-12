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
ARG FASTERCAP_REF=afca8f5e55bbc8e19555f65fc29962fb475c8ebb
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
 #
 # HEADLESS, AND THE `base` wx COMPONENT TO MATCH. Upstream's README is explicit
 # for exactly this environment — "Linux 64 bits headless: you **must** set the
 # switch FASTFIELDSOLVERS_HEADLESS as ON, as in a headless Linux distro you have
 # no drivers at all for video" — and the first version of this recipe did not,
 # so it produced the GUI build. wxWidgets initialises its GUI before FasterCap
 # parses argv, so in a container EVERY entry point except `-b` died at
 # `Unable to initialize GTK+, is DISPLAY set properly?` with rc=255 and an
 # empty stdout. Measured on the binary this recipe replaced (the base image's,
 # /foss/tools/bin/FasterCap, 469 624 bytes) the same invocations answer
 # normally: `-v` -> rc=74 with the banner and a named file error, `-h` -> rc=64
 # with the usage text, no arguments -> rc=64 with "launched without enough
 # arguments". So the base image, which carries a VNC desktop and could have
 # shipped the GUI build, shipped the headless one; going headless here is a
 # return to what users had, not a capability we are taking away. (Our headless
 # rebuild is 469 624 bytes too.)
 #
 # `--libs base` / `--cxxflags base`, not the bare query. The bare form resolves
 # to -lwx_gtk3u_core and friends, so the binary links GTK regardless of what
 # cmake was told, and `--cxxflags base` is also where wx's own -DwxUSE_GUI=0
 # comes from. Passing the switch without the components leaves the GUI
 # libraries on the link line and the defect in place.
 && WXFLAGS="$(wx-config --cxxflags base)" \
 && WXLIBS="$(wx-config --libs base)" \
 && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
      -DFASTFIELDSOLVERS_HEADLESS=ON \
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
#
# TWO THINGS WERE WRONG HERE UNTIL 2026-08-12, and they hid each other.
#
# 1. THE GATE COULD NOT FAIL. The line was
#        "$BIN" -v 2>&1 | head -1;
#    and a pipeline's status is its LAST command's, so `head` returned 0 no
#    matter what the binary did. Under `set -e` that reads as an assertion and
#    is not one: measured in image 0.2.84, both binaries exit 255 here and the
#    build went green anyway.
#
# 2. `-v` WAS THE WRONG PROBE FOR A HEADLESS IMAGE. It routes through the GUI's
#    about-box path, so in a container with no DISPLAY it dies before printing
#    anything:
#        $ FasterCap -v ; echo $?
#        21:41:39: Error: Unable to initialize GTK+, is DISPLAY set properly?
#        255
#    Fixing only (1) would therefore have turned a silent gate into a red build.
#    The first repair moved the probe onto `-b`, the one entry point a GUI build
#    still reaches without a display, and declared the tool healthy. That was the
#    wrong conclusion, and it is repaired properly above: `-b` was not the only
#    thing users touch, it was the only thing left working.
#
# 3. THE PROBE MUST ASSERT THE PROPERTY, NOT THE ONE SURVIVING PATH. What a
#    headless image owes its users is that the tool STARTS without a display,
#    whatever you type — so that is what is asserted here, on the plainest
#    invocation there is (no arguments at all, which is what a doctor probe or a
#    confused human runs). Upstream's own headless build answers it:
#        $ FasterCap ; echo $?
#        FasterCap version 6.0.7
#        FasterCap launched without enough arguments! ...
#        64
#    A usage error with a name on it is a healthy tool; rc=255 and an empty
#    stdout is not, and no exit code alone can tell those apart — hence the
#    assertion is on the CONTENT (banner present, display error absent) with the
#    status captured before any pipe.
#
# 4. AND THE SOLVER IS ASSERTED ON ITS VALUE. `FasterCap -b` with no input file
#    is not a smoke test; it is the tool declining to work. (In the GUI build it
#    even exited 0 while doing so, which is how it came to be used as a gate.)
#    The probe below solves a real two-plate geometry and requires a capacitance
#    matrix to come out, so the number-producing path is covered, not just the
#    process-starts path.
#
# 5. LINKAGE, BOTH DIRECTIONS. wx must still be there (the base image's build is
#    wx-linked, and dropping it would ship a quieter tool under the same name),
#    and GTK must NOT be — that is the structural signature of the headless
#    build, and naming it makes a future recipe that loses
#    -DFASTFIELDSOLVERS_HEADLESS=ON fail with the cause printed rather than with
#    a runtime GTK error to interpret. It is also the only form of this check
#    that still holds if the builder ever has a DISPLAY of its own.
RUN set -e; \
    mkdir -p /tmp/fastercap-smoke; \
    printf '0 lower plate\nQ g1 0.0 0.0 0.0  1.0 0.0 0.0  1.0 1.0 0.0  0.0 1.0 0.0\n' \
      > /tmp/fastercap-smoke/plate1.txt; \
    printf '0 upper plate\nQ g2 0.0 0.0 1.0  1.0 0.0 1.0  1.0 1.0 1.0  0.0 1.0 1.0\n' \
      > /tmp/fastercap-smoke/plate2.txt; \
    printf '0 two parallel plates\nC plate1.txt 1.0 0.0 0.0 0.0\nC plate2.txt 1.0 0.0 0.0 0.0\n' \
      > /tmp/fastercap-smoke/two.lst; \
    for BIN in /foss/tools/fastercap/bin/FasterCap \
               /foss/tools/rftoolkit/bin/FasterCap; do \
      [ -x "$BIN" ] || { echo "FAIL: no FasterCap at $BIN"; \
                         find /foss/tools -name 'FasterCap*' | head -20; exit 1; }; \
      "$BIN" < /dev/null > /tmp/fastercap-nodisplay.out 2>&1 && rc=0 || rc=$?; \
      grep -q 'FasterCap version' /tmp/fastercap-nodisplay.out \
        || { echo "FAIL: $BIN printed no version banner when run with no"; \
             echo "      arguments (rc=$rc) — in a container with no DISPLAY this"; \
             echo "      is indistinguishable from a broken install. Build the"; \
             echo "      headless variant (-DFASTFIELDSOLVERS_HEADLESS=ON), which"; \
             echo "      is what upstream's README requires for a headless Linux."; \
             cat /tmp/fastercap-nodisplay.out; exit 1; }; \
      ! grep -qiE 'unable to initialize|is DISPLAY set' /tmp/fastercap-nodisplay.out \
        || { echo "FAIL: $BIN needs an X display to start (rc=$rc)"; \
             cat /tmp/fastercap-nodisplay.out; exit 1; }; \
      ( cd /tmp/fastercap-smoke && "$BIN" -b two.lst -a0.01 < /dev/null ) \
        > /tmp/fastercap-solve.out 2>&1 && rc=0 || rc=$?; \
      [ "$rc" = 0 ] \
        || { echo "FAIL: $BIN -b exited $rc on the two-plate smoke geometry"; \
             tail -30 /tmp/fastercap-solve.out; exit 1; }; \
      grep -q 'Capacitance matrix is:' /tmp/fastercap-solve.out \
        || { echo "FAIL: $BIN -b produced no capacitance matrix — it ran and"; \
             echo "      extracted nothing, which is what a broken solver looks like"; \
             tail -30 /tmp/fastercap-solve.out; exit 1; }; \
      head -1 /tmp/fastercap-nodisplay.out; \
      ldd "$BIN" | grep -qi wx \
        || { echo "FAIL: $BIN is not linked against wxWidgets -- the base image's"; \
             echo "      build is, and dropping it would ship a quieter tool under"; \
             echo "      the same name"; ldd "$BIN" | head -20; exit 1; }; \
      ! ldd "$BIN" | grep -qi gtk \
        || { echo "FAIL: $BIN is linked against GTK — this is the GUI build, and"; \
             echo "      in a container it dies at GUI init on every entry point"; \
             echo "      except -b. Configure with -DFASTFIELDSOLVERS_HEADLESS=ON"; \
             echo "      and wx-config's 'base' component."; \
             ldd "$BIN" | grep -i -E 'wx|gtk'; exit 1; }; \
    done; \
    rm -rf /tmp/fastercap-nodisplay.out /tmp/fastercap-solve.out /tmp/fastercap-smoke; \
    cmp -s /foss/tools/fastercap/bin/FasterCap /foss/tools/rftoolkit/bin/FasterCap \
      || { echo "FAIL: the two install paths hold DIFFERENT binaries"; exit 1; }; \
    echo "fastercap: starts with no display, solves the two-plate geometry at both paths, wx-linked, no GTK, identical"

RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"fastercap","repo":"%s","ref":"%s"}\n' \
      "${FASTERCAP_REPO}" "${FASTERCAP_REF}" > /vibeic/provenance/fastercap.json

FROM scratch
COPY --from=build /foss/tools/fastercap /foss/tools/fastercap
COPY --from=build /foss/tools/rftoolkit/bin/FasterCap /foss/tools/rftoolkit/bin/FasterCap
COPY --from=build /vibeic/provenance/fastercap.json /vibeic/provenance/fastercap.json
