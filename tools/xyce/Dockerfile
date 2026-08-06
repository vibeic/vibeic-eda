# eda-tool-xyce — one tool, one image, keyed on the commit that built it.
#
# See tools/gtkwave/Dockerfile for why these four moved: forked, and the image
# still consumed the base image's build, which `check_fork_only` is blind to.
#
# BOTH SOURCES ARE OURS NOW, and both are pinned to an immutable SHA with the
# branch of ours that carries it named in the trailing comment — the pattern
# `CADICAL_REF` uses, and the one that survives the branch being deleted.
# `vibeic/Trilinos` exists (mirrored by clone+push after the fork API refused),
# and Trilinos moved from 12.12.1 (2017) to 16.2.1 (2025) to 17.2.0 (2026).
#
# 16.2 WAS THE CEILING ONLY WHILE WE TOOK UPSTREAM'S PACKAGE SET AS GIVEN.
# Upstream b5069cff528 ("Remove deprecated packages and subpackages",
# 2025-07-30) changed 3731 files, 3726 of them outright deletions
# (`git show --numstat --diff-filter=D`), and everything above 16.2 is missing the
# Epetra stack Xyce is written against. That is a packaging decision, and it is
# one we can reverse in our own fork, so it is no longer a ceiling: branch
# `vibeic/xyce-trilinos-17.2-epetra-restored` is upstream master 763aa751877
# (17.2.0-dev) with those packages restored from our own 16.2.1 tip
# cf47480689f, byte-identical to b5069cff528^ over the restored paths.
#
# SEVEN PACKAGES, NOT FIVE. The count this file used to carry — AztecOO,
# Amesos, Ifpack, EpetraExt, Isorropia — was short by two, and an undercounted
# dependency list is how the next person drops one. Measured against
# vibeic/Xyce d72b5846, `cmake/tps.cmake:77-81` reads literally:
#
#   find_package(Trilinos CONFIG
#     REQUIRED Amesos Epetra EpetraExt Ifpack NOX Teuchos Sacado Triutils
#     AztecOO Belos TrilinosCouplings
#     OPTIONAL_COMPONENTS Isorropia Zoltan ShyLU ShyLU_DDCore
#     Amesos2 Stokhos ROL MKL)
#
# so of the packages upstream removed, Xyce names SEVEN:
#
#   Epetra      REQUIRED on line 78. The base of the whole stack.
#   EpetraExt   REQUIRED on line 78.
#   Ifpack      REQUIRED on line 78.
#   Amesos      REQUIRED on line 78.
#   AztecOO     REQUIRED on line 79.
#   Triutils    REQUIRED on line 78 — it supplies `Trilinos_Util.h`, included at
#               `src/DeviceModelPKG/OpenModels/N_DEV_ROM.C:70`, for
#               `Trilinos_Util_coocsr()` called at N_DEV_ROM.C:305 and :413
#               (the ROM device's COO->CSR conversion). Nothing else provides it.
#   Isorropia   nominally OPTIONAL on line 80, de-facto REQUIRED for us: under
#               MPI, tps.cmake:97-105 sets `Trilinos_IS_MISSING_FEATURES TRUE`
#               when `Isorropia::all_libs` is absent, and tps.cmake:255-258 then
#               does `message(FATAL_ERROR "Halting the Xyce configure due to
#               missing features in Trilinos.")`. We build with
#               `TPL_ENABLE_MPI=ON`, so for this recipe it is not optional.
#
# All seven are named by `Trilinos_ENABLE_*=ON` in the cmake call below. They
# are named rather than inherited because TriBITS enables a package by name or
# not at all, and because a dependency that arrives implicitly can leave
# implicitly without the recipe changing.
#
# THE BUILD IS NOW XYCE'S OWN CMAKE, not IIC-OSIC-TOOLS' autotools reconfigure
# scripts. That is the configuration Xyce upstream supports and tests, and it is
# what lets us track a modern Trilinos at all (its cmake path floors at 13.5).
# The scripts under `scripts/` are kept for reference and are no longer invoked.
#
# WHAT THE SWITCH WAS VERIFIED AGAINST, before it was made:
#   * Xyce's cmake `find_package` resolves every REQUIRED component against
#     16.2.1 — including Epetra and Triutils, which the old reconfigure script
#     never enabled by name (Epetra arrived as a dependency; Triutils did not).
#     It resolves every REQUIRED component against 17.2.0 too: the installed
#     `TrilinosConfig.cmake` reports `Trilinos_VERSION "17.2.0"` with
#     `Epetra;EpetraExt;AztecOO;Amesos;Ifpack;Isorropia;Triutils` all present in
#     `Trilinos_PACKAGE_LIST`.
#   * NUMERICAL A/B against the shipped 12.12.1 + autotools build: an RC
#     transient with a nonlinear clamp (139 points) and a DC sweep through a
#     diode knee (52 points) came out with a maximum relative difference of
#     0.000e+00. Identical values AND identical timestep counts.
#   * The ADMS user-plugin capability was END-TO-END verified, not assumed: a
#     Verilog-A resistor was authored, built with `buildxyceplugin.sh`, loaded
#     with `-plugin`, and simulated — Xyce reported `VRES level 1 (ADMS vres)`
#     and returned 2 mA for 2 V across 1 kohm.
#
# That last check is why the assertion at the bottom of the Xyce stage exists.
# The FIRST cmake build succeeded, passed the numerical A/B, and had quietly
# dropped ADMS entirely: cmake gates it behind `Xyce_PLUGIN_SUPPORT`, which
# defaults OFF. Two green indicators and a smaller Xyce.
#
# BUILD COST: Trilinos plus Xyce is the longest build in this repo by a wide
# margin — hours, not minutes on a CI runner. That cost is exactly what the
# per-tool split was for. Before it, this would have been paid on every release;
# now it is paid once per commit of `vibeic/Xyce`, and every release that does
# not move the pin copies the artefact instead of rebuilding it. Measured cold
# at the 17.2 pin on a 32-core host: Trilinos 757.9s, Xyce compile+install
# 186.7s, everything else under 100s.
#
# THE TRILINOS PIN IS NOT IN THE TAG, AND DOES NOT NEED TO BE. `tool_tags` builds
# `eda-tool-xyce:<short XYCE_REF>-<XYCE_RECIPE>`, and XYCE_RECIPE is
# `sha256(tools/xyce/Dockerfile)[:6]` — this file, which is where TRILINOS_REF
# lives. Moving the Trilinos pin therefore changes the recipe digest and so the
# tag, and a tag that does not exist yet is what triggers a rebuild. Bump
# XYCE_RECIPE in docker-bake.hcl and IMG_XYCE in the root Dockerfile whenever
# this file changes; `tools/check_pins_agree.py` is what says whether you did.

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
ARG TRILINOS_REF=7d2da31987c8ef21516c58bc9f466931a462c298  # branch vibeic/xyce-trilinos-17.2-epetra-restored -- Trilinos 17.2.0-dev (upstream master 763aa751877) with the SEVEN packages Xyce requires restored: Epetra, EpetraExt, AztecOO, Amesos, Ifpack, Isorropia, Triutils. Stock 17.x does NOT build Xyce; only this branch does. Previous pin: cf47480689f (branch vibeic/xyce-trilinos-16.2.1), which stays untouched as the fallback.

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git cmake gfortran ca-certificates \
      libopenmpi-dev openmpi-bin \
      libblas-dev liblapack-dev libsuitesparse-dev \
 && rm -rf /var/lib/apt/lists/*

ENV XYCE_NAME=xyce
COPY scripts/ /images/xyce/scripts/

# Explicit cmake rather than the reconfigure script. Same package set, plus
# Epetra and Triutils BY NAME: Xyce's cmake lists both as REQUIRED, and the old
# script enabled neither (Epetra arrived as a dependency of EpetraExt/AztecOO;
# Triutils did not arrive at all). With the autotools front end that never
# surfaced; with cmake it stops the build at find_package.
#
# EXACTLY TWO OPTIONS BELOW ARE NEW AT THE 17.2 PIN, and both exist only because
# of it. (No comments inline: the Dockerfile parser strips whole-line comments
# out of a continued RUN, but `cmake`'s argument list is easier to diff without
# them.) Nothing else in this call changed — at 17.2 `cmake` accepted every
# other option with zero "Manually-specified variables were not used by the
# project" warnings and zero CMake Warnings, so 17.x renamed or removed none of
# what 16.2.1 took.
#
#   -DTrilinos_ENABLE_DEVELOPMENT_MODE=OFF
#       16.2.1 is a RELEASE branch and 17.2.0-dev is master, and that alone
#       flips this: `Version.cmake:11` reads
#       `SET(Trilinos_ENABLE_DEVELOPMENT_MODE_DEFAULT OFF)` at cf47480689f and
#       `... ON` at 5edda67161c. TriBITS derives four things from it
#       (`cmake/tribits/core/package_arch/TribitsGlobalMacros.cmake:715-761`):
#       `Trilinos_ASSERT_DEFINED_DEPENDENCIES` and
#       `Trilinos_ASSERT_CORRECT_TRIBITS_USAGE` default to FATAL_ERROR instead
#       of IGNORE, and STRONG_C/STRONG_CXX_COMPILE_WARNINGS default ON. A tree
#       whose seven packages were restored by hand is exactly the tree those
#       assertions fire on. OFF is not a loosening relative to what we shipped:
#       it is the value the 16.2.1 build already had by default, restated
#       because the pin no longer supplies it.
#
#   -DCMAKE_CXX_STANDARD=20
#       17.x moved the floor. `cmake/SetTrilinosCxxStandard.cmake` at
#       cf47480689f: default 17, allowed `(17|20|23)`. At 5edda67161c: default
#       20, allowed `(20|23)` — passing 17 is now a FATAL_ERROR. 20 equals the
#       new default, so this is stated rather than needed, and it is stated on
#       purpose: the standard the Trilinos headers are compiled at is part of
#       what Xyce links against, and it should move only when this file says so.
RUN mkdir -p /tmp/xyce \
 && cd /tmp/xyce \
 && git clone --filter=blob:none "${TRILINOS_REPO}" trilinos \
 && cd trilinos && git checkout ${TRILINOS_REF} \
 && mkdir -p /tmp/xyce/trilinos_build && cd /tmp/xyce/trilinos_build && cmake \
      -G "Unix Makefiles" \
      -DCMAKE_C_COMPILER=mpicc -DCMAKE_CXX_COMPILER=mpic++ \
      -DCMAKE_Fortran_COMPILER=mpif77 \
      -DCMAKE_CXX_FLAGS="-O3 -fPIC" -DCMAKE_C_FLAGS="-O3 -fPIC" \
      -DCMAKE_Fortran_FLAGS="-O3 -fPIC" \
      -DCMAKE_INSTALL_PREFIX=/tmp/xyce/xycelibs/parallel \
      -DCMAKE_BUILD_TYPE=RELEASE \
      -DCMAKE_CXX_STANDARD=20 \
      -DTrilinos_ENABLE_DEVELOPMENT_MODE=OFF \
      -DTrilinos_ENABLE_Epetra=ON \
      -DTrilinos_ENABLE_Triutils=ON \
      -DTrilinos_ENABLE_NOX=ON -DNOX_ENABLE_LOCA=ON \
      -DTrilinos_ENABLE_EpetraExt=ON \
        -DEpetraExt_BUILD_BTF=ON -DEpetraExt_BUILD_EXPERIMENTAL=ON \
        -DEpetraExt_BUILD_GRAPH_REORDERINGS=ON \
      -DTrilinos_ENABLE_TrilinosCouplings=ON \
      -DTrilinos_ENABLE_Ifpack=ON -DTrilinos_ENABLE_Isorropia=ON \
      -DTrilinos_ENABLE_AztecOO=ON -DTrilinos_ENABLE_Belos=ON \
      -DTrilinos_ENABLE_Teuchos=ON -DTrilinos_ENABLE_COMPLEX_DOUBLE=ON \
      -DTrilinos_ENABLE_Amesos=ON -DAmesos_ENABLE_KLU=ON \
      -DTrilinos_ENABLE_Amesos2=ON -DAmesos2_ENABLE_KLU2=ON \
        -DAmesos2_ENABLE_Basker=ON \
      -DTrilinos_ENABLE_Sacado=ON -DTrilinos_ENABLE_Stokhos=ON \
      -DTrilinos_ENABLE_Kokkos=ON -DTrilinos_ENABLE_Zoltan=ON \
      -DTrilinos_ENABLE_ALL_OPTIONAL_PACKAGES=OFF \
      -DTPL_ENABLE_AMD=ON -DAMD_LIBRARY_DIRS="/usr/lib" \
        -DTPL_AMD_INCLUDE_DIRS="/usr/include/suitesparse" \
      -DTPL_ENABLE_BLAS=ON -DTPL_ENABLE_LAPACK=ON -DTPL_ENABLE_MPI=ON \
      /tmp/xyce/trilinos \
 && make -j"$(nproc)" install \
 && rm -rf /tmp/xyce/trilinos /tmp/xyce/trilinos_build

FROM ubuntu:24.04 AS build

ARG XYCE_REPO=https://github.com/vibeic/Xyce.git
ARG XYCE_REF=d72b5846a0397ddf852a49305cb6f395457685ca
ARG TRILINOS_REPO=https://github.com/vibeic/Trilinos.git
ARG TRILINOS_REF=7d2da31987c8ef21516c58bc9f466931a462c298  # branch vibeic/xyce-trilinos-17.2-epetra-restored -- Trilinos 17.2.0-dev (upstream master 763aa751877) with the SEVEN packages Xyce requires restored: Epetra, EpetraExt, AztecOO, Amesos, Ifpack, Isorropia, Triutils. Stock 17.x does NOT build Xyce; only this branch does. Previous pin: cf47480689f (branch vibeic/xyce-trilinos-16.2.1), which stays untouched as the fallback.
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
 && cd /tmp/xyce && git checkout ${XYCE_REF}

# Xyce's own cmake. MPI compilers are not optional here: Trilinos is built with
# -DTPL_ENABLE_MPI=ON, so its installed headers include <mpi.h> transitively
# (Teuchos_Time.hpp does). Configuring with the default compiler CONFIGURES
# cleanly and then fails at the first object file with "mpi.h: No such file or
# directory" -- measured. The autotools recipe passed CXX=mpicxx CC=mpicc
# F77=mpif77 and this one must too.
#
# Xyce_PLUGIN_SUPPORT=ON is what builds the ADMS user-plugin machinery. It
# defaults OFF, and with it off the build SUCCEEDS while shipping no
# `buildxyceplugin` and no shared libxyce -- see the assertion below.
RUN cd /tmp/xyce \
 && mkdir -p cmake_build && cd cmake_build && cmake \
      -DCMAKE_INSTALL_PREFIX=/foss/tools/xyce \
      -DTrilinos_DIR=/tmp/xyce/xycelibs/parallel/lib/cmake/Trilinos \
      -DCMAKE_BUILD_TYPE=RELEASE \
      -DCMAKE_C_COMPILER=mpicc \
      -DCMAKE_CXX_COMPILER=mpicxx \
      -DCMAKE_Fortran_COMPILER=mpif77 \
      -DCMAKE_CXX_FLAGS="-O3" \
      -DXyce_PLUGIN_SUPPORT=ON \
      /tmp/xyce \
 && make -j"$(nproc)" && make install \
 && printf 'xyce %s\ntrilinos %s\n' "${XYCE_REF}" "${TRILINOS_REF}" \
      > /foss/tools/xyce/SOURCES

# CAPABILITY GATE, not a note to self.
#
# The autotools build shipped `libADMSrlc.*` and `buildxyceplugin`. The first
# cmake build here shipped ONLY `libxyce.a` and `Xyce`, exited 0, and matched the
# old build's numbers exactly on an A/B -- two green indicators and a Xyce that
# could no longer build a Verilog-A device. Assert the capability so a future
# option change fails the build instead of quietly shipping less.
RUN set -e; \
    test -x /foss/tools/xyce/bin/buildxyceplugin.sh \
      || test -x /foss/tools/xyce/bin/buildxyceplugin \
      || { echo "FAIL: plugin builder missing -- ADMS user-plugin capability lost"; \
           ls -R /foss/tools/xyce/bin /foss/tools/xyce/share 2>/dev/null | head -40; \
           exit 1; }; \
    ls /foss/tools/xyce/lib/libxyce.so* >/dev/null 2>&1 \
      || { echo "FAIL: no shared libxyce -- plugin support needs BUILD_SHARED_LIBS"; \
           ls -l /foss/tools/xyce/lib; exit 1; }; \
    /foss/tools/xyce/bin/Xyce -v | head -2; \
    echo "plugin capability present: $(ls /foss/tools/xyce/bin | tr '\n' ' ')" 

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
