# `drt` reports `NS Metal` (min-area) for a via-pad fragment, on a polygon 10x the rule

**Status: OPEN.** Present in `vibeic-eda:0.3.40`
(`sha256:fad41245fbff4f863a49f8663e3902a3d7100ce63c8fc073159452bd0c80a4a9`),
i.e. **after** `vibeic/OpenROAD` #9, #10 and #11 landed. Filed 2026-08-30.

This is a **wrong-answer** defect, not a crash. `detailed_route` completes, exits
0, and writes a routed DEF; the answer it publishes is wrong.

PDK: gf180mcuD, `gf180mcu_fd_sc_mcu7t5v0`. Design: a 1693-instance digital block.

---

## 1. It is NOT #9 and NOT #11 — measured, not argued

#9 and #11 are both SIGSEGV/use-after-free defects in `drt`. This one shares the
module and nothing else. The distinction was **tested on the image that carries
all three fixes**, not reasoned about:

| | #9 / #11 | this defect |
|---|---|---|
| symptom | SIGSEGV (rc 139) / abort (rc 134) | **exit 0**, normal completion |
| what is wrong | the process dies | the *published DRC result* is wrong |
| trigger | a second route in one session (#9); a destroyed net's stale `frVia` in the region query (#11) | a single route, first and only, in a fresh process |
| determinism | depends on freed-chunk recycling | **bit-identical across images and runs** |
| status on 0.3.40 | fixed | **still present** |

**The decisive run.** The same generated `pnr.tcl`, the same generated inputs,
byte-identical apart from a mechanical output-path rewrite, replayed on both
images:

| image | digest | in-loop `DRT-0199` | `DRT-0701` | `routed_router.drc.rpt` sha256 |
|---|---|---|---|---|
| 0.3.39 | `sha256:0ff7a56a0ab3…` | 260 / 54 / 46 / **0** | line 862, **1 violation** | `1a3cce5ff843b361…` |
| **0.3.40** | `sha256:fad41245fbff…` | 260 / 54 / 46 / **0** | line 862, **1 violation** | **`1a3cce5ff843b361…`** |

**The two reports are byte-identical.** `grep -cE 'Segmentation|SIGSEGV|terminate
called|Assertion|DRT-1010'` on the 0.3.40 log returns **0** — nothing crashed.
So the three landed fixes do not touch this path, and this is a second, distinct
question rather than a second answer to one already closed.

---

## 2. Symptom

`detailed_route`'s in-loop count reaches 0 and post-route verification then
contradicts it:

```
819: [INFO DRT-0199]   Number of violations = 0.
862: [WARNING DRT-0701] Post-route verification found 1 violation(s)
     that the routing loop did not report (0 in-loop). The published result is the verified one.
```

With `-output_drc` the violation is named:

```
violation type: NS Metal
	srcs: net:__uuf__._044_
	bbox = (226.3900, 177.3900) - (226.4300, 177.4450) on Layer Metal1
```

The route never converges, and a flow that gates on convergence correctly
refuses to stream a sign-off GDS behind it.

---

## 3. Eight instances, one signature

Across three die sizes, six nets, two images and two plugin versions, **every
marker is bit-identically 0.0400 x 0.0550 um = 0.0022 um^2**:

| # | die um | net | driver | orient | via | marker |
|---|---|---|---|---|---|---|
| A | 412 | `__uuf__._040_` | `_348_` aoi211_1 | N | Via1_HV | 0.0400 x 0.0550 |
| B | 412 | `__uuf__._048_` | `_356_` aoi211_1 | N | Via1_HV | 0.0400 x 0.0550 |
| C | 285 | `__uuf__._044_` | `_352_` aoi211_1 | N | Via1_HV | 0.0400 x 0.0550 |
| D | 329 | `__uuf__._034_` | `_342_` aoi211_1 | **FS** | Via1_HV | 0.0400 x 0.0550 |
| E,F,G | 329 | `_038_`, `_043_`, `_044_` | — | — | — | 0.0400 x 0.0550 |
| H | 285 | `__uuf__._044_` | `_352_` aoi211_1 | N | Via1_HV | **on 0.3.40** |

Driver is `gf180mcu_fd_sc_mcu7t5v0__aoi211_1` pin `ZN` through a `Via1_HV` in
every case.

---

## 4. What the marker is

Each violating net has **no Metal1 wire at all** — only a Metal2 wire and two
bare `Via1_HV` landing pads:

```
- __uuf__._044_ ( __uuf__._352_ ZN ) ( _178_ I0 ) + USE SIGNAL
  + ROUTED Metal2 ( 451360 348880 ) ( * 355040 )
  NEW Metal2 ( 451360 355040 ) ( 452480 * )
  NEW Metal1 ( 451360 348880 ) Via1_HV
  NEW Metal1 ( 452480 355040 ) Via1_HV ;
```

The `Via1_HV` Metal1 pad is 0.380 x 0.260 um. **In every instance the reported
marker is a corner of that pad, sharing two of the pad's own edges exactly:**

```
A pad=[226.050 294.990]-[226.430 295.250]  marker = pad right-bottom corner
B pad=[325.170 177.390]-[325.550 177.650]  marker = pad right-bottom corner
C pad=[226.050 177.390]-[226.430 177.650]  marker = pad right-bottom corner
D pad=[ 57.490 175.150]-[ 57.870 175.410]  marker = pad right-TOP    corner
```

**D's driver is placed `FS` (mirrored); the other three are `N`. The fragment
flips with the cell orientation.** In all four the cut sits at a constant
**(+0.150, -0.075) um from the via origin** — a feature of the *driver cell
master*, not of the die and not of the route. `aoi211_1` exposes `ZN` as several
abutting rectangles, and the cut falls on one of their seams.

**Why it cannot be a real violation.** 0.0400 um is **5.75x below Metal1's own
`MINWIDTH 0.230`** (`gf180mcu_fd_sc_mcu7t5v0__nom.tlef`). No router creates a
40 nm shape in a 180 nm process. The connected Metal1 polygon actually present at
that site measures **1.4533 um^2 — 10.1x the `AREA 0.1444` (Mn.3) rule**
(reconstructed from PDK LEF + routed DEF and merged with `klayout.db.Region`;
the reconstruction validates against the LEF's own `SIZE 5.600 BY 3.920` and the
3.92 um = 7 x `PITCH 0.56` rail pitch).

Separately: **every** Via1 Metal1 pad in this PDK is 0.0988 um^2, below
`AREA 0.1444`, and is legal only by merging with the pin metal it lands on. The
design has 2171 Metal1 up-vias and one to three are flagged per run — so this is
not "the checker ignores cell pin metal" generally.

---

## 5. Mechanism, as far as it is measured

The shipped binary defines
`drt::FlexGCWorker::Impl::checkMetalShape_minArea(drt::gcPin*, bool)`, and
`NS Metal` is a violation-type string in the same binary. In the OpenROAD sources
for that function:

```cpp
auto poly = pin->getPolygon();
auto actArea = gtl::area(*poly);
if (actArea >= reqArea) { return; }              // merged polygon would exit here
gtl::extents(bbox, *pin->getPolygon());          // <-- the reported bbox
odb::Rect bbox2(...);
for (auto& edges : pin->getPolygonEdges())
  for (auto& edge : edges)
    if (edge->isFixed()) { return; }             // <-- bails on fixed geometry
```

Two consequences follow, and they constrain the defect tightly:

1. **The reported bbox is `gtl::extents` of the polygon the checker actually
   evaluated.** So the checker genuinely held a polygon whose extents are
   0.0400 x 0.0550 um — it is not reporting a sub-region of a larger shape it was
   reasoning about correctly.
2. **The function returns without a marker if ANY edge is fixed.** Cell pin metal
   is fixed. So the polygon it evaluated contained **no cell-pin metal**: it was
   the via-pad fragment *in isolation*.

Taken together: a driver pin exposed as several abutting rectangles yields more
than one `gcPin`, and the pad fragment overlapping one of them is evaluated as a
standalone polygon against `AREA`. The merged pad-plus-pin polygon — which
satisfies the rule by 10.1x, and would have hit the `actArea >= reqArea` early
return — is never formed on this path.

---

## 6. Reproducer

```
python3 <vibe-ic>/programs/phase3_one_shot_runner.py <tree> \
    --pdk gf180mcuD --die-um 285x285 --util 0.4 --force-step pnr
```

`--die-um` is load-bearing: it pins the geometry so the caller's auto-loosen
ladder cannot walk the die out from under the repro (it records
`"action": "declined", "reason": "explicit_die_requested"`). Reproduces in ~4 min.

Result: `phase3/stage3/pnr/routed_router.drc.rpt` (118 B) names the violation;
`openroad.log` carries `DRT-0701 ... 1 violation(s)`. Also reproduces at
`--die-um 329x329 --util 0.25` (1 violation) and at 329x329 under the ladder
(3 violations, all the same signature).

---

## 7. Why it is worth fixing

The caller's auto-loosen ladder walks the die up until the marker stops firing.
On this design that is **285x285 -> 388x388: 1.85x the area, ending at 17%
utilisation**. Every residual escaped along the way is an instance of this one
artefact, on a polygon 10x the rule. **The die growth buys no real DRC
improvement** — so the fix is worth roughly 46% of this die's area.

---

## 8. Toolchain provenance — stated, not overclaimed

`openroad -version` reports `26Q3-1887-g24ea077e76` on 0.3.40 and
`26Q3-1884-g80f878d28a` on 0.3.39. **A version banner is a build-time label, not
proof of file content**, and this image ships no OpenROAD sources, so the fixes
in #9/#10/#11 are recorded here as **PRESENT-BY-DIGEST and NOT VERIFIED BY
CONTENT**. Nothing in this report rests on the banner.

What *is* verified by content, from the binaries themselves:

| probe | 0.3.39 | 0.3.40 |
|---|---|---|
| binary sha256 (first 32) | `fef3969f7bfa2c2d5b98bc2fc0dc9612` | `5c60962db49b27534ad753ff200a9b6d` |
| exported dynamic symbols | **56** | **148844** |
| `removeDRObj` symbols | 2 | 2 |
| `checkMetalShape_minArea` | 1 | 1 |

* The 56 -> 148844 jump **is** #10 (`-rdynamic`), verified by content.
* `removeDRObj` is present in **both**, so its presence proves nothing about #11;
  #11 changes a *call site*, which is not visible as a new symbol. #11 and #9
  remain present-by-digest only.
* Neither image carries a GNU build-id, so there is no content-level route from
  binary to source commit for either.

The load-bearing claim of this report needs none of that: it is **behavioural**,
from what the shipped 0.3.40 binary does on this design.

---

## 9. NOT_MEASURED — stated, never defaulted

* **The exact set-op inside FlexGC** that leaves the pad fragment a separate
  `gcPin`. The fork was not built and the checker was not stepped through. What
  is measured is that the emitted marker corresponds to no polygon present in the
  design, and is bit-exactly a corner of a via pad, eight times independently.
* **Whether the sources quoted in section 5 are the sources that built either
  binary.** See section 8. The symbol signature and the `NS Metal` string in the
  binary corroborate them; nothing more is claimed.
* **Whether other PDKs or other cell masters are affected.** Only gf180mcuD and
  only `aoi211_1`/`ZN` were observed. Whether the trigger is "any pin exposed as
  abutting rectangles" is a plausible generalisation that was **not** tested.
