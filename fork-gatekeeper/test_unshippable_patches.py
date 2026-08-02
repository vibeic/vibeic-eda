"""vibeic-eda#60 — a patch we carry that cannot reach the image.

We fork `ciel`, `open_pdks`, `sv2v` and `IHP-Open-PDK`; the image ships all four
from the BASE image, not from our forks. Nothing is lost today — all four are
byte-identical to upstream — and that is exactly why it is easy to miss.

The defect activates on the FIRST patch: the ledger then reports `ahead=1`, our
patch correctly counted, on a fork the image does not build from, and the row
reads as success. A number that goes UP is the last place anyone looks for a
failure.

    ahead > 0  AND  integrated = false     is a contradiction

PROVEN BY INJECTION, not by reading the code: a copy of the real ledger with
`ahead=1` planted on `sv2v` exits 1 and names it; the unmodified ledger exits 0.
The probe distinguishes a crash from a verdict, because a traceback also exits 1
— an earlier version of this check "passed" on a `Traceback`.

WHAT THIS DOES NOT DECIDE: whether each fork should be wired in or dropped is an
owner call, and the issue says so. This refuses only the third state — tracked,
reported, and unreachable.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "cup", _HERE / "check_unshippable_patches.py")
C = importlib.util.module_from_spec(_spec)
sys.modules["cup"] = C
_spec.loader.exec_module(C)


def _ledger(tmp_path, rows):
    d = tmp_path / "ledger"
    d.mkdir()
    for r in rows:
        (d / f"{r['tool']}.json").write_text(json.dumps(r), encoding="utf-8")
    return d


def test_a_patch_on_an_unintegrated_fork_is_refused(tmp_path):
    """THE DEFECT, in the state it reaches on the first patch."""
    d = _ledger(tmp_path, [{"tool": "sv2v", "ahead": 1, "integrated": False,
                            "role": "SystemVerilog to Verilog"}])
    rc, msg = C.report(d)
    assert rc == 1, msg
    assert "CANNOT REACH THE IMAGE" in msg
    assert "sv2v" in msg


def test_a_patch_on_an_INTEGRATED_fork_is_fine(tmp_path):
    """THE ACCEPT CASE, and the one that matters most: 14 forks carry patches
    that DO ship. If this reddened, every landing would fail."""
    d = _ledger(tmp_path, [{"tool": "yosys", "ahead": 7, "integrated": True}])
    rc, msg = C.report(d)
    assert rc == 0, msg


def test_an_unintegrated_fork_with_NO_patches_is_fine(tmp_path):
    """Today's state for all four. Carrying no patch on a fork the image does
    not build from costs nothing, and refusing it would be refusing the mirrors
    themselves."""
    d = _ledger(tmp_path, [{"tool": "ciel", "ahead": 0, "integrated": False}])
    assert C.report(d)[0] == 0


def test_an_UNMEASURED_ahead_is_not_a_violation_but_is_disclosed(tmp_path):
    """LOAD-BEARING in the other direction. `ahead: null` means nobody measured
    it — turning that into a finding would be "we could not tell" reported as
    "we found something", which is the same error this repo keeps fixing the
    other way round. It is named in the PASS line rather than dropped."""
    d = _ledger(tmp_path, [{"tool": "open_pdks", "ahead": None,
                            "integrated": False}])
    rc, msg = C.report(d)
    assert rc == 0, msg
    assert "could not be put to them" in msg and "open_pdks" in msg


def test_a_missing_ledger_is_rc_2_not_a_pass(tmp_path):
    """An absence rendering as a pass is what this whole class of guard exists
    to stop. No ledger means the question was never put."""
    rc, msg = C.report(tmp_path / "nope")
    assert rc == 2
    assert "could not be put" in msg


def test_an_empty_ledger_directory_is_also_rc_2(tmp_path):
    d = tmp_path / "ledger"
    d.mkdir()
    assert C.report(d)[0] == 2


def test_the_state_path_is_imported_not_respelled():
    """The first version spelled `~/.gatekeeper` while the real state lives at
    `~/.cache/eda-fork-gatekeeper`, so the guard reported "could not be put" on
    a machine where the ledger was sitting right there. Two derivations of
    "where is the state" drift; one does not."""
    src = (_HERE / "check_unshippable_patches.py").read_text(encoding="utf-8")
    assert "gk_state.state_dir()" in src
    # CODE lines only. The first version of this assertion scanned the whole
    # file and failed on the COMMENT that documents the very mistake it guards
    # against — a test red on its own explanation.
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith(("#", '"', "'")))
    assert "expanduser" not in code
    assert "GK_STATE_DIR" not in code, (
        "the state path is being re-derived from the environment here instead "
        "of asked of gk_state")


def test_the_real_ledger_satisfies_the_invariant_today():
    """The measurement that made adding this cheap. If it ever fails here, the
    guard has found a real one on the production ledger."""
    import gk_state
    d = gk_state.state_dir() / "ledger"
    if not d.is_dir():
        return
    rc, msg = C.report(d)
    assert rc in (0, 2), msg
