# Tool inventory

Three tables, each stating what it cannot see. Regenerated 2026-07-29.

- **Table A** — every directory in the image. Measured with `ls /foss/tools`.
- **Table B** — every tool IIC-OSIC-TOOLS ships, and whether we use it.
- **Table C** — PDK data, which neither A nor B can see.

The rule they audit (owner, 2026-07-29): every OSS tool we use is forked into
`vibeic/`, tracked by the daily fork-gatekeeper, and PDK data counts as well.

## Three ways the fork column lied, and what fixed each

This column was rebuilt from scratch. The previous version was wrong in three
separate ways, and each failure produced output indistinguishable from a correct
answer — which is why none of them surfaced on their own.

1. **`gh repo list --limit 100` truncates.** It reported 14 forks. `gh api
   --paginate` reports 53 repos and **45 forks**. A truncated list does not
   announce itself; it just looks like a smaller org.
2. **The list endpoint returns `parent: null` for every row.** Only the
   single-repo endpoint populates it. Comparing against the list's `parent`
   yields "nothing is forked", which is byte-identical to a genuinely unforked
   org. Verifying `parent` on one repo and assuming the list carries it too is
   how this was missed.
3. **Upstreams get renamed.** The metadata records the old name, so a renamed
   repo looks unforked. Resolved by asking the API for each slug's canonical
   `full_name` rather than hard-coding a rename table — a hard-coded table is
   wrong again at the next rename. One case found: `povik/yosys-slang` is now
   `povik/sv-elab`, and our fork is of the new name.

Matching is by **upstream repo**, never by name: `vibeic/<X>` existing does not
mean we forked X's upstream, and `vibeic/<X>` not existing does not mean we did
not.

Three states are kept distinct from `no`, because collapsing them turns "could
not determine" into "does not exist":

| state | meaning |
|---|---|
| `n/a (pip-installed)` | a PyPI package in a venv, not a git clone — verified in the image via `lib/python*/site-packages/*.dist-info`. Forking its repo would not change what is installed. |
| `? (upstream unconfirmed)` | source not established. Not recorded as unforked. |
| `—` | not a tool directory (`bin`, `sak`, `fpga`). |

## Table A — every directory in the image

Measured: `ls /foss/tools` in `ghcr.io/vibeic/vibeic-eda:0.2.31`, diffed against
the same command in `hpretl/iic-osic-tools:latest`. **58 directories: 54
inherited from the base, 4 added by us (`align`, `klayout-vibeic`,
`verilator-vibeic`, `yices`), 0 removed.**

The *used* column comes from searching `mcp-eda/src`, `programs/*.py` and
`flow/*.yaml` for each tool name and each of its executables, then hand-checking
every hit — a tool name collides with prose (`spike` matched "spike-suppression",
`uv` matched `worst_ir_uv`, `covered` matched the English word). Eight such false
positives were removed.

| directory | function | origin | forked | used | status |
|---|---|---|---|---|---|
| `align` | Analog layout automation | added by us | yes `vibeic/ALIGN-public` | yes | compliant |
| `bin` | Shared PATH directory (not a tool) | iic-osic-tools base | — | yes | not a tool directory |
| `charlib` | Standard-cell library characterisation | iic-osic-tools base | n/a (pip-installed) | no | not a git clone; forking would not change what is installed |
| `covered` | Verilog coverage | iic-osic-tools base | no | no | unused; no fork required |
| `cvc_rv` | C-to-RTL equivalence | iic-osic-tools base | no | no | unused; no fork required |
| `fpga` | FPGA vendor-tool shim (not a tool dir) | iic-osic-tools base | — | no | not a tool directory |
| `gaw3-xschem` | Waveform viewer (GUI) | iic-osic-tools base | no | no | unused; no fork required |
| `gds3d` | GDS 3D viewer (GUI) | iic-osic-tools base | no | no | unused; no fork required |
| `ghdl` | VHDL simulator | iic-osic-tools base | no | no | unused; no fork required |
| `ghdl-yosys-plugin` | VHDL front end for yosys | iic-osic-tools base | no | no | unused; no fork required |
| `gtkwave` | Waveform viewer | iic-osic-tools base | yes `vibeic/gtkwave` | yes | compliant |
| `irsim` | Switch-level simulation (GUI) | iic-osic-tools base | no | no | unused; no fork required |
| `iverilog` | Verilog simulator | iic-osic-tools base | yes `vibeic/iverilog` | yes | compliant |
| `kactus2` | IP-XACT packaging (GUI) | iic-osic-tools base | no | no | unused; no fork required |
| `kepler-formal` | Formal property verification | iic-osic-tools base | no | no | unused; no fork required |
| `klayout` | Layout / DRC engine | iic-osic-tools base | yes `vibeic/klayout` | yes | compliant |
| `klayout-vibeic` | KLayout, our build with the native svrfdrc DRC engine | added by us | yes `vibeic/klayout` | yes | compliant |
| `klayout_gdsfactory8` | KLayout + gdsfactory 8 | iic-osic-tools base | n/a (pip-installed) | yes | not a git clone; forking would not change what is installed |
| `klayout_gdsfactory9` | KLayout + gdsfactory 9 | iic-osic-tools base | n/a (pip-installed) | yes | not a git clone; forking would not change what is installed |
| `libman` | Liberty management | iic-osic-tools base | no | no | unused; no fork required |
| `magic` | Layout editing + parasitic extraction | iic-osic-tools base | yes `vibeic/magic` | yes | compliant |
| `netgen` | LVS comparison | iic-osic-tools base | yes `vibeic/netgen` | yes | compliant |
| `ngspice` | SPICE simulation | iic-osic-tools base | yes `vibeic/ngspice` | yes | compliant |
| `ngspyce` | ngspice Python bindings | iic-osic-tools base | no | no | unused; no fork required |
| `nvc` | VHDL simulator | iic-osic-tools base | no | no | unused; no fork required |
| `openems` | Electromagnetic field solver | iic-osic-tools base | no | no | unused; no fork required |
| `openroad` | Digital place and route | iic-osic-tools base | yes `vibeic/OpenROAD` | yes | compliant |
| `openroad-librelane` | ? | iic-osic-tools base | yes `vibeic/OpenROAD` | no | forked, not currently used |
| `openvaf` | Verilog-A compiler | iic-osic-tools base | no | no | unused; no fork required |
| `osic-multitool` | IIC script collection | iic-osic-tools base | no | no | unused; no fork required |
| `padring` | IO pad-ring generator | iic-osic-tools base | no | no | unused; no fork required |
| `palace` | Finite-element electromagnetics | iic-osic-tools base | no | no | unused; no fork required |
| `pulp` | PULP platform RTL | iic-osic-tools base | ? (upstream unconfirmed) | no | upstream unconfirmed; NOT recorded as unforked |
| `pyopus` | Circuit optimisation | iic-osic-tools base | no | no | unused; no fork required |
| `qflow` | Complete digital flow | iic-osic-tools base | no | no | unused; no fork required |
| `qucs-s` | Circuit simulation (GUI) | iic-osic-tools base | no | no | unused; no fork required |
| `rftoolkit` | RF design toolkit | iic-osic-tools base | no | no | unused; no fork required |
| `riscv-gnu-toolchain` | RISC-V cross-compiler | iic-osic-tools base | no | no | unused; no fork required |
| `sak` | Swiss-army script collection | iic-osic-tools base | — | no | not a tool directory |
| `slang` | SystemVerilog front end | iic-osic-tools base | yes `vibeic/slang` | yes | compliant |
| `slang-yosys-plugin` | slang front end for yosys | iic-osic-tools base | yes `vibeic/sv-elab` + **25 duplicates** | yes | compliant |
| `spicebind` | SPICE bindings | iic-osic-tools base | no | no | unused; no fork required |
| `spike` | RISC-V ISA simulator | iic-osic-tools base | no | no | unused; no fork required |
| `surelog` | SystemVerilog parser | iic-osic-tools base | no | no | unused; no fork required |
| `surfer` | Waveform viewer (GUI) | iic-osic-tools base | no | no | unused; no fork required |
| `svck` | SystemVerilog lint | iic-osic-tools base | no | no | unused; no fork required |
| `uv` | Python package manager | iic-osic-tools base | no | no | unused; no fork required |
| `vacask` | Circuit simulator | iic-osic-tools base | no | no | unused; no fork required |
| `verible` | SystemVerilog lint / format | iic-osic-tools base | no | no | unused; no fork required |
| `verilator` | Compiled Verilog simulator | iic-osic-tools base | yes `vibeic/verilator` | yes | compliant |
| `verilator-vibeic` | Verilator, our build | added by us | yes `vibeic/verilator` | yes | compliant |
| `veryl` | Veryl hardware language | iic-osic-tools base | no | no | unused; no fork required |
| `vlsirtools` | VLSIR interchange format | iic-osic-tools base | n/a (pip-installed) | no | not a git clone; forking would not change what is installed |
| `xcircuit` | Schematic capture (GUI) | iic-osic-tools base | no | no | unused; no fork required |
| `xschem` | Schematic capture | iic-osic-tools base | yes `vibeic/xschem` | yes | compliant |
| `xyce` | Parallel SPICE | iic-osic-tools base | yes `vibeic/Xyce` | yes | compliant |
| `yices` | SMT solver (used by sby) | added by us | yes `vibeic/yices2` | yes | compliant |
| `yosys` | RTL synthesis | iic-osic-tools base | yes `vibeic/yosys` | yes | compliant |

### What changed in this column

| directory | was | now | why |
|---|---|---|---|
| `gtkwave` | no | yes | forked 2026-07-28 |
| `slang` | no | yes | forked 2026-07-28 (`MikePopoloski/slang`) |
| `slang-yosys-plugin` | no | yes | upstream renamed `povik/yosys-slang` -> `povik/sv-elab`; ours is of the new name |
| `xschem` | no | yes | forked 2026-07-28 |
| `xyce` | no | yes | forked 2026-07-28 |
| `yices` | no | yes | forked 2026-07-28 (`SRI-CSL/yices2`) |
| `klayout_gdsfactory8` | yes | n/a | **over-claim.** pip-installed gdsfactory, not a KLayout fork; the old table counted it as one |
| `klayout_gdsfactory9` | yes | n/a | same |

The first six were stale — forked after the table was built. The last two were
wrong when written.

### 25 forks of one repository

`slang-yosys-plugin` resolves to `vibeic/sv-elab` **plus `sv-elab-1` through
`sv-elab-24`: 25 forks of the same upstream.** All created 2026-07-28 between
22:19:58 and 22:25:12 — five minutes fourteen seconds — all 850 KB, all
identical. Thirty forks were created that day in total.

This is the most likely source of the `HTTP 403 "You cannot fork this repository
at this time"` that blocks the entries in `tools/PENDING_FORKS.json`. That file
records the reason as "not an API budget problem (4872/5000), not permissions
(five other forks succeeded in the same session)". Both statements are true, and
both only say what the cause is *not*.

Note the ordering, because it bounds the claim: Actions has been disabled at the
account level since 2026-07-20 (vibe-ic#550), which is **eight days before** the
fork burst. So the burst cannot be the cause of the account-level block; at most
it compounded an existing flag. Twenty-four of the forks are redundant and should
be deleted — deleting a repository is irreversible, so that is the owner's call.

### Counts

- 58 directories: 55 tools, 3 non-tool
- **19** tools used by the flow
- **18** rows forked (45 fork repos, **21** distinct upstreams)
- **used tools with no fork: 0**
- pip-installed, outside the fork rule: 4

## Table B — every tool IIC-OSIC-TOOLS ships

From the upstream source of truth, not a README: the union of
`_build/tool_metadata.yml` (55 entries) and `_build/images/*/` (52 directories),
minus 5 aggregate build targets (`base`, `base-dev`, `iic-osic-tools`,
`fpga-tools`, `pulp-tools`). **57 tools.** Neither source alone is complete — 10
tools have metadata and no image directory, 7 have a directory and no metadata.

| tool | function | used | forked | upstream | reason if unused |
|---|---|---|---|---|---|
| `bender` | Hardware dependency manager | no | no | `pulp-platform/bender.git` | Hardware dependency management. Our dependencies are declared in the L-layer JSON. |
| `covered` | Verilog coverage | no | no | `iic-jku/verilog-covered.git` | Verilog coverage. We use verilator coverage, which we do ship. |
| `cvc_rv` | C-to-RTL equivalence | no | no | `d-m-bailey/cvc.git` | C-to-RTL equivalence. We have no C reference model; equivalence runs through yosys equiv. |
| `gaw3-xschem` | Waveform viewer (GUI) | no | no | `StefanSchippers/xschem-gaw.git` | GUI. The fleet is headless; waveforms are read programmatically by cocotb/verilator. |
| `gds3d` | GDS 3D viewer (GUI) | no | no | `trilomix/GDS3D.git` | GUI viewer. GDS correctness is established by KLayout DRC plus geometric assertions, not by looking at it. |
| `ghdl` | VHDL simulator | no | no | `ghdl/ghdl.git` | The flow's input is Verilog/SystemVerilog; there are no VHDL sources. |
| `ghdl-yosys-plugin` | VHDL front end for yosys | no | no | `ghdl/ghdl-yosys-plugin.git` | Same reason: there is no VHDL to feed yosys. |
| `gtkwave` | Waveform viewer | yes | `vibeic/gtkwave` | `gtkwave/gtkwave.git` | — |
| `icestorm` | Lattice iCE40 FPGA bitstream | no | no | `YosysHQ/icestorm.git` | Lattice FPGA bitstream. Our FPGA path is Intel Quartus. |
| `irsim` | Switch-level simulation (GUI) | no | no | `rtimothyedwards/irsim.git` | GUI switch-level simulation. Timing is signed off by OpenSTA. |
| `iverilog` | Verilog simulator | yes | `vibeic/iverilog` | `steveicarus/iverilog.git` | — |
| `kactus2` | IP-XACT packaging (GUI) | no | no | `kactus2/kactus2dev.git` | IP-XACT GUI. Our IP contracts are the L-layer JSON. |
| `kepler-formal` | Formal property verification | no | no | `keplertech/kepler-formal.git` | An alternative formal engine. Step 5 uses SymbiYosys, which we have forked. |
| `klayout` | Layout / DRC engine | yes | `vibeic/klayout` | `KLayout/klayout.git` | — |
| `libman` | Liberty management | no | no | `IHP-GmbH/LibMan.git` | Liberty management. We read the PDK liberty directly. |
| `magic` | Layout editing + parasitic extraction | yes | `vibeic/magic` | `rtimothyedwards/magic.git` | — |
| `netgen` | LVS comparison | yes | `vibeic/netgen` | `rtimothyedwards/netgen.git` | — |
| `nextpnr` | FPGA place and route | no | no | `YosysHQ/nextpnr.git` | FPGA place and route, same reason as icestorm. |
| `ngspice` | SPICE simulation | yes | `vibeic/ngspice` | `danchitnis/ngspice-sf-mirror.git` | — |
| `ngspyce` | ngspice Python bindings | no | no | `ignamv/ngspyce.git` | Python bindings. We invoke ngspice directly. |
| `nvc` | VHDL simulator | no | no | `nickg/nvc.git` | VHDL simulator, same reason as ghdl and redundant with it. |
| `open_pdks` | PDK installer | yes | no | `RTimothyEdwards/open_pdks.git` | — |
| `openems` | Electromagnetic field solver | no | no | `thliebig/openEMS-Project.git` | Electromagnetic field solving, not needed by the digital flow. |
| `openroad` | Digital place and route | yes | `vibeic/OpenROAD` | `The-OpenROAD-Project/OpenROAD.git` | — |
| `openroad-librelane` | ? | no | `vibeic/OpenROAD` | `The-OpenROAD-Project/OpenROAD.git` | LibreLane flow wrapper. Our Phase 3 drives OpenROAD directly. |
| `openvaf` | Verilog-A compiler | no | no | `arpadbuermen/OpenVAF.git` | Verilog-A compilation. The PDKs already provide compiled device models. |
| `osic-multitool` | IIC script collection | no | no | `iic-jku/osic-multitool.git` | IIC script collection, overlapping what our programs/ already does. |
| `padring` | IO pad-ring generator | no | no | `iic-jku/padring.git` | IO pad ring. Current designs have no pad frame; they are core-only. |
| `palace` | Finite-element electromagnetics | no | no | `awslabs/palace.git` | Finite-element electromagnetics, outside the digital flow. |
| `pyopus` | Circuit optimisation | no | no | `https://fides.fe.uni-lj.si/pyopus/download` | Circuit optimisation. Analog sizing is handled by the A2-A4 corner sweep. |
| `qflow` | Complete digital flow | no | no | `RTimothyEdwards/qflow.git` | A complete alternative flow. We build on OpenROAD, and running both would create two sources of truth. |
| `qucs-s` | Circuit simulation (GUI) | no | no | `ra3xdh/qucs_s.git` | GUI circuit simulation. Batch runs go through ngspice/Xyce, both of which we use. |
| `rftoolkit` | RF design toolkit | no | no | `ediloren/FastHenry2.git` | RF design, outside the flow's scope. |
| `riscv-gnu-toolchain` | RISC-V cross-compiler | no | no | `riscv-collab/riscv-gnu-toolchain.git` | C cross-compilation. The flow compiles no software. |
| `riscv-pk` | RISC-V proxy kernel | no | no | `riscv-software-src/riscv-pk.git` | RISC-V proxy kernel, same reason. |
| `slang` | SystemVerilog front end | yes | `vibeic/slang` | `MikePopoloski/slang.git` | — |
| `slang-yosys-plugin` | slang front end for yosys | yes | `vibeic/sv-elab` + 25  | `povik/yosys-slang.git` | — |
| `spicebind` | SPICE bindings | no | no | `themperek/spicebind.git` | SPICE bindings. We call the simulator directly. |
| `spike` | RISC-V ISA simulator | no | no | `riscv-software-src/riscv-isa-sim.git` | RISC-V ISA simulator. The flow runs no software. |
| `surelog` | SystemVerilog parser | no | no | `chipsalliance/Surelog.git` | SystemVerilog parser, already covered by yosys's slang front end. |
| `surfer` | Waveform viewer (GUI) | no | no | `https://gitlab.com/surfer-project/surfer.git` | GUI waveforms, same reason as gaw3. |
| `sv2v` | SystemVerilog to Verilog | no | no | `zachjs/sv2v.git` | SystemVerilog to Verilog conversion. yosys reads SystemVerilog directly. |
| `svck` | SystemVerilog lint | no | no | `AsFigo/svck.git` | SystemVerilog lint. Ours runs through our own gates, because the criteria have to be measurable by the 63x8 matrix. |
| `trilinos` | Numerical solver library | no | no | `trilinos/Trilinos.git` | A numerical library (a Xyce dependency), not a standalone tool. |
| `uv` | Python package manager | no | no | `astral-sh/uv.git` | Python package manager. The image ships its dependencies pre-installed. |
| `vacask` | Circuit simulator | no | no | `https://codeberg.org/arpadbuermen/VACASK.git` | Another circuit simulator, already covered by ngspice and Xyce. |
| `verible` | SystemVerilog lint / format | no | no | `chipsalliance/verible.git` | SystemVerilog lint/format, same reason as svck. |
| `verilator` | Compiled Verilog simulator | yes | `vibeic/verilator` | `verilator/verilator.git` | — |
| `veryl` | Veryl hardware language | no | no | (not declared as a repo in the Dockerfile) | Veryl language. The flow's input is not Veryl. |
| `xcircuit` | Schematic capture (GUI) | no | no | `RTimothyEdwards/XCircuit.git` | GUI schematics. The analog path uses xschem, which we do use. |
| `xschem` | Schematic capture | yes | `vibeic/xschem` | `StefanSchippers/xschem.git` | — |
| `xyce` | Parallel SPICE | yes | `vibeic/Xyce` | `Xyce/Xyce.git` | — |
| `yices2` | SMT solver | yes | `vibeic/yices2` | `SRI-CSL/yices2.git` | — |
| `yosys` | RTL synthesis | yes | `vibeic/yosys` | `YosysHQ/yosys.git` | — |
| `yosys_eqy` | Equivalence checking (EQY) | no | no | `YosysHQ/eqy.git` | Equivalence checking. We use yosys equiv, which we do ship. |
| `yosys_mcy` | Mutation coverage (MCY) | no | no | `YosysHQ/mcy.git` | Mutation coverage. Our mutation testing is the in-house gate_cli_mutation_probe. |
| `yosys_sby` | Formal verification (SymbiYosys) | yes | `vibeic/sby` | `YosysHQ/sby.git` | — |

### Counts

- **57** upstream tools
- we use **16**
- we fork **16**
- **used but not forked: 1 — `open_pdks`**

`open_pdks` produces `/foss/pdks/sky130A` and `gf180mcuD` — verified in the image
by its output structure (`libs.ref`, `libs.tech`, `SOURCES`) — and every DRC and
LVS run reads its `libs.tech`. `vibeic/open_pdks` returns 404.

**Table A cannot see this**, because Table A is `ls /foss/tools` and PDKs live in
`/foss/pdks`. Both tables are built per *tool*, so PDK data is outside the frame
of each. That is what Table C is for.

Forked but not currently used: `openroad-librelane`. Not waste — the rule says
fork what you use, not use what you fork, and the cost of forking early is paid
once, whereas forking on the day it is needed meets the same 403 as now.

### What Table B cannot see

This compares what upstream *declares* against whether we use it. It is not an
audit of the image filesystem: a tool listed in the metadata but never installed
looks identical here to one that shipped. Table A is the one measured from the
image.

## Table C — PDK data

Tables A and B are both per-tool, so **PDK data is structurally outside both** —
while the rule explicitly covers it. A rule that no audit can see is a rule that
is not being audited.

Measured: `ls /foss/pdks` in `ghcr.io/vibeic/vibeic-eda:0.2.31`.

| PDK directory | contents | upstream | forked | used |
|---|---|---|---|---|
| `sky130A` | SkyWater 130nm, open sign-off | `RTimothyEdwards/open_pdks` | **no** | yes — spm cell 26/6/3 |
| `gf180mcuD` | GlobalFoundries 180nm MCU | `RTimothyEdwards/open_pdks` | **no** | yes — spm cell |
| `ihp-sg13g2` | IHP SG13G2 130nm BiCMOS | `IHP-GmbH/IHP-Open-PDK` | **no** | yes — spm cell 28/4/2 |
| `ihp-sg13cmos5l` | IHP SG13 CMOS 5V | `IHP-GmbH/IHP-Open-PDK` | **no** | same repo |
| `asap7` | ASAP7 7nm predictive PDK | `The-OpenROAD-Project/asap7_pdk_r1p7` | **no** | yes — PnR |
| `nangate45` | Nangate 45nm open cell library | `The-OpenROAD-Project/OpenROAD-flow-scripts` | **no** | yes — PnR |
| `ciel` | PDK version manager | `fossi-foundation/ciel` | **no** | installer |

**7 directories, 5 distinct upstreams, 0 forked.** Adding KLayout's ASAP7 layer
map (`laurentc2/ASAP7_for_KLayout`, also unforked) makes **6 distinct upstreams
missing on the PDK side**.

Every retry hits the same 403. Most recent attempt, at the time of writing:
`open_pdks`, `IHP-Open-PDK` and `ciel` all returned it.

### What Table C cannot see

It matches a PDK directory to an upstream repository. It does **not** establish
that the data in the image came from a particular commit of that repository:
PDKs have no pin like `tools/*/Dockerfile` and no `/vibeic/provenance/*.json`.
"Which sky130A is this" is currently unanswerable beyond "open_pdks produced it"
— the same problem the per-tool split just fixed for tools, still open for PDKs.
