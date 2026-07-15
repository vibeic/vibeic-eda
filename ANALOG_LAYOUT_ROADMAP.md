# vibeic-eda — Analog Auto-Layout Fork & Enhance Roadmap

> Companion to `OSS_EDA_FORK_ROADMAP.md` / `FIX_STATUS.md`. Covers the **A5 analog
> place-and-route gap** (vibe-ic issue #144): `eda_analog_layout` streams empty
> geometry while reporting success — there is no real OSS analog auto-layout wired.
> This doc records the exhaust-OSS-investigation result: a **working feasibility
> spike** (ALIGN lays out the run's real LDO into GDS) plus a concrete integration
> plan. Nothing here is shipped in the image yet.

## 1. The gap (evidence)

- `mcp_eda_analog_layout` produces a GDS with **no placed devices / no routes** for real
  blocks (LDO, delta-sigma), and `analog_a5_layout_check` is presence+size-only — a gate
  hole (issue #144). Commercial equivalent: Cadence Virtuoso Layout-GXL / MAGIC-assisted
  or Mentor Pyxis analog P&R; there is no OSS analog auto-P&R in the current image.
- Digital OpenROAD P&R does **not** substitute: it needs a std-cell library and a gate
  netlist; an analog block is a transistor-level netlist with matching/symmetry/common-
  centroid constraints OpenROAD has no concept of.

## 2. OSS candidates evaluated

| Tool | What it is | Maintained | Input → Output | sky130? | Verdict |
|---|---|---|---|---|---|
| **ALIGN** (ALIGN-analoglayout/ALIGN-public) | Full analog P&R: hierarchical constraint-graph recognition → ILP sequence-pair placement → MILP detailed router → power grid → GDS/LEF | **YES** — last commit 2026-07-05, 1397 PRs, cp310–cp313 cibuildwheels on PyPI | SPICE subckt (+optional `.const.json`) → **GDS + LEF** | **YES** — official `ALIGN-pdk-sky130` PDK repo (last commit 2026-07-05) | ✅ **CHOSEN — spike-proven end-to-end on the real LDO** (§3) |
| **MAGICAL** (magical-eda/MAGICAL) | Analog P&R research framework (UT Austin) | Stale (research-cadence; last substantive activity years back) | SPICE → GDS | partial/example-only | ✗ higher build risk, less active, no clear sky130 DRC path; ALIGN strictly dominates on maintenance + PDK |
| **glayout / OpenFASOC** | Parameterised sky130 analog *generators* (not netlist→layout P&R) | active | Python generator calls → GDS | sky130-native | Complementary substrate (device/PCell generation), not a drop-in for arbitrary sized topologies; keep as a fallback device-generator |
| **IHP gdsfactory PCells** | Programmatic PCell/device generation for sg13g2 | active | Python → GDS | sg13g2 only | Device-generator substrate for a future sg13g2 ALIGN PDK; not auto-P&R |

**Chosen: ALIGN.** It is the only candidate that (a) is actively maintained, (b) ships a
cp312 binary wheel (no local C++ build needed for the engine), (c) has an official sky130
PDK, and (d) **actually produced real geometry from our LDO in the spike**.

## 3. Feasibility spike — RESULT: real geometry out (success bar met)

All in the running `vibeic-eda:0.2.17` container, entirely under a scratch dir
(`/home/reyerchu/.cache/align_spike/`) — **no baked image files modified**.

**Install (no C++ build):**
```
pip3 install --target=<scratch>/pyenv --prefer-binary align-analoglayout   # 0.9.8 cp312 wheel
#  + gdspy built from source (abandoned, no cp312 wheel) — builds clean with the container gcc
#  + soname shim: auditwheel mangles the bundled COIN-OR libs (libCgl-<hash>.so.1.10.3);
#    the engine dlopens the UNmangled SONAME, so symlink libCgl.so.1 -> the mangled file
#    and put the shim dir on LD_LIBRARY_PATH.
```
Engine imports: `from align import PnR` → OK (C++ SeqPair-ILP placer + MILP router via pybind11).

**Spike A — shipped OTA on the mock PDK (engine smoke):**
`five_transistor_ota` → topology recognised (diff-pair, current-mirror) → 4 ILP placements →
global+detail route → power grid → **GDS 87 KB, 32 layers, 1543 polygons, 4.16×5.9 µm**.

**Spike B — THE RUN'S ACTUAL LDO on the Bulk65nm mock PDK (the real deliverable):**
The `benchmark-data/.../phase3/analog/ldo/ldo.sp` topology (bias mirror + NMOS diff-pair +
PMOS mirror load + PMOS series-pass + Miller cap + feedback divider), device names mapped
`sky130_fd_pr__nfet/pfet_01v8 → n/p`, scale 1u→meters:
- **Output: `LDO_0.gds` 202 KB + `LDO_0.lef` 46.54 × 141.8 µm, 23 layers, 3039 polygons.**
- Connectivity: **SHORT: 0, OPEN: 0** (18 different-width segments — not DRC-clean, expected
  on a mock PDK; the spike bar is *any* real geometry, DRC-clean not required).
- Two ALIGN primitive constraints surfaced + solved (feed the future device-mapper):
  current-mirror legs must share W (express the ratio via `m`, not different W);
  MOM cap ≤ 1000 fF on the mock PDK (5 pF Miller → capped for the spike).

**Spike C — same LDO on the REAL sky130 PDK (`ALIGN-pdk-sky130`):**
Reached primitive-gen + placement + **routing (SHORT: 0, OPEN: 2)**, then failed the final
GDS write on `KeyError: 'm1Pitch'` — an ALIGN-0.9.8-vs-community-PDK **config-schema skew**
(the sky130 PDK's `layers.json` nests metal pitch differently than the 0.9.8 engine reads).
Real sky130 device constraints learned (device-mapper requirements): **W must be a multiple
of the 210 nm fin pitch**, and **fins-per-device < cell height** (use small per-finger width
~1.05 µm × more fingers). So the sky130 path is ~90% wired: engine + placement + routing run;
what remains is (1) reconcile the ALIGN/PDK version, (2) close 2 opens, (3) DRC-iterate.

## 4. Integration plan (candidate → image → MCP)

### 4.1 Fork shape
- Fork **`github.com/ALIGN-analoglayout/ALIGN-public` → `github.com/vibeic/ALIGN`** and
  **`ALIGN-pdk-sky130` → `github.com/vibeic/ALIGN-pdk-sky130`** (the PDK carries the fixes:
  `m1Pitch` schema reconcile, ratio-aware mirrors, cap/res limits). Fork-committed enhancements
  are proven the FIX_STATUS way (FAIL→PASS repro per fix).

### 4.2 New Docker stage in `vibeic-eda` (next 0.2.x)
- A builder stage that `pip install`s the cp312 `align-analoglayout` wheel + builds `gdspy`
  into `/foss/tools/align/`, drops the COIN-OR soname shim, bakes `ALIGN_HOME` + the vibeic
  sky130 PDK, and puts `schematic2layout.py` on the global `ENV PATH` (same non-login-PATH
  idiom as 0.2.12). Prior stages byte-identical → cached; only the new trailing stage builds.
- Smoke gate: `schematic2layout.py $ALIGN_HOME/examples/five_transistor_ota -p <mock>` exits 0
  and writes a non-empty GDS (assert polygons > 0) — the same shape as the existing 7/7 smoke.

### 4.3 MCP wiring (`eda_analog_layout`, plugin side — NOT in this repo)
- Replace the empty-geometry emitter with: **(a) device-fingering mapper** — sized W/L from
  the A4 sizing point → sky130 fin-pitch-snapped `w`/`nf`/`m`, mirror ratios via `m`, cap/res
  within PDK limits (the three constraints the spike catalogued); **(b)** write the ALIGN
  subckt + optional `.const.json` (symmetry/common-centroid on the diff-pair & mirror);
  **(c)** shell `schematic2layout.py <dir> -p <pdk> -w <work>`; **(d)** return `LDO_0.gds` +
  `LDO_0.lef`; **(e)** feed the GDS into the image's **native svrfdrc/klayout DRC** (already
  shipped, 0.2.11) so `analog_a5_layout_check` becomes a real DRC gate, not presence-only.

### 4.4 Honest effort estimate
| Milestone | Effort | Basis |
|---|---|---|
| Mock-PDK auto-layout live in image + MCP (real geometry, DRC-waived) | **LOW** | spike done; just package + wire |
| Real **sky130** DRC-iterated geometry | **MEDIUM** | adopt `ALIGN-pdk-sky130` + fix `m1Pitch` schema skew + close 2 opens + DRC loop against native svrfdrc |
| **sg13g2** (IHP) auto-layout | **HIGH** | no community ALIGN sg13g2 PDK — author the ALIGN PDK abstraction (`Align_primitives.py`/`layers.json`/`mos.py`) atop the IHP gdsfactory/klayout PCell substrate |
| Constraint-driven quality (common-centroid, guard-ring, matching) | MEDIUM | ALIGN supports `.const.json`; author a spec→constraint generator |

## 5. Status

**◐ PARTIAL — spike-proven, image-integration pending.** ALIGN produces real GDS+LEF from the
run's actual LDO on a mock PDK (0 shorts/0 opens); the sky130 real-PDK path is wired through
routing and blocked only on a bounded PDK-config reconcile. Next concrete step: the §4.2 Docker
stage as a new 0.2.x, then the §4.3 MCP rewrite (plugin-side, filed to #144). No claim of
DRC-clean silicon; the mock-PDK geometry is a capability proof, not a signoff artifact.
