#!/usr/bin/env python3
"""Measurement metadata is retained; the public snapshot does not age itself.

The historical #91 regression introduced measurement-time provenance. That
producer contract remains intact: a count and its measurement stamp travel
together, and an unmeasured count has a null stamp, never borrowed generated_at.

The owner changed the public display contract on 2026-10-07: present the last
completed snapshot with its absolute check time, not browser-clock warnings.
Actual emitted-page tests below cover recent, old, absent, invalid and future
measurement metadata without fabricating values. The separate certified-boundary
suite still refuses incomplete or expired evidence before page replacement.
Local previews here run with empty PATH and never publish to the real site.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gk_state  # noqa: E402

# The instant every page in this file is "viewed" at. Fixed, so the verdicts are
# arithmetic rather than a race with the wall clock.
NOW = "2026-08-05T09:00:00+08:00"

#: Ages, in hours before NOW, chosen either side of the 30 h threshold.
FRESH_H, AGED_H = 2, 45


def _stamp(hours_before: float) -> str:
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(NOW) - timedelta(hours=hours_before)).isoformat(timespec="seconds")


def _row(tool: str, behind: int, measured_at: str | None, *, omit_stamp: bool = False) -> dict:
    """One ledger row shaped like the real ones, in the state under test.

    `ahead: 0` puts it in BOTH places a count is published: the summed headline and
    the per-tool tracking-gap list.
    """
    d = {
        "tool": tool, "role": "test", "upstream": f"up/{tool}",
        "upstream_url": f"https://github.com/up/{tool}",
        "fork_url": f"https://github.com/vibeic/{tool}",
        "image_version": "0.2.63",
        # DELIBERATELY FRESH ON EVERY ROW, including the ones under test. If the page
        # ever falls back to this field, the stale row and the unstamped row would
        # both read as measured a minute ago and these tests go red.
        "generated_at": _stamp(0.5),
        "pinned_ref": "0" * 12, "pin_kind": "pin", "integrated": True,
        "ahead": 0, "behind_commits": behind,
        "sync_lag": behind, "release_lag": 0, "lag_split_exact": True,
    }
    if not omit_stamp:
        d["behind_measured_at"] = measured_at
    return d


def _build(tmp: Path, rows: list[dict]) -> Path:
    """Write a ledger, build the page from it, return the HTML path."""
    led = tmp / "state" / "ledger"
    led.mkdir(parents=True, exist_ok=True)
    for r in rows:
        (led / f"{r['tool']}.json").write_text(json.dumps(r, indent=2) + "\n", encoding="utf-8")
    out = tmp / "page.html"
    env = dict(os.environ)
    env.update({"PATH": str(tmp / "nobin"), "GK_STATE_DIR": str(tmp / "state"),
                "GK_INVENTORY_IMAGE": "absent:test"})
    (tmp / "nobin").mkdir(exist_ok=True)
    r = subprocess.run([sys.executable, str(HERE / "build_page.py"), "--out", str(out)],
                       capture_output=True, text=True, timeout=300, env=env, cwd=str(HERE))
    assert r.returncode == 0, f"build_page failed:\n{r.stdout}\n{r.stderr}"
    return out


def _render(page: Path, now: str = NOW) -> dict[str, str]:
    """What a viewer at NOW would see, per element id."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available; the rule under test is JavaScript in the page")
    r = subprocess.run([node, str(HERE / "jstests" / "render_page.mjs"), str(page), now],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"rendering the page failed:\n{r.stdout}\n{r.stderr}"
    out: dict[str, str] = {}
    cur = None
    for line in r.stdout.splitlines():
        m = re.match(r"^===== #(\w+) =====$", line)
        if m:
            cur = m.group(1)
            out[cur] = ""
        elif cur:
            out[cur] += line + "\n"
    return out


def _item(fragment: str, tool: str) -> str:
    """The one <li> that names `tool`. Its absence is a failure, not an empty string:
    a test that silently matched nothing would pass for a page that rendered nothing."""
    for li in re.findall(r"<li>.*?</li>", fragment, re.S):
        if f"<code>{tool}</code>" in li:
            return li
    raise AssertionError(f"no list item for {tool!r} in the rendered gap list:\n{fragment[:2000]}")


THREE_STATES = [
    _row("alpha", 12, _stamp(FRESH_H)),                     # measured this morning
    _row("bravo", 7, _stamp(AGED_H)),                       # measured before yesterday's round
    _row("charlie", 5, None, omit_stamp=True),              # every ledger on disk today
]


@pytest.fixture(scope="module")
def three(tmp_path_factory) -> dict[str, str]:
    return _render(_build(tmp_path_factory.mktemp("three"), THREE_STATES))


@pytest.mark.parametrize("tool,count", [("alpha", 12), ("bravo", 7), ("charlie", 5)])
def test_snapshot_counts_do_not_carry_age_badges(three, tool, count):
    li = _item(three["forkGap"], tool)
    assert f"<b>{count}</b>" in li, li
    assert "fork-age" not in li and "STALE" not in li and "UNKNOWN-AGE" not in li
    assert not re.search(r"measured \d+ [hd] ago", li)


def test_the_summed_headline_preserves_counts_without_age_tool_lists(three):
    kpi = three["forkMetrics"]
    assert re.search(r'<div class="n">24</div>', kpi), kpi[:1500]
    assert "are STALE" not in kpi and "NO measurement time" not in kpi
    assert "bravo" not in kpi and "charlie" not in kpi
    assert "⚠" not in kpi


def test_changing_measurement_time_does_not_reclassify_a_published_count(tmp_path):
    recent = _render(_build(tmp_path / "a", [_row("bravo", 7, _stamp(FRESH_H))]))
    earlier = _render(_build(tmp_path / "b", [_row("bravo", 7, _stamp(AGED_H))]))
    assert recent == earlier, "measurement metadata changed public snapshot verdicts"
    assert "<b>7</b>" in _item(recent["forkGap"], "bravo")


@pytest.mark.parametrize("stamp", [None, "not a date", "2026-08-09T00:00:00+08:00"])
def test_preview_does_not_forge_measurement_metadata(tmp_path, stamp):
    # A permissive local preview is not a certificate. Preserve its missing or
    # invalid stamp as data; do not silently replace it by generated_at or now.
    page = _build(tmp_path, [_row("charlie", 5, stamp)])
    embedded = re.search(r"const LEDGERS = ([^\n]+);", page.read_text()).group(1)
    row = json.loads(embedded)[0]
    assert row["behind_measured_at"] == stamp
    assert row["behind_commits"] == 5
    view = _render(page)
    assert "<b>5</b>" in _item(view["forkGap"], "charlie")
    assert "fresh" not in _item(view["forkGap"], "charlie").lower()


# --- the publish boundary ---------------------------------------------------------

def test_strip_provenance_removes_the_provenance_and_keeps_the_measurement_time():
    """The timestamp has to survive the step that sanitizes rows for publication.

    Asserted BEHAVIOURALLY on `strip_provenance`'s output rather than by reading its
    implementation, because "it happens to be a denylist of one key today" is exactly
    the property a future edit is free to change.
    """
    row = {"tool": "alpha", "behind_commits": 12,
           "behind_measured_at": "2026-08-05T05:31:00+08:00",
           gk_state.PROVENANCE_KEY: {"host": "8HD-d", "checkout": "/home/x/vibeic-eda"}}
    out = gk_state.strip_provenance(row)
    assert gk_state.PROVENANCE_KEY not in out, "provenance reached the publish boundary"
    assert out.get("behind_measured_at") == "2026-08-05T05:31:00+08:00", \
        "the measurement time was stripped; the page can state a number but not its age"
    for k in gk_state.PUBLISHED_KEYS:
        assert k in out, f"{k} is declared load-bearing for the page but does not survive"


# --- the writer -------------------------------------------------------------------

def _df():
    import importlib.util
    spec = importlib.util.spec_from_file_location("df91", HERE / "discover_forks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_count_and_its_time_are_one_write():
    """A measured count gets a stamp; an unmeasurable one gets an explicit null.

    Null, NEVER absent and never `now`: "we could not measure" must not be able to
    borrow the freshness of the attempt that failed.
    """
    df = _df()
    led: dict = {}
    df.record_behind(led, 12)
    assert led["behind_commits"] == 12
    assert isinstance(led[df.BEHIND_MEASURED_AT], str) and led[df.BEHIND_MEASURED_AT], \
        "a measured count came out with no measurement time"

    led2: dict = {}
    df.record_behind(led2, None)
    assert led2["behind_commits"] is None
    assert df.BEHIND_MEASURED_AT in led2 and led2[df.BEHIND_MEASURED_AT] is None, \
        "an unmeasurable row must SAY so, not omit the question"


def test_nothing_writes_the_count_without_the_time():
    """The pairing is structural, not a convention two adjacent lines keep.

    A source check, deliberately: the failure it guards against is a FUTURE edit
    assigning `behind_commits` somewhere new and not the stamp, which no fixture can
    exercise because the code path would not exist yet.
    """
    import ast
    src = (HERE / "discover_forks.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    owners: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "behind_commits"):
                    owners.append(fn.name)
    assert owners == ["record_behind"], (
        "`behind_commits` is assigned outside `record_behind`, so it can be written "
        f"without the time it was measured — assigned in: {owners}")


def test_a_row_whose_compare_failed_records_no_measurement_time():
    """The error path — the one `generated_at` cannot distinguish. Such a row still
    gets a `generated_at` seconds old; it must NOT get a measurement time."""
    df = _df()
    led = {"generated_at": "2026-08-05T05:31:00+08:00"}
    df.record_behind(led, None)
    assert led[df.BEHIND_MEASURED_AT] is None
    assert led["generated_at"], "sanity: the row still carries its write time"
