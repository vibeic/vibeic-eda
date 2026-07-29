# vibeic-eda — composed from per-tool artefacts, not compiled here.
#
# Each tool is built ONCE as `ghcr.io/vibeic/eda-tool-<name>:<commit-sha>` (see
# `tools/<name>/Dockerfile`) and copied in. A release is therefore a set of COPYs
# from immutable images, and moving one pin rebuilds one tool.
#
# Until 2026-07-29 this file compiled all eight tools inline and `release.yml`
# passed neither `cache-from` nor `cache-to` (measured: 0 occurrences), so a
# one-character change to `NGSPICE_REF` rebuilt everything from source. That is
# what made "fork one more tool" read as "double the build time" — the cost was
# never the new tool, it was that nothing was built once.
#
# THE TAG IS THE PIN. `IMG_YOSYS` below names the exact artefact, and the SHA in
# it is the commit of `vibeic/yosys` that produced those binaries. Moving a pin
# is a one-line diff here, and the per-tool image for the new SHA must already
# exist — which is what stops a pin from naming something nobody built.
#
# The `alpine/git` stages further down are kept INLINE on purpose: they clone,
# they do not compile. Publishing them as artefacts would add a registry
# round-trip and save no build time.
#
# Sources are `vibeic/*` only (owner rule, 2026-07-29). That is enforced by
# `tools/check_fork_only.py` in the `fork-only` workflow, not by review — the
# rule previously held only as long as whoever edited 605 lines remembered it.

# Declared HERE, above every FROM, because an ARG after the first FROM is
# stage-scoped: `FROM ${BASE_IMAGE}` then resolves to blank and the build
# fails with `base name should not be blank`. That is exactly what happened
# on the first attempt — the IMG_* args below work for the same reason and I
# put the new one next to the FROM it feeds instead of next to them.
ARG BASE_IMAGE=hpretl/iic-osic-tools@sha256:7371bae55da486f492cc270ea6137c4fcf3b11971de7a4506a74f62be143537a
ARG IMG_OPENROAD=ghcr.io/vibeic/eda-tool-openroad:92b079b-7ac820
ARG IMG_YOSYS=ghcr.io/vibeic/eda-tool-yosys:b35f2c6-74a892
ARG IMG_SAT_SOLVERS=ghcr.io/vibeic/eda-tool-sat-solvers:8af8e56-c607304-755999
ARG IMG_NGSPICE=ghcr.io/vibeic/eda-tool-ngspice:2d15ecb-be7db2
ARG IMG_LVS=ghcr.io/vibeic/eda-tool-lvs:9d3ed4b-0334b7d-e2e322
ARG IMG_IVERILOG=ghcr.io/vibeic/eda-tool-iverilog:fe9dfab-940079
ARG IMG_KLAYOUT=ghcr.io/vibeic/eda-tool-klayout:39b6a09-636653
ARG IMG_VERILATOR=ghcr.io/vibeic/eda-tool-verilator:d9f4670-8c0ab6
ARG IMG_GTKWAVE=ghcr.io/vibeic/eda-tool-gtkwave:7d7b4db-2166b3
ARG IMG_XSCHEM=ghcr.io/vibeic/eda-tool-xschem:c8b26a1-382491
ARG IMG_SLANG=ghcr.io/vibeic/eda-tool-slang:24809c8-b569b8
ARG IMG_XYCE=ghcr.io/vibeic/eda-tool-xyce:a592a42-9d3df7
ARG IMG_YICES2=ghcr.io/vibeic/eda-tool-yices2:05178c0-04c594
ARG IMG_SV_ELAB=ghcr.io/vibeic/eda-tool-sv-elab:b2b718c-0656db

# BuildKit does not expand a variable in `COPY --from=`, so each pinned
# artefact is named once here as a stage. These are pure aliases: nothing is
# built, nothing is layered on top — the FROM only gives the pin a name that
# `COPY --from` accepts.
#
# It also gives `docker-bake.hcl` a key to redirect: a `contexts` entry naming
# the same image ref swaps the registry pull for a locally built target, which
# is what makes `bake eda-local` work without a push.

FROM ${IMG_OPENROAD} AS img-openroad
FROM ${IMG_YOSYS} AS img-yosys
FROM ${IMG_SAT_SOLVERS} AS img-sat-solvers
FROM ${IMG_NGSPICE} AS img-ngspice
FROM ${IMG_LVS} AS img-lvs
FROM ${IMG_IVERILOG} AS img-iverilog
FROM ${IMG_KLAYOUT} AS img-klayout
FROM ${IMG_VERILATOR} AS img-verilator
FROM ${IMG_GTKWAVE} AS img-gtkwave
FROM ${IMG_XSCHEM} AS img-xschem
FROM ${IMG_SLANG} AS img-slang
FROM ${IMG_XYCE} AS img-xyce
FROM ${IMG_YICES2} AS img-yices2
FROM ${IMG_SV_ELAB} AS img-sv-elab


# ---------------------------------------------------------------------------
# Stage 7 — RETIRED. The SVRF/Calibre DRC deck is now executed by the NATIVE C++
#   `svrfdrc` buddy compiled in Stage 6 (part of the svrf-native-drc branch), NOT by
#   the old pure-Python `run_svrf_drc.py` interpreter. No separate source stage: the
#   buddy ships inside /klayout/bld and is surfaced on PATH in the runtime stage.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stage 8 — vibeic verification toolchain (cocotb / cocotb-coverage / pyuvm / sby)
#   The professional testbench generator (plugin `professional_tb_gen`, MCP
#   `eda_professional_tb`) emits cocotb TBs + cocotb-coverage covergroups + an SVA
#   bind that RUN on this toolchain — so we OWN it (Bucket-T: fork every OSS EDA tool
#   so we can enhance it, never a "the tool can't do it" excuse). Pure-source fetch
#   here; editable-installed in the runtime so an in-image Python patch is live.
#   sby (SymbiYosys) drives our vibeic/yosys for the SVA-bind / formal path.
#   Cloned from the vibeic org forks (github.com/vibeic/{cocotb,cocotb-coverage,
#   pyuvm,sby}); the pinned SHAs live in those forks (shared upstream history) so
#   a vibeic patch is a branch/commit on top. Keep COCOTB_REF on the stable 2.0.x
#   line the base ships until a real vibeic patch lands.
# ---------------------------------------------------------------------------
FROM alpine/git AS tb-src
ARG COCOTB_REF=4b3383fec9df0d1ea8c617e38dc438de0ed68540  # branch vibeic/parallel-regression-dispatch (PLL1)
ARG COCOTB_COVERAGE_REF=be916da99520662f77cfccb8dd17861c8f986ce0  # branch vibeic/integration is V15 crv scalability + V36 rank + V10/V11/V35 bins-closure; union verified per-definition (20/20, 0 dropped)
ARG PYUVM_REF=04add2a48c0701dee8e1c84cdd70737f5a3815c1  # branch vibeic/integration is V5 RAL accessors + V7 TLM comparators + V6 sequencer arbitration; suite is the exact union (441/535), 0 failures
ARG SBY_REF=742213689ee1bff65bc34e27011438edf8ce09f2  # branch vibeic/integration: V23/V24/V26/V30 (main) + V42/V27/V19/V18/V28 (w3) + V39/V49/V46/V50/V38/V40 (w2/w4), package layout, 11 version-drift reds fixed at root
RUN git clone https://github.com/vibeic/cocotb.git           /tb/cocotb          && git -C /tb/cocotb          checkout ${COCOTB_REF} \
 && git clone https://github.com/vibeic/cocotb-coverage.git  /tb/cocotb-coverage && git -C /tb/cocotb-coverage checkout ${COCOTB_COVERAGE_REF} \
 && git clone https://github.com/vibeic/pyuvm.git            /tb/pyuvm           && git -C /tb/pyuvm           checkout ${PYUVM_REF} \
 && git clone https://github.com/vibeic/sby.git              /tb/sby             && git -C /tb/sby             checkout ${SBY_REF}

# ---------------------------------------------------------------------------
# Stage 9 — NanGate45 / FreePDK45 Open Cell Library (Si2, Apache-2.0).
#   A GENERIC, NON-FOUNDRY 45nm std-cell enablement (LEF + Liberty + GDS): synth /
#   PnR / CTS / STA / area all run, and the FreePDK45 KLayout deck gives an
#   EDUCATIONAL DRC — but it is NOT a manufacturable foundry sign-off (FreePDK45 is a
#   fictional process; no real foundry, no LVS deck). The iic-osic-tools base ships
#   sky130/gf180/sg13g2 but NOT nangate45, so we fetch it from the OpenROAD-flow-scripts
#   `nangate45` platform (the reference open 45nm flow, pinned to the v3.0 tag) and, in
#   the runtime stage, re-stage it into the open_pdks libs.ref/<scl>/ layout the plugin's
#   PDK resolvers expect. Registered in the plugin as PDK `nangate45`
#   (vibe-ic programs/pdk_registry.json, tapeout_capable=false).
# ---------------------------------------------------------------------------
FROM alpine/git AS nangate45-src
ARG ORFS_REF=v3.0
# One clone, both open non-foundry platforms: nangate45 (FreePDK45 45nm) AND
# asap7 (ASU/ARM 7nm predictive, BSD). Both are re-staged into the open_pdks
# libs.ref/<scl>/ layout in the runtime stage below.
RUN git clone --depth 1 --branch ${ORFS_REF} --filter=blob:none --sparse \
      https://github.com/vibeic/OpenROAD-flow-scripts.git /orfs \
 && git -C /orfs sparse-checkout set flow/platforms/nangate45 flow/platforms/asap7

# ---------------------------------------------------------------------------
# Stage 9b — ASAP7 device-LVS source-of-truth (B1/#174; all PUBLIC + BSD).
#   The ORFS asap7 platform (Stage 9) ships only the physical enablement (LEF /
#   Liberty / GDS / KLayout DRC) — NO transistor-level golden netlist, which is
#   why device-level LVS was long deferred. The golden DOES exist and is freely
#   fetchable:
#     * asap7sc7p5t_28 (BSD-3) — CDL/LVS/asap7sc7p5t_28_{L,R,SL,SRAM}.cdl: the
#       transistor-level schematic of every std cell (one .SUBCKT per cell,
#       4-terminal FinFETs nmos_rvt/pmos_rvt with nfin=N) = the LVS golden.
#     * asap7_pdk_r1p7 (BSD-3) — models/hspice/7nm_{TT,SS,FF}_160803.pm: the
#       BSIM-CMG (level 72) FinFET device models.
#     * laurentc2/ASAP7_for_KLayout (BSD-2) — asap7.lyt / asap7.lyp: the KLayout
#       layer stack + connectivity used to author the FinFET LVS extraction.
#   Cloned sparse/blobless; staged in the runtime stage into libs.tech/ mirroring
#   the sky130/nangate45 golden-CDL convention so the plugin's LVS resolvers find
#   them with no per-PDK special-casing. Wired in vibe-ic programs/pdk_registry.json
#   (asap7: cdl_netlist / spice_models / klayout_lvs_tech) and consumed by
#   programs/asap7_finfet_lvs.py (KLayout geometric FinFET extract + NetlistComparer).
# ---------------------------------------------------------------------------
FROM alpine/git AS asap7-lvs-src
ARG ASAP7SC_REF=main
ARG ASAP7PDK_REF=main
ARG ASAP7KL_REF=main
RUN git clone --depth 1 --branch ${ASAP7SC_REF} --filter=blob:none --sparse \
      https://github.com/vibeic/asap7sc7p5t_28.git /a7sc \
 && git -C /a7sc sparse-checkout set CDL/LVS \
 && git clone --depth 1 --branch ${ASAP7PDK_REF} --filter=blob:none --sparse \
      https://github.com/vibeic/asap7_pdk_r1p7.git /a7pdk \
 && git -C /a7pdk sparse-checkout set models/hspice \
 && git clone --depth 1 --branch ${ASAP7KL_REF} \
      https://github.com/vibeic/ASAP7_for_KLayout.git /a7kl \
 && test -f /a7sc/CDL/LVS/asap7sc7p5t_28_R.cdl \
 && test -f /a7pdk/models/hspice/7nm_TT_160803.pm \
 && test -f /a7kl/asap7.lyt

# ---------------------------------------------------------------------------
# Stage 11 — ALIGN (analog place & route: device-level SPICE netlist -> placed +
#   routed GDS) sources. The analog counterpart of the digital OpenROAD flow.
#   Bucket-T: we OWN it. BOTH repos are vibeic forks pinned by SHA and the engine is
#   BUILT FROM OUR SOURCE in the runtime stage — deliberately NOT the published PyPI
#   `align-analoglayout` wheel — so ALIGN is patchable in-tree like every other tool
#   here and no upstream-published binary enters a sign-off toolchain.
#     * vibeic/ALIGN-public      — the engine (Python + the PnR C++ pybind11 extension).
#     * vibeic/ALIGN-pdk-sky130  — the sky130 ALIGN PDK; carries OUR channel-length fix.
#   Both trees are kept on disk in the runtime (/opt/align-src, mirroring
#   /opt/vibeic-forks) because neither is reachable from the installed package:
#   ALIGN-public's setup.py package_data ships only align/pdk/finfet (NO sky130) and
#   does not install examples/, and the sky130 PDK lives entirely in the second fork.
# ---------------------------------------------------------------------------
FROM alpine/git AS align-src
ARG ALIGN_PUBLIC_REF=e392ae4789eb49193a4865244d8cc31dbe1744b7  # pinned; branch master — vibeic fork is 0 commits ahead / 0 behind upstream ALIGN-analoglayout/ALIGN-public (align/__init__.py declares __version__ 0.9.8); forked so ALIGN is patchable in-tree, and BUILT FROM SOURCE below rather than pip-installed from PyPI
ARG ALIGN_PDK_SKY130_REF=427b3b94242fdcf8009e418f6bbe14286fc71334  # pinned; branch main — carries our fix(mos): honour netlist channel length L instead of drawing every gate at 150nm, guarded by tests/test_channel_length.py (which ships its own negative control)
RUN git clone https://github.com/vibeic/ALIGN-public.git     /align/ALIGN-public     && git -C /align/ALIGN-public     checkout ${ALIGN_PUBLIC_REF} \
 && git clone https://github.com/vibeic/ALIGN-pdk-sky130.git /align/ALIGN-pdk-sky130 && git -C /align/ALIGN-pdk-sky130 checkout ${ALIGN_PDK_SKY130_REF} \
 && test -f /align/ALIGN-public/setup.py \
 && test -f /align/ALIGN-pdk-sky130/SKY130_PDK/mos.py

# ===========================================================================
# Runtime: layer the patched tools onto the iic-osic-tools base.
# ===========================================================================
# THE BASE IS PINNED BY DIGEST (#20). Sixteen forks are pinned to 40-character
# SHAs and were, until now, built onto `:latest` — a moving tag, so a daily
# rebuild could change ~90% of the image with no pin moving and nothing
# recording it. This digest is the one every image up to 0.2.34 was actually
# built from, so pinning it changes nothing today and makes tomorrow honest.
#
# Adopting a NEW base is deliberately not the fork rule: we do not control this
# image and cannot review it commit by commit, so it is a considered bump with a
# smoke pass behind it, never an automatic merge.
FROM ${BASE_IMAGE}
LABEL org.opencontainers.image.title="vibeic-eda"
LABEL org.opencontainers.image.description="Forked+enhanced OSS EDA toolchain (vibeic): iic-osic-tools base + vibeic/* patched EDA-tool forks with gatekeeper-verified FAIL->PASS proofs (see FIX_STATUS.md)."
LABEL org.opencontainers.image.source="https://github.com/vibeic"

# /foss/tools is root-owned and the base runs as user 1000 — become root to mutate it.
USER root
# --- vibeic/OpenROAD (native 24.04 build → RUNPATH /opt/or-tools, no lib bundling) ---
COPY --from=img-openroad /opt/or-tools /opt/or-tools
COPY --from=img-openroad /foss/tools/openroad/bin/openroad /foss/tools/openroad/bin/openroad
# Clean-replace the base tool dirs FIRST so no stale base files survive the COPY merge —
# e.g. the base's ghdl.so yosys plugin is built against the old ABI and would crash yosys 0.66.
#
# YICES IS A LODGER IN THE YOSYS DIRECTORY (vibeic-eda#13). The base ships the
# Yices 2.7.0 binaries inside /foss/tools/yosys/bin, so the rm below deleted
# them and every /foss/tools/bin/yices* symlink was left dangling. Nothing named
# the missing binary: `sby` with the common default engine line `smtbmc yices`
# just ended `rc=16 ERROR: Engine terminated without status`, which reads as a
# property that could not be proved rather than a tool that was not there.
# Measured in 0.2.30 before this change: all four links dangling, `yices-smt2`
# not resolvable on PATH at all.
#
# They are moved out rather than re-copied because they are not ours to rebuild:
# `ldd` shows libgmp and libc only, nothing from the yosys tree, so they carry
# no dependency on the directory they happened to live in.
# eqy (equivalence checking) and mcy (mutation coverage) are separate YosysHQ
# projects that the base image installs INTO the yosys prefix. `rm -rf` below
# takes them with it, and their symlinks in /foss/tools/bin were left pointing at
# nothing — five dangling links advertising tools the image no longer had (#19).
# Rescued the same way yices is, one line down.
#
# They locate their support files relative to their own script, so they are
# restored INTO our yosys prefix rather than beside it: eqy then finds our
# yosys's share/yosys/python3, which is the yosys it will actually drive.
# mcy-dash and mcy-gui fail on --help in the BASE image too (`config.mcy not
# found`), so they are carried without being claimed to work.
RUN mkdir -p /foss/rescue/bin /foss/rescue/share \
 && for t in eqy mcy mcy-dash mcy-gui; do \
      [ -e "/foss/tools/yosys/bin/$t" ] && cp -a "/foss/tools/yosys/bin/$t" "/foss/rescue/bin/$t"; \
    done; \
    cp -a /foss/tools/yosys/share/mcy /foss/rescue/share/mcy \
 && cp -a /foss/tools/yosys/share/yosys/python3/eqy_job.py /foss/rescue/eqy_job.py \
 && test -x /foss/rescue/bin/eqy

# yices2 is now OUR build (tools/yices2), so nothing has to be rescued out of the
# base's yosys prefix before it is deleted. The old step copied four binaries out
# of /foss/tools/yosys/bin first, under a comment saying "they are not ours to
# rebuild" — vibeic-eda#13 was those four going missing. They are ours now, and
# the copy is gone rather than left as a no-op.
RUN rm -rf /foss/tools/yosys /foss/tools/ngspice /foss/tools/magic /foss/tools/netgen /foss/tools/iverilog
# --- vibeic/yosys (replaces base yosys install; bin symlinked into /foss/tools/bin) ---
COPY --from=img-yosys /foss/tools/yosys /foss/tools/yosys

# Put eqy/mcy back on top of OUR yosys, and DELETE the last dangling link rather
# than repointing it.
#
# `/foss/tools/bin/sby` pointed into the deleted prefix while the working sby
# comes from our own SBY_REF build at /usr/local/bin, which wins on PATH — so the
# break was invisible. Repointing the link at /usr/local/bin/sby looks like the
# tidier fix and breaks sby: it finds its modules relative to the path it was
# INVOKED by, so running it as /foss/tools/bin/sby sends it looking in
# /foss/tools/share/yosys/python3 and it dies with `No module named
# 'sby_cmdline'`. That failed the vibeic-eda#13 sby/yices guard, which is the
# guard doing exactly its job.
#
# The dangling link was accidentally protective: PATH skips a link it cannot
# execute, so the real sby ran. Deleting it keeps that behaviour and stops the
# image advertising a tool at a path that cannot work.
#
# eqy and mcy are verified at the END of this file, not here: eqy resolves its
# modules through PYTHONPATH, which the stock base sets only in a LOGIN shell,
# and a Dockerfile RUN is not one. Checking here tested the tool under conditions
# no user has.
RUN cp -a /foss/rescue/bin/. /foss/tools/yosys/bin/ \
 && cp -a /foss/rescue/share/mcy /foss/tools/yosys/share/mcy \
 && cp -a /foss/rescue/eqy_job.py /foss/tools/yosys/share/yosys/python3/eqy_job.py \
 && rm -rf /foss/rescue \
 && rm -f /foss/tools/bin/sby
# external CDCL SAT solvers for the fork yosys sat backend (vibe-ic#354)
COPY --from=img-sat-solvers /usr/local/bin/kissat /foss/tools/bin/kissat
COPY --from=img-sat-solvers /usr/local/bin/cadical /foss/tools/bin/cadical
# --- vibeic/ngspice ---
COPY --from=img-ngspice /foss/tools/ngspice /foss/tools/ngspice
# --- vibeic/magic + vibeic/netgen ---
COPY --from=img-lvs /foss/tools/magic /foss/tools/magic
COPY --from=img-lvs /foss/tools/netgen /foss/tools/netgen
# --- vibeic/iverilog ---
COPY --from=img-iverilog /foss/tools/iverilog /foss/tools/iverilog
# INSTALLED OVER the base image's verilator, which is the convention every
# other tool here follows (yosys, iverilog, magic, netgen, ngspice, openroad
# all land in /foss/tools/<tool>). verilator was the exception, so
# /foss/tools/bin/verilator kept resolving to the base image's April 5.048
# while our 5.051 sat unreferenced beside it (#18).
#
# NO `rm -rf` first, unlike the yosys line above: our build produces the same
# seven binaries the base does, so COPY's merge replaces every one of them.
# Removing the directory would orphan any co-tenant the base keeps there,
# which is exactly what happened to eqy and mcy under yosys (#19).
COPY --from=img-verilator /foss/tools/verilator /foss/tools/verilator

# Forked on 2026-07-28 and still consumed from the BASE image until now: the
# fork existed and nothing built from it. check_fork_only cannot see that — it
# checks what we build and copy, and says in its own docstring that it is blind
# to the tools the base supplies. The COPY overwrites the base directory in
# place, so the base build is replaced rather than shadowed on PATH.

COPY --from=img-gtkwave /foss/tools/gtkwave /foss/tools/gtkwave
COPY --from=img-xschem /foss/tools/xschem /foss/tools/xschem
COPY --from=img-slang /foss/tools/slang /foss/tools/slang
COPY --from=img-xyce /foss/tools/xyce /foss/tools/xyce

# The last two of the forked-but-not-built set. yices2 was a lodger in the yosys
# prefix (upstream builds it with --prefix=$TOOLS/yosys); it now has its own.
# sv-elab is the yosys plugin, built against OUR yosys because it links that
# ABI and is loaded into that exact binary.

COPY --from=img-yices2 /foss/tools/yices /foss/tools/yices
# NOT INSTALLED — deliberately. yosys 0.67+ contains the slang frontend itself
# (1472 slang symbols in the binary; `yosys -p "read_slang …"` works with no
# plugin), so this .so is a SECOND copy of a frontend the process already has.
# The duplicated statics double-free at exit: `yosys -m …/slang.so -p "help
# read_slang"` writes a correct netlist and then aborts with
# `double free or corruption`, exit 134 (#24).
#
# Nothing in the flow loads it — `synth_frontend.resolve_slang_load_prefix`
# probes the container and returns an empty prefix when slang is built in — so
# shipping it gains nothing and leaves a loaded gun for anyone who runs
# `yosys -m` by hand.
#
# The vibeic/sv-elab fork stays tracked and its artefact stays built: the fork
# should exist and be pinned (vibeic-eda#25 was right about that). What changes
# is that a duplicate frontend does not go into the image.
# COPY --from=img-sv-elab /foss/tools/slang-yosys-plugin /foss/tools/slang-yosys-plugin
#
# AND THE BASE'S COPY IS REMOVED. Commenting out our COPY was not enough and I
# shipped 0.2.39 believing it was: that directory comes from the BASE image, so
# dropping our COPY merely reverted it to the base's build — which is the copy
# that aborts under our yosys in the first place. Verified in the published
# image: md5 4766483813543906b01ac9b5c94d8544 (the base's), `yosys -m …` exit 134.
RUN rm -rf /foss/tools/slang-yosys-plugin


# --- vibeic/klayout (parallel streamout install; base klayout untouched) ---
# build.sh emits the Qt-less db-lib + pymod + db_plugins/liblefdef.so into its -build dir.
COPY --from=img-klayout /klayout/bld /foss/tools/klayout-vibeic

# Provenance: what each artefact was actually built from, carried INSIDE the
# image. The tag says what was asked for; these say what arrived, and a tag can
# be moved while a file in a layer cannot. `check_image_provenance.py` reads
# them back out of the built image and fails if any disagrees with its pin.

COPY --from=img-openroad /vibeic/provenance/openroad.json /vibeic/provenance/openroad.json
COPY --from=img-yosys /vibeic/provenance/yosys.json /vibeic/provenance/yosys.json
COPY --from=img-sat-solvers /vibeic/provenance/sat-solvers.json /vibeic/provenance/sat-solvers.json
COPY --from=img-ngspice /vibeic/provenance/ngspice.json /vibeic/provenance/ngspice.json
COPY --from=img-lvs /vibeic/provenance/lvs.json /vibeic/provenance/lvs.json
COPY --from=img-iverilog /vibeic/provenance/iverilog.json /vibeic/provenance/iverilog.json
COPY --from=img-klayout /vibeic/provenance/klayout.json /vibeic/provenance/klayout.json
COPY --from=img-verilator /vibeic/provenance/verilator.json /vibeic/provenance/verilator.json
COPY --from=img-gtkwave /vibeic/provenance/gtkwave.json /vibeic/provenance/gtkwave.json
COPY --from=img-xschem /vibeic/provenance/xschem.json /vibeic/provenance/xschem.json
COPY --from=img-slang /vibeic/provenance/slang.json /vibeic/provenance/slang.json
COPY --from=img-xyce /vibeic/provenance/xyce.json /vibeic/provenance/xyce.json
COPY --from=img-yices2 /vibeic/provenance/yices2.json /vibeic/provenance/yices2.json
COPY --from=img-sv-elab /vibeic/provenance/sv-elab.json /vibeic/provenance/sv-elab.json

# Re-point the /foss/tools/bin symlinks the base created to our installs.
RUN for t in yosys yosys-abc; do ln -sf /foss/tools/yosys/bin/$t /foss/tools/bin/$t 2>/dev/null || true; done \
 && ln -sf /foss/tools/ngspice/bin/ngspice /foss/tools/bin/ngspice 2>/dev/null || true \
 && ln -sf /foss/tools/magic/bin/magic /foss/tools/bin/magic 2>/dev/null || true \
 && ln -sf /foss/tools/netgen/bin/netgen /foss/tools/bin/netgen 2>/dev/null || true \
 && for t in iverilog vvp iverilog-vpi vvp; do ln -sf /foss/tools/iverilog/bin/$t /foss/tools/bin/$t 2>/dev/null || true; done \
 && for t in yices yices-sat yices-smt yices-smt2; do ln -sf /foss/tools/yices/bin/$t /foss/tools/bin/$t; done
# fault (AUCOHL DFT toolchain) ships from the iic-osic-tools base at
# /usr/local/bin/fault (already on PATH). Surface it under /foss/tools/bin too so
# its path is consistent with every other EDA tool — eda_dft invokes bare `fault`
# (PATH) and eda_doctor probes bare `fault`, so both work with or without this
# symlink; it's a path-consistency convenience, not a functional requirement.
RUN command -v fault >/dev/null 2>&1 && ln -sf "$(command -v fault)" /foss/tools/bin/fault 2>/dev/null || true
# --- vibeic/klayout svrfdrc (NATIVE C++ SVRF/Calibre DRC buddy) ---
# The `svrfdrc <deck> <layout> <report> [--cell=TOP]` binary was compiled in Stage 6
# (svrf-native-drc branch) and shipped inside /klayout/bld -> already copied to
# /foss/tools/klayout-vibeic above. The `svrfdrc()` entry + the whole native SVRF
# engine (dbSVRFDeck/dbSVRFEngine) are baked into the FORK's libklayout_bd.so there.
# NO Python interpreter, NO `-r` script, NO GUI macro — byte-parity with the retired
# run_svrf_drc.py proven on a real commercial foundry deck.
#
# WRAPPER (not a bare symlink): the buddy's ELF carries DT_RUNPATH=/foss/tools/klayout-vibeic,
# but the runtime env sets LD_LIBRARY_PATH=/foss/tools/klayout:... and DT_RUNPATH is
# searched AFTER LD_LIBRARY_PATH. A bare symlink therefore loads the STOCK
# /foss/tools/klayout/libklayout_bd.so (which lacks the svrfdrc symbol + engine) →
# `undefined symbol: svrfdrc(int, char**)`. The wrapper prepends the fork lib dir to
# LD_LIBRARY_PATH so ALL klayout libs resolve consistently from the fork build.
RUN printf '#!/bin/sh\nexec env LD_LIBRARY_PATH=/foss/tools/klayout-vibeic:${LD_LIBRARY_PATH} /foss/tools/klayout-vibeic/svrfdrc "$@"\n' > /foss/tools/bin/svrfdrc \
 && chmod +x /foss/tools/bin/svrfdrc \
 && LD_LIBRARY_PATH=/foss/tools/klayout /foss/tools/bin/svrfdrc --help >/dev/null 2>&1 \
      && echo "svrfdrc buddy OK" || echo "WARN: svrfdrc buddy self-test failed"
# --- vibeic verification toolchain (cocotb / cocotb-coverage / pyuvm / sby) ---
# Editable-installed from the vibeic forks so an in-image Python patch is live; this
# overrides the base's stock cocotb with our fork. sby installs its driver + libs into
# /usr/local (it drives vibeic/yosys for the SVA-bind / formal path). Build isolation
# stays ON so cocotb's C-extension build pulls its own build deps.
# cocotb 2.x's `cocotb/simulator` extension #include's <Python.h>; the base ships the
# python3.12 runtime but NOT the dev headers, so install python3-dev first (g++/gcc/make
# are already in the base). Otherwise the editable build dies with
# "fatal error: Python.h: No such file or directory".
COPY --from=tb-src /tb /opt/vibeic-forks
# libxcb-cursor0: the 2026-07-29 daily merge moved OpenROAD's default build to
# Bazel, and the resulting binary links a newer Qt needing libxcb-cursor.so.0,
# which the base does not ship. MEASURED on the first composed image after the
# merge: `openroad -version` died with `error while loading shared libraries:
# libxcb-cursor.so.0`, and ldd showed it as the ONLY unresolved library. The
# pre-merge binary linked xcb but never this one.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3-dev \
      libxcb-cursor0 \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m pip install --break-system-packages \
      -e /opt/vibeic-forks/cocotb \
      -e /opt/vibeic-forks/cocotb-coverage \
      -e /opt/vibeic-forks/pyuvm \
 && make -C /opt/vibeic-forks/sby install PREFIX=/usr/local \
 && chmod -R a+rX /opt/vibeic-forks

# vibeic-eda#13 — PROVE THE ENGINE ANSWERS, not merely that a file is present.
# `--version` alone would pass against a binary that cannot solve, and this
# defect was invisible precisely because everything downstream reads only
# pass/fail: a dangling yices link ended every `smtbmc yices` run as
# `rc=16 ERROR: Engine terminated without status`, which reads as a property
# that could not be proved rather than a tool that was not there.
#
# So the check runs the whole real path — sby -> yosys -> smtbmc -> yices — on a
# property with a known answer. It sits HERE, after sby's own install, and NOT
# beside the symlink repair that fixes #13: placed there it failed the build
# with `sby: not found`, because sby is not on PATH until this stage. A guard
# that cannot reach the thing it guards reports on its own placement instead.
# PATH is set explicitly: the image-wide `ENV PATH` that puts /foss/tools/bin in
# front comes near the end of this file, so at this layer neither `yosys` nor
# `yices-smt2` resolves. Both were found the hard way — the guard failed twice
# on its own environment before it failed on anything real, which is the guard
# behaving correctly and the placement being wrong.
RUN set -eu; export PATH=/foss/tools/bin:$PATH; d=$(mktemp -d); cd "$d"; \
    printf 'module top(input clk);\n  reg [1:0] c = 0;\n  always @(posedge clk) c <= c + 1;\n  always @(posedge clk) assert (c <= 3);\nendmodule\n' > top.sv; \
    printf '[options]\nmode bmc\ndepth 4\n\n[engines]\nsmtbmc yices\n\n[script]\nread -formal top.sv\nprep -top top\n\n[files]\ntop.sv\n' > p.sby; \
    yosys -V; \
    /foss/tools/bin/yices-smt2 --version; \
    sby -f p.sby > sby.log 2>&1 || { echo "vibeic-eda#13 GUARD: sby/yices smoke FAILED"; tail -40 sby.log; exit 1; }; \
    grep -q "DONE (PASS" sby.log || { echo "vibeic-eda#13 GUARD: sby did not report PASS"; tail -40 sby.log; exit 1; }; \
    cd /; rm -rf "$d"

# --- NanGate45 / FreePDK45 enablement (GENERIC 45nm; tapeout_capable=false) ---
# Re-stage the ORFS nangate45 platform into the open_pdks libs.ref/<scl>/ layout the
# plugin's PDK resolvers expect: mcp-eda pdkConfig(), phase3_one_shot_runner _detect_pdk(),
# and programs/pdk_registry.json ALL resolve
# /foss/pdks/nangate45/libs.ref/NangateOpenCellLibrary/{lib,techlef,lef,gds}/... plus
# libs.tech/klayout/drc/FreePDK45.lydrc. The cell LEF is the ORFS `.macro.mod.lef`
# (rect-pin variant the router uses), staged under the canonical NangateOpenCellLibrary.lef
# name the resolvers reference; the CDL source netlist is kept for structural LVS (no
# KLayout LVS deck ships — see the registry entry's lvs_deck=null).
COPY --from=nangate45-src /orfs/flow/platforms/nangate45 /tmp/ng45
RUN NG=/foss/pdks/nangate45/libs.ref/NangateOpenCellLibrary \
 && mkdir -p "$NG"/lib "$NG"/techlef "$NG"/lef "$NG"/gds \
      /foss/pdks/nangate45/libs.tech/klayout/drc \
      /foss/pdks/nangate45/libs.tech/cdl \
 && cp /tmp/ng45/lib/NangateOpenCellLibrary_typical.lib   "$NG"/lib/ \
 && cp /tmp/ng45/lef/NangateOpenCellLibrary.tech.lef      "$NG"/techlef/ \
 && cp /tmp/ng45/lef/NangateOpenCellLibrary.macro.mod.lef "$NG"/lef/NangateOpenCellLibrary.lef \
 && cp /tmp/ng45/gds/NangateOpenCellLibrary.gds           "$NG"/gds/ \
 && cp /tmp/ng45/drc/FreePDK45.lydrc  /foss/pdks/nangate45/libs.tech/klayout/drc/ \
 && cp /tmp/ng45/cdl/NangateOpenCellLibrary.cdl /foss/pdks/nangate45/libs.tech/cdl/ \
 && chmod -R a+rX /foss/pdks/nangate45 \
 && rm -rf /tmp/ng45 \
 && test -f "$NG"/lib/NangateOpenCellLibrary_typical.lib \
 && test -f "$NG"/techlef/NangateOpenCellLibrary.tech.lef \
 && test -f "$NG"/lef/NangateOpenCellLibrary.lef \
 && test -f "$NG"/gds/NangateOpenCellLibrary.gds \
 && test -f /foss/pdks/nangate45/libs.tech/klayout/drc/FreePDK45.lydrc \
 && echo "nangate45 PDK staged OK"
# --- ASAP7 enablement (GENERIC 7nm PREDICTIVE; tapeout_capable=false) ---
# ASAP7 is the ASU/ARM 7nm *predictive* academic PDK (BSD-3-Clause): a realistic
# but NON-FOUNDRY 7nm FinFET std-cell enablement (LEF + Liberty + GDS + KLayout
# DRC), so synth / PnR / CTS / STA / area all run at a 7nm-representative node and
# the asap7 KLayout deck gives an EDUCATIONAL DRC — but it is NOT a manufacturable
# foundry sign-off (no real foundry, no LVS deck; ASAP7 uses a 4x-scaled drawn
# geometry convention). Re-stage the ORFS asap7 platform (v3.0) into the open_pdks
# libs.ref/<scl>/ layout the plugin's PDK resolvers expect. The std-cell library is
# `asap7sc7p5t` (7.5-track). We stage the DEFAULT RVT (R) VT flavor at the TYPICAL
# (TT / "TC") corner: asap7 splits Liberty into 5 functional groups (AO / INVBUF /
# OA / SEQ / SIMPLE) rather than one monolithic .lib, and ships most .lib gzipped
# (SEQ is plain) — we gunzip the 4 gzipped TT libs and keep only the TT set in lib/
# so any `*.lib` consumer sees a corner-consistent RVT-TT set. Cell LEF / GDS are
# the per-VT RVT files (asap7 GDS is per-VT-group, not per-cell). Registered in the
# plugin as PDK `asap7` (vibe-ic programs/pdk_registry.json, tapeout_capable=false).
# B2/#175 — also stage the ORFS OpenRCX extraction model so post-route SPEF EXTRACTS
# against a real per-layer RC model instead of the tech-LEF-RC fallback: the platform's
# `rcx_patterns.rules` (BSD; header "Extraction Rules for OpenRCX", the `-ext_model_file`
# consumed by `extract_parasitics`) is staged as
# `libs.tech/librelane/rules.openrcx.asap7.nom` — the SAME captable-glob convention the
# runner already uses for sky130A/gf180 (phase3_one_shot_runner `_emit_spef` globs
# `libs.tech/{librelane,openlane}/rules.openrcx.*.nom[.magic]`). `setRC.tcl` (per-layer
# set_layer_rc estimate) is staged alongside as `setRC.asap7.tcl` for reference. ASAP7
# ships ONE (typical) corner only → single-corner `.nom` SPEF (min/max disclosed absent).
COPY --from=nangate45-src /orfs/flow/platforms/asap7 /tmp/asap7
RUN A7=/foss/pdks/asap7/libs.ref/asap7sc7p5t \
 && mkdir -p "$A7"/lib "$A7"/techlef "$A7"/lef "$A7"/gds \
      /foss/pdks/asap7/libs.tech/klayout/drc \
      /foss/pdks/asap7/libs.tech/librelane \
 && zcat /tmp/asap7/lib/asap7sc7p5t_AO_RVT_TT_nldm_211120.lib.gz     > "$A7"/lib/asap7sc7p5t_AO_RVT_TT_nldm_211120.lib \
 && zcat /tmp/asap7/lib/asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib.gz > "$A7"/lib/asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib \
 && zcat /tmp/asap7/lib/asap7sc7p5t_OA_RVT_TT_nldm_211120.lib.gz     > "$A7"/lib/asap7sc7p5t_OA_RVT_TT_nldm_211120.lib \
 && zcat /tmp/asap7/lib/asap7sc7p5t_SIMPLE_RVT_TT_nldm_211120.lib.gz > "$A7"/lib/asap7sc7p5t_SIMPLE_RVT_TT_nldm_211120.lib \
 && cp /tmp/asap7/lib/asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib   "$A7"/lib/ \
 && cp /tmp/asap7/lef/asap7_tech_1x_201209.lef                 "$A7"/techlef/ \
 && cp /tmp/asap7/lef/asap7sc7p5t_28_R_1x_220121a.lef          "$A7"/lef/ \
 && cp /tmp/asap7/gds/asap7sc7p5t_28_R_220121a.gds             "$A7"/gds/ \
 && cp /tmp/asap7/drc/asap7.lydrc  /foss/pdks/asap7/libs.tech/klayout/drc/ \
 && cp /tmp/asap7/rcx_patterns.rules  /foss/pdks/asap7/libs.tech/librelane/rules.openrcx.asap7.nom \
 && cp /tmp/asap7/setRC.tcl           /foss/pdks/asap7/libs.tech/librelane/setRC.asap7.tcl \
 && chmod -R a+rX /foss/pdks/asap7 \
 && rm -rf /tmp/asap7 \
 && test -f "$A7"/lib/asap7sc7p5t_AO_RVT_TT_nldm_211120.lib \
 && test -f "$A7"/lib/asap7sc7p5t_INVBUF_RVT_TT_nldm_220122.lib \
 && test -f "$A7"/lib/asap7sc7p5t_OA_RVT_TT_nldm_211120.lib \
 && test -f "$A7"/lib/asap7sc7p5t_SIMPLE_RVT_TT_nldm_211120.lib \
 && test -f "$A7"/lib/asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib \
 && test -f "$A7"/techlef/asap7_tech_1x_201209.lef \
 && test -f "$A7"/lef/asap7sc7p5t_28_R_1x_220121a.lef \
 && test -f "$A7"/gds/asap7sc7p5t_28_R_220121a.gds \
 && test -f /foss/pdks/asap7/libs.tech/klayout/drc/asap7.lydrc \
 && test -f /foss/pdks/asap7/libs.tech/librelane/rules.openrcx.asap7.nom \
 && test -f /foss/pdks/asap7/libs.tech/librelane/setRC.asap7.tcl \
 && echo "asap7 PDK staged OK"

# B1/#174 — stage the ASAP7 device-LVS source-of-truth (Stage 9b) into libs.tech/.
#   CDL golden  -> libs.tech/cdl/       (mirrors the nangate45 `cdl_netlist` glob the
#                                        plugin's LVS resolver already understands)
#   FinFET SPICE models -> libs.tech/hspice/   (BSIM-CMG level-72 device models)
#   KLayout LVS layer stack -> libs.tech/klayout/lvs/   (asap7.lyt/.lyp, authored the
#                                        FinFET extraction in asap7_finfet_lvs.py)
#   All four CDL VT flavors are staged (R is the flavor matching the staged R GDS +
#   the pdk_registry `cdl_netlist` pointer; L/SL/SRAM kept for completeness).
COPY --from=asap7-lvs-src /a7sc/CDL/LVS /tmp/a7cdl
COPY --from=asap7-lvs-src /a7pdk/models/hspice /tmp/a7hspice
COPY --from=asap7-lvs-src /a7kl /tmp/a7kl
RUN A7T=/foss/pdks/asap7/libs.tech \
 && mkdir -p "$A7T"/cdl "$A7T"/hspice "$A7T"/klayout/lvs \
 && cp /tmp/a7cdl/asap7sc7p5t_28_L.cdl    "$A7T"/cdl/ \
 && cp /tmp/a7cdl/asap7sc7p5t_28_R.cdl    "$A7T"/cdl/ \
 && cp /tmp/a7cdl/asap7sc7p5t_28_SL.cdl   "$A7T"/cdl/ \
 && cp /tmp/a7cdl/asap7sc7p5t_28_SRAM.cdl "$A7T"/cdl/ \
 && cp /tmp/a7hspice/7nm_TT_160803.pm "$A7T"/hspice/ \
 && cp /tmp/a7hspice/7nm_SS_160803.pm "$A7T"/hspice/ \
 && cp /tmp/a7hspice/7nm_FF_160803.pm "$A7T"/hspice/ \
 && cp /tmp/a7kl/asap7.lyt "$A7T"/klayout/lvs/ \
 && cp /tmp/a7kl/asap7.lyp "$A7T"/klayout/lvs/ \
 && chmod -R a+rX "$A7T"/cdl "$A7T"/hspice "$A7T"/klayout/lvs \
 && rm -rf /tmp/a7cdl /tmp/a7hspice /tmp/a7kl \
 && grep -q "BSD 3-Clause" "$A7T"/cdl/asap7sc7p5t_28_R.cdl \
 && grep -q "BSD 3-Clause" "$A7T"/hspice/7nm_TT_160803.pm \
 && test -f "$A7T"/cdl/asap7sc7p5t_28_R.cdl \
 && test -f "$A7T"/hspice/7nm_TT_160803.pm \
 && test -f "$A7T"/klayout/lvs/asap7.lyt \
 && echo "asap7 device-LVS source-of-truth staged OK"

# --- ALIGN analog place & route (BUILT FROM vibeic/ALIGN-public SOURCE, isolated venv) ---
# ALIGN turns a device-level SPICE netlist straight into a placed + routed GDS: the analog
# counterpart of the digital OpenROAD flow. Built HERE from our own pinned fork (Stage 11),
# deliberately NOT `pip install align-analoglayout` — every other tool in this image is our
# source at a pinned SHA, and an upstream-published binary has no place in a sign-off
# toolchain we claim to own. Three MEASURED facts shape this block:
#
# 1. DEPENDENCY CONFLICT. ALIGN requires `pydantic>=1.9.2,<2.0`; this image ships
#    gdsfactory 9.44 on pydantic 2.x. A system-wide (--break-system-packages) install
#    breaks whichever of the two loses. So ALIGN gets its OWN venv at /foss/tools/align
#    and NOTHING is installed into the system interpreter.
# 2. A VENV ALONE IS NOT ISOLATION. The base's profile.d exports a global PYTHONPATH that
#    includes /usr/local/lib/python3.12/dist-packages, and PYTHONPATH is searched BEFORE a
#    venv's site-packages. Measured in 0.2.26:
#        /foss/tools/align/bin/python -c "import pydantic; print(pydantic.VERSION)" -> 2.12.5
#        env -u PYTHONPATH  (same command)                                          -> 1.10.26
#    The first one breaks ALIGN. Every entry point below therefore runs under
#    `env -u PYTHONPATH` — that, not the venv, is what actually insulates it.
# 3. NO LD_LIBRARY_PATH CRUTCH IS NEEDED, *because* we build from source. The published
#    PyPI wheel needs LD_LIBRARY_PATH=<venv>/lib at solve time (auditwheel left its
#    vendored libCbc-*.so requiring unmangled COIN-OR sonames). Building here links the
#    COIN-OR ILP stack (CBC/Clp/Cgl/Osi/CoinUtils/SYMPHONY) statically into align/PnR*.so.
#    Measured: `readelf -d align/PnR*.so` lists NO COIN-OR NEEDED entry — the only shared
#    dependency is liblpsolve55.so.5, resolved by the extension's own
#    RUNPATH $ORIGIN/thirdparty — and a full five_transistor_ota run completes with
#    LD_LIBRARY_PATH unset. The wrappers below therefore do NOT set it. If ALIGN is ever
#    switched back to the wheel, this changes and the wrappers must set it again.
#
# The C++ side (boost / spdlog / nlohmann-json / superlu / lpsolve + the COIN-OR stack) is
# fetched and compiled by ALIGN's CMake during pip install. CBC's own build is serial
# (`make -j1`, fixed upstream) and dominates the wall time of this layer. That cost is
# accepted deliberately. `_skbuild` (~1.4 GB of intermediates) is deleted in the SAME layer.
COPY --from=align-src /align /opt/align-src
ENV ALIGN_HOME=/foss/tools/align \
    ALIGN_PDK_SKY130=/opt/align-src/ALIGN-pdk-sky130/SKY130_PDK
RUN unset PYTHONPATH \
 && python3 -m venv /foss/tools/align \
 && /foss/tools/align/bin/pip install --no-cache-dir -q -U pip setuptools wheel \
 && /foss/tools/align/bin/pip install --no-cache-dir /opt/align-src/ALIGN-public \
 && /foss/tools/align/bin/pip install --no-cache-dir -q pytest \
 && rm -rf /opt/align-src/ALIGN-public/_skbuild \
 && printf '#!/bin/sh\n# vibeic wrapper: run ALIGN from its isolated venv (/foss/tools/align).\n# `env -u PYTHONPATH` is load bearing: this image exports a global PYTHONPATH that\n# would otherwise shadow the venv with the system pydantic 2 and break ALIGN.\n# No LD_LIBRARY_PATH needed: the COIN-OR solvers are linked statically into PnR*.so.\nexec env -u PYTHONPATH /foss/tools/align/bin/python /foss/tools/align/bin/schematic2layout.py "$@"\n' > /foss/tools/bin/align-schematic2layout \
 && printf '#!/bin/sh\n# vibeic wrapper: the ALIGN venv interpreter, insulated from the global PYTHONPATH.\nexec env -u PYTHONPATH /foss/tools/align/bin/python "$@"\n' > /foss/tools/bin/align-python \
 && chmod +x /foss/tools/bin/align-schematic2layout /foss/tools/bin/align-python \
 && mkdir -p /foss/tools/align/Viewer/INPUT \
 && chmod -R a+rX /foss/tools/align /opt/align-src \
# SELF-TEST — a successful pip install proves nothing about a P&R tool. Generate a real
# layout at a NON-nominal channel length and require (a) the GDS to actually contain
# geometry and (b) the drawn poly gates to equal the netlist L. (b) is the discriminating
# check: upstream's sky130 PDK draws every gate at the fixed 150nm Poly.Width, so if this
# image ever picks up upstream instead of vibeic/ALIGN-pdk-sky130 the build FAILS here.
 && cp -r /opt/align-src/ALIGN-pdk-sky130/examples/five_transistor_ota /tmp/align-selftest \
 && sed -i s/L=150e-9/L=500e-9/g /tmp/align-selftest/five_transistor_ota.sp \
 && cd /tmp/align-selftest \
 && { /foss/tools/bin/align-schematic2layout . -f /tmp/align-selftest/five_transistor_ota.sp \
        -p ${ALIGN_PDK_SKY130} > /tmp/align-selftest/run.log 2>&1 \
      || { echo '=== ALIGN self-test FAILED — run.log follows (the build layer is about to be discarded, so it is printed here) ==='; \
           tail -120 /tmp/align-selftest/run.log; exit 1; }; } \
 && /foss/tools/bin/align-python -c 'import gdspy; lib=gdspy.GdsLibrary(infile="/tmp/align-selftest/FIVE_TRANSISTOR_OTA_0.gds"); cell=lib.top_level()[0]; ps=cell.get_polygons(by_spec=True); n=sum(len(v) for v in ps.values()); gates=sorted({round((p[:,0].max()-p[:,0].min())*1e9) for p in ps[(66,20)] if (p[:,1].max()-p[:,1].min()) >= (p[:,0].max()-p[:,0].min())}); print("ALIGN self-test: top cell %s, geometry polygons=%d, vertical poly 66/20 gate lengths(nm)=%s" % (cell.name, n, gates)); assert n > 0, "FAIL: ALIGN emitted a GDS with no geometry"; assert gates == [500], "FAIL: gates not drawn at the netlist L=500nm -> the sky130 PDK in this image is NOT our patched fork"' \
# and the PDK fork's own regression guard, which ships a NEGATIVE CONTROL proving the
# guard is capable of failing (revert the fix -> 3 of its 6 tests fail).
 && cd /opt/align-src/ALIGN-pdk-sky130 \
 && env -u PYTHONPATH PATH=/foss/tools/align/bin:$PATH /foss/tools/align/bin/python -m pytest -q tests/test_channel_length.py \
 && rm -rf /tmp/align-selftest /opt/align-src/ALIGN-pdk-sky130/.pytest_cache \
 && echo "ALIGN OK: built from vibeic source; sky130 PDK honours netlist L; 6/6 channel-length guards pass"

# restore the base's non-root runtime user
USER 1000

# --- bare `docker exec` PATH (vibeic enhancement over stock iic-osic-tools) ---
# The stock base only puts /foss/tools/* on PATH via /etc/profile.d/iic-osic-tools-setup.sh,
# which runs for LOGIN shells only. A non-login `docker exec <c> <tool>` (and
# `docker exec <c> bash -c '<tool>'`, the idiom the Vibe-IC MCP uses) therefore could not
# resolve yosys/openroad/sta/... ("executable file not found in $PATH") — including the bare
# `docker exec vibeic-eda yosys --version` in this repo's README Quick Start. Bake the tool
# dirs into a global ENV PATH so tools resolve WITHOUT a login shell or a per-command export.
# Additive only (login shells still re-prepend via profile.d — harmless duplicate).
ENV PATH=/headless/.local/bin:/foss/tools/bin:/foss/tools/sak:/foss/tools/kactus2:/foss/tools/klayout:/foss/tools/osic-multitool:${PATH}

# NO DANGLING SYMLINKS. `/foss/tools/bin` is what PATH resolves through, and our
# installs `rm -rf` five base prefixes before replacing them — which is how eqy
# and mcy vanished while their links stayed, advertising tools the image did not
# have (#19). A link that dangles is a worse failure than one that is absent: it
# reads as installed.
#
# LAST INSTRUCTION IN THE FILE ON PURPOSE. My first attempt put this immediately
# after the yosys restore and it failed the build listing magic, netgen, ngspice,
# iverilog and sby — every one of them a tool whose COPY comes LATER. The check
# was right and the position was wrong: "nothing dangles" is only answerable once
# everything is installed.
# --- bare `docker exec` PYTHONPATH (same gap as the PATH line above) ---
# The stock base exports PYTHONPATH only from profile.d, i.e. for LOGIN shells,
# so `docker exec <c> eqy` — and any non-login invocation, which is what the
# Vibe-IC MCP uses — cannot import eqy_job and dies. PATH was already baked into
# ENV here for exactly this reason; PYTHONPATH was not, and eqy is the tool that
# exposed it. Additive: login shells still re-prepend via profile.d.
# NO trailing `:${PYTHONPATH}`. Nothing defines PYTHONPATH at this point, so it
# expanded to empty and left `…/python3:` — and an EMPTY element in PYTHONPATH
# means the CURRENT WORKING DIRECTORY. Measured in 0.2.37: one empty entry in
# `sys.path`. In a container where designs are run from arbitrary project
# directories, a stray `os.py` or `random.py` beside a netlist would shadow the
# stdlib for every tool written in Python.
#
# buildkit warned about it — `UndefinedVar: Usage of undefined variable
# '$PYTHONPATH' (line 591)` — and I could not see the warning, because until
# this same release the build output was captured and discarded rather than
# streamed. The fix that made builds watchable is what surfaced it.
#
# Login shells still prepend the full list via profile.d; this is the additive
# entry that makes `docker exec` work.
ENV PYTHONPATH=/foss/tools/yosys/share/yosys/python3

# eqy and mcy through the LINK in /foss/tools/bin, which is how anyone invokes
# them, and in a NON-LOGIN shell, which is what `docker exec` gives. Verifying
# via the real path under a login shell — my first two attempts — passes on an
# image where both would fail for a user.
RUN /foss/tools/bin/eqy --help >/dev/null \
 && /foss/tools/bin/mcy --help >/dev/null \
 && echo "eqy and mcy run from /foss/tools/bin without a login shell"

RUN n=0; for f in /foss/tools/bin/*; do \
      [ -L "$f" ] && [ ! -e "$f" ] && { echo "DANGLING $f -> $(readlink "$f")"; n=$((n+1)); }; \
    done; \
    [ "$n" = "0" ] || { echo "refusing to ship $n dangling symlink(s)"; exit 1; }; \
    echo "no dangling symlinks in /foss/tools/bin"
