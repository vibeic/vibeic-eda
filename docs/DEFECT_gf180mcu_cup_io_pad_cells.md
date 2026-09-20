# `CUP.2`/`CUP.3` report the bond-pad metal the PDK itself drew

**Status: NOT fixed upstream. This repository owns the correction.**
Tracker: vibeic-eda#214.

## The defect

`libs.tech/klayout/tech/drc/rule_decks/cup.rb`, exactly as the gf180mcuD volume
ships it:

```ruby
cup2_l1 = metal_drawn.interacting(pad).not_interacting(guard_ring_mk).width(1.um).interacting(pad)
cup3_l1 = metal_drawn.interacting(pad).not_interacting(guard_ring_mk).notch(1.um).interacting(pad)
```

`pad` is `37/0` and `metal_drawn` is the whole layout's metal — the foundry IO
library's included. Every pad-bearing `gf180mcu_fd_io` cell carries **both**: its
own `Pad` opening *and* its own slotted bond-pad metal inside that opening. So
each such cell, extracted into a GDS of its own and checked **alone**, unmodified,
reports **87 `CUP.3`** against the PDK's own qualified geometry.

The violating shapes are the slots in that metal — 0.3 µm wide, 59.31/60.0 µm
long, on a 2 µm pitch, on Metal2/3/4, entirely inside the cell's own `Pad`
opening. In `gf180mcu_fd_io__bi_t` (75 × 350 µm, `Pad` at (7.5, 2)–(67.5, 62)):

```
shape census (w,h): (0.3, 59.31) x56, (0.3, 60.0) x28, (0.96, 59.31) x2, (0.96, 60.0) x1
```

A die inherits it: on a measured SPM die, 38 bond pads × 87 = **3306** in flat
mode, 7 pad masters × 87 = **609** deep, with the flow's own fill, PDN, pad ring
and seal ring contributing **0** — and `drc` the only failing step of the run.

## Standalone census, measured (pinned image `sha256:89a8fd72…`, KLayout 0.30.12)

Every top cell of `libs.ref/gf180mcu_fd_io/gds/gf180mcu_fd_io.gds`, each extracted
alone and run with `-rd decks=cup`:

| cell | `Pad` 37/0 | `CUP.3` as shipped | after this correction |
|---|---|---|---|
| `__asig_5p0`, `__bi_24t`, `__bi_t`, `__dvdd`, `__dvss`, `__in_c`, `__in_s` | yes | **87** each | **0** each |
| `__brk2`, `__brk5`, `__cor`, `__fill1`, `__fill5`, `__fill10`, `__fillnc` | no | 0 | 0 |

`CUP.2` is 0 on all of them in both arms.

## The deck's only existing escape makes it worse

`not_interacting(guard_ring_mk)` (`167/5`) is the sole exemption in the rule.
Measured on the real die by painting a single `167/5` rectangle over ONE bond pad:

```
decks=cup          base 348  ->  marked 261      (-87, exactly one pad's worth)
decks=guard_ring   base   0  ->  marked 762      (GR.1 x1, GR.2 x148, GR.4 x613)
```

`GUARD_RING_MK` is the guard-ring/seal-ring **structure** marker — `GR.1` wants
guard-ring COMP under it, `GR.2` wants 10 µm to prime-die COMP/NWELL/Poly2/Metal,
`GR.4` wants ≥ 12 µm metal width inside it. It is not a per-bond-pad exemption
stamp, and using it trades 87 violations for 762.

## There is no marker to key an exemption on, so the identity is the cell master

Measured on `gf180mcu_fd_io__bi_t` (dbu 0.001, `Pad` opening y = 2…62 µm): every
marker layer the IO library carries lies **above** y = 68.7 µm —

| layer | name | y range of its shapes |
|---|---|---|
| 119/5 | `lvs_io` | 68.7 … 207.9 µm |
| 24/5 | `esd_mk` | 71.8 … 199.1 µm |
| 137/5 | `latchup_mk` | 75.7 … 186.1 µm |
| 115/5 | `diode_mk` | 247.2 … 335.3 µm |
| 110/5 | `res_mk` | 269.7 … 305.4 µm |
| 166/5 | `mos_cap_mk` | 269.5 … 346.5 µm |

— i.e. over the cell's devices, never over the pad metal. `63/63` holds texts
only (`RBA::Region` count 0). Nothing the PDK draws marks the metal in question,
and `grep` finds no IO-cell awareness anywhere in the deck: `lvs_io` appears in
`esd.rb`, `sab.rb` and `geom.rb` and nowhere else. The remaining identity the PDK
does offer is the **cell master**, and that is what this correction uses.

## Not fixed upstream — and this is the difference from the PL.8 entry

`fossi-foundation/globalfoundries-pdk-libs-gf180mcu_fd_pv` at `035e6d1f` (tip when
this was written) ships `cup.rb` **byte-identical** to the volume's copy
(md5 `6a4e724731d04b657f9a96e73e45256b`), with one touch in its whole history
(`f1b9021`, "Upstream wafer.space changes"). Unlike
`docs/DEFECT_gf180mcu_pl8_poly_dummy_density.md`, where advancing the PDK volume
*would* bring the real fix, here **advancing the volume brings nothing**, because
there is no upstream correction to bring. This is a patch worth sending upstream.

## What this repository changes

`tools/pdk/pdk_gf180_cup_io_pad_patch.py`, declared in `tools/pdk/local_mods.json`
as `gf180mcu-cup-io-pad-masters-are-not-design-metal`, makes **two** edits in one
file, `rule_decks/cup.rb`:

```ruby
CUP_IO_PAD_MASTER_CELLS = 'gf180mcu_fd_io__*'

layer, datatype = METAL_MAP_CUP[lvl][:source]
shapes = ctx.drc.source.cell_obj.begin_shapes_rec(
  ctx.drc.source.layout.layer(layer, datatype)
)
shapes.unselect_cells(CUP_IO_PAD_MASTER_CELLS)
design_metal = RBA::Region.new(shapes)
design_metal.merge
metal_drawn = ctx.drc.polygon_layer
metal_drawn.data = design_metal
```

The two rule expressions are **untouched**, the 1 µm limits included. Only their
subject moves: from the whole layout's metal to the metal the design drew.
`pad` and `guard_ring_mk` are **not** filtered, so design metal reaching into a
foundry cell's bond-pad opening — the literal circuit-under-pad — is still the
subject and is still checked.

### Why an iterator and not `source.select`

`ctx.drc.source.select('-gf180mcu_fd_io__*').polygons(l, d)` says the same thing
in one line and was the first thing tried. It works under `-rd decks=cup` and
kills the deck under `-rd decks=all`:

```
ERROR: 'nets': Internal error: deep shape store isn't singular. This may happen
if you try to mix hierarchical layers from different sources ...  in Region::nets
  gf180mcu.drc:190   (raised from rule_decks/dnwell.rb, rule DN.1)
```

A second DRC source is a second deep shape store, and the deck's connectivity
rules then fail in places that have nothing to do with CUP. `decks=all` is what
LibreLane's own gf180 configuration runs (`decks=all,-antenna,-density`), so this
would have shipped as a worse defect than the one being fixed. The
`RecursiveShapeIterator` is over the deck's **own** layout: same source, no second
store. It costs a flat region of one metal layer per level.

## Correction, not relaxation — the measurement

A relaxation makes a real violation stop being reported. `tools/pdk/pdk_gf180_cup_check.py`
is a build step that runs the deck itself, both directions, and returns **rc=1
against the deck as shipped** — it is a check that can fail:

| fixture | rule | as shipped | after |
|---|---|---|---|
| each of the 7 pad-bearing `gf180mcu_fd_io` masters, alone | CUP.3 | 87 | **0** |
| each of the 7 masters with no `Pad` layer, alone | CUP.2/3 | 0 | 0 |
| design-drawn 0.5 µm notch on pad metal, no IO cell | CUP.3 | 1 | **1** |
| design-drawn 0.5 µm wide metal at a pad, no IO cell | CUP.2 | 1 | **1** |
| IO master + design notch beside it | CUP.3 | 88 | **1** |
| IO master + design notch INSIDE its own `Pad` opening | CUP.3 | 88 | **1** |
| IO master nested under a wrapper cell + design notch | CUP.3 | 88 | **1** |
| IO master + design 0.5 µm wide metal | CUP.2 | 1 | **1** |

The last six rows are the whole argument: a design that genuinely violates the
bond-pad rules is still reported, including when it violates them *inside a
foundry pad's own opening*. **If any of them ever reaches 0 the rules have been
relaxed and this patch is wrong.**

`decks=all` on the same fixtures: the set of rules that fire is identical before
and after except for CUP, and the `deep` run mode agrees with `flat`.

The fixtures are generated from rectangles and the shipped IO library; nothing is
copied from any design.

## What a reader could previously believe that they no longer can

That a `CUP.3` on a gf180mcuD die run through this image meant the design had
drawn something wrong at a bond pad.
