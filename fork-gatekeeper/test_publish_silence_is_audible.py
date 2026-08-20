#!/usr/bin/env python3
"""Detection that lands in a log nobody opens is not an alert.

vibeic-eda#58 added the measurement after three silent days were found by a
person. On 2026-08-20 three more silent days were found the same way, because
the measurement's whole delivery was a line in `daily_0530.log`. These tests pin
the last hop: the streak is counted, the PR goes out once, and -- the half that
matters -- a program that cannot deliver says NORECORD instead of "all clear".
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_publish_silence as S                          # noqa: E402


class Escalator:
    """Records calls; configurable outcome."""

    def __init__(self, ok=True, detail="PR #1 opened"):
        self.ok, self.detail, self.calls = ok, detail, []

    def __call__(self, summary, report_md):
        self.calls.append((summary, report_md))
        return (self.ok, self.detail)


def _run(history, *, published, reason="", round_at="2026-08-20T05:30:01+08:00",
         threshold=2, escalate=None):
    return S.assess(history, published=published, reason=reason,
                    round_at=round_at, threshold=threshold,
                    escalate=escalate or Escalator())


# ── counting ────────────────────────────────────────────────────────────────

def test_a_published_round_is_never_an_alert(tmp_path):
    rc, result = _run(tmp_path / "h.jsonl", published=True)
    assert rc == S.RC_OK
    assert result["streak"] == 0


def test_one_silent_round_is_below_the_threshold(tmp_path):
    esc = Escalator()
    rc, result = _run(tmp_path / "h.jsonl", published=False,
                      reason="AI authorization NORECORD", escalate=esc)
    assert rc == S.RC_OK
    assert result["streak"] == 1
    assert not esc.calls, "one quiet round is not yet a stuck pipeline"


def test_the_threshold_round_opens_exactly_one_pr(tmp_path):
    h = tmp_path / "h.jsonl"
    esc = Escalator()
    _run(h, published=False, reason="pytest red", round_at="r1", escalate=esc)
    rc, result = _run(h, published=False, reason="AI NORECORD",
                      round_at="r2", escalate=esc)

    assert rc == S.RC_SILENT
    assert result["streak"] == 2
    assert result["escalated"] is True
    assert len(esc.calls) == 1
    summary, report = esc.calls[0]
    assert "2 consecutive rounds" in summary
    # every round's OWN reason survives into the report: a streak with several
    # different reasons is several bugs, not one recurring one.
    assert "pytest red" in report and "AI NORECORD" in report


def test_a_streak_already_escalated_does_not_open_a_pr_a_day(tmp_path):
    h = tmp_path / "h.jsonl"
    esc = Escalator()
    for i in range(4):
        rc, result = _run(h, published=False, reason=f"r{i}",
                          round_at=f"r{i}", escalate=esc)
    assert rc == S.RC_SILENT
    assert result["streak"] == 4
    assert len(esc.calls) == 1, "one PR per streak, not one per round"


def test_publishing_again_clears_the_streak(tmp_path):
    h = tmp_path / "h.jsonl"
    esc = Escalator()
    _run(h, published=False, reason="a", round_at="r1", escalate=esc)
    _run(h, published=False, reason="b", round_at="r2", escalate=esc)
    rc, result = _run(h, published=True, round_at="r3", escalate=esc)
    assert rc == S.RC_OK
    assert result["streak"] == 0

    # and a NEW streak escalates again — the old PR was about the old outage
    _run(h, published=False, reason="c", round_at="r4", escalate=esc)
    rc, result = _run(h, published=False, reason="d", round_at="r5", escalate=esc)
    assert rc == S.RC_SILENT
    assert len(esc.calls) == 2


# ── refusing rather than degrading ──────────────────────────────────────────

def test_an_escalation_that_does_not_go_out_is_NORECORD_not_success(tmp_path):
    """THE LOAD-BEARING ONE. rc=1 claims the alert is out. If the PR failed,
    saying so is the difference between a guard and a decoration."""
    h = tmp_path / "h.jsonl"
    esc = Escalator(ok=False, detail="gh: not authenticated")
    _run(h, published=False, reason="a", round_at="r1", escalate=esc)
    rc, result = _run(h, published=False, reason="b", round_at="r2", escalate=esc)

    assert rc == S.RC_NORECORD
    assert result["escalated"] is False
    assert any("escalation did not go out" in e for e in result["errors"])


def test_an_unreadable_history_is_NORECORD_not_a_clean_slate(tmp_path):
    h = tmp_path / "h.jsonl"
    h.write_text('{"round_at": "r1", "published": false}\nnot json at all\n')
    rc, result = _run(h, published=False, reason="a")
    assert rc == S.RC_NORECORD
    assert any("unreadable" in e for e in result["errors"])


def test_a_row_without_a_verdict_is_not_treated_as_published(tmp_path):
    h = tmp_path / "h.jsonl"
    h.write_text('{"round_at": "r1"}\n')
    rc, result = _run(h, published=False, reason="a")
    assert rc == S.RC_NORECORD


def test_a_history_that_cannot_be_appended_is_NORECORD(tmp_path):
    # The FILE, not its directory: appending to an existing file needs no write
    # permission on the directory, so a read-only dir does not exercise this.
    h = tmp_path / "h.jsonl"
    h.write_text("")
    h.chmod(0o400)
    try:
        rc, result = _run(h, published=False, reason="a")
    finally:
        h.chmod(0o600)
    assert rc == S.RC_NORECORD
    assert any("could not be appended" in e for e in result["errors"])


def test_the_row_is_appended_before_any_verdict_is_reached(tmp_path):
    """The history is the evidence. It must survive even the quiet rounds."""
    h = tmp_path / "h.jsonl"
    _run(h, published=False, reason="why", round_at="r1")
    rows = [json.loads(l) for l in h.read_text().splitlines() if l.strip()]
    assert rows == [{"round_at": "r1", "published": False, "reason": "why"}]


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_reports_the_streak_and_refuses_a_nonsense_threshold(tmp_path, capsys):
    h = tmp_path / "h.jsonl"
    assert S.main(["--history", str(h), "--round-at", "r1",
                   "--published", "0", "--reason", "x",
                   "--no-escalate"]) == S.RC_OK
    assert S.main(["--history", str(h), "--round-at", "r2",
                   "--published", "0", "--reason", "y",
                   "--no-escalate"]) == S.RC_SILENT
    assert S.main(["--history", str(h), "--round-at", "r3",
                   "--published", "1", "--threshold", "0"]) == S.RC_NORECORD


# ---------------------------------------------------------------------------
# The bug these two catch: every test above stubs the escalator, so all of them
# passed while the REAL call was `pr_notify.open_pr(<str>, ...)` — a function
# that wants the fork-sync summary DICT and calls `.get()` on it. The first real
# escalation (2026-08-20, streak 2) died with
# `AttributeError: 'str' object has no attribute 'get'`. Stubbing the boundary
# proves the logic; only touching the real one proves the contract.
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# THE ALARM FILES ON THIS REPO, NOT ON vibe-ic.
#
# The first version routed through `pr_notify.open_alert_pr`, which files on
# vibe-ic. Ownership was wrong — a stalled fork page is THIS pipeline's failure
# — and so were the mechanics: an open PR on vibe-ic is counted by
# `fork_github_convergence`, whose denominator is the registered forks plus
# `vibeic/vibe-ic`, so the alarm about a stalled page would have stalled the
# page. `vibeic/vibeic-eda` is not in that denominator; checked against the
# round's own convergence sidecar rather than assumed.
#
# Stubbing a boundary proves the logic; only touching the real one proves the
# contract. The first cut of this program passed eleven stubbed tests while
# calling a function that wanted a different argument shape entirely.
# ---------------------------------------------------------------------------

def test_the_alarm_targets_this_repo_not_vibe_ic():
    assert S.GH_REPO == "vibeic/vibeic-eda", (
        "filing on vibe-ic makes fork_github_convergence non-zero, which skips "
        "the next round's publish — the alarm would stall what it reports on")


def test_the_real_escalator_runs_without_reaching_github(monkeypatch, tmp_path):
    """A clean refusal proves the arguments were understood and it got as far
    as looking for the checkout. AttributeError/TypeError means the wrong shape."""
    monkeypatch.setattr(S, "REPO", tmp_path / "no-such-repo")
    ok, detail = S._default_escalate(
        "fork page has not published for 2 consecutive rounds",
        "## body\n\n- a\n", "2026-08-21T05:30:01+08:00")
    assert ok is False
    assert "not found" in detail, detail
    for bad in ("AttributeError", "TypeError", "Traceback"):
        assert bad not in detail, detail


def test_the_alarm_path_refuses_an_empty_alert(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(S, "REPO", repo)
    for title, body in (("", "body"), ("title", "   "), ("  ", "")):
        ok, detail = S._default_escalate(title, body)
        assert ok is False and "empty title or body" in detail, detail


def test_a_streak_already_on_origin_is_not_raised_twice(monkeypatch, tmp_path):
    """A branch surviving a restart must not become a second PR."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(S, "REPO", repo)
    calls = []

    def _sh(*argv, **k):
        calls.append(argv)
        if "ls-remote" in argv:
            return 0, "abc123\trefs/heads/publish-silence-2026-08-21\n"
        return 0, ""

    monkeypatch.setattr(S, "_sh", _sh)
    ok, detail = S._default_escalate("t", "b", "2026-08-21T05:30:01+08:00")
    assert ok is True and "already raised" in detail
    assert not any("create" in a for a in calls), \
        "it reached gh pr create despite the branch already existing"
