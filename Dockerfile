# vibeic-eda — forked + enhanced OSS EDA toolchain (vibeic)
# = hpretl/iic-osic-tools runtime + vibeic/* patched EDA-tool forks.
#
# Each tool is built from its vibeic fork branch in a ubuntu24.04-family builder so
# the binary matches the iic-osic-tools runtime (python3.12 / glibc2.39). An
# ubuntu22.04 build fails in the runtime (wants libpython3.10).
#
# Every PATCHED fork branch below (Stages 1-7) has a gatekeeper-verified FAIL->PASS
# proof recorded in FIX_STATUS.md. Version-jump note: yosys is a deliberate uplift
# (0.4x -> 0.66+slang, the roadmap's prescribed "make slang the default SV path");
# magic/netgen/ngspice track the same rolling releases the base ships. The Stage-8
# verification toolchain (cocotb/cocotb-coverage/pyuvm/sby) is baked as
# ENHANCEMENT-READY forks (editable install, no FAIL->PASS patch yet) so the
# professional TB generator runs on tools we own and can patch. A full plugin-flow
# validation pass gates promotion of 0.2.0 over 0.1.0 (see FIX_STATUS.md).

# ---------------------------------------------------------------------------
# Stage 1 — vibeic/OpenROAD (post-detailed-route repair on real parasitics; Signal-11 fix)
# ---------------------------------------------------------------------------
FROM openroad/ubuntu24.04-dev:latest AS openroad-builder
# Pinned to the F-A2 commit (tapcell -bound_to_placement) shipped in vibeic-eda:0.2.7,
# a fast-forward over the F-A1 repair_antennas -reroute commit (0.2.6).
# HEAD of branch vibeic/post-route-detailed-routing-repair as of 2026-07-09.
ARG OPENROAD_REF=1bade74e7224d9c631b13dec626d258af3f65196  # pinned; branch vibeic/openroad-integration (0.2.22 consolidation: route DR6 + place FP2 + pdn PD2/PD4 + timing/fill/cts/dpl-leak; src/sta=1e21c3f superset fetched from vibeic/OpenSTA fork; commit-msg NDA-purged 2026-07-19)
RUN git clone https://github.com/vibeic/OpenROAD.git /src \
 && cd /src && git checkout ${OPENROAD_REF} \
 && git submodule update --init --recursive --depth 1 \
 && ./etc/Build.sh -threads=$(nproc) -cmake="-DCMAKE_BUILD_TYPE=Release"

# ---------------------------------------------------------------------------
# Stage 2 — vibeic/yosys (tri-state fanin preservation + modern slang SV frontend)
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS yosys-builder
ARG YOSYS_REF=330b3eb197398f5d9e568c72f364fd3c0efa6f82  # pinned; branch vibeic/synth-fixes-integration (0.2.22: v0.67 base + synth-fixes + w2/w3/w4 + icg consolidated; 15 non-merge commits; build PASS, smoke green)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake git bison flex gawk pkg-config \
      libreadline-dev tcl-dev libffi-dev zlib1g-dev python3 \
      libboost-system-dev libboost-python-dev libboost-filesystem-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/vibeic/yosys.git /yosys \
 && cd /yosys && git checkout ${YOSYS_REF} && git submodule update --init --recursive \
 && cmake -S /yosys -B /yosys/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/foss/tools/yosys \
 && cmake --build /yosys/build -j"$(nproc)" \
 && cmake --install /yosys/build

# ---------------------------------------------------------------------------
# Stage 3 — vibeic/ngspice (batch-honesty rc + $& scalar + control-mode .param + native MC)
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS ngspice-builder
ARG NGSPICE_REF=6e9f78fb5dd56fa56c4d5599ca5c11717a4403ea  # pinned; branch vibeic/batch-honesty-integration (0.2.22: batch-honesty + #11 process-parallel AC (5.5x) + #68 hardened DSPF + w4/w5; make check 54/54)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git autoconf automake libtool bison flex \
      libx11-dev libxaw7-dev libreadline-dev libncurses-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/vibeic/ngspice.git /ngspice \
 && cd /ngspice && git checkout ${NGSPICE_REF} \
 && ./autogen.sh \
 && ./configure --prefix=/foss/tools/ngspice --with-ngshared=no --enable-xspice --enable-openmp --disable-debug \
 && make -j"$(nproc)" && make install

# ---------------------------------------------------------------------------
# Stage 4 — vibeic/magic + vibeic/netgen (LVS-fidelity pair)
#   magic:  ext2spice label->port promotion (feeds netgen portless guard)
#   netgen: property-error verdict, portless guard, -auto-global, -nopower, black-box match
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS lvs-builder
ARG MAGIC_REF=19185c197fbaa4a91ec52877a2c13ec08a97b7ed  # pinned; branch vibeic/integration is the LVS-fidelity line (gk-merge/2026-07-19) MERGED with vibeic/bridge-tech-multimetal, so the DEF/LVS robustness fixes the image already ran AND the 2026-07-18 batch (#46 foundry layer-map, #47/#37 grid snap, #28 SPEF, #48 NDR, #32 tech-from-LEF, #38) are both present. Neither line was dropped.
ARG NETGEN_REF=0334b7dfb1d6adce0a8079f5552f68982815d3d9  # pinned; branch vibeic/connectivity-match (netgen PR#1: plain-mode lvs finishes + stdout==report, resolves vibe-ic#191 tool layer; report byte-identical so #189 classifier/Step-31 unaffected)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git m4 tcl-dev tk-dev libx11-dev libcairo2-dev libncurses-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/vibeic/magic.git /magic \
 && cd /magic && git checkout ${MAGIC_REF} \
 && ./configure --prefix=/foss/tools/magic && make -j"$(nproc)" && make install
RUN git clone https://github.com/vibeic/netgen.git /netgen \
 && cd /netgen && git checkout ${NETGEN_REF} \
 && ./configure --prefix=/foss/tools/netgen && make -j"$(nproc)" && make install

# ---------------------------------------------------------------------------
# Stage 5 — vibeic/iverilog (->> nonblocking-event codegen segfault fix + package ordering)
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS iverilog-builder
ARG IVERILOG_REF=42a15a5c6125f093dbe8f664a5826d0cada86109  # pinned; branch vibeic/sv-tb-coverage (0.2.27: ff-verified descendant of 110cadd5 — no fix dropped; adds vibe-ic#125 $dumpvars/$dumpports forward-ref bind-against-completed-scope + graft guard test, plus SDF INCREMENT/COND/scale/mtm honouring + forward-ref elab)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git autoconf gperf flex bison ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/vibeic/iverilog.git /iverilog \
 && git config --global --add safe.directory /iverilog \
 && cd /iverilog && git checkout ${IVERILOG_REF} \
 && sh autoconf.sh && ./configure --prefix=/foss/tools/iverilog \
 && make -j"$(nproc)" && make install

# ---------------------------------------------------------------------------
# Stage 6 — vibeic/klayout (streamout MANUFACTURINGGRID snap + merge-abutting + foundry
#   layer-map, AND the native SVRF/Calibre DRC engine + `svrfdrc` buddy). Qt-less db-lib
#   + pymod + buddies; shipped parallel as klayout-vibeic so the base 0.30.6 GUI/DRC stays
#   intact. Phase-3 streamout AND commercial sign-off DRC both point here.
#   The svrf-native-drc branch = streamout-fixes + db::SVRFDeck/db::SVRFEngine + the
#   `svrfdrc <deck> <layout> <report>` buddy (native C++, NO Python interpreter). Byte-
#   parity vs the old run_svrf_drc.py proven on a real commercial foundry deck.
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS klayout-builder
ARG KLAYOUT_REF=bc4e211b5e37d9ae11b57286cff3662cc5a4ab40  # pinned; branch vibeic/klayout-signoff-int (0.2.23: + tl::Thread pthread_join heap-race ROOT-CAUSE fix — svrfdrc --threads use-after-free, TSan-proven 6race->0, report byte-identical; recompiles tlThreads.cc + relinks libklayout_tl.so)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git python3-dev zlib1g-dev libexpat1-dev libcurl4-openssl-dev libpng-dev \
      qtbase5-dev qttools5-dev-tools ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# build.sh needs qmake in PATH to drive the build system even with -without-qt.
RUN git clone https://github.com/vibeic/klayout.git /klayout \
 && cd /klayout && git checkout ${KLAYOUT_REF} \
 && ./build.sh -without-qt -noruby -nolibgit2 -j"$(nproc)" -bin /foss/tools/klayout-vibeic -build /klayout/bld
# ---------------------------------------------------------------------------
# Stage 6b — vibeic/verilator. Previously NOT layered: the note here read "no honest
#   fix warranted on v5.051 — nothing to layer", which was true at the time. It is no
#   longer: two constrained-randomization fixes landed on the fork with unfakeable
#   gates (V3Randomize Pow lowering generalized from base-2 to ANY power-of-2 base;
#   $countbits with a single runtime 1-bit control, previously E_UNSUPPORTED), each
#   with a proven-negative and 177/177 on the constraint+randomize suite. The base
#   image ships verilator, so ours is built in parallel and takes PATH precedence.
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS verilator-builder
ARG VERILATOR_REF=0782026557405c5ee7967a4975e9e6f20ee82154  # pinned; branch vibeic/sv-tb-coverage (id 12: pow2-base Pow lowering + $countbits var-ctrl)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git autoconf flex bison ca-certificates help2man perl python3 \
      libfl2 libfl-dev zlib1g zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/vibeic/verilator.git /verilator \
 && git config --global --add safe.directory /verilator \
 && cd /verilator && git checkout ${VERILATOR_REF} \
 && autoconf && ./configure --prefix=/foss/tools/verilator-vibeic \
 && make -j"$(nproc)" && make install

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
ARG COCOTB_REF=297211d359e81f6d48465e82752ef1866d1c8b0d  # branch vibeic/parallel-regression-dispatch (PLL1)
ARG COCOTB_COVERAGE_REF=be916da99520662f77cfccb8dd17861c8f986ce0  # branch vibeic/integration is V15 crv scalability + V36 rank + V10/V11/V35 bins-closure; union verified per-definition (20/20, 0 dropped)
ARG PYUVM_REF=f6ccec0ecebe504b209ac0cad74dc0716888a96f  # branch vibeic/integration is V5 RAL accessors + V7 TLM comparators + V6 sequencer arbitration; suite is the exact union (441/535), 0 failures
ARG SBY_REF=37298228f565ab35549bd7b27c0551ddefb55802  # branch vibeic/integration: V23/V24/V26/V30 (main) + V42/V27/V19/V18/V28 (w3) + V39/V49/V46/V50/V38/V40 (w2/w4), package layout, 11 version-drift reds fixed at root
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
      https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git /orfs \
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
      https://github.com/The-OpenROAD-Project/asap7sc7p5t_28.git /a7sc \
 && git -C /a7sc sparse-checkout set CDL/LVS \
 && git clone --depth 1 --branch ${ASAP7PDK_REF} --filter=blob:none --sparse \
      https://github.com/The-OpenROAD-Project/asap7_pdk_r1p7.git /a7pdk \
 && git -C /a7pdk sparse-checkout set models/hspice \
 && git clone --depth 1 --branch ${ASAP7KL_REF} \
      https://github.com/laurentc2/ASAP7_for_KLayout.git /a7kl \
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
FROM hpretl/iic-osic-tools:latest
LABEL org.opencontainers.image.title="vibeic-eda"
LABEL org.opencontainers.image.description="Forked+enhanced OSS EDA toolchain (vibeic): iic-osic-tools base + vibeic/* patched EDA-tool forks with gatekeeper-verified FAIL->PASS proofs (see FIX_STATUS.md)."
LABEL org.opencontainers.image.source="https://github.com/vibeic"

# /foss/tools is root-owned and the base runs as user 1000 — become root to mutate it.
USER root
# --- vibeic/OpenROAD (native 24.04 build → RUNPATH /opt/or-tools, no lib bundling) ---
COPY --from=openroad-builder /opt/or-tools /opt/or-tools
COPY --from=openroad-builder /src/build/bin/openroad /foss/tools/openroad/bin/openroad
# Clean-replace the base tool dirs FIRST so no stale base files survive the COPY merge —
# e.g. the base's ghdl.so yosys plugin is built against the old ABI and would crash yosys 0.66.
RUN rm -rf /foss/tools/yosys /foss/tools/ngspice /foss/tools/magic /foss/tools/netgen /foss/tools/iverilog
# --- vibeic/yosys (replaces base yosys install; bin symlinked into /foss/tools/bin) ---
COPY --from=yosys-builder /foss/tools/yosys /foss/tools/yosys
# --- vibeic/ngspice ---
COPY --from=ngspice-builder /foss/tools/ngspice /foss/tools/ngspice
# --- vibeic/magic + vibeic/netgen ---
COPY --from=lvs-builder /foss/tools/magic /foss/tools/magic
COPY --from=lvs-builder /foss/tools/netgen /foss/tools/netgen
# --- vibeic/iverilog ---
COPY --from=iverilog-builder /foss/tools/iverilog /foss/tools/iverilog
COPY --from=verilator-builder /foss/tools/verilator-vibeic /foss/tools/verilator-vibeic
# --- vibeic/klayout (parallel streamout install; base klayout untouched) ---
# build.sh emits the Qt-less db-lib + pymod + db_plugins/liblefdef.so into its -build dir.
COPY --from=klayout-builder /klayout/bld /foss/tools/klayout-vibeic
# Re-point the /foss/tools/bin symlinks the base created to our installs.
RUN for t in yosys yosys-abc; do ln -sf /foss/tools/yosys/bin/$t /foss/tools/bin/$t 2>/dev/null || true; done \
 && ln -sf /foss/tools/ngspice/bin/ngspice /foss/tools/bin/ngspice 2>/dev/null || true \
 && ln -sf /foss/tools/magic/bin/magic /foss/tools/bin/magic 2>/dev/null || true \
 && ln -sf /foss/tools/netgen/bin/netgen /foss/tools/bin/netgen 2>/dev/null || true \
 && for t in iverilog vvp iverilog-vpi vvp; do ln -sf /foss/tools/iverilog/bin/$t /foss/tools/bin/$t 2>/dev/null || true; done
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
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3-dev \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m pip install --break-system-packages \
      -e /opt/vibeic-forks/cocotb \
      -e /opt/vibeic-forks/cocotb-coverage \
      -e /opt/vibeic-forks/pyuvm \
 && make -C /opt/vibeic-forks/sby install PREFIX=/usr/local \
 && chmod -R a+rX /opt/vibeic-forks
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
