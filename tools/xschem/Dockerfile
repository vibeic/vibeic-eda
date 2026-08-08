# eda-tool-xschem — one tool, one image, keyed on the commit that built it.
#
# See tools/gtkwave/Dockerfile for why these four tools moved: we had forked them
# and the image still consumed the base image's build, which `check_fork_only` is
# structurally blind to.
#
# THE HEADLESS PATCH IS THE REASON THIS ONE IS NOT A STRAIGHT COPY. Upstream
# IIC-OSIC-TOOLS does not just build xschem — its `install.sh` PATCHES
# `src/xschem.tcl` first, guarding a `focus` call that is unsafe without an X
# display. The fleet is headless. Building from our fork without that patch would
# have produced a binary that looks identical, passes a version check, and hangs
# the first time a batch netlist run reaches that line.
#
# So the patch is applied here, and it is applied the way upstream does it —
# match-once, report if the anchor is missing — rather than by a blind `sed` that
# would silently no-op when xschem's source moves. That distinction matters: a
# patch that quietly fails to apply is worse than no patch, because the build
# still succeeds.
#
# It belongs in `vibeic/xschem` as a real commit, not in this Dockerfile. It is
# here because moving it into the fork is a separate change to a separate repo,
# and doing both at once would mean neither could be reverted alone. Recorded so
# the next reader knows this is a staging post, not a resting place.
#
# The pin is what the base image ACTUALLY ships (`/foss/tools/xschem/SOURCES` ->
# c8b26a17), which is NEWER than the commit in upstream's current Dockerfile.

FROM ubuntu:24.04 AS build

ARG XSCHEM_REPO=https://github.com/vibeic/xschem.git
ARG XSCHEM_REF=210aea296d30ee6cd83c85fbebf104b806bdcba5

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git flex bison pkg-config ca-certificates python3 \
      libx11-dev libxpm-dev libxrender-dev libxcb1-dev libcairo2-dev \
      tcl-dev tk-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --filter=blob:none "${XSCHEM_REPO}" /xschem \
 && cd /xschem && git checkout ${XSCHEM_REF}

# Upstream IIC-OSIC-TOOLS patches src/xschem.tcl here, guarding a `focus` call
# that is unsafe without an X display. At the pinned commit that patch is
# OBSOLETE: the anchor line does not exist (measured: 0 occurrences), because
# xschem restructured the code and every remaining `focus` site now carries its
# own `[info exists has_x]` guard.
#
# So this does NOT apply the patch, and it does NOT simply skip it either. It
# asserts the HAZARD is absent rather than that the anchor is absent — those are
# different claims, and only the first one is what the patch was for. A build
# that skipped a missing anchor would pass identically whether upstream had fixed
# the problem or merely moved it.
RUN python3 - /xschem/src/xschem.tcl <<'PYEOF'
import re, sys
fname = sys.argv[1]
lines = open(fname).read().split("\n")
# A `focus` on the drawing widget with no has_x guard on the same line is the
# headless hazard. Upstream's patch guarded exactly one such line; this checks
# that none is left, whoever fixed it and however they did.
bad = [(i + 1, l.strip()) for i, l in enumerate(lines)
       if re.search(r"\bfocus\b", l) and "top_path" in l and "has_x" not in l]
if bad:
    for n, l in bad:
        print("  line %d: %s" % (n, l))
    sys.exit("[FATAL] %d unguarded focus-on-drawing-widget call(s) remain. This "
             "xschem would hang in a headless batch run. Re-derive the guard "
             "before bumping XSCHEM_REF." % len(bad))
print("[INFO] no unguarded focus-on-drawing-widget call; upstream's headless "
      "guard is present at this commit and the IIC patch is obsolete here.")
PYEOF

RUN cd /xschem \
 && ./configure --prefix=/foss/tools/xschem \
 && make -C src expandlabel.c expandlabel.h eval_expr.c parselabel.c \
 && make -j"$(nproc)" \
 && make install \
 && echo "xschem ${XSCHEM_REF}" > /foss/tools/xschem/SOURCES

RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"xschem","repo":"%s","ref":"%s"}\n' \
      "${XSCHEM_REPO}" "${XSCHEM_REF}" \
      > /vibeic/provenance/xschem.json

FROM scratch
COPY --from=build /foss/tools/xschem /foss/tools/xschem
COPY --from=build /vibeic/provenance/xschem.json /vibeic/provenance/xschem.json
