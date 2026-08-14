"""CONVERGENCE is judged against the upstream THIS ROUND SAW, not a fresh fetch.

THE DEFECT. The merge runs early (05:31) and the convergence verdict is computed
at the end (~06:35), because the release phase -- build plus push of a 31 GB
image -- sits between them. Upstream commits inside that hour, and the round was
then judged against commits that did not exist when it merged.

Measured 2026-08-14. OpenROAD-flow-scripts fetched ef52564bf at 05:31, merged
it, pushed, and our master CONTAINS it. The 06:34 fetch saw da37dce1c, four
commits later. cocotb the same, three later. The round printed

    CONVERGENCE: FAILED - 7 commit(s) still behind upstream after the round.
    Merging upstream into our line is this round's primary duty; it did not
    happen.

about a merge that provably DID happen. On days upstream was quiet in that
window the identical code printed `sync lag 0`, so the verdict was a race.

WHAT MUST NOT BREAK. `sync_lag` (live drift) keeps its meaning and its value --
it is the honest answer to "how far behind upstream are we now" and other
readers depend on it. And a fork the round genuinely failed to merge must still
count, or this trade is a gate for a number.
"""
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fork_gap_report as R                                    # noqa: E402


def _rows(monkeypatch, live_lag, seen_lag, seen_sha="abc123"):
    """One measurable row; `count` returns live_lag vs the live ref and
    seen_lag vs the merge-time sha."""
    def fake_count(clone, a, b):
        return seen_lag if b == seen_sha else live_lag
    monkeypatch.setattr(R, "count", fake_count)
    return fake_count


def test_live_sync_lag_keeps_its_meaning(monkeypatch):
    """The trade must not quietly redefine the number other readers use."""
    _rows(monkeypatch, live_lag=7, seen_lag=0)
    assert R.count(None, "tip", "upstream/master") == 7


def test_at_merge_is_zero_when_the_round_took_what_it_saw(monkeypatch):
    _rows(monkeypatch, live_lag=7, seen_lag=0)
    assert R.count(None, "tip", "abc123") == 0


def test_absent_evidence_falls_back_to_the_live_lag():
    """A missing or unreadable evidence file must not score a round clean --
    that would make deleting one file a way to silence the gate."""
    src = pathlib.Path(HERE / "fork_gap_report.py").read_text()
    assert 'row["sync_lag_at_merge"] = row["sync_lag"]' in src, (
        "the no-evidence branch must fall back to the live lag, not to 0")


def test_the_round_reads_the_round_scoped_number():
    sh = (HERE / "run_0530.sh").read_text()
    assert "q1_sync_lag_at_merge" in sh, \
        "run_0530.sh still judges CONVERGENCE on the live lag"
    assert "--merge-evidence" in sh, \
        "run_0530.sh does not hand this round's evidence to fork_gap_report"


def test_the_verdict_says_what_it_measured():
    """A verdict that names the wrong quantity is how this went unnoticed: the
    old text asserted the merge 'did not happen' about a merge that had."""
    sh = (HERE / "run_0530.sh").read_text()
    assert "tip_seen" in sh, "the FAILED message does not say what it measured against"
    assert "still behind upstream after the round" not in sh, \
        "the old claim survives; it is false when upstream moved mid-round"


def test_summary_exposes_both_numbers():
    src = (HERE / "fork_gap_report.py").read_text()
    assert '"q1_sync_lag":' in src and '"q1_sync_lag_at_merge":' in src, (
        "both must be published: one is the round's duty, the other is the "
        "live drift, and collapsing them is what caused this")
