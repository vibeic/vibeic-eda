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

FROM ubuntu:24.04 AS build
ARG KLAYOUT_REPO=https://github.com/vibeic/klayout.git
ARG KLAYOUT_REF=e89f9b272eb4dcf676029672bd7bc9aef6a961cb  # pinned; branch vibeic/klayout-signoff-int (0.2.23: + tl::Thread pthread_join heap-race ROOT-CAUSE fix — svrfdrc --threads use-after-free, TSan-proven 6race->0, report byte-identical; recompiles tlThreads.cc + relinks libklayout_tl.so)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git python3-dev zlib1g-dev libexpat1-dev libcurl4-openssl-dev libpng-dev \
      qt6-base-dev qt6-base-dev-tools qt6-tools-dev qt6-tools-dev-tools \
      qt6-multimedia-dev qt6-svg-dev qt6-5compat-dev libqt6opengl6-dev \
      ruby-dev \
      ca-certificates \
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
RUN git clone "${KLAYOUT_REPO}" /klayout \
 && cd /klayout && git checkout ${KLAYOUT_REF} \
 && ./build.sh -qmake /usr/lib/qt6/bin/qmake -nolibgit2 -j"$(nproc)" -bin /foss/tools/klayout-vibeic -build /klayout/bld \
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
COPY --from=build /vibeic/provenance/klayout.json /vibeic/provenance/klayout.json
