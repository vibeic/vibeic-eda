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
docker pull ghcr.io/vibeic/vibeic-eda:0.2.30
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

"How many forks?" has more than one right answer, so every count below states the
command that reproduces it. The numbers this section used to carry — *15 forked
repos, 13 shipping, 12 pinned as `ARG`s* — were pasted, and all three were wrong
by the time anyone read them. `fork-gatekeeper/inventory.py` already exists
because the fork **status page** shipped "all 15 forks" above a 21-row ledger;
this file was the same failure one directory over. So the counts now live in
tables that [`tools/check_doc_counts.py`](./tools/check_doc_counts.py) executes
and fails on drift.

<!-- counts:local -->

| in this repository | count | reproduce (at the repo root) |
|---|---|---|
| upstream projects the fork-gatekeeper tracks | **21** | `python3 -c 'import json;print(len(json.load(open("fork-gatekeeper/FORKS.json"))["forks"]))'` |
| `vibeic/*` sources the build clones | **24** | `grep -rhoE 'github\.com/vibeic/[A-Za-z0-9_.-]+' Dockerfile tools/*/Dockerfile \| sed 's/\.git$//' \| sort -u \| wc -l` |
| per-tool build artefacts (`tools/<name>/`) | **12** | `ls tools/*/Dockerfile \| wc -l` |
| `ARG *_REF` in the composing `Dockerfile` alone | **10** | `grep -c '^ARG .*_REF=' Dockerfile` |
| source refs pinned across all Dockerfiles | **26** | `grep -rhoE '^ARG [A-Z0-9_]+_REF=' Dockerfile tools/*/Dockerfile \| wc -l` |
| …of those, pinned to a full commit SHA | **22** | `grep -rhoE '^ARG [A-Z0-9_]+_REF=[0-9a-f]{40}' Dockerfile tools/*/Dockerfile \| wc -l` |

<!-- /counts:local -->

Those 20 cloned sources and the 21 tracked projects are **not the same set**, so
the join is derived rather than eyeballed — the checker prints it:

```bash
python3 tools/check_doc_counts.py
# shipping: 19 of 21 tracked forks reach the image; the other 2 are named in the
#           doc (sv-elab, yices2)
# the build clones 20 vibeic sources, 14 of which are tracked forks
```

Read that as three facts. Eighteen of the cloned sources are tracked forks;
adding **OpenSTA**, which has no clone URL because it arrives as OpenROAD's
`src/sta` submodule (‡ below), makes **19 of the 21 tracked forks reach this
image**. The
other **6 cloned sources are not forks at all** — they are mirrors of upstream
data and solver repos (`kissat`, `cadical`, ORFS, the three ASAP7 data repos)
created when GitHub's fork API refused, and a mirror carries `fork = false`, so
it cannot appear in a fork registry however thoroughly it is ours. And **6
tracked projects do not ship**: `gtkwave`, `slang`, `sv-elab`, `xschem`, `Xyce`,
`yices2`. Each is forked because we depend on it, but the image still takes all
six from the iic-osic-tools base, so a fix landed in one of those forks would not
reach a user yet. They are named rather than netted out of a total, because "21
tracked" and "15 shipping" answer different questions and only one of them is
about what you pull.

**"Ships" here means the image CONTAINS the build — not that the flow runs it,
which is a third question and the answer is not 15.** Two of those fifteen are
in the image and inert: `verilator` resolves on `PATH` to the base image's April
5.048 while our 5.051 sits unreferenced (#18), and our `klayout` fork's LEF/DEF
plugin is loaded by nothing but `svrfdrc` (#17). Both were found by
`fork-gatekeeper/fork_reaches_flow_check.py`, which resolves what the flow would
invoke and asks whether it lands in a path we copied from our own artefact. The
distinction is exactly what let those two hide: every count on this page was
right about them.

The org-side count cannot be reproduced from this checkout, so it is dated
instead of asserted:

<!-- counts:github -->

| in the `vibeic` org, measured 2026-07-29 | count | reproduce |
|---|---|---|
| repos with `fork = true` | **45** | `gh api --paginate 'orgs/vibeic/repos?per_page=100' --jq '[.[] \| select(.fork)] \| length'` |
| distinct upstream projects behind them | **21** | `gh api --paginate 'orgs/vibeic/repos?per_page=100' --jq '.[] \| select(.fork) \| .name' \| xargs -I@ gh api repos/vibeic/@ --jq .parent.full_name \| sort -u \| wc -l` |

<!-- /counts:github -->

**45 and 21 differ by a defect, not by a definition.** Twenty-five of the 45 repos
are `sv-elab` and `sv-elab-1` … `sv-elab-24`: one upstream, forked 25 times on
2026-07-28 inside 5 m 14 s, every copy 850 KB and identical. Twenty-four are
redundant. They are named here rather than averaged into a tidier number, and
they are left in place because deleting a repo is irreversible and is the owner's
call. **21 is the count that means *projects*,** and it agrees with `FORKS.json`
in both directions — nothing declared that the org does not have, nothing forked
that is undeclared. Full detail:
[`docs/TOOL_INVENTORY.md`](./docs/TOOL_INVENTORY.md).

The second command costs one API call per fork on purpose: the repo-**list**
endpoint returns `parent: null` on every row, and only the single-repo endpoint
populates it. A count taken from the list's `parent` reads "nothing is forked",
which is byte-identical to a genuinely unforked org.

**Both org numbers count only what GitHub calls a fork, and that is now a real
undercount.** When the fork API refuses — it throttles with a 403 that has
nothing to do with rate limits — the source is mirrored into `vibeic/` instead,
and a mirror carries `fork = false`. Six of the build's sources arrived that way
and are invisible to both commands above however thoroughly they are ours. The
guarantee that every source is ours is therefore asserted by
[`check_fork_only.py`](./tools/check_fork_only.py), which reads the Dockerfiles
rather than the org (*31 references across 9 files, 31 ours, 0 pending*), and the
named-and-dated exception list in
[`tools/PENDING_FORKS.json`](./tools/PENDING_FORKS.json) is currently empty.
Read the org counts as a description of the org, not as the fork-only guarantee.

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
| **ALIGN-public** | analog place & route, SPICE netlist → GDS; forked to be patchable in-tree and built from source rather than installed from PyPI (clean fork, 0 commits ahead) | `master` |
| **ALIGN-pdk-sky130** | the sky130 MOS generator honours the netlist channel length `L` instead of drawing every gate at the fixed 150 nm poly width | `main` |

‡ **OpenSTA is the special case.** It is **not** a Dockerfile `ARG`. It is pinned as
OpenROAD's **`src/sta` git submodule**: the integration branch's `.gitmodules` was
repointed from the upstream relative URL to `https://github.com/vibeic/OpenSTA.git`, so
`git submodule update --init --recursive` in the OpenROAD build stage checks out our
superset commit on `vibeic/sta-timing-eco`. That is why no `ARG` mentions OpenSTA at all.
**Regen invariant:** any OpenROAD ref whose `src/sta` points at a vibeic commit *must*
keep `.gitmodules` on `vibeic/OpenSTA`, and that commit *must* be pushed there, or the
build fails with `upload-pack: not our ref`.

### Where a pin lives

There is no single "pinned tools" number, because since the per-tool split the
pins live in three places by design:

- **The per-tool artefacts** (`tools/<name>/`) — each source pinned there is
  written down three times: the tool `Dockerfile`, the `docker-bake.hcl` variable,
  and the composing `Dockerfile`'s `IMG_*` tag. Rather than restate the totals
  here, ask the checker that owns them:

  ```bash
  python3 tools/check_pins_agree.py    # 18 pin(s) across 8 tool(s) agree in all three places
  ```

  Eight artefacts, ten sources — `sat-solvers` is kissat + cadical and `lvs` is
  magic + netgen, which is why the two numbers differ. See
  [`tools/README.md`](./tools/README.md) for why the tag **is** the pin.
- **The `ARG *_REF` in the composing [`Dockerfile`](./Dockerfile)** (count in the
  table above) — sources built in that file rather than copied out of an artefact.
  Six are our forks (`cocotb`, `cocotb-coverage`, `pyuvm`, `sby`, `ALIGN-public`,
  `ALIGN-pdk-sky130`); the other four are not forks at all and are listed just below.
- **1 git submodule** — OpenSTA, as OpenROAD's `src/sta` (‡ above).

**So `grep -c '^ARG .*_REF=' Dockerfile` is not a fork count.** Four of those ten
are upstream refs staged as *data*, not as tools —

- `ORFS_REF` (`v3.0`) — an upstream
  [OpenROAD-flow-scripts](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts)
  tag, cloned sparsely only to stage two open PDK platforms (below).
- `ASAP7SC_REF` / `ASAP7PDK_REF` / `ASAP7KL_REF` — upstream ASAP7 *data* repos
  (`asap7sc7p5t_28`, `asap7_pdk_r1p7`, `laurentc2/ASAP7_for_KLayout`) staged for the
  ASAP7 device-LVS source-of-truth. These three track `main`, not a SHA — see
  [Build from source](#build-from-source) on what that means for reproducibility.

### Analog auto-layout track (ALIGN)

Two of the 15 shipping forks carry the analog auto-layout track. **ALIGN ships**,
since `0.2.27` — a venv at `/foss/tools/align`, built from the two pinned sources
below, reachable as `align-schematic2layout` and `align-python` on `PATH`:

```bash
docker exec vibeic-eda align-python -c 'import align; print(align.__version__)'   # 0.9.8
docker exec vibeic-eda ls -d /foss/tools/align
```

(An earlier version of this section said the two ALIGN forks were "forked but not
yet shipped in any image" and told you to treat the capability as "spike-proven,
image-integration pending" — three paragraphs below its own sentence stating that
the ALIGN Docker stage had shipped since `0.2.27`. A section that contradicted
itself within one screen survived because nothing read it. That is the reason
this file now has a checker.)

| Fork | Upstream | State, measured 2026-07-29 |
|---|---|---|
| `vibeic/ALIGN-public` | `ALIGN-analoglayout/ALIGN-public` | clean fork, **0 commits ahead** of upstream; forked so ALIGN is patchable in-tree and built from source rather than pulled from PyPI |
| `vibeic/ALIGN-pdk-sky130` | `ALIGN-analoglayout/ALIGN-pdk-sky130` | **2 commits ahead**, both sizing fixes in the sky130 MOS generator: `db6d7f1a` honours the netlist channel length `L` instead of drawing every gate at the fixed 150 nm poly width, and `427b3b94` honours the netlist width `W` instead of quantising it to a 210 nm fin pitch on a planar bulk process |

The `L` fix is enforced at **build** time, not asserted here: the Docker stage
generates a real layout at a deliberately non-nominal `L = 500 nm` and fails the
build unless the drawn poly gates measure 500 nm, then runs the PDK fork's own
`tests/test_channel_length.py` (which ships a negative control — reverting the
fix fails 3 of its 6 tests). If this image ever picked up upstream's sky130 PDK
instead of our fork, it would not build.

Remaining plan and open blockers:
[`ANALOG_LAYOUT_ROADMAP.md`](./ANALOG_LAYOUT_ROADMAP.md) and the Bucket-T row of
[`FIX_STATUS.md`](./FIX_STATUS.md).

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
docker run -d --name vibeic-eda ghcr.io/vibeic/vibeic-eda:0.2.30 --skip sleep infinity
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
  ghcr.io/vibeic/vibeic-eda:0.2.30 --skip sleep infinity
# then point the MCP at it:  EDA_CONTAINER=vibeic-eda
```

**Interactive desktop (VNC / noVNC in the browser):**
```bash
docker run -d --name vibeic-eda \
  -p 5901:5901 -p 8080:80 \
  ghcr.io/vibeic/vibeic-eda:0.2.30
# noVNC:  http://localhost:8080     VNC: localhost:5901   (default password: abc123)
```

**Mount your design directory:**
```bash
docker run -it --rm -v "$PWD:/foss/designs/work" -w /foss/designs/work \
  ghcr.io/vibeic/vibeic-eda:0.2.30 bash
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

1. **Discover** — [`FORKS.json`](./fork-gatekeeper/FORKS.json) is the **fleet list**:
   the checked-in registry of the **21 upstream projects** the tick tracks, one entry
   per upstream (e.g. `OpenROAD → The-OpenROAD-Project/OpenROAD`,
   `klayout → KLayout/klayout`). It is maintained by hand and is *wider* than what
   ships: 19 of the 21 reach the image, and the other two (`sv-elab`, `yices2`)
   are tracked with no pin, because we depend on them and want to know when
   upstream moves even before we build them ourselves. `gtkwave`, `slang`,
   `xschem` and `Xyce` used to sit here too and now build from our forks.
   `discover_forks.py` **reads** that list and writes the per-tool ledger — for each
   entry it resolves the pinned ref, the fork point, and the upstream's newer
   releases. It does not populate `FORKS.json` from the org, so adding a fork to the
   org does not add it to the fleet: `check_fork_only.py` and the "Adding a tool"
   checklist in [`tools/README.md`](./tools/README.md) are what keep the two in step,
   and [`tools/check_doc_counts.py`](./tools/check_doc_counts.py) asserts the registry
   never carries two entries for one upstream — the counts above are project counts
   only as long as that holds.
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

The image is built entirely from source. **16 of the 20 source refs are pinned to a
full commit SHA** — every tool, ours and upstream alike — so the *tool* half of a
rebuild is reproducible. The four that are not are the ORFS tag and the three ASAP7
data refs, all covered under *What is not SHA-pinned* below:

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
- `ghcr.io/vibeic/vibeic-eda:latest` — intended to be the newest released `X.Y.Z`.
  **It is not, right now.** Measured against the registry on 2026-07-26:

  | tag | manifest digest |
  |---|---|
  | `0.2.30` | `sha256:d2bbc5c8b004…` |
  | `0.2.28` | `sha256:3ba01229b38c…` |
  | `latest` | `sha256:3ba01229b38c…` — **the same image as `0.2.28`** |

  So `docker pull …:latest` currently gets **0.2.28**, two releases behind. Pin an
  explicit `X.Y.Z` until the moving tag is re-pointed; re-tagging a published image
  changes what every existing `:latest` user receives, so it is left to the owner
  rather than done as a side effect of a docs fix. (The previous text here claimed
  `latest` matched `0.2.26` — `sha256:cafd850169ee…` — which was also untrue.)

Current: **0.2.30** — the from-source release that is pinned by the plugin's
image-version gate. `0.2.29` was assigned and never published; the registry has no
such tag, which is what that gate now blocks. For what each earlier tag added, see
the table below.

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
