#!/usr/bin/env python3
"""A conflict the AI resolved and LANDED must not leave the round frozen.

Measured 2026-08-20 on OpenROAD. Step 1 hit a `src/sta` conflict taking 12
upstream commits, aborted, and handed the case on. Step 2b resolved it, pushed
it, and the landing verified — every upstream commit was in. But two fields
written BEFORE the AI ran were never revisited: `upstream_merge_outcome` stayed
"conflict", and `ours_skipped` still said consolidation "waits for the required
upstream-first base" after that base had arrived. The gate read them, correctly
called discovery incomplete, and returned NORECORD; run_tick was skipped,
build_page was skipped, and the public page served three-day-old numbers until a
person noticed.

The cure is to DO the deferred work, never to assume it. Every test below that
does not end in a resumption is therefore load-bearing: each is a way the round
must keep refusing.
"""
from __future__ import annotations

import pathlib
import sys
import types

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_0530 as D                                     # noqa: E402


UPSTREAM_REF = "upstream/master"
PUBLISHED = "e" * 40


def _rep(**over):
    """A fork exactly as step 1 leaves it after an upstream conflict."""
    rep = {
        "main": "master",
        "needs_human": True,
        "upstream": "CONFLICT taking 12 upstream commit(s)",
        "upstream_merge_outcome": "conflict",
        "ours_skipped": D.OURS_DEFERRED_BY_UPSTREAM_CONFLICT,
        "conflicted": [{
            "branch": UPSTREAM_REF,
            "direction": "upstream -> our mainline",
            "commits": 12,
            "conflicting_files": ["src/sta"],
        }],
    }
    rep.update(over)
    return rep


def _ai(verified=True, branch=UPSTREAM_REF, fork="OpenROAD"):
    return {"verification": [{
        "fork": fork, "branch": branch, "mainline": "master",
        "source_tip": "f" * 40, "published_tip": PUBLISHED,
        "verified": verified,
        "reason": "source tip is an ancestor of the published mainline",
    }]}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Stub the repository boundary; the logic under test is the decision."""
    (tmp_path / "OpenROAD").mkdir()
    monkeypatch.setattr(D, "FORKS", tmp_path)
    calls = types.SimpleNamespace(step2=[], ff=[], tip=PUBLISHED)

    monkeypatch.setattr(D, "out", lambda *a, **k: calls.tip)
    monkeypatch.setattr(
        D, "sh",
        lambda *a, **k: (calls.ff.append(a)
                         or types.SimpleNamespace(returncode=0, stdout="", stderr="")))

    def _step2(g, main, rep):
        calls.step2.append((tuple(g), main))
        rep["merged"] = []
        rep.setdefault("conflicted", [])
    monkeypatch.setattr(D, "step2_ours", _step2)
    return calls


# ── the round must resume ────────────────────────────────────────────────────

def test_a_verified_landing_runs_the_consolidation_it_deferred(wired):
    report = {"OpenROAD": _rep()}

    summary = D.resume_ours_after_ai_landing(report, _ai(), apply=True)

    assert wired.step2, "step2_ours never ran — the deferred work was skipped again"
    rep = report["OpenROAD"]
    assert "ours_skipped" not in rep
    assert rep["upstream_merge_outcome"] == "conflict_resolved"
    assert rep["upstream_landing"] == {
        "verified": True, "published_tip": PUBLISHED, "source_tip": "f" * 40,
        "reason": "source tip is an ancestor of the published mainline",
        "ours_resumed": True,
    }
    assert summary["resumed"] == [{"fork": "OpenROAD", "published_tip": PUBLISHED}]


def test_a_mainline_behind_the_published_tip_is_fast_forwarded(wired, monkeypatch):
    monkeypatch.setattr(D, "out", lambda *a, **k: "0" * 40)   # local != published
    report = {"OpenROAD": _rep()}

    D.resume_ours_after_ai_landing(report, _ai(), apply=True)

    assert any("--ff-only" in a for call in wired.ff for a in call), wired.ff
    assert report["OpenROAD"]["upstream_merge_outcome"] == "conflict_resolved"


# ── the round must keep refusing ─────────────────────────────────────────────

def _refused(report, fork="OpenROAD"):
    rep = report[fork]
    assert rep.get("ours_skipped"), "the deferral was cleared without doing the work"
    assert rep["upstream_merge_outcome"] != "conflict_resolved"
    assert "upstream_landing" not in rep


def test_an_unverified_landing_resumes_nothing(wired):
    report = {"OpenROAD": _rep()}
    D.resume_ours_after_ai_landing(report, _ai(verified=False), apply=True)
    assert not wired.step2
    _refused(report)


def test_a_verified_case_on_a_DIFFERENT_branch_does_not_unlock_the_upstream_one(wired):
    """A verified OURS-direction case proves nothing about the upstream landing."""
    report = {"OpenROAD": _rep()}
    D.resume_ours_after_ai_landing(report, _ai(branch="origin/topic"), apply=True)
    assert not wired.step2
    _refused(report)


def test_a_landing_for_another_fork_does_not_unlock_this_one(wired):
    report = {"OpenROAD": _rep()}
    D.resume_ours_after_ai_landing(report, _ai(fork="yosys"), apply=True)
    assert not wired.step2
    _refused(report)


def test_a_branch_list_that_was_never_read_is_never_resumed(wired):
    """`BRANCH LIST UNKNOWN` is a different skip: no upstream evidence can
    supply the ours-side measurement it never took."""
    report = {"OpenROAD": _rep(ours_skipped="BRANCH LIST UNKNOWN — fetch failed")}
    D.resume_ours_after_ai_landing(report, _ai(), apply=True)
    assert not wired.step2
    _refused(report)


def test_a_consolidation_that_cannot_measure_leaves_the_round_refusing(wired, monkeypatch):
    def _blind(g, main, rep):
        wired.step2.append(g)
        rep["ours_skipped"] = "BRANCH LIST UNKNOWN — the fetch of origin did not succeed"
    monkeypatch.setattr(D, "step2_ours", _blind)
    report = {"OpenROAD": _rep()}

    summary = D.resume_ours_after_ai_landing(report, _ai(), apply=True)

    assert wired.step2, "it must try"
    _refused(report)
    assert any("could not measure" in d["reason"] for d in summary["declined"])


def test_a_mainline_that_will_not_fast_forward_is_declined(wired, monkeypatch):
    monkeypatch.setattr(D, "out", lambda *a, **k: "0" * 40)
    monkeypatch.setattr(
        D, "sh",
        lambda *a, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="fatal: Not possible to fast-forward"))
    report = {"OpenROAD": _rep()}

    summary = D.resume_ours_after_ai_landing(report, _ai(), apply=True)

    assert not wired.step2, "it must not consolidate onto a line that never landed"
    _refused(report)
    assert any("fast-forward" in d["reason"] for d in summary["declined"])


def test_a_crash_declines_instead_of_taking_the_round_down(wired, monkeypatch):
    def _boom(g, main, rep):
        raise RuntimeError("git exploded")
    monkeypatch.setattr(D, "step2_ours", _boom)
    report = {"OpenROAD": _rep()}

    summary = D.resume_ours_after_ai_landing(report, _ai(), apply=True)

    _refused(report)
    assert any("git exploded" in d["reason"] for d in summary["declined"])


def test_a_dry_run_touches_nothing(wired):
    report = {"OpenROAD": _rep()}
    summary = D.resume_ours_after_ai_landing(report, _ai(), apply=False)
    assert not wired.step2
    assert summary["applied"] is False
    _refused(report)


def test_round_level_keys_are_not_mistaken_for_forks(wired):
    report = {"_ai_decisions": {"cases": 1}, "_round": {"run_id": "x"},
              "OpenROAD": _rep()}
    D.resume_ours_after_ai_landing(report, _ai(), apply=True)
    assert report["OpenROAD"]["upstream_merge_outcome"] == "conflict_resolved"


def test_a_consolidation_that_MERGED_is_not_certified_because_it_is_unpublished(
        wired, monkeypatch):
    """The push step already ran. Anything merged now sits on the local mainline
    with no publication behind it, and a release certified on top of it would be
    exactly the false report this change exists to stop."""
    def _merges(g, main, rep):
        wired.step2.append(g)
        rep["merged"] = ["origin/feature-a", "origin/feature-b"]
        rep.setdefault("conflicted", [])
    monkeypatch.setattr(D, "step2_ours", _merges)
    report = {"OpenROAD": _rep()}

    summary = D.resume_ours_after_ai_landing(report, _ai(), apply=True)

    _refused(report)
    assert "NOT published" in report["OpenROAD"]["ours_skipped"]
    assert summary["declined"] == [
        {"fork": "OpenROAD", "merged_unpublished": 2,
         "reason": "consolidation merged after the push step"}]


def test_an_empty_merge_list_still_certifies(wired):
    """`merged: []` is the ordinary case and means the measurement completed."""
    report = {"OpenROAD": _rep()}
    D.resume_ours_after_ai_landing(report, _ai(), apply=True)
    assert report["OpenROAD"]["upstream_merge_outcome"] == "conflict_resolved"
