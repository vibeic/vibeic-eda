"""A row must not answer one question with two contradicting halves.

WHY THIS EXISTS (measured 2026-08-06)
=====================================
The target-direction refusal was appended to every row that had one. Regenerating
the daily report produced four rows shaped like this:

    | gtkwave | CLEAN | on the latest upstream release (v3.3.116)
                        TARGET REFUSED: `v3.3.116` — ... our pin is 491 commit(s)
                        ahead of it ... advancing to it would be a DOWNGRADE |

Both halves are TRUE and they answer DIFFERENT questions:

  * CLEAN is release-level — "is there a newer tag than the one we are on?" No.
  * the refusal is directional — "would advancing to that tag drop work?" Yes,
    because we track the branch and this tag sits off it.

But a CLEAN row PROPOSES NOTHING, so there is no target for the refusal to refuse.
Printed together in one cell it reads as a single sentence disagreeing with
itself, and a reader cannot tell which half to act on. That is the same shape as
the defects this report exists to surface: an answer that cannot be checked
because it is really two answers wearing one label.

WHAT THIS ASSERTS
-----------------
The invariant, on the real rendered report: no row carries both a CLEAN verdict
and a target refusal. Not "gtkwave is fine" — pinning today's four tools would
need editing every time a pin moves, and a test that must be edited on every
release is one people edit without reading.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _rows(md: str):
    """(tool, verdict, note) for every data row of the verdict table."""
    out = []
    for ln in md.splitlines():
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if cells[0] in ("Tool", "---") or set(cells[0]) <= {"-", ":"}:
            continue
        out.append((cells[0], cells[1], " ".join(cells[2:])))
    return out


def _render(summary: dict) -> str:
    import gatekeeper as G
    return G._report_md(summary)


def _real_summary():
    """The most recent summary the tick actually wrote, or skip.

    Deliberately NOT a hand-built dict. My first draft invented one with an
    `entries` key; the real shape uses `results`, so `_report_md` raised
    `CountsUnavailable` on BOTH the fixed and the pristine tree -- a test that was
    red everywhere and therefore discriminated nothing. It was measuring my mock,
    not the program.
    """
    import gatekeeper as G
    d = Path(G.REPORTS)
    js = sorted(d.glob("*.json")) if d.is_dir() else []
    if not js:
        pytest.skip("no summary written on this host yet")
    import json as _j
    return _j.loads(js[-1].read_text())


def _with_rows(summary, rows):
    """The real summary with its result rows replaced, so the RENDERER is under
    test and the surrounding structure is whatever the program really produces."""
    from collections import Counter
    s = dict(summary)
    s["results"] = rows
    c = Counter(r.get("verdict") for r in rows)
    s["counts"] = {k: c.get(k, 0) for k in
                   ("MERGED", "DEFERRED", "RESOLVED", "UNMEASURABLE",
                    "CLEAN", "NOT_LAYERED")}
    return s


def test_the_suppression_lives_where_the_note_is_built():
    """THE REGRESSION, asserted at the layer that can actually fix it.

    My first draft handed `_report_md` a row whose note ALREADY contained the
    refusal and expected the renderer to strip it. That tests the wrong layer:
    the renderer's job is to print the note it is given, and stripping text there
    would mean the JSON and the markdown disagree about what the row says. The
    suppression belongs where the note is COMPOSED, in `tick()`.

    So this reads the composition site: the refusal must be appended under a
    condition that excludes CLEAN. Source-level, because the alternative is to
    run a full tick (network, clones, ~2 min) inside a unit test.
    """
    src = (HERE / "gatekeeper.py").read_text(encoding="utf-8")
    lines = src.splitlines()

    # SCAN THE WHOLE FILE. Three earlier drafts of this test carved a window out
    # of the source first -- by character offset, then between two landmarks --
    # and each time the window silently excluded the line being asserted about,
    # so the test was red on a tree where the guard was present. A window is an
    # assumption; the file is a fact.
    appends = [i for i, ln in enumerate(lines)
               if 'entry["note"] += entry_target_refusal' in ln]
    assert appends, "the refusal is no longer appended to the note at all"

    for at in appends:
        preceding = [ln for ln in lines[max(0, at - 4):at]
                     if ln.strip() and not ln.strip().startswith("#")]
        assert any("CLEAN" in ln for ln in preceding), (
            f"line {at + 1} appends the target refusal to EVERY row, including "
            f"CLEAN ones. A CLEAN row proposes no target, so the refusal "
            f"contradicts it in one cell -- measured on the 2026-08-06 report, "
            f"four rows read 'CLEAN ... TARGET REFUSED'. "
            f"Preceding code lines: {preceding!r}")


def test_the_refusal_still_appears_where_a_target_IS_proposed():
    """BIDIRECTIONAL CONTROL. Suppressing the clash must not suppress the
    refusal — a fix that simply deleted it would pass the test above."""
    rows = [
        {"tool": "Trilinos", "verdict": "DEFERRED", "new_releases": 3,
         "latest_release": "trilinos-release-17-1-1",
         "note": "3 new release(s) → trilinos-release-17-1-1."
                 " TARGET REFUSED: `trilinos-release-17-1-1` — our pin is 450"
                 " commit(s) ahead of it"},
    ]
    md = _render(_with_rows(_real_summary(), rows))
    got = [(t, v, "TARGET REFUSED" in n) for t, v, n in _rows(md)]
    assert any(v == "DEFERRED" and refused for _, v, refused in got), (
        f"a DEFERRED row that proposes a downgrade no longer says so: {got}. "
        f"The refusal is the whole point on rows that DO offer a target.")


def test_the_real_report_has_no_self_contradicting_row():
    """The fleet-shaped assertion, against the report the tick last wrote."""
    import gatekeeper as G
    reports = sorted(Path(G.REPORTS).glob("*.md")) if Path(G.REPORTS).is_dir() else []
    if not reports:
        pytest.skip("no rendered report on this host")
    md = reports[-1].read_text(errors="replace")
    bad = [(t, v) for t, v, n in _rows(md)
           if v == "CLEAN" and "TARGET REFUSED" in n]
    assert not bad, (
        f"{reports[-1].name}: {bad} claim CLEAN and a refused target at once")
