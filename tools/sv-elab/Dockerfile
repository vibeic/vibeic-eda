# eda-tool-sv-elab — the yosys SystemVerilog plugin, built from our fork.
#
# NAMES, because three things here are called "slang" and only one of them is
# this:
#   povik/sv-elab            THIS — the yosys plugin, renamed from
#                            povik/yosys-slang. Ships as `slang.so` and is loaded
#                            with `yosys -m`.
#   MikePopoloski/slang      the standalone front end at /foss/tools/slang,
#                            already its own artefact (tools/slang).
#   yosys' vendored libs/slang  compiled inside yosys itself.
# The rename is also why `povik/yosys-slang` sat in PENDING_FORKS.json as an
# unforked exception for something we had already forked under its new name.
#
# BUILT AGAINST OUR YOSYS, NOT THE BASE'S. The plugin links against yosys' ABI
# and is loaded into that exact binary, so building it against a different yosys
# than the one it will run in is how you get a plugin that loads and then
# misbehaves. `yosys-config` from our artefact supplies the flags, which is the
# same reason upstream puts $TOOLS/yosys/bin on PATH before building it.
#
# That coupling is real and worth stating: bumping YOSYS_REF can require
# rebuilding this artefact even though SV_ELAB_REF did not move. The RECIPE
# component of the tag does not capture it — it digests this Dockerfile, not the
# yosys it copied — so the pin below is the honest place to look, and a yosys
# bump that breaks plugin loading will surface in the composed image's smoke
# test rather than here.

ARG IMG_YOSYS=ghcr.io/vibeic/eda-tool-yosys:b35f2c6-74a892

FROM ${IMG_YOSYS} AS yosys-artefact

FROM ubuntu:24.04 AS build

ARG SV_ELAB_REPO=https://github.com/vibeic/sv-elab.git
ARG SV_ELAB_REF=99900f9185abc5375400da4a72b1ed689290d014

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git cmake ninja-build pkg-config ca-certificates \
      python3 libreadline-dev tcl-dev zlib1g-dev libffi-dev \
 && rm -rf /var/lib/apt/lists/*

COPY --from=yosys-artefact /foss/tools/yosys /foss/tools/yosys
ENV PATH=/foss/tools/yosys/bin:$PATH

# `--recursive` matters: the plugin vendors its own slang, so a non-recursive
# clone configures cleanly and fails deep in the build with missing headers.
RUN git clone --filter=blob:none "${SV_ELAB_REPO}" /sv-elab \
 && cd /sv-elab && git checkout ${SV_ELAB_REF} \
 && git submodule update --init --recursive \
 && cmake -B build -DCMAKE_BUILD_TYPE=Release . \
 && make -C build -j"$(nproc)" \
 && mkdir -p /foss/tools/slang-yosys-plugin \
 && cp build/slang.so /foss/tools/slang-yosys-plugin/ \
 && echo "slang-yosys-plugin ${SV_ELAB_REF}" > /foss/tools/slang-yosys-plugin/SOURCES

# A plugin that builds and cannot be loaded is the failure this guards: the link
# succeeds, the .so exists, and the first `yosys -m` in a real run is where anyone
# finds out. Loading it here costs seconds.
#
# IT ASSERTS THE FUNCTION, NOT THE EXIT CODE, and that distinction is measured
# rather than assumed. `read_slang` completes its work and yosys then aborts on
# teardown with "double free or corruption (!prev)" — exit 134. The SHIPPING
# 0.2.31 image does exactly the same with the base's own slang.so: same abort,
# same code, and a real synthesis through it still writes correct Verilog and
# prints "End of script" first. So the abort is pre-existing and not caused by
# building from our fork, and failing the build on it would block a change that
# fixes nothing about it.
#
# It is NOT ignored. Anything other than that exact known abort fails, so a NEW
# way of dying cannot hide behind the old one — and the abort itself is reported
# on every build so it stays visible until the fork fixes it (vibeic-eda#24).
RUN set +e; \
    yosys -m /foss/tools/slang-yosys-plugin/slang.so -p "help read_slang" > /tmp/l.log 2>&1; \
    rc=$?; set -e; \
    grep -qi "read_slang" /tmp/l.log \
      || { echo "slang.so did not register read_slang:"; tail -20 /tmp/l.log; exit 1; }; \
    grep -q "End of script" /tmp/l.log \
      || { echo "slang.so loaded but yosys did not finish the script:"; tail -20 /tmp/l.log; exit 1; }; \
    if [ "$rc" != "0" ]; then \
      grep -q "double free or corruption" /tmp/l.log \
        || { echo "plugin exited $rc for a reason OTHER than the known teardown abort:"; \
             tail -20 /tmp/l.log; exit 1; }; \
      echo "NOTE: read_slang works and yosys aborts on teardown (exit $rc)."; \
      echo "      Pre-existing: the base image's own slang.so does the same."; \
      echo "      Tracked as vibeic-eda#24 — every caller that checks an exit"; \
      echo "      code sees failure on work that succeeded."; \
    fi; \
    echo "plugin loads, registers read_slang, and completes its script"

RUN mkdir -p /vibeic/provenance \
 && printf '{"tool":"sv-elab","repo":"%s","ref":"%s"}\n' \
      "${SV_ELAB_REPO}" "${SV_ELAB_REF}" \
      > /vibeic/provenance/sv-elab.json

FROM scratch
COPY --from=build /foss/tools/slang-yosys-plugin /foss/tools/slang-yosys-plugin
COPY --from=build /vibeic/provenance/sv-elab.json /vibeic/provenance/sv-elab.json
