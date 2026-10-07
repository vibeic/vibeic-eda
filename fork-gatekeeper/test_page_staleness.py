"""The public page retains its completed snapshot until a new round publishes.

Owner directive, 2026-10-07: no browser-clock age suffix, stale banner, or long
stale-tool lists. Keep the absolute snapshot time and unknown measurement state.
These tests execute the real emitted JavaScript at clocks beyond 30 hours;
publication authority and freshness remain in the backend boundary tests.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from test_page_ages_each_count import _build, _render, _row, _stamp, NOW

_HERE = pathlib.Path(__file__).resolve().parent


@pytest.mark.parametrize("hours", [31, 48, 168])
def test_public_snapshot_does_not_age_in_the_viewers_browser(tmp_path, hours):
    from datetime import datetime, timedelta
    page = _build(tmp_path, [_row("alpha", 7, _stamp(2))])
    first = _render(page, NOW)
    later = _render(page, (datetime.fromisoformat(NOW) + timedelta(hours=hours)).isoformat())
    assert later == first, "the completed snapshot mutated with the viewer's clock"
    assert "2026-08-05 08:30" in later["forkMetrics"]
    assert "<b>7</b>" in later["forkGap"]
    shown = "\n".join(later.values())
    assert " h old" not in shown and " d old" not in shown
    assert "are STALE" not in shown and "UNKNOWN-AGE" not in shown
    assert "fork-age" not in shown and "is-stale" not in shown


def test_public_snapshot_preserves_unmeasured_counts(tmp_path):
    page = _build(tmp_path, [_row("alpha", 7, _stamp(2)), _row("unmeasured", None, None)])
    rendered = _render(page, "2026-08-12T09:00:00+08:00")
    assert "+1?" in rendered["forkMetrics"], rendered["forkMetrics"]
    assert "<b>7</b>" in rendered["forkGap"]


def test_full_emitted_snapshot_is_clock_independent(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable; the subject is the emitted JavaScript")
    page = _build(tmp_path, [_row("alpha", 7, _stamp(2))])
    result = subprocess.run([node, str(_HERE / "jstests" / "staleness.mjs"), str(page)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_page_has_no_clock_driven_stale_ui():
    source = (_HERE / "build_page.py").read_text(encoding="utf-8")
    assert 'id="forkStale"' not in source
    assert "Date.now()" not in source
    assert "fork-age-stale" not in source and "fork-kpi.is-stale" not in source
