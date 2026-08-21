"""The KPI showed live drift on every certified page, and could not have shown
anything else.

`commitsBehind = convergedRound ? 0 : liveBehind` exists to keep the record of a
gap that was actually closed — upstream keeps moving, so the live sum can
essentially never read 0. On a CERTIFIED page that branch was dead:
`convergedRound` reads `REPORT.q1_sync_lag_at_merge`, and a certified page's
REPORT is the small round view `{date, round_at, run_id}` with none of the gap
keys. The page's own comments record two earlier attempts, the second of which
fell back to summing a per-fork `sync_lag_at_merge` the ledger rows do not
carry. Both ended on the LIVE sum.

The certified round view had it all along, under
`convergence.sync_lag_at_merge` — and, once the number stops meaning "now", the
page has to say which moment it DOES mean. That is `merged_at`, the finish of
the merge round: `sync_lag_at_merge` is counted against `tip_seen`, the upstream
tip daily_0530's own fetch resolved before merging. Not `round_at`, which is ten
minutes earlier, and not `captured_at`, which is forty-five minutes later.

MEASURED 2026-08-21: sync_lag_at_merge 0, live 1 (verilator), round_at 18:10,
merged_at 18:20. The card read "1 · 目前落後上游的 commit 數".
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

HERE = pathlib.Path(__file__).resolve().parent
SOURCE = (HERE / "build_page.py").read_text(encoding="utf-8")

MERGED_AT = "2026-08-21T18:20:24.753966+08:00"
ROUND_AT = "2026-08-21T18:10:38.428423+08:00"
LEDGERS = [{"tool": "verilator", "sync_lag": 1, "behind_commits": 1},
           {"tool": "yosys", "sync_lag": 0, "behind_commits": 0}]


def _page_rule() -> str:
    """The page's OWN code for this rule, not a copy of it in a test.

    A reimplementation here would pass while the page did something else, which
    is the failure this whole file exists to catch one layer down.
    """
    start = SOURCE.index("  const syncRows = LEDGERS.filter(")
    marker = 'replace("T", " ") : "";'
    end = SOURCE.index(marker, start) + len(marker)
    rule = SOURCE[start:end]
    assert "convergedRound" in rule and "roundStamp" in rule, rule[:200]
    return rule


def _last_check_rule() -> str:
    """The page's own `checkStamp`/`lastCheck` code, for the card that names the
    moment. Same reason as `_page_rule`: a copy here would drift."""
    start = SOURCE.index("  const checkStamp = (typeof REPORT")
    marker = "      || generatedAt;"
    end = SOURCE.index(marker, start) + len(marker)
    tail_start = SOURCE.index("  const lastCheck = checkStamp")
    tail_end = SOURCE.index(': "—";', tail_start) + len(': "—";')
    rule = SOURCE[start:end] + "\n" + SOURCE[tail_start:tail_end]
    assert "checkStamp" in rule and "lastCheck" in rule, rule[:200]
    return rule


def _evaluate(*, current_round: dict, report: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available; the rule under test is JavaScript in the page")
    script = "\n".join([
        f"const LEDGERS = {json.dumps(LEDGERS)};",
        f"const CURRENT_ROUND = {json.dumps(current_round)};",
        f"const REPORT = {json.dumps(report)};",
        _page_rule(),
        # The two lines the rule feeds, kept verbatim from the page.
        'const liveBehind = LEDGERS.filter(d=>typeof d.behind_commits === "number")'
        ".reduce((a,d)=>a+(d.behind_commits||0),0);",
        "const commitsBehind = (convergedRound && roundStamp) ? 0 : liveBehind;",
        'const generatedAt = "2026-08-21T18:57:00+08:00";',
        _last_check_rule(),
        "console.log(JSON.stringify({commitsBehind, roundStamp, convergedRound,"
        " lastCheck}));",
    ])
    run = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def _certified(*, at_merge=0, merged_at=MERGED_AT) -> dict:
    round_view = {"round_at": ROUND_AT,
                  "convergence": {"sync_lag_at_merge": at_merge,
                                  "live_sync_lag": 1}}
    if merged_at is not None:
        round_view["merged_at"] = merged_at
    return round_view


# A certified page's REPORT — none of the gap keys, which is the whole problem.
SMALL_REPORT = {"date": "2026-08-21", "round_at": ROUND_AT, "run_id": "a" * 64}


def test_a_certified_round_that_converged_shows_the_gap_it_closed():
    out = _evaluate(current_round=_certified(), report=SMALL_REPORT)
    assert out["convergedRound"] is True
    assert out["commitsBehind"] == 0


def test_the_stamp_is_the_merge_boundary_not_the_round_start():
    """`round_at` is ten minutes before the boundary the figure is counted
    against. Stamping the wrong minute is its own untruth."""
    out = _evaluate(current_round=_certified(), report=SMALL_REPORT)
    assert out["roundStamp"] == "2026-08-21 18:20"
    assert "18:10" not in out["roundStamp"]


def test_a_round_still_behind_at_its_merge_boundary_shows_the_live_count():
    """THE DIRECTION THAT MAKES IT SAFE. Judging at the boundary is not the same
    as not judging: a round that did NOT take what it saw still reads nonzero."""
    out = _evaluate(current_round=_certified(at_merge=2), report=SMALL_REPORT)
    assert out["convergedRound"] is False
    assert out["commitsBehind"] == 1


def test_without_a_boundary_stamp_the_zero_is_not_shown():
    """The converged figure and its "as of" travel together. A 0 above a table
    row reading 1 is only coherent when the card says which moment it means, so
    a page that cannot say WHEN keeps showing the live count."""
    out = _evaluate(current_round=_certified(merged_at=None), report=SMALL_REPORT)
    assert out["roundStamp"] == ""
    assert out["commitsBehind"] == 1


def test_a_gap_report_page_still_answers_from_the_report():
    """The uncertified page carries the real gap report; that path is unchanged
    and still wins over the round view."""
    out = _evaluate(current_round=_certified(at_merge=5),
                    report={**SMALL_REPORT, "q1_sync_lag_at_merge": 0})
    assert out["convergedRound"] is True


def test_the_certified_round_view_carries_the_boundary():
    """If the producer stops emitting `merged_at`, the card silently reverts to
    live drift — which is exactly the state this fixed. Fail there instead."""
    assert '"merged_at"' in SOURCE
    assert "fork_github" in SOURCE.split('"merged_at"')[1][:400]


# --- the card that names the moment ------------------------------------------

def test_a_certified_page_dates_its_numbers_at_the_merge_boundary():
    """`最後檢查時間` fell through to `round_at` on a certified page — the moment
    the round STARTED, ten minutes before it had measured anything — while the
    count beside it is taken at the merge boundary. A page whose two cards name
    two different moments has dated its own numbers wrong."""
    out = _evaluate(current_round=_certified(), report=SMALL_REPORT)
    assert out["lastCheck"] == "2026-08-21 18:20"
    assert out["lastCheck"] != "2026-08-21 18:10"


def test_a_gap_report_page_still_dates_itself_when_the_gap_closed():
    """Unchanged where it was already right: a page published straight after a
    release shows LIVE figures, measured at the gap report's completion, and
    that is the moment it must keep naming."""
    out = _evaluate(
        current_round=_certified(),
        report={**SMALL_REPORT, "completed_at": "2026-08-21T18:56:47+08:00"})
    assert out["lastCheck"] == "2026-08-21 18:56"


def test_the_two_cards_never_name_different_moments():
    """The count and its date travel together. Whenever the converged figure is
    shown, the card beside it must be showing the same boundary."""
    out = _evaluate(current_round=_certified(), report=SMALL_REPORT)
    assert out["commitsBehind"] == 0
    assert out["lastCheck"] == out["roundStamp"]
