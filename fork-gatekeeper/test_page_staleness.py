"""vibeic-eda#58 — two days of total publishing failure produced no signal.

The daily tick exited 1 on both steps, into a log nobody reads. The ledger simply
stopped advancing, which from the OUTPUT is indistinguishable from "upstream had
no changes" — and the only visible symptom was this page quietly describing an
older world, noticed by a person.

WHY THE CHECK RUNS IN THE VIEWER'S BROWSER. The page is rebuilt BY the tick, so a
staleness check evaluated while building it is a check inside the process that
stopped running: it would never render on the mornings that matter. Computing the
age when the page is VIEWED works even when nothing has rebuilt it for a week.

WHY THESE TESTS SHELL OUT TO NODE. The rule is JavaScript in the emitted page. A
Python re-implementation would test the re-implementation; `jstests/staleness.mjs`
EXTRACTS the shipped expressions out of `build_page.py` and evaluates those, with
`Date.now` pinned so the cases are deterministic.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

_HERE = pathlib.Path(__file__).resolve().parent


def _node():
    return shutil.which("node")


def test_the_shipped_staleness_rule_behaves(capsys):
    """Six cases, including the two that must NEVER read as fresh: a
    clock-skewed future stamp and an unparseable one."""
    node = _node()
    if not node:
        return
    r = subprocess.run([node, str(_HERE / "jstests" / "staleness.mjs"),
                        str(_HERE / "build_page.py")],
                       capture_output=True, text=True, timeout=120)
    print(r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_page_carries_the_banner_element():
    """The rule can be right and render nowhere. `forkStale` is the element the
    rule unhides, and it must exist in the emitted HTML."""
    src = (_HERE / "build_page.py").read_text(encoding="utf-8")
    assert 'id="forkStale"' in src
    assert 'staleEl.hidden = false' in src


def test_the_threshold_is_stated_with_its_limitation():
    """LOAD-BEARING as documentation. A timestamp cannot distinguish "ran and
    found nothing" from "did not run" until enough time has passed that no
    successful round could have left data this old — so this fires the morning
    AFTER a missed round, and the comment must keep saying so. A future reader
    tightening the threshold to "catch it sooner" would only add false alarms."""
    src = (_HERE / "build_page.py").read_text(encoding="utf-8")
    seg = src[src.index("THRESHOLD 30 h"):]
    seg = seg[:seg.index("const stale")]
    assert "morning AFTER a missed round" in seg
    assert "2026-08-01T22:51" in seg, "the incident it was measured against"
