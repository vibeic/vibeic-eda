#!/usr/bin/env python3
"""test_assess.py — token-free unit tests for the selective-merge assessment engine.

Exercises the deterministic + combine logic without any gh/git/claude calls:
the clearly-safe gate, the stub/degraded classify normalization, and the markdown
render. Run:  python3 test_assess.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import assess_release as A


def test_clearly_safe_gate():
    safe = {"category": "bugfix", "risk": "low", "relevant": True, "recommend": "adopt"}
    assert A._clearly_safe(safe, touches_our_files=False, clean_pick=True) is True
    assert A._clearly_safe(safe, touches_our_files=True, clean_pick=True) is False   # overlaps our patch
    assert A._clearly_safe(safe, touches_our_files=False, clean_pick=False) is False  # dirty pick
    assert A._clearly_safe(safe, touches_our_files=False, clean_pick=None) is False   # unknown pick
    assert A._clearly_safe({**safe, "risk": "medium"}, False, True) is False
    assert A._clearly_safe({**safe, "category": "feature"}, False, True) is False
    assert A._clearly_safe({**safe, "relevant": False}, False, True) is False
    assert A._clearly_safe({**safe, "recommend": "manual"}, False, True) is False
    assert A._clearly_safe(dict(A._DEGRADED), False, True) is False


def test_classify_stub_and_degraded_fill():
    commits = [{"sha": "aaa111", "title": "fix null deref in drc"},
               {"sha": "bbb222", "title": "add feature X"}]
    stub = {"aaa111": {"category": "bugfix", "risk": "low", "relevant": True,
                       "recommend": "adopt", "summary": "fix drc null"}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(stub, f)
        sp = f.name
    try:
        os.environ["GK_ASSESS_STUB"] = sp
        cm = A.classify_commits("magic", "DRC", commits)
        assert cm["aaa111"]["category"] == "bugfix"
        assert cm["bbb222"]["recommend"] == "manual"        # omitted sha → degraded
    finally:
        os.environ.pop("GK_ASSESS_STUB", None)
        os.unlink(sp)


def test_classify_failsafe_on_bad_stub():
    commits = [{"sha": "aaa111", "title": "x"}, {"sha": "bbb222", "title": "y"}]
    os.environ["GK_ASSESS_STUB"] = "/no/such/file.json"
    try:
        cm = A.classify_commits("magic", "DRC", commits)
        assert all(v["recommend"] == "manual" for v in cm.values())
    finally:
        os.environ.pop("GK_ASSESS_STUB", None)


def test_classify_empty():
    assert A.classify_commits("magic", "DRC", []) == {}


def test_render_md():
    rep = {"tool": "magic", "status": "assessed", "base_release": "8.3.400", "latest": "8.3.675",
           "our_ref": "9f91cd24", "our_patch_files": 3, "commit_count": 2, "aggregate_files": 5,
           "clearly_safe": ["aaa111"],
           "commits": [
               {"sha": "aaa111", "title": "fix drc", "category": "bugfix", "risk": "low",
                "relevant": True, "touches_our_patches": False, "clean_cherrypick": True,
                "recommend": "adopt", "decision": "auto-safe", "summary": "fix drc null",
                "reproduce": "run drc on empty cell"},
               {"sha": "bbb222", "title": "add feature X", "category": "feature", "risk": "medium",
                "relevant": False, "touches_our_patches": None, "clean_cherrypick": None,
                "recommend": "manual", "decision": "human", "summary": ""}]}
    md = A.render_md(rep)
    assert "selective-merge assessment" in md
    assert "8.3.400 → 8.3.675" in md
    assert "auto-safe" in md and "Reproduce-before-adopt" in md
    assert "run drc on empty cell" in md


def test_render_md_clean_and_error():
    assert "nothing to assess" in A.render_md({"tool": "yosys", "status": "clean"})
    assert "assessment error" in A.render_md({"tool": "yosys", "error": "boom"})


def test_our_patch_files_unknown_on_error_fails_safe():
    # gh error → None (UNKNOWN), never set() — so the conflict gate can't read it as "no overlap"
    orig = A._gh
    try:
        A._gh = lambda path: {"_err": "403 rate limit"}
        assert A.our_patch_files("YosysHQ/yosys", "main", "deadbeef", "yosys") is None
        # and the assess() touch rule: unknown our_files (None) OR unknown commit files → touches=True
        our_files, cf = None, {"foo.cc"}
        touches = True if (our_files is None or cf is None) else bool(our_files & cf)
        assert touches is True
        assert A._clearly_safe({"category": "bugfix", "risk": "low", "relevant": True,
                                "recommend": "adopt"}, touches, True) is False   # cannot be auto-safe
    finally:
        A._gh = orig


def test_gh_never_raises():
    import subprocess as _sp
    orig = _sp.run
    try:
        _sp.run = lambda *a, **k: (_ for _ in ()).throw(OSError("gh not found"))
        r = A._gh("repos/x/y")
        assert isinstance(r, dict) and r.get("_err"), "OSError → _err, not a raise"
    finally:
        _sp.run = orig


def test_classify_maps_judge_verdicts():
    # the SAFE tool-less judge's {useful,reason,risk} maps into the assess() classify shape
    import llm_judge
    os.environ.pop("GK_ASSESS_STUB", None)
    orig = llm_judge.judge
    try:
        llm_judge.judge = lambda tool, role, commits: {
            "u1": {"useful": True, "reason": "fixes DRC crash", "risk": "low"},
            "n1": {"useful": False, "reason": "CI only", "risk": "low"}}
        out = A.classify_commits("magic", "DRC", [{"sha": "u1", "title": "x"}, {"sha": "n1", "title": "y"}])
        assert out["u1"]["category"] == "bugfix" and out["u1"]["recommend"] == "adopt" and out["u1"]["relevant"] is True
        assert out["n1"]["category"] == "other" and out["n1"]["recommend"] == "skip" and out["n1"]["relevant"] is False
    finally:
        llm_judge.judge = orig


def test_classify_degrades_when_judge_returns_none():
    import llm_judge
    os.environ.pop("GK_ASSESS_STUB", None)
    orig = llm_judge.judge
    try:
        llm_judge.judge = lambda *a, **k: None   # no token / API error
        out = A.classify_commits("magic", "DRC", [{"sha": "a", "title": "x"}])
        assert out["a"]["recommend"] == "manual", "judge None → degrade to manual (never auto-adopt)"
    finally:
        llm_judge.judge = orig


def test_llm_judge_never_raises_without_token():
    import llm_judge
    orig = llm_judge.CRED
    try:
        llm_judge.CRED = Path("/no/such/cred.json")   # no credential file
        assert llm_judge.judge("magic", "DRC", [{"sha": "a", "title": "x"}]) is None
        assert llm_judge.judge("magic", "DRC", []) == {}
    finally:
        llm_judge.CRED = orig


# ── determinism / idempotency (2026-07-25) ───────────────────────────────────
# The magic range 8.3.674→8.3.676 was re-assessed 7 days running with NO upstream
# change, and the clearly-safe count oscillated 1,1,1,0,0,0,1 — a sampled `risk`
# flipping a commit between human-review and auto-adopt. Two guards below.

def _cache_fixture(tmp, risks):
    """assess() wired to stub layers; `risks` is consumed one per classify call."""
    os.environ["GK_STATE_DIR"] = str(tmp)
    import importlib
    importlib.reload(A)
    (tmp / "ledger").mkdir(parents=True, exist_ok=True)
    (tmp / "ledger" / "magic.json").write_text(json.dumps({
        "tool": "magic", "integrated": True, "behind_releases": 1,
        "upstream": "up/magic", "upstream_default_branch": "master",
        "pinned_ref_full": "a" * 40, "base_release": "8.3.674",
        "upstream_latest_release": "8.3.676", "role": "DRC"}))
    A.upstream_commits = lambda *a: ([{"sha": "cc4da9a05fde", "sha_full": "c" * 40,
                                       "title": "fix substrate extraction", "body": "",
                                       "url": "", "author": "x"}], ["ext.c"])
    A.our_patch_files = lambda *a: set()
    A._commit_files = lambda *a: {"ext.c"}
    A.clean_cherrypick = lambda *a: True
    seq = list(risks)
    calls = []
    def classify(tool, role, commits):
        calls.append(1)
        r = seq.pop(0) if seq else "low"
        return {c["sha"]: {"category": "bugfix", "relevant": True, "risk": r,
                           "summary": "s", "reproduce": "", "recommend": "adopt"}
                for c in commits}
    A.classify_commits = classify
    return calls


def test_unchanged_range_replays_and_does_not_redrift():
    """Identical input ⇒ stored verdict replayed, no second judgment, no drift."""
    with tempfile.TemporaryDirectory() as d:
        calls = _cache_fixture(Path(d), ["low", "medium"])   # 2nd call WOULD drift
        r1 = A.assess("magic")
        r2 = A.assess("magic")
        assert r1["clearly_safe"] == ["cc4da9a05fde"]
        assert r2["clearly_safe"] == r1["clearly_safe"], "verdict drifted on identical input"
        assert r2.get("cached") is True
        assert len(calls) == 1, f"re-judged an unchanged range ({len(calls)} calls)"


def test_new_range_is_reassessed():
    """A real upstream move must NOT be masked by the cache."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        calls = _cache_fixture(tmp, ["low", "low"])
        A.assess("magic")
        led = json.loads((tmp / "ledger" / "magic.json").read_text())
        led["upstream_latest_release"] = "8.3.677"           # upstream advanced
        (tmp / "ledger" / "magic.json").write_text(json.dumps(led))
        r = A.assess("magic")
        assert not r.get("cached"), "a NEW range was wrongly served from cache"
        assert len(calls) == 2


def test_degraded_assessment_is_not_cached():
    """A judge outage must not freeze a provisional 'needs human' verdict forever."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["GK_STATE_DIR"] = d
        import importlib
        importlib.reload(A)
        tmp = Path(d)
        _cache_fixture(tmp, [])
        A.classify_commits = lambda tool, role, commits: {
            c["sha"]: dict(A._DEGRADED) for c in commits}
        r1 = A.assess("magic")
        assert r1["clearly_safe"] == []
        assert not r1.get("cached")
        r2 = A.assess("magic")
        assert not r2.get("cached"), "degraded (AI-unavailable) verdict was cached"


def test_judge_request_pins_temperature_zero():
    """`risk` gates auto-adopt, so the judgment must not be sampled."""
    src = (Path(__file__).parent / "llm_judge.py").read_text()
    assert '"temperature": 0' in src, "llm_judge must pin temperature=0"


# ── carried / recorded-decision filters (2026-07-25) ─────────────────────────
# magic 8.3.674->8.3.676 reported "2 need human decision" for 7 days while BOTH
# were already in what we ship: cc4da9a05fde as a direct ancestor, a22b7508acfe
# as cherry-pick fe91f011 (identical patch-id). behind_releases compares RELEASE
# TAGS but selective-merge adopts COMMITS, so the tag never advances and the
# fork reads "behind" forever, re-proposing work that is already done.

def test_already_carried_is_fail_open_without_a_clone():
    """UNKNOWN must read as NOT-carried: a genuinely new commit must never be
    silently dropped from review because we could not check."""
    orig = A.FORKS_DIR
    try:
        A.FORKS_DIR = Path("/nonexistent-fork-dir")
        got = A.already_carried("magic", "someref",
                                [{"sha": "aaa111", "sha_full": "a" * 40}])
        assert got == set()
    finally:
        A.FORKS_DIR = orig


def test_already_carried_empty_inputs():
    assert A.already_carried("magic", "ref", []) == set()
    assert A.already_carried("magic", "", [{"sha": "a", "sha_full": "a" * 40}]) == set()


def test_recorded_decisions_reads_the_register():
    d = A.recorded_decisions("magic")
    assert isinstance(d, dict)
    for sha, rec in d.items():
        assert rec.get("decision") in ("skip", "adopt"), (sha, rec)
        assert len(str(rec.get("reason", ""))) >= 20, f"{sha} has no real reason"
        assert rec.get("decided_by") and rec.get("decided_on")


def test_recorded_decisions_fail_open():
    """Unknown tool, and a missing/corrupt register, yield {} — never an
    exception, and never a decision we did not make."""
    assert A.recorded_decisions("no-such-tool-xyz") == {}
    orig = A.DECISIONS
    try:
        A.DECISIONS = Path(tempfile.mkdtemp()) / "missing.json"
        assert A.recorded_decisions("magic") == {}
        bad = Path(tempfile.mkdtemp()) / "bad.json"
        bad.write_text("{not json")
        A.DECISIONS = bad
        assert A.recorded_decisions("magic") == {}
    finally:
        A.DECISIONS = orig


def test_render_marks_a_fully_settled_range_as_decided():
    rep = {"tool": "magic", "status": "assessed", "base_release": "8.3.674",
           "latest": "8.3.676", "our_patch_files": 59, "commit_count": 2,
           "carried": ["aaa111"], "decided": ["bbb222"], "clearly_safe": [],
           "outstanding": [],
           "commits": [
               {"sha": "aaa111", "category": "carried", "risk": None,
                "relevant": None, "touches_our_patches": None,
                "clean_cherrypick": None, "recommend": "carried",
                "decision": "carried", "summary": "already ours", "title": ""},
               {"sha": "bbb222", "category": "decided", "risk": None,
                "relevant": None, "touches_our_patches": None,
                "clean_cherrypick": None, "recommend": "skip",
                "decision": "recorded:skip", "summary": "UI only", "title": ""}]}
    md = A.render_md(rep)
    assert "needs human decision: 0" in md
    assert "DECIDED — no action required" in md
    assert "RELEASE TAGS" in md          # explains why "behind" is not "owed work"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
        passed += 1
    print(f"ALL {passed} PASS")


# ── the summary must CONSUME the assessment's classification (vibe-ic#369) ──
# `assess_release` resolves three categories: clearly-safe, CARRIED (ancestry
# or cherry-pick patch-id) and DECIDED (recorded gatekeeper decision). The
# sync-log note re-derived "needs human" as `commit_count - clearly_safe`,
# discarding the last two. Measured on magic 8.3.674 -> 8.3.676: 2 carried +
# 1 recorded skip = nothing outstanding, yet it reported "3 need human
# review" and the settled range was re-proposed on 07-23, 07-24 and 07-26.

def _note_for(rep):
    """Drive the real summary branch with a synthetic assessment report."""
    import importlib.util, sys as _s
    from pathlib import Path as _P
    spec = importlib.util.spec_from_file_location(
        "_gk", _P(__file__).resolve().parent / "gatekeeper.py")
    gk = importlib.util.module_from_spec(spec)
    _s.modules["_gk"] = gk
    spec.loader.exec_module(gk)
    return gk


def test_369_carried_and_decided_are_not_counted_as_open_work():
    src = (Path(__file__).resolve().parent / "gatekeeper.py").read_text()
    # the crude re-derivation must be gone from the summary branch
    assert 'f"{safe} clearly-safe, {cc - safe} need human review' not in src
    assert 'rep.get("outstanding")' in src
    assert '"carried": carried' in src and '"decided": decided' in src


def test_369_unknown_outstanding_still_reads_as_needing_review():
    """An older cached report without `outstanding` must fall back to the
    conservative arithmetic — unknown may never read as 'nothing to do'."""
    src = (Path(__file__).resolve().parent / "gatekeeper.py").read_text()
    assert "if outstanding is not None else cc - safe" in src


def test_369_nothing_outstanding_is_not_reported_as_deferred():
    """DEFERRED on a fully-resolved range is what turned settled work into a
    recurring proposal."""
    src = (Path(__file__).resolve().parent / "gatekeeper.py").read_text()
    assert 'entry["verdict"] = "RESOLVED"' in src
    i = src.index('entry["verdict"] = "RESOLVED"')
    window = src[max(0, i - 400):i]
    assert "n_open == 0" in window and "safe == 0" in window
