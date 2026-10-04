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
import json
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


@pytest.fixture
def round_main(monkeypatch, tmp_path):
    """Run the real main/resume/rc path with no Git, network, AI or cache I/O.

    Only the effectful boundaries are replaced; the final sidecar is really
    written and read, and resume_ours_after_ai_landing is never mocked.
    """
    forks = tmp_path / "forks"
    (forks / "OpenROAD" / ".git").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    calls = types.SimpleNamespace(resumed=[], ai_apply=[], mode="resolved")
    monkeypatch.setattr(D, "FORKS", forks)
    monkeypatch.setattr(D, "image_pins", lambda: {})
    monkeypatch.setattr(D.gk_state, "state_dir", lambda: state)
    monkeypatch.setattr(D.watch_only_reset, "load_registry",
                        lambda p: {"policies": {}, "errors": []})
    monkeypatch.setattr(D, "registered_fleet", lambda p: (
        {"OpenROAD": {"branch": "master"},
         **({"missing-fork": {"branch": "master"}}
            if calls.mode == "other-fork" else {})}, []))
    monkeypatch.setattr(D, "provision_registered_checkouts", lambda *a: {})
    monkeypatch.setattr(D, "_upstream_pull_request_evidence",
                        lambda g: {"outcome": "complete", "open_count": 0})
    monkeypatch.setattr(D, "_fetch", lambda *a: (True, ""))
    monkeypatch.setattr(D, "published_mainline", lambda *a: "master")
    monkeypatch.setattr(D, "checkout_published_line",
                        lambda *a: (True, {"outcome": "natural_success"}))
    monkeypatch.setattr(D, "same_content_divergence", lambda *a: None)
    monkeypatch.setattr(D, "post_merge_checks", lambda *a: [])
    monkeypatch.setattr(D, "run_post_merge_checks", lambda *a: (
        [{"name": "consumer", "ok": False, "rc": 1, "detail": "failed"}]
        if calls.mode == "post-check" else []))

    def out(*args, **kw):
        if "status" in args:
            return ""
        assert "rev-parse" in args, args
        return "0" * 40 if calls.mode == "ff-refused" else PUBLISHED

    def sh(*args, **kw):
        if "push" in args:
            rc = (D.watchdog.RC_STALLED if calls.mode == "push-stalled" else
                  1 if calls.mode == "push-rejected" else 0)
        else:
            assert "--ff-only" in args, args
            rc = 1 if calls.mode == "ff-refused" else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="refused")

    def upstream(g, main, rep):
        rep.update(_rep())
        if calls.mode == "upstream-unmeasurable":
            rep["upstream_merge_outcome"] = "unmeasurable"
            rep["conflicted"] = []

    def ours(g, main, rep):
        calls.resumed.append(g)
        rep["merged"] = []
        if calls.mode == "resume-exception":
            raise RuntimeError("consolidation failed")
        if calls.mode == "branch-list-unknown":
            rep["ours_skipped"] = "BRANCH LIST UNKNOWN"
            rep["needs_human"] = True
        elif calls.mode == "unpublished":
            rep["merged"] = [{"branch": "origin/topic", "commits": 1}]
        elif calls.mode == "new-conflict":
            # The real step2_ours appends conflict cases but does not set the
            # flag: normally the subsequent AI step adjudicates those cases.
            rep["conflicted"].append({
                "branch": "origin/topic", "direction": "our branch -> our mainline"})
        elif calls.mode == "resume-unmeasurable":
            rep["merge_unmeasurable"] = [{"stage": "pull_request_state"}]
            rep["needs_human"] = True

    def prune(g, main, rep, apply):
        if calls.mode == "prune-unknown":
            rep["prune_skipped"] = "WORKTREE LIST UNKNOWN"
            rep["needs_human"] = True

    def ai(report, state_dir, apply, **kw):
        calls.ai_apply.append(apply)
        result = _ai(branch="origin/topic" if calls.mode == "wrong-branch"
                     else UPSTREAM_REF,
                     fork="other-fork" if calls.mode == "wrong-fork" else "OpenROAD")
        result.update(cases=1, invoked=apply, outcome="natural", rc=0,
                      verified=True, verified_cases=1)
        if not apply:
            result.update(verified=False, verified_cases=0)
        if calls.mode == "ai-unverified":
            result.update(verified=False, verified_cases=0)
            result["verification"][0]["verified"] = False
        elif calls.mode == "ai-stalled":
            result.update(outcome="stalled", rc=D.watchdog.RC_STALLED)
        elif calls.mode == "ai-failed":
            result["rc"] = 1
        elif calls.mode == "upstream-unmeasurable":
            result.update(cases=0, invoked=False, verification=[])
        return result

    monkeypatch.setattr(D, "out", out)
    monkeypatch.setattr(D, "sh", sh)
    monkeypatch.setattr(D, "step1_upstream", upstream)
    monkeypatch.setattr(D, "step2_ours", ours)
    monkeypatch.setattr(D, "step4_prune", prune)
    monkeypatch.setattr(D, "step2b_ai_decisions", ai)
    monkeypatch.setattr(D.round_record, "write",
                        lambda *a, **kw: {"stamp": "fixture", "rows": 1})

    def run(mode="resolved", *, apply=True, no_ai=False):
        calls.mode = mode
        path = tmp_path / "round.json"
        args = ["--skip-build", "--json", str(path)]
        if apply:
            args.append("--apply")
        if no_ai:
            args.append("--no-ai")
        rc = D.main_(args)
        return rc, json.loads(path.read_text()), calls

    return run


def test_the_round_clears_only_the_verified_upstream_conflict_cause(round_main):
    rc, report, calls = round_main()
    rep = report["OpenROAD"]
    assert calls.resumed, "the final verdict requires the deferred measurement"
    assert rep["upstream_merge_outcome"] == "conflict_resolved"
    assert rep["conflicted"] == _rep()["conflicted"], "retain conflict history"
    assert rc == 0, "a completed verified recovery must not leave the cron at rc1"
    assert rep["needs_human"] is False


@pytest.mark.parametrize("mode", [
    "post-check", "push-rejected", "push-stalled", "prune-unknown",
    "branch-list-unknown", "resume-exception", "ff-refused", "unpublished",
    "new-conflict", "resume-unmeasurable", "wrong-branch", "wrong-fork",
    "ai-unverified", "ai-stalled", "ai-failed", "upstream-unmeasurable",
    "other-fork",
])
def test_the_final_round_keeps_every_independent_refusal(round_main, mode):
    rc, report, calls = round_main(mode)
    assert rc == 1, f"{mode} must not be erased by a verified upstream case"
    rep = report["OpenROAD"]
    if mode == "other-fork":
        assert rep["needs_human"] is False
        assert report["missing-fork"]["needs_human"] is True
    elif mode not in {"ai-stalled", "ai-failed"}:
        assert rep["needs_human"] is True
    if mode == "new-conflict":
        assert len(rep["conflicted"]) == 2
        assert report["_ours_resumed"]["resumed"] == []
        assert report["_ours_resumed"]["declined"]
        assert rep["ours_skipped"], "a new case after the AI turn needs a named hold"


def test_no_ai_leaves_the_original_conflict_pending(round_main):
    rc, report, calls = round_main(no_ai=True)
    assert calls.ai_apply == [False]
    assert calls.resumed == []
    assert rc == 1
    assert report["OpenROAD"]["needs_human"] is True


def test_a_dry_round_does_not_clear_an_independent_prune_refusal(round_main):
    rc, report, calls = round_main("prune-unknown", apply=False)
    assert calls.ai_apply == [False]
    assert calls.resumed == []
    assert rc == 1
    assert report["OpenROAD"]["needs_human"] is True
