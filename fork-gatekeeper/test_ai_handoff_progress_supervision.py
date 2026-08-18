#!/usr/bin/env python3
"""The 05:30 AI handoff is progress-supervised and delivery-verified."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_0530 as D  # noqa: E402
import capture_release_ll as LL  # noqa: E402


_HERE = Path(__file__).resolve().parent
_FORK_ENTRIES = json.loads(
    (_HERE / "FORKS.json").read_text(encoding="utf-8"))["forks"]
_REGISTERED_FORKS = sorted(entry["tool"] for entry in _FORK_ENTRIES)
_REGISTRY_SHA256 = hashlib.sha256(
    (_HERE / "FORKS.json").read_bytes()).hexdigest()
_REGISTERED_REPOSITORIES = sorted(
    f"vibeic/{entry['tool']}" for entry in _FORK_ENTRIES)
_REQUIRED_REPOSITORIES = sorted(
    _REGISTERED_REPOSITORIES + ["vibeic/vibeic-eda"])
_EDA_HEAD = subprocess.run(
    ["git", "-C", str(_HERE.parent), "rev-parse", "HEAD^{commit}"],
    check=True, capture_output=True, text=True,
).stdout.strip()


def _write_no_watch_policy_registry(tmp_path: Path) -> tuple[Path, str]:
    """Pin LL-only fixtures to the contract they exercise.

    These tests predate watch-only repair and intentionally exercise AI handoff
    and release evidence in a fleet with no reset policy.  Do not let a policy
    later added to the production registry silently add an unrelated required
    sidecar to these fixtures.
    """
    registry = json.loads(
        (_HERE / "FORKS.json").read_text(encoding="utf-8"))
    for entry in registry["forks"]:
        entry.pop("watch_only_reset", None)
    path = tmp_path / "FORKS.no-watch-policy.json"
    path.write_text(
        json.dumps(registry, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _bind_ai_record_to_registry(record: dict, path: Path, digest: str) -> None:
    record["registry"] = {"source": str(path), "sha256": digest}


CASE = {
    "branch": "origin/vibeic/topic",
    "direction": "our branch -> our mainline",
    "commits": 1,
    "conflicting_files": ["src/x.cc"],
    "commits_detail": [{"sha": "abc123", "subject": "fix x"}],
}


def _report():
    return {"Demo": {"main": "main", "conflicted": [dict(CASE)]}}


def _supervised(*, outcome="natural", rc=0, out=""):
    return SimpleNamespace(
        outcome=outcome, rc=rc, out=out, err="", elapsed_s=25000.0,
    )


def _proofs(cases, verified):
    return [{
        "fork": c["fork"], "branch": c["branch"],
        "mainline": c.get("mainline"), "source_tip": c.get("source_tip"),
        "published_tip": "f" * 40 if verified else None,
        "verified": verified,
        "reason": "published" if verified else "still absent",
    } for c in cases]


def test_ai_can_run_past_the_old_total_limit_when_progressing(
        tmp_path, monkeypatch):
    captured = {}

    def run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _supervised(
            out=json.dumps({"type": "result", "result": "landed"}) + "\n")

    monkeypatch.setattr(D, "FORKS", tmp_path / "forks")
    monkeypatch.setattr(D, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(D.watchdog, "run_supervised", run)
    monkeypatch.setattr(D, "_verify_ai_cases", lambda cases: _proofs(cases, True))

    result = D.step2b_ai_decisions(
        _report(), tmp_path / "state", apply=True, stall_grace_s=90,
    )

    assert result["elapsed_seconds"] == 25000.0  # beyond the old 3600/5400 cap
    assert result["outcome"] == "natural" and result["verified"] is True
    assert result["tail"] == "landed"
    assert "--output-format" in captured["cmd"]
    assert "stream-json" in captured["cmd"]
    assert captured["kwargs"]["stall_grace_s"] == 90
    assert captured["kwargs"]["output_progress"] is True
    assert captured["kwargs"]["process_tree_cpu_progress"] is True
    assert "timeout" not in captured["kwargs"]
    assert json.loads((tmp_path / "state" / "ai_decisions_pending.json").read_text()) == []
    assert D._ai_needs_attention(result) is False


def test_stalled_ai_keeps_unverified_cases_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "FORKS", tmp_path / "forks")
    monkeypatch.setattr(D, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(
        D.watchdog, "run_supervised",
        lambda *a, **k: _supervised(outcome="stalled", rc=D.watchdog.RC_STALLED),
    )
    monkeypatch.setattr(D, "_verify_ai_cases", lambda cases: _proofs(cases, False))

    result = D.step2b_ai_decisions(
        _report(), tmp_path / "state", apply=True, stall_grace_s=12,
    )

    assert result["outcome"] == "stalled"
    assert result["rc"] == D.watchdog.RC_STALLED
    assert result["verified"] is False
    assert result["verified_cases"] == 0
    assert len(result["unverified_cases"]) == 1
    pending = json.loads(
        (tmp_path / "state" / "ai_decisions_pending.json").read_text())
    assert len(pending) == 1 and pending[0]["fork"] == "Demo"
    assert "still absent" in pending[0]["verification"]
    assert D._ai_needs_attention(result) is True


def test_rc_zero_alone_cannot_clear_an_unverified_case(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "FORKS", tmp_path / "forks")
    monkeypatch.setattr(D, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(
        D.watchdog, "run_supervised", lambda *a, **k: _supervised(rc=0),
    )
    monkeypatch.setattr(D, "_verify_ai_cases", lambda cases: _proofs(cases, False))

    result = D.step2b_ai_decisions(_report(), tmp_path / "state", apply=True)

    assert result["rc"] == 0 and result["outcome"] == "natural"
    assert result["verified"] is False
    assert D._ai_needs_attention(result) is True


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_postcondition_reads_the_published_mainline_not_process_rc(tmp_path):
    repo = tmp_path / "fork"
    origin = tmp_path / "origin.git"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    (repo / "x").write_text("base\n")
    _git(repo, "add", "x")
    _git(repo, "commit", "-qm", "base")
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)],
        check=True, timeout=30,
    )
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    _git(repo, "checkout", "-qb", "topic")
    (repo / "x").write_text("fixed\n")
    _git(repo, "commit", "-qam", "fix")
    source_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    case = {"fork": "Demo", "branch": "topic", "mainline": "main",
            "source_tip": source_tip, "path": str(repo)}
    before = D._verify_ai_case(case)
    assert before["verified"] is False
    assert "remain absent" in before["reason"]

    _git(repo, "merge", "--ff-only", "topic")
    _git(repo, "push", "-q", "origin", "main")
    after = D._verify_ai_case(case)
    assert after["verified"] is True
    assert after["published_tip"] == source_tip


def test_cron_wires_a_stall_grace_not_a_total_timeout():
    source = (Path(__file__).resolve().parent / "run_0530.sh").read_text()
    invocation = source[source.index('python3 "${DIR}/daily_0530.py"'):]
    invocation = invocation[:invocation.index("SIX=$?")]
    assert "--ai-stall-grace" in invocation
    assert "GK_AI_STALL_GRACE" in invocation
    assert "--ai-timeout" not in invocation


def _semantic_record(*, rc: int, outcome: str) -> dict:
    return {
        "schema": 1,
        "mechanism": "validated_semantic_progress_lease",
        "started_at": "2026-08-17T05:31:00+08:00",
        "total_runtime_ceiling_seconds": None,
        "stall_grace_seconds": 300,
        "outcome": outcome,
        "exit_code": rc,
        "elapsed_seconds": 42.0,
        "reason": "fixture",
        "progress": {"stage": "running" if rc == 2 else "finished"},
    }


def _gap_record() -> dict:
    def gap_row(tool: str) -> dict:
        return {
            "tool": tool,
            "integrated": True,
            "ahead": 0,
            "pin": "c" * 40,
            "pin_disagreement": None,
            "kind": "pin",
            "asserted_contents": None,
            "sync_lag": 0,
            "sync_lag_at_merge": 0,
            "release_lag": 0,
            "image_behind": 0,
            "ours_unshipped": 0,
            "ours_unshipped_substantive": 0,
            "unshipped_commits": [],
            "tip_state": "current",
            "tip_note": None,
            "merge_ref_seen": "b" * 40,
        }

    return {
        "program": "fork_gap_report",
        "schema_version": 1,
        "started_at": "2026-08-17T06:00:00+08:00",
        "completed_at": "2026-08-17T06:01:00+08:00",
        "status": "complete",
        "source": {
            "kind": "daily_0530_merge_round",
            "merge_evidence": "/state/daily_0530.json",
            "round_at": "2026-08-17T05:30:00+08:00",
            "_round": {
                "schema_version": 1,
                "run_id": "a" * 64,
                "started_at": "2026-08-17T05:30:01+08:00",
                "finished_at": "2026-08-17T05:59:00+08:00",
                "applied": True,
            },
        },
        "source_errors": [],
        "assertions": [],
        "q1_sync_lag_at_merge": 0,
        "q1_unmeasured_at_merge": [],
        "q1_sync_lag": 0,
        "q1_release_lag": 0,
        "q1_image_behind_upstream": 0,
        "q1_forks_behind": 0,
        "q1_unmeasured": [],
        "q2_forks_not_built_from_ours": [],
        "q2_our_commits_stranded": 0,
        "q2_ours_past_the_pin": 0,
        "q2_ours_past_the_pin_substantive": 0,
        "q2_unshipped_commits": [],
        "q2_unmeasured_ship": [],
        "q2_unmeasured_stranded": [],
        "tip_rejected": [],
        "tip_unverified": [],
        "registered_forks": _REGISTERED_FORKS,
        "ledger_forks": _REGISTERED_FORKS,
        "merge_tips_seen": {tool: "b" * 40
                            for tool in _REGISTERED_FORKS},
        "fleet_errors": [],
        "rows": [gap_row(tool) for tool in _REGISTERED_FORKS],
        "measured_with_fetch": True,
    }


def _fork_github_record(*, status="converged", started_at=None) -> dict:
    catalog_repositories = [
        {
            "repository": repository,
            "is_fork": repository in _REGISTERED_REPOSITORIES,
            "is_archived": False,
            "has_issues_enabled": True,
        }
        for repository in sorted(
            _REQUIRED_REPOSITORIES + ["vibeic/vibe-ic"])
    ]
    return {
        "program": "fork_github_convergence",
        "schema_version": 2,
        "started_at": started_at or "2026-08-17T05:32:00+08:00",
        "completed_at": "2026-08-17T05:33:00+08:00",
        "actor": "reyerchu",
        "expected_actor": "reyerchu",
        "round": {
            "round_at": "2026-08-17T05:30:00+08:00",
            "schema_version": 1,
            "run_id": "a" * 64,
            "started_at": "2026-08-17T05:30:01+08:00",
            "finished_at": "2026-08-17T05:31:00+08:00",
        },
        "inventory": {
            "status": "complete",
            "catalog": {
                "outcome": "complete",
                "total_repositories": len(catalog_repositories),
                "repositories": catalog_repositories,
                "live_fork_repositories": list(_REGISTERED_REPOSITORIES),
                "unregistered_live_fork_repositories": [],
            },
            "canonical_registered_repositories": list(
                _REGISTERED_REPOSITORIES),
            "required_repositories": list(_REQUIRED_REPOSITORIES),
            "required_repository_count": len(_REQUIRED_REPOSITORIES),
            "completed_repository_count": len(_REQUIRED_REPOSITORIES),
            "repositories": [
                {
                    "repository": repository,
                    "scope": (
                        "registered_fork"
                        if repository in _REGISTERED_REPOSITORIES
                        else "vibeic_eda"
                    ),
                    "status": "complete",
                    "is_fork": repository in _REGISTERED_REPOSITORIES,
                    "is_archived": False,
                    "has_issues_enabled": True,
                    "issues": {"outcome": "complete", "open_count": 0},
                    "pull_requests": {
                        "outcome": "complete", "open_count": 0},
                }
                for repository in _REQUIRED_REPOSITORIES
            ],
        },
        "delivery": {
            "repository": "vibeic/vibe-ic",
            "status": "complete",
            "pr_head_patterns": sorted(LL.DELIVERY_HEAD_PATTERN_TEXT),
            "issues": {
                "outcome": "excluded_no_durable_producer_marker"},
            "pull_requests": {
                "outcome": "complete", "open_count": 0,
                "matched_count": 0, "excluded_count": 0, "excluded": [],
            },
        },
        "upstream_actor": {
            "status": "complete",
            "issues": {
                "outcome": "complete", "seen_count": 0,
                "matched_count": 0},
            "pull_requests": {
                "outcome": "complete", "seen_count": 0,
                "matched_count": 0},
        },
        "round_upstream": {
            "status": "complete",
            "required_fork_count": len(_FORK_ENTRIES),
            "completed_fork_count": len(_FORK_ENTRIES),
            "forks": [
                {
                    "tool": entry["tool"],
                    "repository": entry["upstream"],
                    "outcome": "complete",
                    "open_count": 0,
                }
                for entry in _FORK_ENTRIES
            ],
        },
        "status": status,
        "counts": {
            "items": 0, "issues": 0, "pull_requests": 0,
            "registered_fork_items": 0, "upstream_items": 0,
            "vibeic_eda_items": 0, "fork_delivery_items": 0,
            "by_source": {
                "exact_repository_issues": 0,
                "exact_repository_pull_requests": 0,
                "fork_delivery_pull_requests": 0,
                "viewer_upstream_issues": 0,
                "viewer_upstream_pull_requests": 0,
                "round_upstream_pull_requests": 0,
            },
        },
        "items": [],
        "source_errors": [],
    }


def _ai_authorization_record(*, status="authorized", run_id=None) -> dict:
    verified = status == "authorized"
    result = {
        "fork": "Demo", "branch": "origin/vibeic/topic",
        "mainline": "main", "source_tip": "b" * 40,
        "published_tip": "c" * 40 if verified else "d" * 40,
        "verified": verified,
        "reason": "published" if verified else "still absent",
    }
    return {
        "program": "check_ai_release_authorization",
        "schema_version": 1,
        "checked_at": "2026-08-17T06:02:00+08:00",
        "round_at": "2026-08-17T05:30:00+08:00",
        "source": "/state/daily_0530.json",
        "registered_forks": _REGISTERED_FORKS,
        "registered_count": len(_REGISTERED_FORKS),
        "registry": {
            "source": str(_HERE / "FORKS.json"),
            "sha256": _REGISTRY_SHA256,
        },
        "round": {
            "schema_version": 1, "run_id": run_id or "a" * 64,
            "started_at": "2026-08-17T05:30:01+08:00",
            "finished_at": "2026-08-17T05:59:00+08:00",
            "applied": True,
        },
        "status": status,
        "case_count": 1,
        "verified_count": int(verified),
        "unverified_count": int(not verified),
        "transport": {"invoked": True, "outcome": "natural", "rc": 0},
        "case_results": [result],
        "reasons": [] if verified else [
            "1 AI conflict case(s) lack a verified published-mainline postcondition"],
        "source_errors": [],
    }


def _release_pins_record(version="0.3.5") -> dict:
    return {
        "program": "check_release_pins_committed",
        "tool": "check_release_pins_committed",
        "schema_version": 1,
        "checked_at": "2026-08-17T06:03:00+08:00",
        "round_at": "2026-08-17T05:30:00+08:00",
        "run_id": "a" * 64,
        "verdict": "PASS",
        "findings": [],
        "version": version,
        "rev": "HEAD",
        "revision": _EDA_HEAD,
        "recorded_count": 34,
        "committed_count": 34,
        "outstanding": [],
    }


def test_cron_does_not_invoke_release_without_all_green_controls():
    source = (Path(__file__).resolve().parent / "run_0530.sh").read_text()
    gate = source[source.index('TICK_STATE="ran"'):
                  source.index("# Step 6")]
    assert 'if [ "${TESTS}" -ne 0 ]' in gate
    assert 'elif [ "${CODE}" -ne 0 ]' in gate
    assert 'elif [ "${AI_AUTH}" -ne 0 ]' in gate
    assert gate.count('"${DIR}/run_tick.sh"') == 1
    assert 'TICK=2' in gate and "NOT CHECKED" in gate

    page_gate = source[source.index("if [ \"${DISC}\""):
                       source.index("# THE ROUND MUST NOTICE")]
    assert '[ "${CODE}" -eq 0 ]' in page_gate
    assert '--semantic-pytest-json "${PYTEST_RETAINED}"' in source
    assert '--ai-authorization-json "${AI_AUTH_JSON}"' in source
    assert '--tick-state "${TICK_STATE}"' in source


def test_stalled_norecord_is_retained_without_reusing_old_release(
        tmp_path, capsys):
    forks_json, registry_digest = _write_no_watch_policy_registry(tmp_path)
    old_release = tmp_path / "daily-release.json"
    old_release.write_text(json.dumps({
        "program": "daily_release", "status": "published",
        "started_at": "2026-08-16T05:31:00+08:00",
        "version": {"to": "0.3.0"},
    }))
    semantic = tmp_path / "semantic.json"
    semantic.write_text(json.dumps(_semantic_record(rc=2, outcome="stalled")))
    gap = tmp_path / "gap.json"
    gap.write_text(json.dumps(_gap_record()))
    fork_github = tmp_path / "fork-github.json"
    fork_github.write_text(json.dumps(_fork_github_record()))
    ai_authorization = tmp_path / "ai-authorization.json"
    ai_record = _ai_authorization_record()
    _bind_ai_record_to_registry(ai_record, forks_json, registry_digest)
    ai_authorization.write_text(json.dumps(ai_record))

    rc = LL.main([
        "--state-dir", str(tmp_path / "state"),
        "--release-json", str(old_release),
        "--release-pins-json", str(tmp_path / "release-pins-not-read.json"),
        "--gap-json", str(gap),
        "--fork-github-json", str(fork_github),
        "--ai-authorization-json", str(ai_authorization),
        "--forks-json", str(forks_json),
        "--expected-github-actor", "reyerchu",
        "--semantic-pytest-json", str(semantic),
        "--round-at", "2026-08-17T05:30:00+08:00",
        "--tick-exit", "2",
        "--tick-state", "not_checked_pytest_norecord",
    ])
    capsys.readouterr()
    record = json.loads(
        (tmp_path / "state" / "release-ll" / "latest.json").read_text())
    assert rc == 0
    assert record["release"]["status"] == "not_checked"
    assert record["release"]["target"] is None  # not yesterday's 0.3.0
    assert record["selftest"]["status"] == "norecord"
    assert record["selftest"]["outcome"] == "stalled"
    assert record["lesson"]["trigger"] == "pytest_stalled"
    assert record["lesson"]["outcome"] == "release_not_checked"
    assert record["source_errors"] == []


def _capture_ai_gate(tmp_path: Path, ai_value: dict | None,
                     tick_state: str) -> tuple[int, dict, dict]:
    forks_json, registry_digest = _write_no_watch_policy_registry(tmp_path)
    semantic = tmp_path / "semantic.json"
    semantic.write_text(json.dumps(_semantic_record(rc=0, outcome="natural")))
    gap = tmp_path / "gap.json"
    gap.write_text(json.dumps(_gap_record()))
    fork_github = tmp_path / "fork-github.json"
    fork_github.write_text(json.dumps(_fork_github_record()))
    ai = tmp_path / "ai-authorization.json"
    if ai_value is not None:
        _bind_ai_record_to_registry(
            ai_value, forks_json, registry_digest)
        ai.write_text(json.dumps(ai_value))
    state = tmp_path / "state"
    rc = LL.main([
        "--state-dir", str(state),
        "--release-json", str(tmp_path / "release-must-not-be-read.json"),
        "--release-pins-json", str(tmp_path / "release-pins-not-read.json"),
        "--gap-json", str(gap),
        "--fork-github-json", str(fork_github),
        "--ai-authorization-json", str(ai),
        "--forks-json", str(forks_json),
        "--expected-github-actor", "reyerchu",
        "--semantic-pytest-json", str(semantic),
        "--round-at", "2026-08-17T05:30:00+08:00",
        "--tick-exit", "2", "--tick-state", tick_state,
    ])
    full = json.loads((state / "release-ll" / "latest.json").read_text())
    narrow = json.loads(
        (state / "release-ll" / "index.jsonl").read_text().splitlines()[-1])
    return rc, full, narrow


def test_ll_retains_the_exact_ai_postcondition_that_blocked_release(
        tmp_path, capsys):
    blocked = _ai_authorization_record(status="blocked")
    rc, full, narrow = _capture_ai_gate(
        tmp_path, blocked, "not_checked_ai_unverified")
    capsys.readouterr()

    assert rc == 0  # a measured rejection is a complete LL input
    assert full["release"]["status"] == "not_checked"
    assert full["ai_authorization"] == blocked
    assert narrow["ai_authorization"] == blocked
    assert full["convergence"]["ai_authorized"] is False
    assert full["convergence"]["converged"] is False
    assert full["lesson"]["trigger"] == "ai_postcondition_unverified"
    assert full["lesson"]["outcome"] == (
        "release_blocked_by_unverified_ai_decision")
    assert full["source_errors"] == []


@pytest.mark.parametrize("kind", ["missing", "malformed"])
def test_ll_retains_norecord_when_ai_authorization_is_missing_or_malformed(
        tmp_path, capsys, kind):
    value = None
    if kind == "malformed":
        value = _ai_authorization_record()
        value["case_count"] = 2  # contradicts its one-result list
    rc, full, narrow = _capture_ai_gate(
        tmp_path, value, "not_checked_ai_norecord")
    capsys.readouterr()

    assert rc == 1
    assert full["release"]["status"] == "not_checked"
    assert full["ai_authorization"]["status"] == "norecord"
    assert narrow["ai_authorization"]["status"] == "norecord"
    assert full["convergence"]["ai_authorized"] is False
    assert full["convergence"]["converged"] is False
    assert full["lesson"]["outcome"] == "could_not_measure_inputs"
    assert any("ai-authorization.json" in error
               for error in full["source_errors"])


def test_red_and_norecord_remain_different_ll_facts():
    red = LL.make_record(
        {"status": "not_checked"}, _gap_record(),
        "2026-08-17T05:30:00+08:00", 2,
        semantic_pytest=_semantic_record(rc=1, outcome="natural"),
        tick_state="not_checked_pytest_red",
    )
    norecord = LL.make_record(
        {"status": "not_checked"}, _gap_record(),
        "2026-08-17T05:30:00+08:00", 2,
        semantic_pytest=_semantic_record(rc=2, outcome="stalled"),
        tick_state="not_checked_pytest_norecord",
    )
    assert (red["selftest"]["status"], red["selftest"]["outcome"],
            red["lesson"]["trigger"]) == ("red", "natural", "pytest_red")
    assert (norecord["selftest"]["status"],
            norecord["selftest"]["outcome"],
            norecord["lesson"]["trigger"]) == (
                "norecord", "stalled", "pytest_stalled")


def test_a_ran_tick_cannot_reuse_a_prior_round_release(tmp_path, capsys):
    forks_json, registry_digest = _write_no_watch_policy_registry(tmp_path)
    release = tmp_path / "daily-release.json"
    release.write_text(json.dumps({
        "program": "daily_release", "status": "published",
        "started_at": "2026-08-16T05:31:00+08:00",
        "version": {"to": "0.3.0"},
    }))
    semantic = tmp_path / "semantic.json"
    semantic.write_text(json.dumps(_semantic_record(rc=0, outcome="natural")))
    gap = tmp_path / "gap.json"
    gap.write_text(json.dumps(_gap_record()))
    fork_github = tmp_path / "fork-github.json"
    fork_github.write_text(json.dumps(_fork_github_record()))
    ai_authorization = tmp_path / "ai-authorization.json"
    ai_record = _ai_authorization_record()
    _bind_ai_record_to_registry(ai_record, forks_json, registry_digest)
    ai_authorization.write_text(json.dumps(ai_record))
    release_pins = tmp_path / "release-pins.json"
    release_pins.write_text(json.dumps(_release_pins_record("0.3.0")))

    rc = LL.main([
        "--state-dir", str(tmp_path / "state"),
        "--release-json", str(release),
        "--release-pins-json", str(release_pins),
        "--gap-json", str(gap),
        "--fork-github-json", str(fork_github),
        "--ai-authorization-json", str(ai_authorization),
        "--forks-json", str(forks_json),
        "--expected-github-actor", "reyerchu",
        "--semantic-pytest-json", str(semantic),
        "--round-at", "2026-08-17T05:30:00+08:00",
        "--tick-exit", "1", "--tick-state", "ran",
    ])
    capsys.readouterr()
    record = json.loads(
        (tmp_path / "state" / "release-ll" / "latest.json").read_text())
    assert rc == 1
    assert record["release"]["status"] == "unmeasured"
    assert record["release"]["target"] is None
    assert record["lesson"]["outcome"] == "could_not_measure_inputs"
    assert any("predates this 05:30 round" in e
               for e in record["source_errors"])


def _capture_ran_tick(tmp_path: Path, release: dict,
                      semantic: dict) -> tuple[int, dict]:
    forks_json, registry_digest = _write_no_watch_policy_registry(tmp_path)
    release_path = tmp_path / "daily-release.json"
    release_path.write_text(json.dumps(release))
    semantic_path = tmp_path / "semantic.json"
    semantic_path.write_text(json.dumps(semantic))
    gap = tmp_path / "gap.json"
    gap.write_text(json.dumps(_gap_record()))
    fork_github = tmp_path / "fork-github.json"
    fork_github.write_text(json.dumps(_fork_github_record()))
    ai_authorization = tmp_path / "ai-authorization.json"
    ai_record = _ai_authorization_record()
    _bind_ai_record_to_registry(ai_record, forks_json, registry_digest)
    ai_authorization.write_text(json.dumps(ai_record))
    target = LL._target(release) or "0.3.5"
    release_pins = tmp_path / "release-pins.json"
    release_pins.write_text(json.dumps(_release_pins_record(target)))

    rc = LL.main([
        "--state-dir", str(tmp_path / "state"),
        "--release-json", str(release_path),
        "--release-pins-json", str(release_pins),
        "--gap-json", str(gap),
        "--fork-github-json", str(fork_github),
        "--ai-authorization-json", str(ai_authorization),
        "--forks-json", str(forks_json),
        "--expected-github-actor", "reyerchu",
        "--semantic-pytest-json", str(semantic_path),
        "--round-at", "2026-08-17T05:30:00+08:00",
        "--tick-exit", "0", "--tick-state", "ran",
    ])
    record = json.loads(
        (tmp_path / "state" / "release-ll" / "latest.json").read_text())
    return rc, record


def test_future_release_evidence_cannot_make_the_ll_green(tmp_path, capsys):
    release = {
        "program": "daily_release", "schema_version": 2,
        "status": "published",
        "started_at": "2099-08-17T05:31:00+08:00",
        "finished_at": "2099-08-17T05:32:00+08:00",
        "version": {"to": "0.3.5"},
    }
    rc, record = _capture_ran_tick(
        tmp_path, release, _semantic_record(rc=0, outcome="natural"))
    capsys.readouterr()

    assert rc == 1
    assert record["release"]["status"] == "unmeasured"
    assert record["convergence"]["converged"] is False
    assert any("daily-release.json is dated in the future" in error
               for error in record["source_errors"])


def test_future_semantic_evidence_cannot_make_the_ll_green(tmp_path, capsys):
    release = {
        "program": "daily_release", "schema_version": 2,
        "status": "published",
        "started_at": "2026-08-17T05:34:00+08:00",
        "finished_at": "2026-08-17T05:35:00+08:00",
        "version": {"to": "0.3.5"},
    }
    semantic = _semantic_record(rc=0, outcome="natural")
    semantic["started_at"] = "2099-08-17T05:31:00+08:00"
    rc, record = _capture_ran_tick(tmp_path, release, semantic)
    capsys.readouterr()

    assert rc == 1
    assert record["selftest"] is None
    assert record["convergence"]["converged"] is False
    assert any("semantic.json is dated in the future" in error
               for error in record["source_errors"])
