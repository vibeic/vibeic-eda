# vibeic-eda

**Forked + bug-fixed open-source EDA toolchain, shipped as one Docker image.**

`vibeic-eda` is the [hpretl/iic-osic-tools](https://github.com/iic-jku/iic-osic-tools)
base (all the open-source EDA tools + the sky130 / gf180mcu / ihp PDKs) with our
**patched `vibeic/*` tool forks** layered in to close the capability gaps where stock
open-source EDA falls short of commercial tools. Every fork that ships is pinned to a
commit SHA in the [Dockerfile](./Dockerfile), and every `DONE` fix carries a reproducible
**FAIL → PASS proof** that was re-run before integration (see
[`FIX_STATUS.md`](./FIX_STATUS.md), which also marks the rows closed by *adopting* a newer
upstream, the deferred ones, and the one that turned out non-reproducible).

This is the toolchain the **Vibe-IC plugin** runs on — the MCP `eda_*` tools drive these
binaries by `docker exec` into a container built from this image.

You do **not** need to fork or build the individual tools yourself — pull one image and
you have the whole fixed toolchain. The image is published to the **GitHub Container
Registry (GHCR)** and is public (no login required):

```bash
docker pull ghcr.io/vibeic/vibeic-eda:0.2.27
```

> The image lives on GHCR (`ghcr.io/vibeic/...`), **not** Docker Hub — always use the
> full `ghcr.io/` prefix. A bare `docker pull vibeic/vibeic-eda` resolves to Docker Hub,
> which does not host this image and returns "repository does not exist / access denied".
> The newest released tag is `ghcr.io/vibeic/vibeic-eda:latest`.

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
| **klayout** | sign-off DRC on a commercial foundry SVRF `.rule` deck needs a **commercial license** | native `svrfdrc` C++ buddy runs the deck directly on KLayout's DRC engine — byte-identical to the reference on a real 87k-line, 4,533-rule foundry deck, license-free |

Full scoreboard with per-fix proofs: [`FIX_STATUS.md`](./FIX_STATUS.md).

---

## The forks it carries

The `vibeic` org currently holds **15 forked tool repos**. **13 of them ship in this
image**; the two ALIGN repos are forked but **not yet shipped in any image** (see
[below](#forked-but-not-yet-shipped-in-the-image)).

Of the 13 that ship: **12 are pinned as Dockerfile `ARG`s**, plus **one more pinned as a
git submodule** (OpenSTA — see the note below).

| Tool | What our fork adds | Branch |
|---|---|---|
| **OpenROAD** | post-route repair on real parasitics; advanced-node `LEF58_MINSTEP MAXEDGES` DRC, per-net-weight IO placement, PDN strap/decap sizing inverses, timing/fill/CTS/placement-leak fixes | `vibeic/openroad-integration` |
| **OpenSTA** ‡ | signoff-SI + timing-ECO kernels | `vibeic/sta-timing-eco` |
| **yosys + abc** | tri-state preserve, slang SV frontend, D-latch liberty mapping, `lift_adder` prefix-adder restructuring, ICG mapping | `vibeic/synth-fixes-integration` |
| **klayout** | streamout grid-snap + merge-abutting + foundry layer-map; native in-KLayout SVRF/Calibre DRC engine + the `svrfdrc` C++ buddy; SHRINK/GROW and DENSITY engine fixes; `tl::Thread` join fix (below) | `vibeic/klayout-signoff-int` |
| **ngspice** | batch rc honesty, `.param` expansion, native Monte-Carlo (LHS), DC homotopy, hardened DSPF, process-parallel AC; built `--enable-openmp` | `vibeic/batch-honesty-integration` |
| **magic** | `ext2spice` label→port, unknown-layer/via retain, SPECIALNET power names, foundry layer-map, grid snap, SPEF, NDR, tech-from-LEF | `vibeic/integration` |
| **netgen** | property-error verdict, portless guard, `-auto-global`, `-nopower`, black-box match, blackbox-zero-pin guard | `vibeic/connectivity-match` |
| **iverilog** | nonblocking-event codegen segfault fix, package ordering | `vibeic/sv-tb-coverage` |
| **verilator** | constrained-randomization fixes — power-of-2-base `Pow` lowering, `$countbits` with a runtime 1-bit control | `vibeic/sv-tb-coverage` |
| **cocotb** | parallel regression dispatch | `vibeic/parallel-regression-dispatch` |
| **cocotb-coverage** | CRV scalability, bin ranking, bins-closure | `vibeic/integration` |
| **pyuvm** | RAL accessors, TLM comparators, sequencer arbitration | `vibeic/integration` |
| **sby** (SymbiYosys) | consolidated formal fixes + package layout; version-drift fixes at root | `vibeic/integration` |

‡ **OpenSTA is the special case.** It is **not** a Dockerfile `ARG`. It is pinned as
OpenROAD's **`src/sta` git submodule**: the integration branch's `.gitmodules` was
repointed from the upstream relative URL to `https://github.com/vibeic/OpenSTA.git`, so
`git submodule update --init --recursive` in the OpenROAD build stage checks out our
superset commit on `vibeic/sta-timing-eco`. That is why no `ARG` mentions OpenSTA at all.
**Regen invariant:** any OpenROAD ref whose `src/sta` points at a vibeic commit *must*
keep `.gitmodules` on `vibeic/OpenSTA`, and that commit *must* be pushed there, or the
build fails with `upload-pack: not our ref`.

**Don't read the `ARG` count as a fork count.** `grep -c '^ARG .*_REF=' Dockerfile`
returns **16**: the 12 tool forks above, plus four refs that are *not* forks —

- `ORFS_REF` (`v3.0`) — an upstream
  [OpenROAD-flow-scripts](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts)
  tag, cloned sparsely only to stage two open PDK platforms (below).
- `ASAP7SC_REF` / `ASAP7PDK_REF` / `ASAP7KL_REF` — upstream ASAP7 *data* repos
  (`asap7sc7p5t_28`, `asap7_pdk_r1p7`, `laurentc2/ASAP7_for_KLayout`) staged for the
  ASAP7 device-LVS source-of-truth. These three track `main`, not a SHA — see
  [Build from source](#build-from-source) on what that means for reproducibility.

### Forked but not yet shipped in the image

Two further `vibeic` forks exist for the analog auto-layout track. **Neither is built into
any published image** — the newest tag (`0.2.26`) contains no ALIGN, and the Docker stage
for it is planned, not built.

| Fork | Upstream | State |
|---|---|---|
| `vibeic/ALIGN-public` | `ALIGN-analoglayout/ALIGN-public` | clean fork, **0 commits ahead** of upstream |
| `vibeic/ALIGN-pdk-sky130` | `ALIGN-analoglayout/ALIGN-pdk-sky130` | **1 commit ahead** (`db6d7f1a`): the sky130 MOS generator now honours the netlist channel length `L` instead of drawing every gate at the fixed 150 nm poly width |

Treat the analog auto-layout capability as **spike-proven, image-integration pending** —
the plan, the spike evidence, and the open blockers are in
[`ANALOG_LAYOUT_ROADMAP.md`](./ANALOG_LAYOUT_ROADMAP.md) and the Bucket-T row of
[`FIX_STATUS.md`](./FIX_STATUS.md). Until a tag ships it, the MCP `eda_analog_layout`
capability gap is still open.

---

## PDKs staged in the image

`/foss/pdks` contains:

| PDK | Source | Status |
|---|---|---|
| `sky130A` | iic-osic-tools base | real foundry enablement |
| `gf180mcuD` | iic-osic-tools base | real foundry enablement |
| `ihp-sg13g2`, `ihp-sg13cmos5l` | iic-osic-tools base | real foundry enablement |
| `ciel` | iic-osic-tools base | PDK manager |
| `nangate45` | staged from the ORFS `v3.0` platform tree | **generic / non-foundry** |
| `asap7` | staged from the ORFS `v3.0` platform tree | **predictive / non-foundry** |

**Be honest about the last two.** NanGate45 (FreePDK45 Open Cell Library, Si2,
Apache-2.0) and ASAP7 (ASU/ARM 7nm FinFET predictive, BSD-3-Clause) are
**research-and-education enablements, not manufacturable processes**:

- Synthesis / PnR / CTS / STA / area all run against them, which makes them useful for
  reproducible flow development and PPA comparison.
- Their KLayout DRC decks (`FreePDK45.lydrc`, `asap7.lydrc`) give an **educational** DRC
  — they are **not sign-off decks**. NanGate45 additionally ships **no LVS deck**.
- Neither corresponds to a real foundry process. Both are registered in the plugin's
  `pdk_registry.json` with `tapeout_capable = false`. **Never claim silicon sign-off or
  tapeout qualification on nangate45 or asap7.**

ASAP7 is staged as the RVT (`R`) VT flavor at the typical (TT) corner — 5 Liberty
functional groups (AO / INVBUF / OA / SIMPLE / SEQ), tech LEF, cell LEF, per-VT GDS.

Two later additions extend what ASAP7 can actually close (both from public BSD sources):

- **OpenRCX extraction model (0.2.24).** The ORFS `rcx_patterns.rules` is staged as
  `libs.tech/librelane/rules.openrcx.asap7.nom`, so post-route SPEF extracts against a
  real per-layer RC model instead of falling back to tech-LEF estimates. ASAP7 ships
  **one (typical) corner only** → single-corner `.nom` SPEF; min/max are absent, not
  silently substituted.
- **Device-LVS source-of-truth (0.2.25).** Golden CDL (`asap7sc7p5t_28_{L,R,SL,SRAM}`),
  the BSIM-CMG level-72 FinFET models (`7nm_{TT,SS,FF}_160803.pm`), and a KLayout layer
  stack are staged under `libs.tech/{cdl,hspice,klayout/lvs}`. Measured on the `R`
  library: **159/208 (76%) device-level MATCH**, with a proven-negative control (a
  one-net corruption does report MISMATCH, so it is not a false-clean).

Neither changes the sign-off status above: ASAP7 still has **no foundry DRC sign-off
deck**, and 76% is a disclosed partial, not a clean LVS.

---

## Notable engine fix — klayout `tl::Thread` use-after-free (shipped in 0.2.23)

`svrfdrc` was intermittently aborting with `malloc(): unaligned tcache chunk` (rc 139/134,
no DRC report emitted, spurious phase-3 FAIL). The root cause was **not** in the SVRF
engine — it was in KLayout's own threading primitive:

> `tl::Thread::wait()` early-returned on the `running` flag **without calling
> `pthread_join`**. A worker's closure and data were therefore freed while its OS thread
> was still unwinding — a use-after-free that intermittently corrupted the heap. The
> `--threads` measurement-rule path hit it because it churns thousands of short-lived
> workers.

Fix: `wait()` now always joins exactly once (guarded), and `running` is atomic.

**Evidence:** ThreadSanitizer **6 data races → 0**; **250+ oversubscribed
`--threads=32` stress runs, 0 crashes**; DRC report **byte-identical across thread counts**
(1 == 8 == 32). Confirmed on a re-run of the benchmark case that surfaced it: DRC produced
its report on the first try, the caller's defense-in-depth retry never fired, and the only
changed engine artifact between images is `libklayout_tl.so`.

This is a fix in `tlThreads.cc` — it repairs **the whole klayout `--threads` path**, not
just `svrfdrc`. Every number above is from the fork commit itself:
[`bc4e211b`](https://github.com/vibeic/klayout/commit/bc4e211b5e37d9ae11b57286cff3662cc5a4ab40)
on `vibeic/klayout-signoff-int`, which is the `KLAYOUT_REF` pinned in the Dockerfile.

---

## Quick start

**Headless / batch (CI, scripted flows):**
```bash
docker rm -f vibeic-eda 2>/dev/null || true   # "name already in use"? drop the old container first
docker run -d --name vibeic-eda ghcr.io/vibeic/vibeic-eda:0.2.27 --skip sleep infinity
docker exec vibeic-eda yosys --version
docker exec vibeic-eda openroad -version
```
Every tool resolves on a non-login `docker exec` PATH — the image bakes `/foss/tools/bin`
(and the other tool dirs) into a global `ENV PATH`, so no login shell and no per-command
`export PATH` is needed.

**Using it with the Vibe-IC plugin (identity bind-mount required).** Flows that write
into the container from the host — the plugin's phase-3 place-&-route step does an in-container
`cd <host_project_path>` — need the project tree mounted at the **same path** inside the
container, or you get `cd: No such file or directory`. Start it with an identity mount:
```bash
docker run -d --name vibeic-eda \
  -v "$PWD:$PWD" -w "$PWD" \
  ghcr.io/vibeic/vibeic-eda:0.2.27 --skip sleep infinity
# then point the MCP at it:  EDA_CONTAINER=vibeic-eda
```

**Interactive desktop (VNC / noVNC in the browser):**
```bash
docker run -d --name vibeic-eda \
  -p 5901:5901 -p 8080:80 \
  ghcr.io/vibeic/vibeic-eda:0.2.27
# noVNC:  http://localhost:8080     VNC: localhost:5901   (default password: abc123)
```

**Mount your design directory:**
```bash
docker run -it --rm -v "$PWD:/foss/designs/work" -w /foss/designs/work \
  ghcr.io/vibeic/vibeic-eda:0.2.27 bash
```

Tools live at the same paths as the iic-osic-tools base (`/foss/tools/bin/...`), so any
flow written for iic-osic-tools runs unchanged — it just gets the fixed binaries.

**Upgrade a running container to a new image version:** a container is pinned to the
image ID its tag resolved to at creation, so pulling a newer image does NOT update it —
the container must be recreated. [`restart-eda.sh`](./restart-eda.sh) does that safely:
it clones the existing container's mounts / cmd / user / workdir onto the new image,
refuses to interrupt an in-flight EDA job (override with `FORCE=1`), and verifies the
image ID after the swap. Run it as your normal user (not root/sudo).
```bash
./restart-eda.sh              # recreate on the PINNED version from ./VERSION
./restart-eda.sh 0.2.11       # bare tag -> $IMAGE_REPO:0.2.11
./restart-eda.sh ghcr.io/vibeic/vibeic-eda:latest       # full ref honored as-is
IMAGE_REPO=ghcr.io/vibeic/vibeic-eda ./restart-eda.sh   # resolve bare tags against GHCR
```
A bare tag is prefixed with `IMAGE_REPO`, which defaults to the local build tag
`vibeic/vibeic-eda` — set `IMAGE_REPO=ghcr.io/vibeic/vibeic-eda` to resolve against the
published registry instead. The no-argument default is deliberately the pinned `VERSION`,
never a floating `latest` — a stale local `latest` would silently hand you an outdated
toolchain.

---

## How fork refs get updated

[`fork-gatekeeper/`](./fork-gatekeeper) is the CI/maintenance tooling that keeps the
`vibeic` forks in sync with their upstreams and rebuilds this image when a fork advances:

1. **Discover** — `discover_forks.py` enumerates the vibeic org's forks and records each
   one's upstream parent into [`FORKS.json`](./fork-gatekeeper/FORKS.json)
   (e.g. `OpenROAD → The-OpenROAD-Project/OpenROAD`, `klayout → KLayout/klayout`).
   The checked-in registry lists the **12 `ARG`-pinned tools** — it is deliberately
   narrower than the org's 15 forks: OpenSTA rides in as OpenROAD's submodule rather
   than as its own tracked ref, and the two ALIGN forks are not image-integrated yet,
   so neither is on the rebuild-on-upstream-release path.
2. **Track & gate** — `gatekeeper.py` / `run_tick.sh` check each upstream for a new
   release; for a candidate they rebase the vibeic fork branch onto the new upstream, bump
   the corresponding `Dockerfile` ARG, docker-build the image, and smoke-regress it
   (`build_and_regress.sh`, `verify_yosys.sh`).
3. **Publish** — `build_page.py` renders the fork status page for the site.

`GK_MODE=verify` (the default) proves the rebuild without touching production;
`GK_MODE=promote` fast-forwards the fork branch and pushes the new image on green. It runs
on the build host via cron; runtime output (`reports/`, `ledger/`,
`last_build_result.json`) is host-local and git-ignored. See
[`fork-gatekeeper/README.md`](./fork-gatekeeper/README.md) for the env knobs.

Before **any** fork push or image publish, refs are scanned for NDA-protected content
(commit messages, source comments, directory names, `.gitignore` headers). Commercial
foundry material is always referred to generically — "a commercial PDK", "a foundry
sign-off deck" — never by process name, SKU, or rule id.

---

## Build from source

The image is built entirely from source, and **all 12 tool forks are pinned to a commit
SHA**, so the *tool* half of a rebuild is reproducible:

```bash
git clone https://github.com/vibeic/vibeic-eda.git
cd vibeic-eda
DOCKER_BUILDKIT=1 docker build --network=host -t vibeic-eda:local .
```

Each tool is compiled in a native ubuntu24.04 builder so the binary matches the
iic-osic-tools runtime (python3.12 / glibc2.39). Override any fork ref with
`--build-arg YOSYS_REF=<sha>` etc. `--network=host` avoids the transient-DNS
submodule-fetch failures seen on some hosts.

**What is *not* SHA-pinned** — be aware before treating a rebuild as bit-reproducible:
the runtime base (`hpretl/iic-osic-tools:latest`) and the OpenROAD builder base
(`openroad/ubuntu24.04-dev:latest`) are `:latest`; `ORFS_REF` is a tag (`v3.0`); and the
three ASAP7 asset refs default to `main`. Pin them with `--build-arg` if you need an
exactly-repeatable rebuild.

**Resources:** a full from-source build takes **1–2 h** (the 0.2.26 release run on the
self-hosted `vibeic-builder` runner ran 1 h 39 m) and needs **≥ 60 GB free disk** — the
GitHub-hosted runners' 14 GB cannot do it. The resulting image is **~27 GB** on disk
(26.9 GB measured at 0.2.22 — see
[`IMAGE_0.2.22_DELIVERY.md`](./IMAGE_0.2.22_DELIVERY.md); not re-measured since). For
reference, the published `0.2.26` manifest on GHCR is 5.99 GB compressed across 80 layers.

---

## Versioning

Semantic versions track the fix-program milestones in `FIX_STATUS.md`:

- `ghcr.io/vibeic/vibeic-eda:X.Y.Z` — immutable; the tool forks it was built from are the
  SHAs pinned at that tag (see [Build from source](#build-from-source) for what else moves).
- `ghcr.io/vibeic/vibeic-eda:latest` — the newest released `X.Y.Z`; it currently resolves
  to the same manifest digest as `0.2.26`.

Current: **0.2.27** — the canonical from-source rebuild that folds the ASAP7 work of the
two preceding tags into the multi-stage build. 0.2.24 and 0.2.25 were verified-correct
*thin-layer overlays* (the sandbox build host had no DNS and a cold tool cache); 0.2.26
rebuilds the identical public/BSD asset staging through `release.yml` on the
`vibeic-builder` runner. No fork ref changed relative to 0.2.25.

The tags it consolidates:

| Tag | What it added |
|---|---|
| `0.2.22` | the **12-fork consolidation** — every fork onto a single integration branch, `vibeic/OpenSTA` published for the first time and wired in as OpenROAD's `src/sta` submodule, the ASAP7 PDK staged, ngspice `--enable-openmp` |
| `0.2.23` | klayout `tl::Thread` `pthread_join` fix (above) — re-pins `KLAYOUT_REF`, recompiles `tlThreads.cc`, relinks `libklayout_tl.so` |
| `0.2.24` | ASAP7 OpenRCX extraction model |
| `0.2.25` | ASAP7 device-LVS source-of-truth (golden CDL + BSIM-CMG models + KLayout stack) |
| `0.2.26` | canonical from-source rebuild carrying both |

See [`IMAGE_0.2.22_DELIVERY.md`](./IMAGE_0.2.22_DELIVERY.md) for the full per-fork
manifest and the regen checklist — note it documents the **0.2.22** image specifically and
has not been regenerated for later tags.

---

## License

The build recipe, scripts, and docs in this repository are licensed under
[Apache-2.0](./LICENSE).

The image **aggregates** upstream open-source EDA tools, each under its own license
(OpenROAD BSD-3, OpenSTA GPL-3, yosys ISC, ngspice BSD, magic/netgen public-domain-style,
klayout GPL-3, iverilog GPL-2, verilator LGPL-3/Apache-2.0, cocotb BSD-3, SymbiYosys/sby
ISC, cocotb-coverage and pyuvm under their own upstream licenses, and the iic-osic-tools
base + PDKs; NanGate45/FreePDK45 Apache-2.0, ASAP7 BSD-3-Clause). Our modifications live
in the public `vibeic/<tool>` forks under each
tool's own license. See [`THIRD_PARTY_LICENSES.md`](./THIRD_PARTY_LICENSES.md) for the
full attribution and links to each fork's source.

---

## Links

- Fix scoreboard + proofs: [`FIX_STATUS.md`](./FIX_STATUS.md)
- Image delivery manifest (per-fork detail, regen checklist): [`IMAGE_0.2.22_DELIVERY.md`](./IMAGE_0.2.22_DELIVERY.md)
- Fork-sync tooling: [`fork-gatekeeper/`](./fork-gatekeeper)
- The AI-native IC-design platform this powers: [vibeic.ai](https://vibeic.ai)
- Upstream base: [iic-osic-tools](https://github.com/iic-jku/iic-osic-tools) (TU Wien / JKU)
