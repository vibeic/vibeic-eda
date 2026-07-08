# Third-party licenses & attribution

`vibeic-eda` is a Docker image that **aggregates** open-source EDA tools. The build recipe
and scripts in this repository are Apache-2.0 (see [`LICENSE`](./LICENSE)); the tools inside
the image remain under their own upstream licenses. Our modifications are published as public
`vibeic/<tool>` forks — each under the **same license as its upstream** — so every change is
inspectable and redistributable.

## Runtime base

| Component | Upstream | License |
|---|---|---|
| iic-osic-tools base image + PDKs | https://github.com/iic-jku/iic-osic-tools | Apache-2.0 (image) / per-PDK (sky130 Apache-2.0, gf180mcu Apache-2.0, IHP SG13G2 open-PDK) |

## Forked & patched tools

Each fork below is pinned to a commit SHA in the [`Dockerfile`](./Dockerfile). "License" is the
upstream license, unchanged by the fork.

| Tool | Our fork (source of modifications) | Upstream | Upstream license |
|---|---|---|---|
| OpenROAD | https://github.com/vibeic/OpenROAD | https://github.com/The-OpenROAD-Project/OpenROAD | BSD-3-Clause |
| yosys (+ abc) | https://github.com/vibeic/yosys | https://github.com/YosysHQ/yosys | ISC (abc: MIT-style) |
| ngspice | https://github.com/vibeic/ngspice | https://ngspice.sourceforge.io/ | BSD-3-Clause / "New BSD" |
| magic | https://github.com/vibeic/magic | https://github.com/RTimothyEdwards/magic | public-domain-style (MIT-compatible) |
| netgen | https://github.com/vibeic/netgen | https://github.com/RTimothyEdwards/netgen | public-domain-style (MIT-compatible) |
| iverilog | https://github.com/vibeic/iverilog | https://github.com/steveicarus/iverilog | GPL-2.0-or-later |
| klayout | https://github.com/vibeic/klayout | https://github.com/KLayout/klayout | GPL-3.0-or-later |
| verilator | https://github.com/vibeic/verilator | https://github.com/verilator/verilator | LGPL-3.0 / Apache-2.0 |

## Inherited tools (from the base, unmodified)

OpenSTA (GPL-3.0), xschem (GPL-3.0), Xyce (GPL-3.0), cocotb (BSD-3), SymbiYosys/SBY (ISC),
Fault (Apache-2.0), GHDL (GPL-2.0) — all under their respective upstream licenses.

## Notes on redistribution

- GPL/LGPL tools (iverilog, klayout, OpenSTA, xschem, Xyce, GHDL, verilator) are included as
  **separate, unmodified-or-forked programs** invoked at runtime; corresponding source is
  available at each `vibeic/<tool>` fork (for the modified ones) or upstream (for the rest).
- No upstream tool is statically linked into another under an incompatible license; each is an
  independent executable in the image.
- If you redistribute this image, retain this file and the per-tool license texts shipped
  inside the image under `/foss/tools/*/`.
