#!/usr/bin/env python3
"""vibe-ic#395 — the NDA push guard fired correctly and its alarm was swallowed.

`_nda_block_push` sits before every `git push` in this package, and it works:
the push is skipped, so nothing leaks. What was broken is what happens NEXT.

`prepare_merge_pr._prepare_one` is annotated `-> dict` and every one of its
twelve other exits returns a dict. The guard returned `(False, msg)` — the
same two lines pasted from `pr_notify._open_pr`, whose contract genuinely IS
`(bool, str)`. `gatekeeper.py` then does:

    for r in prepare_merge_pr.prepare(fresh, date):
        print(f"  [merge-pr] {r.get('tool'):16} {r.get('status')} ...")
    except Exception as e:
        print(f"  [merge-pr] error (ignored): {e}")

so an NDA hit raised AttributeError into that broad handler and printed
"error (ignored)" — the operator was told a blocked NDA push was an ignorable
hiccup — AND the `for` loop died at that row, so every remaining fork in the
tick went unreported as well.

A guard is not finished when it blocks the action. It is finished when the
person who needs to act finds out.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _rows_render(rows):
    """Exactly what gatekeeper.py does with each row, including its handler."""
    seen, err = [], None
    try:
        for r in rows:
            seen.append(f"{r.get('tool')} {r.get('status')} "
                        f"{r.get('url') or r.get('note') or ''}")
    except Exception as e:  # noqa: BLE001 — mirrors gatekeeper.py verbatim
        err = f"error (ignored): {e}"
    return seen, err


def test_a_tuple_row_destroys_the_report_and_the_rest_of_the_tick():
    """The defect, reproduced against the consumer rather than asserted."""
    rows = [{"tool": "yosys", "status": "opened"},
            (False, "NDA token found in the diff — push aborted"),
            {"tool": "magic", "status": "opened"}]
    seen, err = _rows_render(rows)
    assert err and "ignored" in err
    assert len(seen) == 1, "rows after the tuple were never reported"
    assert not any("NDA" in s for s in seen), \
        "the operator never sees why the push was blocked"


def test_the_dict_row_reports_the_block_and_the_tick_continues():
    """The fix. Same three rows, the guard shaped like its siblings."""
    rows = [{"tool": "yosys", "status": "opened"},
            {"tool": "klayout", "status": "nda_blocked",
             "note": "NDA token found in the diff — push aborted"},
            {"tool": "magic", "status": "opened"}]
    seen, err = _rows_render(rows)
    assert err is None
    assert len(seen) == 3, "a blocked fork must not silence the others"
    assert any("nda_blocked" in s and "NDA token" in s for s in seen)


def test_the_source_returns_a_dict_on_the_guard_path():
    """Read from the SOURCE, not from a re-implementation: every `return` in
    `_prepare_one` must be a dict, or the consumer contract is broken again by
    the next paste."""
    import ast
    src = (Path(__file__).resolve().parent / "prepare_merge_pr.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_prepare_one")
    bad = [n.lineno for n in ast.walk(fn)
           if isinstance(n, ast.Return) and n.value is not None
           and not isinstance(n.value, ast.Dict)]
    assert not bad, f"_prepare_one returns a non-dict at line(s) {bad}"


def test_pr_notify_keeps_its_own_tuple_contract():
    """The paired half, and why this is not a blanket rule: `_open_pr` really
    does return (bool, str), so the SAME two lines are correct there. Changing
    it to a dict would break that consumer instead."""
    import ast
    src = (Path(__file__).resolve().parent / "pr_notify.py").read_text()
    tree = ast.parse(src)
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
           and any(isinstance(x, ast.Return) and isinstance(x.value, ast.Tuple)
                   for x in ast.walk(n))]
    assert fns, "pr_notify no longer returns tuples — re-check the guard shape"
