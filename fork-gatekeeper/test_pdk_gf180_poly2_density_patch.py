"""PL.8 counts the poly the PDK drew and not the poly the PDK filled.

The gf180mcu KLayout deck ships all three halves of the contradiction:

    drc/rule_decks/density.rb   PL.8's own text: "Dummy poly2 lines must be
                                added to meet the minimum poly2 density
                                requirement", and then
                                `if (poly2.area / chip_area) * 100 < 14`
    drc/generic_layers.rb       :poly2 = 30/0, :poly2_dummy = 30/4 -- two
                                different layers, and PL.8 reads only the first
    tech/scripts/fill_poly2.rb  `fill_layer = Poly2_Dummy`, which
    tech/scripts/fill_all.rb    defines as `$ly.layer(30, 4)`

So the fill the rule asks for is written on a layer the rule does not count. The
metal density rules in the SAME deck do not have this defect:
`register_numbered_metal_layers` unions `metal_drawn + metal_dummy` and M1.4..
MT.3 read that union.

MEASURED with the deck shipped in
ghcr.io/vibeic/vibeic-eda@sha256:06537f7e8d3c17c6c9c60c20638e94faab0421533a55656ad1819f383c373aba
(image-version:history, label 0.3.46 -- the image the runs were made on, not a
pointer that may be bumped), on three generated 100x100um dies, `-rd
decks=density -rd variant=gf180mcuD -rd run_mode=flat`:

    5% drawn (30/0) + 15% dummy (30/4)   PL.8 FAIL -> PASS   the defect
    5% drawn, no fill                    PL.8 FAIL -> FAIL   the control
    20% drawn, no fill                   PL.8 PASS -> PASS   unchanged path

The middle row is what separates a CORRECTION from a RELAXATION: a die that is
genuinely short of polysilicon is still reported. If it ever goes green the
patch is wrong. Re-run with `-rd decks=all` the whole rule membership is
identical before and after on all three dies except for PL.8 leaving the first.

Upstream fixed this on 2026-07-04 in
fossi-foundation/globalfoundries-pdk-libs-gf180mcu_fd_pv 2404d27b583b, by the
opposite rename (30/0 becomes :poly2_drawn, the union takes the name :poly2).
The prebuilt ciel volume this image ships predates that commit; this repository
ports the PL.8 half of it, and `test_poly2_itself_is_not_redefined` below is
what keeps the smaller port from becoming the larger one by accident.

These tests pin the TRANSFORM. They do not need a PDK: the fixture carries the
same two anchors in miniature.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROG = HERE.parent / "tools" / "pdk" / "pdk_gf180_poly2_density_patch.py"

_spec = importlib.util.spec_from_file_location(
    "pdk_gf180_poly2_density_patch", str(PROG))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


GENERIC_RB = """\
module GF180DRC
  module GenericLayers
    def self.extract_layers_from_design(ctx)
      extract_single_layer_from_design.call(:poly2, 30, 0)
      extract_single_layer_from_design.call(:poly2_dummy, 30, 4)
      ctx
    end

    STATIC_LAYERS = [
      # --- Comps & Gates ---
      { name: :ncomp_con,       calc: ->(ctx) { ctx[:ncomp].not(ctx[:poly2]) } },
      { name: :pcomp_con,       calc: ->(ctx) { ctx[:pcomp].not(ctx[:poly2]) } },
      { name: :tgate,           calc: ->(ctx) { ctx[:poly2].and(ctx[:comp]).not(ctx[:res_mk]) } },

      # --- N-Device Logic ---
      { name: :nactive,         calc: ->(ctx) { ctx[:ncomp].not(ctx[:all_nwell]) } }
    ].freeze

    private_class_method def self.register_numbered_metal_layers(ctx, metal_level)
      (1..metal_level).each do |level|
        names = METAL_NAMES[level]
        ctx.register_layer(names[:metal_result]) { ctx[names[:metal_drawn]] + ctx[names[:metal_dummy]] }
      end
    end
  end
end
"""

DENSITY_RB = """\
GF180_DRC_REGISTRY.register(id: 'density') do
  chip_area = extent.sized(0.0).area

  # Rule PL.8: Poly2 coverage over the entire die shall be 14%.
  ## Dummy poly2 lines must be added to meet the minimum poly2 density requirement.
  logger.info('Executing rule PL.8')
  if (poly2.area / chip_area) * 100 < 14
    extent.output('PL.8', 'PL.8 : ... : 14%')
  end

  logger.info('Executing rule M1.4')
  if (metal1.area / chip_area) * 100 < 30
    extent.output('M1.4', 'M1.4 : ... : 30%')
  end
end
"""

UNRELATED_RB = "# a file no entry in this patch names\n"


def make_pdk(tmp_path, generic=GENERIC_RB, density=DENSITY_RB):
    root = tmp_path / "gf180mcuD"
    (root / "libs.tech/klayout/tech/drc/rule_decks").mkdir(parents=True)
    (root / mod.GENERIC).write_text(generic)
    (root / mod.DENSITY).write_text(density)
    (root / "libs.tech/klayout/tech/drc/poly2.rb").write_text(UNRELATED_RB)
    return root


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_text()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------- the defect


def test_unpatched_deck_counts_only_drawn_poly(tmp_path):
    """The fixture reproduces the defect, or the rest of this file proves nothing."""
    root = make_pdk(tmp_path)
    src = (root / mod.DENSITY).read_text()
    assert "if (poly2.area / chip_area) * 100 < 14" in src
    assert "poly2_dummy" not in src
    # ...while the metal rule beside it already unions drawn + dummy.
    assert "ctx[names[:metal_drawn]] + ctx[names[:metal_dummy]]" in \
        (root / mod.GENERIC).read_text()


def test_patch_makes_pl8_read_the_union(tmp_path):
    root = make_pdk(tmp_path)
    assert mod.main(["--root", str(root), "--expect", "2"]) == 0

    density = (root / mod.DENSITY).read_text()
    generic = (root / mod.GENERIC).read_text()

    assert "if (poly2_result.area / chip_area) * 100 < 14" in density
    assert "if (poly2.area / chip_area) * 100 < 14" not in density
    assert "{ name: :poly2_result,    calc: ->(ctx) " \
           "{ ctx[:poly2] + ctx[:poly2_dummy] } }," in generic


def test_metal_density_rules_are_untouched(tmp_path):
    """The patch is about PL.8. M1.4 must read exactly what it read before."""
    root = make_pdk(tmp_path)
    before = (root / mod.DENSITY).read_text()
    mod.main(["--root", str(root), "--expect", "2"])
    after = (root / mod.DENSITY).read_text()
    for line in before.splitlines():
        if "metal1" in line or "M1.4" in line:
            assert line in after.splitlines()


def test_poly2_itself_is_not_redefined(tmp_path):
    """DPF.5 is `poly2_dummy.separation(poly2, 5.um)`.

    Folding the dummy into :poly2 would make that rule measure a layer against
    itself and would silently change device extraction (`tgate`). The union
    must arrive under a NEW name.
    """
    root = make_pdk(tmp_path)
    mod.main(["--root", str(root), "--expect", "2"])
    generic = (root / mod.GENERIC).read_text()
    assert "extract_single_layer_from_design.call(:poly2, 30, 0)" in generic
    assert "{ name: :tgate,           calc: ->(ctx) " \
           "{ ctx[:poly2].and(ctx[:comp]).not(ctx[:res_mk]) } }," in generic
    # ...and no derived layer claims the name either. Measured: leaving this
    # out, a mutation that registered the union AS :poly2 -- the exact wrong
    # fix this test exists to forbid -- left this test green.
    assert "{ name: :poly2," not in generic
    assert "{ name: :poly2 " not in generic


def test_only_the_two_declared_files_change(tmp_path):
    root = make_pdk(tmp_path)
    before = tree(root)
    mod.main(["--root", str(root), "--expect", "2"])
    after = tree(root)
    assert set(before) == set(after), "the patch added or removed a file"
    changed = sorted(k for k in before if before[k] != after[k])
    assert changed == sorted([mod.DENSITY, mod.GENERIC])


# ------------------------------------------------------- it cannot go quiet


def test_check_mode_writes_nothing_and_reports(tmp_path):
    root = make_pdk(tmp_path)
    before = tree(root)
    assert mod.main(["--root", str(root), "--check", "--expect", "2"]) == 1
    assert tree(root) == before


def test_check_mode_on_an_already_correct_deck_is_clean(tmp_path):
    """rc 0 with nothing to do -- so the check can be used as an assertion."""
    root = make_pdk(tmp_path)
    mod.main(["--root", str(root), "--expect", "2"])
    # already patched: --check must not claim there is work AND must not lie
    # about it succeeding, so it reports the refusal.
    assert mod.main(["--root", str(root), "--check"]) == 2


def test_applying_twice_is_refused(tmp_path):
    """The deck's Context raises on a duplicate derived layer.

    A second application must fail HERE, at build time with a sentence, not at
    DRC time with 'Derived layer poly2_result already defined'.
    """
    root = make_pdk(tmp_path)
    assert mod.main(["--root", str(root), "--expect", "2"]) == 0
    after_first = tree(root)
    assert mod.main(["--root", str(root), "--expect", "2"]) == 2
    assert tree(root) == after_first, "the refused run still wrote something"


def test_a_moved_anchor_fails_instead_of_doing_nothing(tmp_path):
    """A deck this step was not written for must stop the build.

    This is the direction that matters: a patch step that silently matches
    nothing satisfies every post-condition it no longer reaches.
    """
    moved = DENSITY_RB.replace("if (poly2.area / chip_area) * 100 < 14",
                               "if (poly2.area / chip_area) * 100.0 < 14")
    root = make_pdk(tmp_path, density=moved)
    assert mod.main(["--root", str(root), "--expect", "2"]) == 2
    # and the file it COULD have patched is left alone rather than half-done
    assert "poly2_result" not in (root / mod.GENERIC).read_text()


def test_a_duplicated_anchor_fails(tmp_path):
    """Two matching lines means the patch cannot know which one it is for."""
    doubled = DENSITY_RB + DENSITY_RB
    root = make_pdk(tmp_path, density=doubled)
    assert mod.main(["--root", str(root), "--expect", "2"]) == 2


def test_expect_count_fails_in_both_directions(tmp_path):
    root = make_pdk(tmp_path)
    assert mod.main(["--root", str(root), "--check", "--expect", "1"]) == 2
    assert mod.main(["--root", str(root), "--check", "--expect", "3"]) == 2
    assert "poly2_result" not in (root / mod.DENSITY).read_text()


def test_a_missing_deck_is_not_a_clean_run(tmp_path):
    root = make_pdk(tmp_path)
    (root / mod.DENSITY).unlink()
    assert mod.main(["--root", str(root), "--expect", "2"]) == 2
