#!/usr/bin/env python3
"""vibeic-eda#91 — a count published with no record of when it was measured.

WHAT WAS MEASURED, 2026-08-04. The published page rendered **39** commits behind
for a state whose GitHub-fork half was, at that moment, **4**:

    tool        ledger    live (compare, base=ours head=upstream)
    iverilog        12        0
    pyuvm            1        0
    sby              1        0
    slang            1        0
    verilator        4        1
    OpenROAD         2        3

Nothing in the data said which of those numbers was hours old. Every ledger row
carried its `behind_commits` with no measurement timestamp of any kind, so a stale
12 and a just-measured 0 were the same kind of thing. The only signal separating
them was the FILE MTIME — a property of the filesystem, not of the record, which
does not survive the row being read, embedded into the page and published.

WHY `generated_at` WAS NOT ALREADY THE ANSWER, since the row has carried one since
2026-07-14. It is stamped at the top of `discover_one` BEFORE anything is measured
and it survives every early return: a row whose repo meta 404'd, which measured
nothing at all and has no `behind_commits`, still carries a `generated_at` from
seconds ago. Reading it as the age of the count answers "when was this row
written?" to the question "when was this measured?" — an adjacent fact wearing the
asked one's clothes, which is the defect family this repository keeps paying for
and precisely what #91 is about. The count now carries its own stamp, written at
the point the compare answers (`discover_forks.record_behind`).

WHAT THESE TESTS DO. They drive the REAL emitted page — `build_page.py` writes the
HTML, and `jstests/render_page.mjs` executes that page's own <script> against its
own markup and reports what a viewer would see. Not the rule in isolation
(`jstests/measured_age.mjs` does that): a correct rule assigned to nothing, or
rendered into an element the markup does not contain, passes an expression test and
shows a reader nothing.

THE CONTROL. `test_hand_aging_one_row_turns_its_verdict` builds the page TWICE from
ledgers that differ in ONE character-range — one row's measurement timestamp — and
asserts OPPOSITE verdicts for that row. A test that dies on a missing key would
prove only that a key is missing; this one fails on the VERDICT, in both
directions, so a rule that always said STALE and a rule that never did are both red.

HERMETIC. The build subprocess runs with an empty PATH, so the inventory section's
docker/gh probes fail fast and report themselves unmeasured (which is that
section's designed behaviour). Nothing here touches the production ledger, the
production page, or the network.
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


def _render(page: Path) -> dict[str, str]:
    """What a viewer at NOW would see, per element id."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available; the rule under test is JavaScript in the page")
    r = subprocess.run([node, str(HERE / "jstests" / "render_page.mjs"), str(page), NOW],
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


def test_a_freshly_measured_count_states_its_age(three):
    """The ordinary case still prints the number, and now says how old it is."""
    li = _item(three["forkGap"], "alpha")
    assert "<b>12</b>" in li, f"the count itself stopped being rendered:\n{li}"
    assert re.search(r"measured \d+ h ago", li), f"no age beside the count:\n{li}"
    assert "STALE" not in li and "UNKNOWN-AGE" not in li, \
        f"a count measured {FRESH_H} h ago was marked as not current:\n{li}"


def test_a_count_older_than_one_round_is_marked_stale(three):
    """`bravo` was measured {AGED_H} h ago — past 05:30 + 24 h + slack, so a round
    that should have replaced it did not. It must not print as current."""
    li = _item(three["forkGap"], "bravo")
    assert "<b>7</b>" in li, f"the count itself stopped being rendered:\n{li}"
    assert "STALE" in li, f"a {AGED_H} h old count printed as current:\n{li}"
    assert re.search(r"measured \d+ h ago", li), f"STALE without saying how stale:\n{li}"


def test_a_row_with_no_timestamp_renders_as_unknown_age(three):
    """THE MIGRATION CASE, AND IT IS THE COMMON ONE. Every ledger written before this
    field existed has no stamp — 36 of 36 on the day this ships. Such a row is
    UNKNOWN-AGE: not current, and not stale either, because we do not know that."""
    li = _item(three["forkGap"], "charlie")
    assert "<b>5</b>" in li, f"the count itself stopped being rendered:\n{li}"
    assert "UNKNOWN-AGE" in li, f"an unstamped count printed as current:\n{li}"
    assert not re.search(r"measured \d+ [hd] ago", li), \
        f"an age was stated for a row that records none:\n{li}"
    assert "STALE" not in li, \
        f"unknown age was reported as known-and-old, which is a claim we cannot make:\n{li}"


def test_the_summed_headline_says_its_parts_are_not_all_current(three):
    """A total whose parts are of unknown age is of unknown age. The headline is the
    number people act on — two pin-advance proposals already acted on this one (#74,
    #78) — so the caveat is on the number, not only in prose further down."""
    kpi = three["forkMetrics"]
    assert re.search(r'<div class="n">24\s*⚠', kpi), \
        f"the summed count carries no mark despite a stale row:\n{kpi[:1500]}"
    assert "are STALE" in kpi and "bravo" in kpi, "the stale row is not named on the headline"
    assert "NO measurement time" in kpi and "charlie" in kpi, \
        "the unstamped row is not named on the headline"


def test_hand_aging_one_row_turns_its_verdict(tmp_path):
    """THE CONTROL, and the reason this file is not a check on a key's presence.

    Two pages from ledgers differing in ONE field value — `bravo`'s measurement
    timestamp — asserted to reach OPPOSITE verdicts. A rule that always said STALE
    fails the first half; a rule that never did fails the second. Neither can be
    passed by a `KeyError`.
    """
    fresh = _render(_build(tmp_path / "a", [_row("bravo", 7, _stamp(FRESH_H))]))
    aged = _render(_build(tmp_path / "b", [_row("bravo", 7, _stamp(AGED_H))]))

    li_fresh, li_aged = _item(fresh["forkGap"], "bravo"), _item(aged["forkGap"], "bravo")
    assert "STALE" not in li_fresh, f"a {FRESH_H} h old count was marked stale:\n{li_fresh}"
    assert "STALE" in li_aged, f"a {AGED_H} h old count was not marked stale:\n{li_aged}"
    assert "⚠" not in fresh["forkMetrics"], "the headline warned about a fresh row"
    assert "⚠" in aged["forkMetrics"], "the headline did not warn about a stale row"
    # The one thing that must NOT change: the number itself.
    assert "<b>7</b>" in li_fresh and "<b>7</b>" in li_aged


def test_the_page_does_not_fall_back_to_generated_at(tmp_path):
    """`generated_at` is stamped before measurement and survives every early return.
    Every fixture row above carries one from 30 minutes ago; if the page ever reached
    for it, `charlie` would read as freshly measured. This is that assertion made
    directly, so the fallback cannot be reintroduced as an apparent improvement."""
    page = _render(_build(tmp_path, [_row("charlie", 5, None, omit_stamp=True)]))
    li = _item(page["forkGap"], "charlie")
    assert "UNKNOWN-AGE" in li, (
        "the page borrowed `generated_at` (30 min old) as the age of a count that was "
        f"never stamped:\n{li}")


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


# --- the rule itself, as shipped --------------------------------------------------

def test_the_shipped_age_rule_behaves(capsys):
    """Boundary, migration, clock skew and the no-count case, evaluated on the
    expressions the page actually ships. See `jstests/measured_age.mjs`."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    r = subprocess.run([node, str(HERE / "jstests" / "measured_age.mjs"),
                        str(HERE / "build_page.py")],
                       capture_output=True, text=True, timeout=120)
    print(r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr


def test_one_threshold_serves_both_the_page_and_the_rows():
    """30 h appears ONCE as a value. The page-level banner (#58) and the per-row rule
    (#91) measure the same clock, and two literals for one round is how two programs
    came to say opposite things about the same four pins (#29)."""
    src = (HERE / "build_page.py").read_text(encoding="utf-8")
    decls = re.findall(r"const STALE_HOURS = (\d+);", src)
    assert decls == ["30"], f"expected one STALE_HOURS declaration of 30, got {decls}"
    assert not re.search(r"ageH\s*>\s*\d", src), \
        "a staleness comparison against a bare number reappeared beside the constant"
