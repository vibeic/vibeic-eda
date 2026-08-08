#!/usr/bin/env python3
"""vibeic-eda#101 sub-defect 2: the range base an assessment measured from must be
recorded as an exact, resolvable commit -- not left to a reader's trust or a
from-scratch re-derivation.

At the 2026-08-05 tick, the live pins were OpenROAD 9dfaec5fb907, verilator
db524bc, yosys 600df904; RELEASED.json (image 0.2.63) recorded 2f9fbcd47e01,
63eb94a4cfa, 5b942cb. The assessment's stated bases matched NEITHER set except
yosys. `assess()` actually has THREE possible sources for its range base (a
ledger release tag, a fork-point sha, or the live pinned ref) and nothing in the
report said which one had been used, or whether the value was even a real commit
rather than a tag whose target could move.

These tests cover the two new primitives (`resolve_base_sha`,
`assessment_base_is_recorded`) directly, then prove all three of `assess()`'s
selection branches actually stamp the field they claim to.
"""
from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

import assess_release as A


# ── resolve_base_sha ─────────────────────────────────────────────────────────

def _git_repo_with_tag(root: Path, tag: str) -> str:
    """A minimal real git repo with one commit tagged `tag`. Returns the full
    40-hex sha `resolve_base_sha` must reproduce -- ground truth, not a guess."""
    root.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                    capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (root / "f.txt").write_text("x")
    run("add", "f.txt")
    run("commit", "-q", "-m", "init")
    run("tag", tag)
    return run("rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def forks_dir(tmp_path, monkeypatch):
    d = tmp_path / "forks"
    d.mkdir()
    monkeypatch.setenv("GK_FORKS_DIR", str(d))
    importlib.reload(A)
    yield d
    importlib.reload(A)  # restore the real FORKS_DIR for any test that runs after


def test_resolve_base_sha_from_a_tag(forks_dir):
    sha = _git_repo_with_tag(forks_dir / "magic", "8.3.674")
    assert A.resolve_base_sha("magic", "8.3.674") == sha


def test_resolve_base_sha_from_a_short_sha(forks_dir):
    sha = _git_repo_with_tag(forks_dir / "magic", "8.3.674")
    assert A.resolve_base_sha("magic", sha[:10]) == sha


def test_resolve_base_sha_no_clone_is_none_not_invented(forks_dir):
    """A tool with no local clone: unknown, never a guessed/truncated answer."""
    assert A.resolve_base_sha("no-such-tool", "v1") is None


def test_resolve_base_sha_unresolvable_ref_is_none(forks_dir):
    _git_repo_with_tag(forks_dir / "magic", "8.3.674")
    assert A.resolve_base_sha("magic", "no-such-tag-or-sha") is None


def test_resolve_base_sha_empty_ref_is_none(forks_dir):
    assert A.resolve_base_sha("magic", None) is None
    assert A.resolve_base_sha("magic", "") is None


# ── assessment_base_is_recorded ──────────────────────────────────────────────

def test_non_assessed_status_is_not_this_checks_concern():
    for status in ("clean", "pin_ahead_of_release", "error", "not_layered"):
        ok, why = A.assessment_base_is_recorded({"status": status})
        assert ok, f"{status}: {why}"
    ok, why = A.assessment_base_is_recorded({})
    assert ok, why


def test_assessed_with_full_provenance_passes():
    ok, why = A.assessment_base_is_recorded({
        "status": "assessed", "base_ref_source": "ledger_base_release_tag",
        "base_ref_sha": "a" * 40})
    assert ok, why


def test_missing_source_fails():
    ok, why = A.assessment_base_is_recorded({
        "status": "assessed", "base_ref_sha": "a" * 40})
    assert not ok
    assert "source" in why


def test_missing_sha_fails():
    ok, why = A.assessment_base_is_recorded({
        "status": "assessed", "base_ref_source": "our_pinned_ref"})
    assert not ok
    assert "did not resolve" in why


def test_a_short_sha_is_not_accepted_as_resolved():
    """A truncated sha LOOKS resolved and is not -- the whole point of #101 is
    that a reader must be able to git-checkout the EXACT commit named."""
    ok, why = A.assessment_base_is_recorded({
        "status": "assessed", "base_ref_source": "our_pinned_ref",
        "base_ref_sha": "abc1234"})
    assert not ok
    assert "40-hex" in why


# ── end-to-end through assess(): all three selection branches stamp the field
#    they claim to ───────────────────────────────────────────────────────────

def _stub_common(monkeypatch, tmp, commits=None):
    """Stubs every layer `assess()` calls except the base-selection logic itself
    and `resolve_base_sha` (which reads the REAL clone this fixture builds) --
    mirrors test_assess.py's `_cache_fixture`, narrowed to what these tests need."""
    monkeypatch.setenv("GK_STATE_DIR", str(tmp))
    importlib.reload(A)
    monkeypatch.setattr(A, "upstream_commits", lambda *a: (commits or [], []))
    monkeypatch.setattr(A, "our_patch_files", lambda *a: set())
    monkeypatch.setattr(A, "classify_commits", lambda tool, role, todo: {})
    monkeypatch.setattr(A, "_confirm_candidates", lambda *a, **k: {})
    (tmp / "ledger").mkdir(parents=True, exist_ok=True)
    return tmp


def test_release_range_branch_stamps_ledger_base_release_tag(monkeypatch, tmp_path,
                                                              forks_dir):
    sha = _git_repo_with_tag(forks_dir / "magic", "8.3.674")
    _stub_common(monkeypatch, tmp_path)
    monkeypatch.setattr(A, "release_gap_unknown", lambda led: False)
    monkeypatch.setattr(A, "release_gap", lambda led: 1)
    monkeypatch.setattr(A, "target_direction", lambda *a, **k: {
        "verdict": A.FORWARD, "target": None, "pin": None,
        "pin_ahead": None, "target_ahead": None, "why": "stub"})
    (tmp_path / "ledger" / "magic.json").write_text(json.dumps({
        "tool": "magic", "integrated": True, "behind_releases": 1, "behind_commits": 0,
        "upstream": "up/magic", "upstream_default_branch": "master",
        "pinned_ref_full": "b" * 40, "base_release": "8.3.674",
        "upstream_latest_release": "8.3.676", "role": "DRC"}))
    rep = A.assess("magic")
    assert rep["status"] == "assessed", rep
    assert rep["base_ref_source"] == "ledger_base_release_tag"
    assert rep["base_ref_sha"] == sha
    ok, why = A.assessment_base_is_recorded(rep)
    assert ok, why


def test_commit_range_fallback_stamps_our_pinned_ref(monkeypatch, tmp_path, forks_dir):
    """rel_gap unknown/zero but behind_commits > 0: `base_ref` is overridden to
    the live pin, and the label must say so -- this is the exact shape
    (OpenROAD, rolling master, no clean release framing) that motivated #101."""
    sha = _git_repo_with_tag(forks_dir / "openroad", "v1.0")
    _stub_common(monkeypatch, tmp_path)
    monkeypatch.setattr(A, "release_gap_unknown", lambda led: True)
    monkeypatch.setattr(A, "release_gap", lambda led: 0)
    monkeypatch.setattr(A, "target_direction", lambda *a, **k: {
        "verdict": A.FORWARD, "target": None, "pin": None,
        "pin_ahead": None, "target_ahead": None, "why": "stub"})
    (tmp_path / "ledger" / "openroad.json").write_text(json.dumps({
        "tool": "openroad", "integrated": True, "behind_releases": None,
        "behind_commits": 42, "upstream": "up/openroad",
        "upstream_default_branch": "master", "pinned_ref_full": sha,
        "base_release": "v0.9.0-beta", "upstream_latest_release": "v0.9.0-beta",
        "role": "PnR"}))
    rep = A.assess("openroad")
    assert rep["status"] == "assessed", rep
    assert rep["base_ref_source"] == "our_pinned_ref"
    assert rep["base_ref_sha"] == sha
    ok, why = A.assessment_base_is_recorded(rep)
    assert ok, why


def test_fork_point_only_branch_stamps_ledger_fork_point_sha(monkeypatch, tmp_path,
                                                              forks_dir):
    """No `base_release` tag at all, but a `fork_point.sha` -- and the release
    range and commit-range overrides both stay off, so the initial fork-point
    assignment must be what survives to the report."""
    sha = _git_repo_with_tag(forks_dir / "netgen", "1.5.300")
    _stub_common(monkeypatch, tmp_path)
    monkeypatch.setattr(A, "release_gap_unknown", lambda led: False)
    monkeypatch.setattr(A, "release_gap", lambda led: 1)
    monkeypatch.setattr(A, "target_direction", lambda *a, **k: {
        "verdict": A.FORWARD, "target": None, "pin": None,
        "pin_ahead": None, "target_ahead": None, "why": "stub"})
    (tmp_path / "ledger" / "netgen.json").write_text(json.dumps({
        "tool": "netgen", "integrated": True, "behind_releases": 1,
        "behind_commits": 0, "upstream": "up/netgen",
        "upstream_default_branch": "master", "pinned_ref_full": "c" * 40,
        "fork_point": {"sha": sha}, "upstream_latest_release": "1.5.323",
        "role": "LVS"}))
    rep = A.assess("netgen")
    assert rep["status"] == "assessed", rep
    assert rep["base_ref_source"] == "ledger_fork_point_sha"
    assert rep["base_ref_sha"] == sha
    ok, why = A.assessment_base_is_recorded(rep)
    assert ok, why


def test_a_tag_with_no_local_clone_reports_unresolved_honestly(monkeypatch,
                                                                tmp_path, forks_dir):
    """The clone genuinely absent (not this test's fault): base_ref_source is
    still recorded (we know WHICH thing we tried), base_ref_sha is None (we
    could not confirm it), and the check correctly refuses to call that PASS --
    "we could not tell" must not read as resolved."""
    _stub_common(monkeypatch, tmp_path)
    monkeypatch.setattr(A, "release_gap_unknown", lambda led: False)
    monkeypatch.setattr(A, "release_gap", lambda led: 1)
    monkeypatch.setattr(A, "target_direction", lambda *a, **k: {
        "verdict": A.FORWARD, "target": None, "pin": None,
        "pin_ahead": None, "target_ahead": None, "why": "stub"})
    (tmp_path / "ledger" / "ghost-tool.json").write_text(json.dumps({
        "tool": "ghost-tool", "integrated": True, "behind_releases": 1,
        "behind_commits": 0, "upstream": "up/ghost", "upstream_default_branch":
        "master", "pinned_ref_full": "d" * 40, "base_release": "9.9.9",
        "upstream_latest_release": "9.9.10", "role": "misc"}))
    rep = A.assess("ghost-tool")
    assert rep["status"] == "assessed", rep
    assert rep["base_ref_source"] == "ledger_base_release_tag"
    assert rep["base_ref_sha"] is None
    ok, why = A.assessment_base_is_recorded(rep)
    assert not ok
    assert "did not resolve" in why
