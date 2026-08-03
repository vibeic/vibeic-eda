"""A fork with no pin that is AHEAD is carrying a patch that cannot ship. #60.

`open_pdks`, `ciel`, `sv2v` and `IHP-Open-PDK` are forks the image does not build
from — no `ARG <TOOL>_REF`, no clone, the base image's copies. All four carry
ZERO patches today, so nothing is lost, and that is exactly why it is easy to
miss: the defect activates on the first patch, and at that moment the ledger
reports `ahead=1` truthfully and the row reads like success.

TWO CONDITIONS, DELIBERATELY SEPARATE:

* not built from ours — a standing, owner-pending state (#60 states both
  resolutions, wire-in or stop-forking, and declines to pick). Recorded as a
  baseline that MAY ONLY SHRINK. Without it the report is permanently rc=1 for
  a reason nobody is acting on, and a report that is always red is one people
  route around — which would hide the condition below on the day it first
  becomes true.
* ahead > 0 with no pin — the contradiction. NO baseline excuses it: being on a
  known list excuses NOT BEING BUILT FROM, not carrying an unshippable patch.

MEASURED, all four directions:

    the four, zero patches            rc=0
    open_pdks ahead=1, no pin         rc=1   "CANNOT SHIP"
    a NEW unpinned fork               rc=1   "newly not built"
    one got pinned (baseline shrank)  rc=0   "baseline shrank"
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_spec = importlib.util.spec_from_file_location(
    "fork_gap_report", _HERE / "fork_gap_report.py")
F = importlib.util.module_from_spec(_spec)
sys.modules["fork_gap_report"] = F
try:
    _spec.loader.exec_module(F)
except SystemExit:
    pass

_FIELDS = dict(integrated=False, ahead=0, image_behind=None, sync_lag=None,
               release_lag=None, pin=None, ours_unshipped=None,
               ours_unshipped_substantive=None, unshipped_commits=[],
               note="", pin_disagreement=None)


def _run(rows, monkeypatch):
    rep = {
        "q1_image_behind_upstream": 0, "q1_forks_behind": 0, "q1_sync_lag": 0,
        "q1_release_lag": 0, "q1_unmeasured": [], "q2_unmeasured_ship": [],
        "q2_forks_not_built_from_ours": [r["tool"] for r in rows
                                         if not r["integrated"]],
        "q2_our_commits_stranded": sum(r.get("ahead") or 0 for r in rows
                                       if not r["integrated"]),
        "q2_ours_past_the_pin": 0, "q2_ours_past_the_pin_substantive": 0,
        "q2_unshipped_commits": [], "rows": rows,
    }
    monkeypatch.setattr(F, "analyse", lambda *a, **k: rep)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = F.main(["--no-fetch"])
    return rc, buf.getvalue()


def _baseline_rows():
    return [dict(_FIELDS, tool=t) for t in sorted(F._UNPINNED_BASELINE)]


# ── the standing state is not a finding ───────────────────────────────────
def test_the_recorded_four_with_no_patches_are_not_a_finding(monkeypatch):
    """THE ACCEPT CASE, and the one that decides whether this can hold: a report
    that is always red is one people route around."""
    rc, _out = _run(_baseline_rows(), monkeypatch)
    assert rc == 0


def test_the_baseline_is_exactly_the_four_the_issue_names():
    assert F._UNPINNED_BASELINE == frozenset(
        {"open_pdks", "ciel", "sv2v", "IHP-Open-PDK"})


# ── the contradiction, which no baseline excuses ──────────────────────────
def test_a_baselined_fork_that_gains_a_patch_still_FAILS(monkeypatch):
    """LOAD-BEARING. Being on the list excuses NOT BEING BUILT FROM. It does not
    excuse carrying a patch that cannot reach a user — and this is the exact
    moment #60 exists to catch, currently unreachable, which is why guarding it
    now is cheap."""
    rows = _baseline_rows()
    rows[0]["ahead"] = 1
    rc, out = _run(rows, monkeypatch)
    assert rc == 1
    assert "CANNOT SHIP" in out
    assert rows[0]["tool"] in out


def test_the_stranded_check_runs_BEFORE_the_baseline_is_consulted():
    """Order matters: if the baseline were consulted first and returned 0, a
    patched-and-unbuilt fork would pass on the strength of being known."""
    src = (_HERE / "fork_gap_report.py").read_text(encoding="utf-8")
    i = src.index("_stranded_rows = [")
    j = src.index("_unpinned = set(", i)
    assert i < j, "the baseline is consulted before the contradiction"


# ── the baseline may only shrink ──────────────────────────────────────────
def test_a_NEW_unpinned_fork_is_a_finding(monkeypatch):
    rows = _baseline_rows() + [dict(_FIELDS, tool="brand_new")]
    rc, out = _run(rows, monkeypatch)
    assert rc == 1 and "newly not built" in out


def test_a_SHRUNK_baseline_is_reported_and_passes(monkeypatch):
    """Shrinking is the outcome this exists to make visible — it must not be
    silent, or the list stays stale after the work is done."""
    rc, out = _run(_baseline_rows()[:-1], monkeypatch)
    assert rc == 0 and "baseline shrank" in out


def test_the_repo_as_committed_passes():
    """The regression: whatever the real state is, it must be the recorded one."""
    import subprocess
    r = subprocess.run([sys.executable, str(_HERE / "fork_gap_report.py"),
                        "--no-fetch"], capture_output=True, text=True,
                       timeout=180, cwd=str(_HERE.parent))
    assert r.returncode == 0, (r.stdout + r.stderr)[-600:]
