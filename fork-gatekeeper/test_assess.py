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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
        passed += 1
    print(f"ALL {passed} PASS")
