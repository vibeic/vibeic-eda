"""A via patch narrower than its own layer's minimum width (vibe-ic#768).

The sky130 HD tech LEF declares `LAYER met5 ... WIDTH 1.6 ;` and, 430 lines
later, gives all five M4M5 vias a met5 patch of `RECT -0.71 -0.71 0.71 0.71` =
1.42 um. Where a met5 wire ENDS on such a via the patch protrudes past the wire
end and the protrusion is 1.42 um wide, which the sign-off deck reports as
`m5.1 : min. m5 width : 1.6um`.

MEASURED against the shipped deck (`sky130A.lydrc` out of
ghcr.io/vibeic/vibeic-eda:0.2.58 — image-version:history, the image the DRC was
run on, not a pointer that may be bumped), on synthetic met4/via4/met5 geometry
that reproduces one reported site:

    via at a wire END,   patch 1.42   -> items=3 {'m5.1': 3}
    via at a wire END,   patch 1.60   -> items=0
    via MID-wire,        patch 1.42   -> items=0     <- not vias in general
    wire EXTENDED over,  patch 1.42   -> items=0     <- not the patch alone

so the violation is specifically the protrusion, and growing the patch to the
layer's own minimum removes it without introducing anything.

These tests pin the TRANSFORM. They do not need a PDK: the fixture carries the
same contradiction in miniature.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROG = HERE.parent / "tools" / "pdk" / "pdk_via_min_width_patch.py"

_spec = importlib.util.spec_from_file_location(
    "pdk_via_min_width_patch", str(PROG))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# The sky130 contradiction, reduced to the two blocks that carry it.
DEFECTIVE = """\
LAYER via4
  TYPE CUT ;
  WIDTH 0.8 ;
  ENCLOSURE ABOVE 0.31 0.31 ;
END via4

LAYER met5
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  WIDTH 1.6 ;            # Met5 1
  AREA 4 ;
END met5

VIA M4M5_PR DEFAULT
  LAYER via4 ;
  RECT -0.4 -0.4 0.4 0.4 ;
  LAYER met4 ;
  RECT -0.59 -0.59 0.59 0.59 ;
  LAYER met5 ;
  RECT -0.71 -0.71 0.71 0.71 ;
END M4M5_PR

VIARULE M4M5_PR GENERATE
  LAYER met4 ;
  ENCLOSURE 0.19 0.19 ;
  LAYER met5 ;
  ENCLOSURE 0.31 0.31 ;
  LAYER via4 ;
  RECT -0.4 -0.4 0.4 0.4 ;
  SPACING 1.6 BY 1.6 ;
END M4M5_PR
"""


def test_the_patch_reaches_the_layers_own_minimum():
    new, changes = mod.patch(DEFECTIVE)
    assert "RECT -0.8 -0.8 0.8 0.8 ;" in new, changes
    assert "RECT -0.71" not in new
    assert len(changes) == 2, changes


def test_the_viarule_enclosure_follows_the_same_arithmetic():
    """0.8 cut + 2*0.4 = 1.6, the layer's own WIDTH."""
    new, _ = mod.patch(DEFECTIVE)
    assert "  ENCLOSURE 0.4 0.4 ;" in new
    # the met4 enclosure is untouched: met4's WIDTH is not declared here, and a
    # layer the file says nothing about must not be rewritten from a guess.
    assert "  ENCLOSURE 0.19 0.19 ;" in new


def test_the_grown_enclosure_still_clears_the_via_layers_own_minimum():
    """The original 0.31 was presumably minimised against `ENCLOSURE ABOVE
    0.31` on via4, which states a MINIMUM. 0.40 >= 0.31, so growing cannot
    break the rule the small value was chosen for."""
    new, _ = mod.patch(DEFECTIVE)
    enc = [l for l in new.splitlines() if "ENCLOSURE 0.4 0.4" in l]
    assert enc, new
    assert 0.4 >= 0.31


def test_it_is_idempotent():
    once, _ = mod.patch(DEFECTIVE)
    twice, changes = mod.patch(once)
    assert twice == once
    assert changes == []


def test_a_clean_file_is_left_byte_identical():
    clean, _ = mod.patch(DEFECTIVE)
    again, changes = mod.patch(clean)
    assert again == clean and not changes


def test_a_layer_the_file_does_not_declare_as_ROUTING_is_never_touched():
    """The cut layer's own RECT is the via hole, not a patch. Growing it would
    change the via itself."""
    new, _ = mod.patch(DEFECTIVE)
    assert new.count("RECT -0.4 -0.4 0.4 0.4 ;") == 2


def test_the_layer_filter_restricts_the_blast_radius():
    """gf180's Metal2/Metal3 carry the same shape on far denser layers. The
    step must be able to say which layers it was measured for."""
    _new, changes = mod.patch(DEFECTIVE, only={"met4"})
    assert changes == [], changes


def test_a_narrow_patch_is_detected_on_EITHER_axis():
    """A patch can be legal along the wire and short across it."""
    src = DEFECTIVE.replace("RECT -0.71 -0.71 0.71 0.71 ;",
                            "RECT -0.8 -0.71 0.8 0.71 ;")
    new, changes = mod.patch(src)
    assert "RECT -0.8 -0.8 0.8 0.8 ;" in new, changes


def test_values_land_on_the_manufacturing_grid():
    """A layer whose WIDTH is not an exact multiple of the grid must round UP,
    never down onto an illegal-but-tidy number."""
    src = DEFECTIVE.replace("WIDTH 1.6 ;            # Met5 1",
                            "WIDTH 1.607 ;          # Met5 1")
    new, _ = mod.patch(src, grid=0.005)
    assert "RECT -0.805 -0.805 0.805 0.805 ;" in new, new


def test_negative_control_a_wide_enough_patch_is_not_reported():
    src = DEFECTIVE.replace("RECT -0.71 -0.71 0.71 0.71 ;",
                            "RECT -0.9 -0.9 0.9 0.9 ;")
    src = src.replace("  ENCLOSURE 0.31 0.31 ;", "  ENCLOSURE 0.5 0.5 ;")
    _new, changes = mod.patch(src)
    assert changes == [], changes


def test_the_dockerfile_actually_runs_the_step():
    """A transform nothing invokes is a file. vibeic-eda#80 is this shape."""
    df = (HERE.parent / "Dockerfile").read_text(encoding="utf-8")
    assert "pdk_via_min_width_patch.py" in df, (
        "the image build does not apply the PDK via patch, so the shipped "
        "sky130A still carries the 1.42um met5 patch")
    assert "--expect" in df, (
        "the step does not assert how many changes it made; a patch that "
        "silently stops matching would pass by doing nothing")


def test_the_build_context_admits_the_script():
    """`.dockerignore` is `*` plus an allow-list. A COPY of a path the context
    excludes fails the build — but only at build time, not here, so pin it."""
    di = (HERE.parent / ".dockerignore").read_text(encoding="utf-8")
    assert any(l.strip() in ("!tools", "!tools/", "!tools/pdk", "!tools/pdk/")
               for l in di.splitlines()), di
    assert any(l.strip() in ("!tools/pdk/pdk_via_min_width_patch.py",
                             "!tools/pdk", "!tools/pdk/", "!tools", "!tools/")
               for l in di.splitlines()), di
