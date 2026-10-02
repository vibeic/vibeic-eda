# eda-tool-klayout — one tool, one image, keyed on the commit that built it.
#
# Published as `ghcr.io/vibeic/eda-tool-klayout:<ref>`. The tag IS the pin: "which
# klayout is in the EDA image" is answered by reading the tag, not by trusting a
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

# Declared once, before the first stage, because two stages clone it: the
# compiler-cache seed and the build. A stage imports it with a bare `ARG
# KLAYOUT_REPO`; the resolved value, and so every cache key, is the same as a
# stage-local default (measured on a throwaway builder: identical layer).
ARG KLAYOUT_REPO=https://github.com/vibeic/klayout.git

# The toolchain stage carries NO pin ARG. An ARG changes the cache key of every
# later RUN in its stage whether or not the RUN reads it, so while KLAYOUT_REF
# was declared above this apt layer every pin move re-ran it: 147.0 s in the
# 2026-09-27 06:25Z round, installing the same packages again. The OpenROAD
# recipe fixed the same shape in vibeic-eda#233.
#
# `ccache` is installed here so the seed and the build compile with one and the
# same compiler binary: ccache keys a result on the compiler's mtime and size,
# and both stages inherit this layer.
FROM ubuntu:24.04 AS toolchain
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git python3-dev zlib1g-dev libexpat1-dev libcurl4-openssl-dev libpng-dev \
      qt6-base-dev qt6-base-dev-tools qt6-tools-dev qt6-tools-dev-tools \
      qt6-multimedia-dev qt6-svg-dev qt6-5compat-dev libqt6opengl6-dev \
      ruby-dev \
      ca-certificates ccache \
 && rm -rf /var/lib/apt/lists/*
# BUILT WITH Qt, deliberately (vibeic-eda#17). `-without-qt` produces libraries and
# buddy tools but NO `klayout` binary, so the CLI on PATH stayed the base image's
# June build and our fork reached only `svrfdrc`. That mattered: the canonical
# sign-off GDS is produced by `ly.read(def_path, …)` through KLayout's LEF/DEF
# importer — the exact code our fork patches to honour tech-LEF
# MANUFACTURINGGRID — so the grid snap was compiled, pinned, shipped and loaded
# by nothing on the sign-off path.
#
# Qt6, not Qt5: the base image's klayout links libQt6Core, libQt6Widgets and
# twelve more, so a Qt5 build would not be a drop-in replacement for it. Package
# names verified resolvable on ubuntu:24.04 before spending the build, which is
# the check the gtkwave GTK3/GTK4 failure earned.
#
# `-qmake` is required, and is the second thing that check caught: `build.sh`
# resolves `qmake` from PATH, and the Qt6 packages install `qmake6` there while
# the unsuffixed binary lives at /usr/lib/qt6/bin/qmake. With qtbase5-dev gone
# there is no `qmake` on PATH at all, so the build would have failed after the
# apt layer for a reason that has nothing to do with the code.
#
# RUBY IS NOT OPTIONAL HERE, and this is the third thing the first Qt build
# taught. That build succeeded and produced `klayout` — and shipped WITHOUT
# libklayout_drc.so and libklayout_lvs.so, because src/klayout.pro reads
#
#     equals(HAVE_RUBY, "1") { SUBDIRS += drc lvs }
#
# and we passed `-noruby`. The DRC and LVS engines are compiled as Ruby-backed
# subdirectories even when they are driven from Python or from a .lydrc script,
# so `-noruby` silently drops the two things this image exists to run. Installing
# that build over the base's klayout would have removed sign-off DRC and LVS
# while every check we own reported a pass: `check_no_capability_lost` asks
# whether a NAME still resolves, and `libklayout_drc.so` is not a name on PATH.
#
# ---- the compiler cache: a seed LAYER, not a cache mount --------------------
#
# A pin move used to recompile KLayout from nothing: 1200.9 s for the clone +
# build step in the 2026-09-27 06:25Z round, for a move (12205af41 ->
# 80eddc153) that changed two .cc files.
#
# The cache is a ccache directory built once at KLAYOUT_CCACHE_SEED, in its own
# stage, and bind-mounted read-write into the build below. It is keyed only on
# the toolchain layer and KLAYOUT_CCACHE_SEED, so it is CACHED across
# KLAYOUT_REF moves; what the build adds to it is discarded with the mount, so
# the build's own layer holds exactly what it held before.
#
# NOT `--mount=type=cache`. vibeic-builder runs BuildKit's default GC policy,
# whose rule 0 keeps exec.cachemount records to 488 MiB / 48 h, and it runs
# near its 93.13 GiB (100 GB) ceiling -- 88.5 GB on 2026-09-27 before the first
# build of this recipe, 99.0 GB after it -- so its GC does run: a 3.47 GB cache
# mount made for the bazel --disk_cache trial was gone within hours
# (vibeic-eda#233). A throwaway builder with the same default policy kept a
# 734 MB cache mount for over two hours while far below its ceiling, so the
# loss is driven by that pressure, which on vibeic-builder is permanent; and a pin
# that sits still for two days would find the mount expired anyway. A layer
# falls under the 1440 h / 93 GiB rules instead, and a round that uses it keeps
# it recent.
#
# WHY THE HITS SURVIVE A PIN MOVE. build.sh puts KLAYOUT_VERSION_REV (the short
# commit) and KLAYOUT_VERSION_DATE (today) into DEFINES, so every compile line
# carries two -D values that change with every pin and every day, and ccache's
# direct mode, which hashes the command line, misses on all of them. ccache
# then falls back to its preprocessor mode, which hashes the PREPROCESSED
# source and leaves out options that act only through the preprocessor (-D,
# -I): a translation unit that does not use those macros hits, and the ones
# that do (src/version) see the new value in their preprocessed text and miss,
# as they must.
#
# CORRECTNESS DOES NOT DEPEND ON THE SEED. A ccache result is served only for
# an identical preprocessed input, compiler (mtime and size) and remaining
# options, and ccache stores the compiler's own output, so a seed older than
# the pin only means more compiling, never a different object.
#
# MEASURED 2026-09-27 on a throwaway builder, klayout 80eddc153 built with the
# seed at the previous pin 12205af41 (the two .cc files of that pin move):
#     clone + build step, no cache (this recipe before)   924.4 s
#     the same step, from the seed                        113.5 s
#         ccache: 1864 of 1885 cacheable units hit (98.9%), 1833 of them
#         through the preprocessor mode described above
#     seed stage, one-time per KLAYOUT_CCACHE_SEED         907.9 s, 164 MB
# 893 of the 902 files under /klayout/bld are byte-identical to the uncached
# build's. The other 9 are the libraries that embed Qt resources: rcc writes
# each resource file's mtime -- the moment of the clone -- into the resource
# tree, and the ELF build-id hashes those bytes. Every differing byte is one of
# those two, so two uncached builds differ in the same places.
#
# Move KLAYOUT_CCACHE_SEED forward when the build
# step grows long again (the `ccache --show-stats` line below says how many
# units missed); nothing else reads it. Not named *_REF on purpose: every pin
# reader here matches `ARG <NAME>_REF=`, and this is not a pin -- it changes no
# shipped byte.
#
# `QMAKE_CCACHE=1` is build.sh's own switch (it adds CONFIG+=ccache, which puts
# `ccache` in front of the compiler qmake writes into every Makefile).
# CCACHE_MAXSIZE bounds the directory well above one full build, so nothing is
# evicted while the seed is written.
FROM toolchain AS ccache-seed
ARG KLAYOUT_REPO
ARG KLAYOUT_CCACHE_SEED=80eddc1533113248f87129520728e50983a58fb0
RUN git clone "${KLAYOUT_REPO}" /klayout \
 && cd /klayout && git checkout ${KLAYOUT_CCACHE_SEED} \
 && export CCACHE_DIR=/ccache CCACHE_MAXSIZE=5G QMAKE_CCACHE=1 \
 && ./build.sh -qmake /usr/lib/qt6/bin/qmake -nolibgit2 -j"$(nproc)" -bin /foss/tools/klayout-vibeic -build /klayout/bld \
 && ccache --show-stats \
 && cd / && rm -rf /klayout /foss/tools/klayout-vibeic \
 && echo "ccache seeded at ${KLAYOUT_CCACHE_SEED}: $(du -sb /ccache | cut -f1) bytes"

FROM toolchain AS build
ARG KLAYOUT_REPO
ARG KLAYOUT_REF=7c07aed3495c0681e4dfc9dfaafcca8c8371b0ce  # pinned; branch vibeic/klayout-signoff-int (0.2.23: + tl::Thread pthread_join heap-race ROOT-CAUSE fix — svrfdrc --threads use-after-free, TSan-proven 6race->0, report byte-identical; recompiles tlThreads.cc + relinks libklayout_tl.so)
RUN --mount=type=bind,from=ccache-seed,source=/ccache,target=/ccache,rw \
    git clone "${KLAYOUT_REPO}" /klayout \
 && cd /klayout && git checkout ${KLAYOUT_REF} \
 && export CCACHE_DIR=/ccache CCACHE_MAXSIZE=5G QMAKE_CCACHE=1 \
 && ccache --zero-stats \
 && ./build.sh -qmake /usr/lib/qt6/bin/qmake -nolibgit2 -j"$(nproc)" -bin /foss/tools/klayout-vibeic -build /klayout/bld \
 && ccache --show-stats \
 && for e in klayout libklayout_drc.so libklayout_lvs.so libklayout_db.so; do \
      test -e "/klayout/bld/$e" || { echo "FAIL: the build produced no $e"; exit 1; }; \
    done \
 && echo "klayout OK: main binary + DRC/LVS engines present" \
# SHIP THE TOOL, NOT THE BUILD. `-build /klayout/bld` is an object directory,
# and this artefact was copying it whole: 1119 MB in the release image, of which
# 743 MB is `.o` files, 21 MB is unit-test binaries, and 177 are Makefiles.
# Measured on the shipped artefact — pruning leaves 340 MB and klayout, the
# MANUFACTURINGGRID patch, svrfdrc and every strm2* buddy all still work.
 && find /klayout/bld -name '*.o' -delete \
 && find /klayout/bld \( -name 'Makefile' -o -name 'Makefile.*' \
                         -o -name '*.ut' -o -name 'ut_runner' \) -delete \
 && find /klayout/bld -name 'libklayout_*_tests.so*' -delete \
 && test -x /klayout/bld/klayout \
 && test -e /klayout/bld/libklayout_drc.so \
 && test -e /klayout/bld/libklayout_lvs.so \
 && test -d /klayout/bld/pymod \
 && echo "klayout pruned to $(du -sm /klayout/bld | cut -f1) MB; binary, engines and pymod intact"

# STAGE THE STANDALONE SIGN-OFF ENGINES (vibeic/klayout#113 finding 3).
#
# `-build /klayout/bld` is build.sh's OUTPUT directory and build.sh installs no
# repo-root Python, so the twelve standalone engine entry points the fork adds --
# antenna_check.py, xcheck_router.py, metal_fill.py, metal_cheese.py, caa.py,
# cmp_gradient.py, lvs_recon.py, mp_color.py, pattern_match.py, perc_latchup.py,
# gds_antenna_deck_check.py, metal_fill_emit.py -- were in `git log` and in NO
# shipped artefact. Measured on the released 0.2.89, built from this same fork
# ref: all twelve `<<NOT PRESENT>>`, and $VIBEIC_KLAYOUT_TOOLS unset. (The C++
# ports of some of these ops inside dbSVRFEngine DO ship, through the `svrfdrc`
# buddy in /klayout/bld; what did not ship is the standalone Python.)
#
# The list is READ FROM THE FORK, not written here: plugin-wiring/SHIPPED_ENGINES.json
# is the fork's own declaration, and the fork's own gate
# (vibeic-tests/tests/run_shipped_engines_test.sh) fails if an engine directory
# appears in the tree undeclared, or if a directory declared ship=true is not in
# the built image. A hard-coded list here would go stale the first time the fork
# adds an engine -- which is exactly how these twelve accumulated.
#
# LANDING ORDER: this needs KLAYOUT_REF to be at or after the fork commit that
# adds plugin-wiring/SHIPPED_ENGINES.json. The step fails loudly ("the fork
# checkout has no ...") rather than silently staging nothing, which is the right
# way round, but it does mean the fork commit lands first and the ref moves with
# it.
RUN set -eu; \
    m=/klayout/plugin-wiring/SHIPPED_ENGINES.json; \
    test -f "$m" || { echo "FAIL: the fork checkout has no $m"; exit 1; }; \
    root="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["install_root"])' "$m")"; \
    dirs="$(python3 -c 'import json,sys;m=json.load(open(sys.argv[1]));print(" ".join(e["dir"] for e in m["engines"] if e.get("ship")))' "$m")"; \
    entries="$(python3 -c 'import json,sys;m=json.load(open(sys.argv[1]));print("\n".join(e["dir"]+"/"+x for e in m["engines"] if e.get("ship") for x in e.get("entry") or []))' "$m")"; \
    test -n "$dirs" || { echo "FAIL: SHIPPED_ENGINES.json declares nothing to ship"; exit 1; }; \
    mkdir -p "$root"; \
    for d in $dirs; do \
      test -d "/klayout/$d" || { echo "FAIL: SHIPPED_ENGINES.json names $d, which this checkout does not have"; exit 1; }; \
      cp -a "/klayout/$d" "$root/$d"; \
    done; \
    for f in $entries; do \
      test -f "$root/$f" || { echo "FAIL: $f is declared ship=true and was not staged"; exit 1; }; \
    done; \
    find "$root" -name '__pycache__' -type d -prune -exec rm -rf {} + ; \
    echo "staged $(printf '%s\n' $entries | wc -l) sign-off engine entry point(s) into $root"
# ---------------------------------------------------------------------------
# Stage 6b — vibeic/verilator. Previously NOT layered: the note here read "no honest
#   fix warranted on v5.051 — nothing to layer", which was true at the time. It is no
#   longer: two constrained-randomization fixes landed on the fork with unfakeable
#   gates (V3Randomize Pow lowering generalized from base-2 to ANY power-of-2 base;
#   $countbits with a single runtime 1-bit control, previously E_UNSUPPORTED), each
#   with a proven-negative and 177/177 on the constraint+randomize suite. The base
#   image ships verilator, so ours is built in parallel and takes PATH precedence.
# ---------------------------------------------------------------------------
# Record every pin this artefact was built from.
RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"klayout","repo":"%s","ref":"%s"}\n' \
      "${KLAYOUT_REPO}" "${KLAYOUT_REF}" \
      > /vibeic/provenance/klayout.json

# ---------------------------------------------------------------------------
# The published layer carries the tool payload and nothing else — no compiler,
# no source tree, no apt lists. `scratch` enforces that by construction.
#
# The provenance file records what produced this artefact INSIDE it: a tag can
# be moved, a file in the image cannot, so the composing image can assert that
# what it copied is what it asked for.
# ---------------------------------------------------------------------------
FROM scratch
COPY --from=build /klayout/bld /klayout/bld
COPY --from=build /klayout/tools /klayout/tools
COPY --from=build /vibeic/provenance/klayout.json /vibeic/provenance/klayout.json
