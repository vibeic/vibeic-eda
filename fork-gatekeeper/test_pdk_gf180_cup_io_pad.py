"""vibeic-eda#214 -- the gf180mcuD sign-off deck fires CUP.2/CUP.3 on the PDK's
own IO pad cells.

`rule_decks/cup.rb` takes the WHOLE layout's metal as the subject of the two
bond-pad rules, so the foundry IO library's own qualified, slotted bond-pad
metal, inside its own Pad (37/0) opening, is reported against itself: 87 CUP.3
per pad-bearing `gf180mcu_fd_io` master checked ALONE and unmodified, and 87 per
bond pad on a die.

Two programs answer it and both are tested here:

  pdk_gf180_cup_io_pad_patch.py   rewrites the deck, and REFUSES rather than
                                  guesses when the deck is not the one it was
                                  written against.
  pdk_gf180_cup_check.py          runs the deck both directions and is the
                                  build-time gate. Its verdict logic is tested
                                  here against recorded klayout output; the real
                                  DRC runs happen in the image build, where there
                                  is a PDK (it returns rc=2, NOT_MEASURED, when
                                  there is not).

These tests build miniature deck trees. They need no PDK and no image: what they
pin is the DECISION each program makes, including the decisions that must stay
NEGATIVE. A check that cannot fail is not a check, so every green case below has
a red one that differs by one thing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PDK_TOOLS = HERE.parent / "tools" / "pdk"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, str(PDK_TOOLS / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


patch_mod = _load("pdk_gf180_cup_io_pad_patch")
check_mod = _load("pdk_gf180_cup_check")

# The shipped rule_decks/cup.rb, reduced to the parts the step anchors on. This
# is the upstream file at globalfoundries-pdk-libs-gf180mcu_fd_pv 035e6d1f, which
# is byte-identical to the copy the ciel volume carries.
CUP_AS_SHIPPED = """\
# frozen_string_literal: true

METAL_MAP_CUP = {
  1 => { metal_drawn: 'metal1_drawn' },
  2 => { metal_drawn: 'metal2_drawn' },
  3 => { metal_drawn: 'metal3_drawn' },
  4 => { metal_drawn: 'metal4_drawn' },
  5 => { metal_drawn: 'metal5_drawn' },
  6 => { metal_drawn: 'metaltop_drawn' }
}.freeze

GF180_DRC_REGISTRY.register(
  id: File.basename(__FILE__, File.extname(__FILE__)),
  path: __FILE__,
  priority: 0,
  tags: %w[all beol cup]
) do
  (1..6).each do |lvl|
    next unless ctx.metal_level_numerical >= lvl

    metal_drawn = ctx[METAL_MAP_CUP[lvl][:metal_drawn]]

    logger.info('Executing rule CUP.2')
    cup2_l1 = metal_drawn.interacting(pad).not_interacting(guard_ring_mk).width(1.um).interacting(pad)
    cup2_l1.output('CUP.2', "CUP.2 : Minimum width of Metal#{lvl} line used for bond pads: 1")
    cup2_l1.forget

    logger.info('Executing rule CUP.3')
    cup3_l1 = metal_drawn.interacting(pad).not_interacting(guard_ring_mk).notch(1.um).interacting(pad)
    cup3_l1.output('CUP.3', "CUP.3 : Minimum space of Metal#{lvl} line used for bond pads (slots): 1")
    cup3_l1.forget
  end
end
"""


def _pdk(tmp_path: Path, cup_text: str = CUP_AS_SHIPPED) -> Path:
    root = tmp_path / "gf180mcuD"
    p = root / patch_mod.CUP
    p.parent.mkdir(parents=True)
    p.write_text(cup_text)
    return root


# --------------------------------------------------------------- the patch ---

def test_the_patch_changes_the_subject_and_nothing_else(tmp_path):
    root = _pdk(tmp_path)
    assert patch_mod.main(["--root", str(root), "--expect", "2"]) == 0
    text = (root / patch_mod.CUP).read_text()

    # the subject changed ...
    assert "CUP_IO_PAD_MASTER_CELLS = 'gf180mcu_fd_io__*'" in text
    assert "shapes.unselect_cells(CUP_IO_PAD_MASTER_CELLS)" in text
    # the exemption is the CELL MASTER, and the Pad layer is NOT filtered:
    # design metal inside a foundry cell's own bond-pad opening stays the
    # subject. A filter applied to `pad` instead would empty the rule.
    assert "unselect_cells" not in text.split("logger.info('Executing rule CUP.2')")[1]
    assert "ctx[METAL_MAP_CUP[lvl][:metal_drawn]]" not in text

    # ... and NOTHING ELSE did. The rules keep every qualifier they had, the
    # 1 um limits included: this is a correction to what the rule looks at, not
    # a relaxation of what it demands.
    for rule in (
        "cup2_l1 = metal_drawn.interacting(pad).not_interacting(guard_ring_mk)"
        ".width(1.um).interacting(pad)",
        "cup3_l1 = metal_drawn.interacting(pad).not_interacting(guard_ring_mk)"
        ".notch(1.um).interacting(pad)",
    ):
        assert rule in text


def test_a_second_application_is_refused_by_the_marker_it_left(tmp_path, capsys):
    root = _pdk(tmp_path)
    assert patch_mod.main(["--root", str(root), "--expect", "2"]) == 0
    capsys.readouterr()
    before = (root / patch_mod.CUP).read_text()
    assert patch_mod.main(["--root", str(root), "--expect", "2"]) == 2
    err = capsys.readouterr().err
    # refused BY THE MARKER, not incidentally by the anchor having moved: the
    # message must name what it found.
    assert patch_mod.MARK in err and "already carries" in err
    assert (root / patch_mod.CUP).read_text() == before


def test_a_deck_that_already_carries_a_correction_is_refused_not_guessed_at(tmp_path):
    """The day upstream fixes this, the step must stop the build, not apply
    somewhere else."""
    upstream_ish = CUP_AS_SHIPPED.replace(
        "  1 => { metal_drawn: 'metal1_drawn' },",
        "  1 => { metal_drawn: 'metal1_drawn', io: true },")
    root = _pdk(tmp_path, upstream_ish)
    assert patch_mod.main(["--root", str(root), "--expect", "2"]) == 2
    assert (root / patch_mod.CUP).read_text() == upstream_ish


def test_a_moved_rule_body_is_refused(tmp_path):
    moved = CUP_AS_SHIPPED.replace(
        "    metal_drawn = ctx[METAL_MAP_CUP[lvl][:metal_drawn]]",
        "    metal_drawn = ctx[METAL_MAP_CUP[lvl][:metal_drawn]].merged")
    root = _pdk(tmp_path, moved)
    assert patch_mod.main(["--root", str(root), "--expect", "2"]) == 2


def test_a_missing_deck_is_rc2_not_a_silent_success(tmp_path):
    assert patch_mod.main(["--root", str(tmp_path / "nothing-here"),
                           "--expect", "2"]) == 2


def test_check_mode_writes_nothing(tmp_path):
    root = _pdk(tmp_path)
    before = (root / patch_mod.CUP).read_text()
    assert patch_mod.main(["--root", str(root), "--check"]) == 1
    assert (root / patch_mod.CUP).read_text() == before


def test_the_wrong_expect_count_fails_the_step(tmp_path):
    root = _pdk(tmp_path)
    assert patch_mod.main(["--root", str(root), "--expect", "3"]) == 2


# --------------------------------------------------------------- the check ---
# `_counts` is what turns a klayout run into a verdict. Recorded output, from
# this lane's runs against the pinned image.

SHIPPED_DECK_LOG = """\
2026-09-20 12:55:13 +0200: Memory Usage (4912932K) : DRC RESULT: FAILURE (87 violation(s))
2026-09-20 12:55:13 +0200: Memory Usage (4912932K) : Violated Rules:
2026-09-20 12:55:13 +0200: Memory Usage (4912932K) :   'CUP.3'              :    87 violation(s)
"""

CLEAN_LOG = """\
2026-09-20 12:59:05 +0200: Memory Usage (4913788K) : DRC RESULT: SUCCESS (0 violations)
"""

BOTH_RULES_LOG = """\
2026-09-20 13:00:29 +0200: Memory Usage (4912908K) : DRC RESULT: FAILURE (88 violation(s))
2026-09-20 13:00:29 +0200: Memory Usage (4912908K) :   'CUP.2'              :     1 violation(s)
2026-09-20 13:00:29 +0200: Memory Usage (4912908K) :   'CUP.3'              :    87 violation(s)
"""


def test_counts_reads_the_rule_names_and_numbers_klayout_prints():
    assert check_mod._counts(SHIPPED_DECK_LOG) == {"CUP.3": 87}
    assert check_mod._counts(CLEAN_LOG) == {}
    assert check_mod._counts(BOTH_RULES_LOG) == {"CUP.2": 1, "CUP.3": 87}


def test_a_clean_log_is_zero_and_an_unparsable_one_is_not_zero():
    """'Could not read it' must never be reported as 'read it and it was
    clean' -- the two have the same count and opposite meanings."""
    assert check_mod._counts(CLEAN_LOG).get("CUP.3", 0) == 0
    assert "DRC RESULT" in CLEAN_LOG
    assert "DRC RESULT" not in "klayout: command not found"


def test_the_check_reports_NOT_MEASURED_on_a_root_that_has_no_deck(tmp_path, capsys):
    assert check_mod.main(["--root", str(tmp_path)]) == 2
    assert "NOT_MEASURED" in capsys.readouterr().err


@pytest.mark.parametrize("missing", [check_mod.DECK, check_mod.IO_GDS])
def test_each_input_the_check_needs_is_named_when_it_is_absent(tmp_path, missing, capsys):
    root = tmp_path / "gf180mcuD"
    for rel in (check_mod.DECK, check_mod.IO_GDS):
        if rel == missing:
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    assert check_mod.main(["--root", str(root)]) == 2
    err = capsys.readouterr().err
    assert "NOT_MEASURED" in err and Path(missing).name in err


def test_a_missing_klayout_is_NOT_MEASURED_not_a_clean_run(tmp_path, capsys, monkeypatch):
    """The check runs the deck. Where it cannot, it must say so: a tool it could
    not start is not a DRC result."""
    root = tmp_path / "gf180mcuD"
    for rel in (check_mod.DECK, check_mod.IO_GDS):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    monkeypatch.setattr(check_mod.shutil, "which", lambda _name: None)
    assert check_mod.main(["--root", str(root)]) == 2
    err = capsys.readouterr().err
    assert "NOT_MEASURED" in err and "klayout" in err
