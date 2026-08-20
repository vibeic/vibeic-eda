"""Step order was compared across producers that do not agree on precision.

`fork_gap_report` writes a full `isoformat()`. `fork_github_convergence`,
`daily_0530`, `discover_forks` and `gatekeeper` write
`isoformat(timespec="seconds")`. Comparing the two with `>` reads the digits the
second producer never wrote as zeros, so a step that genuinely ran AFTER another
records an earlier-LOOKING stamp whenever both land in the same second.

MEASURED on the round of 2026-08-21:

    fork_gap.completed_at   2026-08-20T20:36:07.244550+00:00
    github.started_at       2026-08-21T04:36:07+08:00   == 20:36:07.000000Z

The same second. The round reported "GitHub convergence started before fork-gap
collection finished" and carried that source error round after round with
nothing whatsoever out of order.

Comparing at the finer precision is not a stricter check — it is an invalid one.
"""
from __future__ import annotations

import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capture_release_ll as LL  # noqa: E402

GAP_DONE = "2026-08-20T20:36:07.244550+00:00"
GH_START = "2026-08-21T04:36:07+08:00"


def test_the_measured_phantom_is_not_out_of_order():
    """The exact pair that fired. Same second, one stamp carrying a fraction and
    one carrying none — that is not evidence of anything running early."""
    pair = LL._at_shared_precision(GAP_DONE, GH_START)
    assert pair is not None
    assert not pair[0] > pair[1], (
        "a same-second sequence still reads as a violation")


def test_a_real_violation_still_fires():
    """THE DIRECTION THAT MAKES IT SAFE. Widening until it stops firing is not a
    fix: a step starting a second before the previous one finished is real, is
    visible at second precision, and must still be caught."""
    pair = LL._at_shared_precision(GAP_DONE, "2026-08-21T04:36:06+08:00")
    assert pair is not None and pair[0] > pair[1]


def test_precision_is_kept_when_both_stamps_carry_it():
    """Truncation is the price of the coarser producer, not a blanket policy. If
    both sides write microseconds, a sub-second violation is real evidence and
    is not thrown away."""
    pair = LL._at_shared_precision("2026-08-20T20:36:07.244550+00:00",
                                   "2026-08-20T20:36:07.244549+00:00")
    assert pair is not None and pair[0] > pair[1]


def test_absent_digits_are_not_zeros():
    """The whole bug in one assertion: `...:07+08:00` does not mean `...:07.000000`,
    it means the producer did not measure that far."""
    assert LL._carries_subsecond(GAP_DONE)
    assert not LL._carries_subsecond(GH_START)
    assert not LL._carries_subsecond("2026-08-21T04:36:07Z")
    assert LL._carries_subsecond("2026-08-21T04:36:07,244550+08:00")


def test_an_unreadable_stamp_is_not_silently_in_order():
    """A missing or malformed stamp must not resolve to "fine" — the caller has
    to see that it could not be judged."""
    assert LL._at_shared_precision(None, GH_START) is None
    assert LL._at_shared_precision(GAP_DONE, "not a time") is None
    assert LL._at_shared_precision(GAP_DONE, "2026-08-21T04:36:07") is None


def _raw_strict_when_comparisons(module: str) -> list[int]:
    """Lines where two parsed stamps are compared without passing through the
    shared-precision helper — the shape that produced the phantom."""
    tree = ast.parse((HERE / module).read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        # ORDER only. `_strict_when(x) is None` asks whether a stamp parsed at
        # all, which is not a comparison between two clocks and carries no
        # precision hazard — flagging it would train the reader to ignore this.
        if not any(isinstance(op, (ast.Gt, ast.GtE, ast.Lt, ast.LtE))
                   for op in node.ops):
            continue
        operands = [node.left, *node.comparators]
        for operand in operands:
            for inner in ast.walk(operand):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "_strict_when"):
                    bad.append(node.lineno)
                elif (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "_strict_when"):
                    bad.append(node.lineno)
    return sorted(set(bad))


def test_neither_consumer_compares_stamps_behind_the_helper_s_back():
    """BOTH consumers run these four order checks, so a fix applied to one is a
    fix that drifts. Nothing may compare `_strict_when` output directly."""
    for module in ("capture_release_ll.py", "build_page.py"):
        assert not _raw_strict_when_comparisons(module), (
            f"{module} compares parsed stamps without a shared precision")
