# `PL.8` counts the poly the PDK drew and not the poly the PDK filled

**Status: fixed upstream 2026-07-04. The volume this image ships predates the fix,
and this repository backports the PL.8 half of it.**

## The defect

The gf180mcu KLayout deck states every half of the contradiction itself, in four
files that ship together:

```
libs.tech/klayout/tech/drc/rule_decks/density.rb
    # Rule PL.8: Poly2 coverage over the entire die shall be 14%.
    ## Dummy poly2 lines must be added to meet the minimum poly2 density requirement.
    if (poly2.area / chip_area) * 100 < 14

libs.tech/klayout/tech/drc/generic_layers.rb
    extract_single_layer_from_design.call(:poly2, 30, 0)
    extract_single_layer_from_design.call(:poly2_dummy, 30, 4)

libs.tech/klayout/tech/scripts/fill_poly2.rb
    fill_layer = Poly2_Dummy

libs.tech/klayout/tech/scripts/fill_all.rb
    Poly2_Dummy = $ly.layer(30, 4)
```

The rule's own text tells the customer to add dummy poly to reach the floor. The
same PDK ships the script that adds it. That script writes it on **30/4**. The
rule counts **30/0** alone. So a die that meets the density requirement *with the
fill this PDK generated* is reported as violating it.

The metal density rules in the same file do not have the defect —
`register_numbered_metal_layers` unions `metal_drawn + metal_dummy` and
M1.4 … MT.3 read that union. Poly2 simply had no equivalent.

Measured on a design under this image (lane `czspmdrc`, then re-derived here):
`fill_poly2` had run and layer 30 density went 0.0007 → 0.2700 over the die,
2,692,632 um² of dummy poly — and `PL.8` was still reported.

## Fixed upstream

`fossi-foundation/globalfoundries-pdk-libs-gf180mcu_fd_pv`

```
commit 2404d27b583b   2026-07-04   fix: ensure poly2 = poly2_drawn + poly2_dummy
                                   Signed-off-by: Leo Moser <leomoser99@gmail.com>
28 files changed
```

Upstream fixed it in the same place, with the opposite naming convention: the
30/0 extraction was **renamed** to `:poly2_drawn`, the name `:poly2` was given to
the union by adding a level-0 row to the existing metal table, and every rule
that must see drawn poly alone was rewritten to say `:poly2_drawn` (27 of the 28
files are that rename).

```ruby
     METAL_NAMES = {
+      0 => { metal_drawn: :poly2_drawn, metal_dummy: :poly2_dummy, metal_result: :poly2 },
       1 => { metal_drawn: :metal1_drawn, metal_dummy: :metal1_dummy, metal_result: :metal1 },

     private_class_method def self.register_numbered_metal_layers(ctx, metal_level)
-      (1..metal_level).each do |level|
+      (0..metal_level).each do |level|
```

**So there is no upstream patch to send.** The answer to "where should the same
correction go upstream" is "it is already there, and what we ship is behind."

Note in particular `dummy_poly2.rb` in the same commit:

```ruby
-  dpf5_l1 = poly2_dummy.separation(poly2, 5.um, euclidian)
+  dpf5_l1 = poly2_dummy.separation(poly2_drawn, 5.um, euclidian)
```

DPF.5 measures dummy poly **against** drawn poly. Any fix that folds the two into
one symbol without that rename makes DPF.5 measure a layer against itself.

## Why this repository owns a copy of it

The gf180mcuD this image ships is **not built from open_pdks source**. It is a
prebuilt `ciel` volume the base image bakes in:

```
/foss/pdks/gf180mcuD -> ciel/gf180mcu/versions/b344c97eacc2aaf8e14ae7e43e2e9dc0871de2c0/gf180mcuD
/foss/pdks/gf180mcuD/SOURCES:  open_pdks b344c97eacc2aaf8e14ae7e43e2e9dc0871de2c0
```

The provenance chain, measured end to end:

| link | value |
|---|---|
| image | `ghcr.io/vibeic/vibeic-eda@sha256:06537f7e…`, label `0.3.46` |
| PDK volume | `open_pdks b344c97eacc2aaf8e14ae7e43e2e9dc0871de2c0` (`SOURCES`) |
| that open_pdks pins | `gf180mcu.json: "gf180mcu_fd_pv": "13f5cb3829f7ec3fe1d4469534fd60c12aaaa477"` |
| `13f5cb38` vs the fix `2404d27b` | **3 commits behind** (`compare` → ahead=3, behind=0) |
| shipped `density.rb` md5 | `9149cc392ad3dc1787e7b479e066cb5f` — **byte-identical** to `13f5cb38`'s copy |

So the deck this image ships is exactly the upstream file from three commits
before the fix, and a commit in `vibeic/open_pdks` would ship nothing, because
nothing in this build compiles open_pdks. This `RUN` is the only place that owns
the bytes a user gets.

**Advancing the PDK volume would bring the real fix.** `fossi-foundation/open-pdks`
`main` today pins `gf180mcu_fd_pv = 035e6d1f4bf0acfb60606b056e7d2159cba30fef`,
which is **19 commits ahead of `2404d27b583b` and 0 behind** — it contains it.
Moving `OPEN_PDKS_VOLUME_CONTENTS_SHA` and cutting a new ciel volume would
therefore make this local modification unnecessary, and would bring the other 19
commits of deck changes with it. That is a decision about the whole PDK, not
about PL.8, and it is not taken here.

## What this repository changes, and what it deliberately does not

`tools/pdk/pdk_gf180_poly2_density_patch.py`, declared in
`tools/pdk/local_mods.json` as `gf180mcu-pl8-poly-density-counts-dummy`, makes
**two** edits:

```ruby
# generic_layers.rb, in STATIC_LAYERS
{ name: :poly2_result,    calc: ->(ctx) { ctx[:poly2] + ctx[:poly2_dummy] } },

# density.rb, PL.8
if (poly2_result.area / chip_area) * 100 < 14
```

It does **not** perform upstream's rename. `:poly2` keeps its drawn-only meaning
and every other rule sees exactly the layer it saw before — including DPF.5,
which the rename exists to protect. The union arrives under a new name whose only
consumer is the density rule.

That is a deliberately smaller change than upstream's, and the difference is
worth stating: upstream's rename also gives the union to every rule that still
says bare `poly2` — `geom.rb`'s off-grid check, `guard_ring.rb`, and others — so
`2404d27b583b` moves more verdicts than PL.8. Those are not measured here.
Adopting them belongs with **advancing the PDK volume**, not with hand-porting 28
files into a binary artefact.

The anchors are exact whole lines and the step declares `--expect 2`, so when the
volume does advance to a deck that already carries upstream's fix, this step
**fails the build** rather than silently applying to something else.

## Correction, not relaxation — the measurement

A relaxation makes a real violation stop being reported. A correction makes the
checker see what the rule counts. Three generated 100×100 µm dies separate them.
Deck run as `-rd decks=density -rd variant=gf180mcuD -rd run_mode=flat -rd
workers=1`, in
`ghcr.io/vibeic/vibeic-eda@sha256:06537f7e8d3c17c6c9c60c20638e94faab0421533a55656ad1819f383c373aba`
(label `0.3.46`):

| fixture | poly 30/0 | dummy 30/4 | PL.8 before | PL.8 after |
|---|---|---|---|---|
| `A_poly2_fill_meets_floor` | 5 % (500 µm²) | 15 % (1500 µm²) | **FAIL** | **PASS** |
| `B_no_fill_below_floor` | 5 % (500 µm²) | none | **FAIL** | **FAIL** |
| `C_drawn_alone_meets_floor` | 20 % (2000 µm²) | none | **PASS** | **PASS** |

Row B is the whole argument: a die genuinely short of polysilicon is still
reported. **If row B ever goes green the rule has been relaxed and the patch is
wrong.**

Re-run with `-rd decks=all`, the set of rules that fire is identical before and
after on all three dies except `PL.8` leaving row A:

```
A  before {DBU, DPF.1, M1.4, M2.4, M3.4, M4.4, M5.4, MT.3, PL.8}
   after  {DBU, DPF.1, M1.4, M2.4, M3.4, M4.4, M5.4, MT.3}
B  identical
C  identical
```

The fixtures are generated from the die size and the target coverage
(`fixtures/gen_pl8_fixtures.py` in the lane record); nothing is copied from any
design.

## What a reader could previously believe that they no longer can

That a `PL.8` on a gf180mcuD die run through this image meant the die was short
of polysilicon.
