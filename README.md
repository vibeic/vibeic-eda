# vibeic-eda

**Forked + bug-fixed open-source EDA toolchain, shipped as one Docker image.**

`vibeic-eda` is the [hpretl/iic-osic-tools](https://github.com/iic-jku/iic-osic-tools)
base (all the open-source EDA tools + the sky130 / gf180mcu / ihp PDKs) with our
**patched `vibeic/*` tool forks** layered in to close the capability gaps where stock
open-source EDA falls short of commercial tools. Every fix ships with a reproducible
**FAIL → PASS proof** (see [`FIX_STATUS.md`](./FIX_STATUS.md)).

You do **not** need to fork or build the individual tools yourself — pull one image and
you have the whole fixed toolchain:

```bash
docker pull vibeic/vibeic-eda:0.2.5
```

(also on GHCR: `docker pull ghcr.io/vibeic/vibeic-eda:0.2.5`)

---

## Why forked, not just wrapped

Most "AI EDA" stacks *call* open-source tools and inherit their bugs. We instead **fork
the tools and fix them** where they silently produce wrong results or crash — then prove
each fix reproducibly against the stock binary. A few of the load-bearing ones:

| Tool | What stock does wrong | vibeic fix (proven) |
|---|---|---|
| **OpenROAD** | post-detailed-route `repair_design` **segfaults** on real parasitics (Signal-11) | routes buffering through the Steiner builder → runs to completion, max-slew violators **289 → 0**, exit 0 |
| **netgen** | a transistor property mismatch is reported as `Circuits match uniquely` — a **silent LVS false-pass** | `Final result:` reflects property errors → correctly `do NOT match uniquely` |
| **yosys** | tri-state fanin dropped in `synth`; gate-level ripple adders never lifted to parallel-prefix | `tribuf` preserved (`$_TBUF_`); `lift_adder` restructures ripple → Kogge-Stone, depth 128 → 73, CEC-proven |
| **ngspice** | a failed `.measure` in `-b` batch exits **rc = 0** — CI reads it as a pass | per-`.measure` PASS/FAIL marker + nonzero rc on failure |
| **magic** | `def read` silently drops an unmapped-layer route (metal open) | retains geometry on unknown layers + warns |

Full scoreboard (8 forks, gatekeeper-verified proofs): [`FIX_STATUS.md`](./FIX_STATUS.md).

---

## Quick start

**Headless / batch (CI, scripted flows):**
```bash
docker run -d --name vibeic-eda vibeic/vibeic-eda:0.2.5 --skip sleep infinity
docker exec vibeic-eda yosys --version
docker exec vibeic-eda openroad -version
```

**Interactive desktop (VNC / noVNC in the browser):**
```bash
docker run -d --name vibeic-eda \
  -p 5901:5901 -p 8080:80 \
  vibeic/vibeic-eda:0.2.5
# noVNC:  http://localhost:8080     VNC: localhost:5901   (default password: abc123)
```

**Mount your design directory:**
```bash
docker run -it --rm -v "$PWD:/foss/designs/work" -w /foss/designs/work \
  vibeic/vibeic-eda:0.2.5 bash
```

Tools live at the same paths as the iic-osic-tools base (`/foss/tools/bin/...`), so any
flow written for iic-osic-tools runs unchanged — it just gets the fixed binaries.

---

## What's inside

**Forked + patched (8 tools)** — each pinned to a commit SHA in the [Dockerfile](./Dockerfile):

| Tool | Fork branch | Focus |
|---|---|---|
| OpenROAD | `vibeic/post-route-detailed-routing-repair` | post-route repair on real parasitics + incremental reroute |
| yosys + abc | `vibeic/synth-fixes` | tri-state preserve, slang SV frontend, D-latch mapping, prefix-adder lift |
| ngspice | `vibeic/batch-honesty` | batch rc honesty, `.param` expansion, native Monte-Carlo (LHS), DC homotopy |
| magic | `vibeic/lvs-fidelity` | `ext2spice` label→port, unknown-layer/via retain, SPECIALNET power names |
| netgen | `vibeic/lvs-fidelity` | property-error verdict, portless guard, `-auto-global`, `-nopower`, black-box match |
| iverilog | `vibeic/sv-tb-coverage` | nonblocking-event codegen segfault fix, package ordering |
| klayout | `vibeic/streamout-fixes` | MANUFACTURINGGRID snap, merge-abutting, foundry layer-map (parallel streamout build) |
| verilator | `vibeic/` (base tree) | forked; no custom patch warranted on the shipped version |

**Inherited from the iic-osic-tools base** — OpenSTA, xschem, Xyce, cocotb, SymbiYosys,
Fault, GHDL, and the PDKs: `sky130A`, `gf180mcuD`, `ihp-sg13g2`, `ihp-sg13cmos5l`, `ciel`.

---

## Build from source (reproducible)

The image is built entirely from source, with every fork pinned to a commit SHA, so a
rebuild is byte-for-byte reproducible:

```bash
git clone https://github.com/vibeic/vibeic-eda.git
cd vibeic-eda
docker build -t vibeic-eda:local .
```

Each tool is compiled in a native ubuntu24.04 builder so the binary matches the
iic-osic-tools runtime (python3.12 / glibc2.39). Override any fork ref with
`--build-arg YOSYS_REF=<sha>` etc. **Note:** a full from-source build produces a ~26 GB
image and takes 1–2 h — use a machine with adequate disk (≥ 60 GB free) and cores.

---

## Versioning

Semantic versions track the fix-program milestones in `FIX_STATUS.md`:

- `vibeic/vibeic-eda:X.Y.Z` — immutable, reproducible from the pinned SHAs at that tag.
- `vibeic/vibeic-eda:latest` — the newest released `X.Y.Z`.

Current: **0.2.5** (yosys `lift_adder`, prefix-adder recipe, OpenROAD incremental reroute
DRT-0073/0155/0218, ngspice native MC-LHS, magic/netgen LVS-fidelity).

---

## License

The build recipe, scripts, and docs in this repository are licensed under
[Apache-2.0](./LICENSE).

The image **aggregates** upstream open-source EDA tools, each under its own license
(OpenROAD BSD-3, yosys ISC, ngspice BSD, magic/netgen public-domain-style, klayout GPL-3,
iverilog GPL-2, verilator LGPL-3/Apache-2.0, and the iic-osic-tools base + PDKs). Our
modifications live in the public `vibeic/<tool>` forks under each tool's own license. See
[`THIRD_PARTY_LICENSES.md`](./THIRD_PARTY_LICENSES.md) for the full attribution and links
to each fork's source.

---

## Links

- Fix scoreboard + proofs: [`FIX_STATUS.md`](./FIX_STATUS.md)
- The AI-native IC-design platform this powers: [vibeic.ai](https://vibeic.ai)
- Upstream base: [iic-osic-tools](https://github.com/iic-jku/iic-osic-tools) (TU Wien / JKU)
