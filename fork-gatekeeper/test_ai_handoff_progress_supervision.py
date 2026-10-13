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
    monkeypatch.setenv("GK_AI_BACKEND", "claude")
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
    monkeypatch.setenv("GK_AI_BACKEND", "claude")
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
    monkeypatch.setenv("GK_AI_BACKEND", "claude")
    monkeypatch.setattr(D, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(
        D.watchdog, "run_supervised", lambda *a, **k: _supervised(rc=0),
    )
    monkeypatch.setattr(D, "_verify_ai_cases", lambda cases: _proofs(cases, False))

    result = D.step2b_ai_decisions(_report(), tmp_path / "state", apply=True)

    assert result["rc"] == 0 and result["outcome"] == "natural"
    assert result["verified"] is False
    assert D._ai_needs_attention(result) is True


@pytest.mark.parametrize("configured", [None, "codex"])
def test_codex_handoff_is_selected_and_supervised(tmp_path, monkeypatch, configured):
    # The same existing conflict handoff must work when Claude subscription
    # access is disabled. A missing CLI or failed turn must never clear work.
    captured = {}
    if configured is None:
        monkeypatch.delenv("GK_AI_BACKEND", raising=False)
    else:
        monkeypatch.setenv("GK_AI_BACKEND", configured)
    monkeypatch.setattr(D, "FORKS", tmp_path / "forks")
    monkeypatch.setattr(D, "_claude_bin", lambda: "/bin/disabled-claude")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/codex" if name == "codex" else None)
    def run(cmd, **kwargs):
        captured.update(cmd=cmd, kwargs=kwargs,
                        brief=(tmp_path / "state" / "ai_decisions_brief.txt").read_text())
        return _supervised(out=json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "source landed"}}) + "\n")
    monkeypatch.setattr(D.watchdog, "run_supervised", run)
    monkeypatch.setattr(D, "_verify_ai_cases", lambda cases: _proofs(cases, True))
    result = D.step2b_ai_decisions(_report(), tmp_path / "state", apply=True)
    assert captured["cmd"][0:2] == ["/bin/codex", "exec"]
    assert "--json" in captured["cmd"]
    assert captured["cmd"][-1] == captured["brief"]
    assert captured["kwargs"]["output_progress"] is True
    assert captured["kwargs"]["process_tree_cpu_progress"] is True
    assert "timeout" not in captured["kwargs"]
    assert result["tail"] == "source landed"
    assert result["verified"] is True


@pytest.mark.parametrize("backend,available,rc,verified", [
    ("codex", False, 0, False),
    ("codex", True, 1, False),
    ("codex", True, 0, False),
    ("unsupported", True, 0, False),
])
def test_backend_refusal_or_undelivered_turn_keeps_the_case_pending(
        tmp_path, monkeypatch, backend, available, rc, verified):
    monkeypatch.setenv("GK_AI_BACKEND", backend)
    monkeypatch.setattr(D, "FORKS", tmp_path / "forks")
    monkeypatch.setattr(D, "_claude_bin", lambda: "/bin/disabled-claude")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/codex" if available else None)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return _supervised(rc=rc)
    monkeypatch.setattr(D.watchdog, "run_supervised", run)
    monkeypatch.setattr(D, "_verify_ai_cases", lambda cases: _proofs(cases, verified))
    result = D.step2b_ai_decisions(_report(), tmp_path / "state", apply=True)
    assert all(cmd[0] != "/bin/disabled-claude" for cmd in calls)
    if not available or backend == "unsupported":
        assert calls == []
        assert result["invoked"] is False and result["error"]
    assert D._ai_needs_attention(result) is True
    assert len(json.loads((tmp_path / "state" / "ai_decisions_pending.json").read_text())) == 1


def test_usage_limit_is_retained_as_unavailable_without_provider_replay(tmp_path, monkeypatch):
    message = "You've hit your usage limit. Try again later."
    stream = json.dumps({"type": "error", "message": message}) + "\n" + json.dumps({
        "type": "turn.failed", "error": {"message": message}})
    monkeypatch.setenv("GK_AI_BACKEND", "codex")
    monkeypatch.setattr(D, "FORKS", tmp_path / "forks")
    monkeypatch.setattr("shutil.which", lambda _: "/bin/codex")
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return _supervised(rc=1, out=stream)
    monkeypatch.setattr(D.watchdog, "run_supervised", run)
    monkeypatch.setattr(D, "_verify_ai_cases", lambda cases: _proofs(cases, False))
    result = D.step2b_ai_decisions(_report(), tmp_path / "state", apply=True)
    assert "usage_limit" in result.get("error", ""), result
    assert result["transport_failure"] == {"kind": "usage_limit", "message": message}
    assert result["verified"] is False and len(result["unverified_cases"]) == 1
    assert len(calls) == 1 and calls[0][0] == "/bin/codex"
    assert len(json.loads((tmp_path / "state" / "ai_decisions_pending.json").read_text())) == 1


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


@pytest.mark.parametrize("delivered", [True, False])
@pytest.mark.parametrize("fetch_fault", [None, "failed", "stalled", "missing"])
def test_isolated_publisher_verification_preserves_checkout(
        tmp_path, monkeypatch, delivered, fetch_fault):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    repo = tmp_path / "observer"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    _git(repo, "add", "base")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-qb", "topic")
    (repo / "fix").write_text("required source change\n")
    _git(repo, "add", "fix")
    _git(repo, "commit", "-qm", "source change")
    source = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "topic")
    _git(repo, "checkout", "-q", "main")
    publisher = tmp_path / "publisher"
    _git(tmp_path, "clone", "-q", str(origin), str(publisher))
    _git(publisher, "config", "user.name", "publisher")
    _git(publisher, "config", "user.email", "publisher@example.invalid")
    if delivered:
        _git(publisher, "merge", "--no-ff", "-qm", "deliver source", "origin/topic")
    else:
        (publisher / "unrelated").write_text("source still absent\n")
        _git(publisher, "add", "unrelated")
        _git(publisher, "commit", "-qm", "unrelated change")
    published = _git(publisher, "rev-parse", "HEAD")
    _git(publisher, "push", "-q", "origin", "main")
    assert subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", published],
        capture_output=True).returncode != 0
    # Verification may import objects, but must preserve refs and local work.
    (repo / "base").write_text("staged local edit\n")
    _git(repo, "add", "base")
    (repo / "base").write_text("unstaged local edit\n")
    (repo / ".git" / "FETCH_HEAD").write_text("preserve this receipt\n")
    def snapshot():
        return (_git(repo, "show-ref"), _git(repo, "rev-parse", "HEAD"),
                _git(repo, "diff"), _git(repo, "diff", "--cached"),
                (repo / ".git" / "FETCH_HEAD").read_bytes())
    before = snapshot()
    real_sh = D.sh
    fetches = []
    def run(*args, **kwargs):
        if "fetch" in args:
            fetches.append((args, kwargs))
            if fetch_fault:
                rc = {"failed": 128, "stalled": D.watchdog.RC_STALLED,
                      "missing": 0}[fetch_fault]
                return subprocess.CompletedProcess(args, rc, "", fetch_fault)
        return real_sh(*args, **kwargs)
    monkeypatch.setattr(D, "sh", run)
    result = D._verify_ai_case({
        "fork": "Demo", "branch": "topic", "mainline": "main",
        "source_tip": source, "path": str(repo)})
    assert result["verified"] is (delivered and fetch_fault is None), result
    assert result["published_tip"] == published
    assert snapshot() == before
    assert len(fetches) == 1
    args, kwargs = fetches[0]
    assert args[-2:] == ("origin", published)
    assert "--no-recurse-submodules" in args
    assert "--no-write-fetch-head" in args and "--refmap=" in args
    assert kwargs["progress"] is True and "timeout" not in kwargs
    if fetch_fault:
        assert "fetch" in result["reason"]
    elif not delivered:
        assert "remain absent" in result["reason"]


def _semantic_record(*, rc: int, outcome: str) -> dict:
    process_rc = 0 if rc == 0 else (1 if rc == 1 else 0)
    cases = 1 if rc in {0, 1} else 0
    return {
        "schema": 1,
        "mechanism": "validated_semantic_progress_lease",
        "started_at": "2026-08-17T06:02:10+08:00",
        "total_runtime_ceiling_seconds": None,
        "stall_grace_seconds": 300,
        "outcome": outcome,
        "process_rc": process_rc,
        "exit_code": rc,
        "elapsed_seconds": 42.0,
        "junit": "/state/fork-gatekeeper.junit.xml",
        "junit_cases": cases,
        "junit_red": 1 if rc == 1 else 0,
        "reason": "fixture",
        "progress": {
            "events": 3 if cases else 0,
            "last_event": "session_finish" if cases else None,
            "stage": "running" if rc == 2 else "finished",
            "selected": cases, "finished": cases,
            "exitstatus": process_rc if cases else None,
            "protocol_error": None,
        },
        "process_tree": {
            "subreaper": True, "census_complete": True,
            "live_descendants_observed": [], "cleanup_observed": [],
            "cleanup_survivors": [], "final_empty": True,
        },
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
        "started_at": "2026-08-17T06:06:00+08:00",
        "completed_at": "2026-08-17T06:07:00+08:00",
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
        # #79 — the round is judged at the merge boundary, so a fixture for a
        # converged round has to carry the at-merge pair and say that nothing
        # went unmeasured. Absent is not zero.
        "q1_image_behind_upstream_at_merge": 0,
        "q1_unmeasured_image_behind_at_merge": [],
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
        "started_at": started_at or "2026-08-17T06:08:00+08:00",
        "completed_at": "2026-08-17T06:09:00+08:00",
        "actor": "reyerchu",
        "expected_actor": "reyerchu",
        "round": {
            "round_at": "2026-08-17T05:30:00+08:00",
            "schema_version": 1,
            "run_id": "a" * 64,
            "started_at": "2026-08-17T05:30:01+08:00",
            "finished_at": "2026-08-17T05:59:00+08:00",
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
            # See the producer: alarms leave `items` and are counted here.
            "alarm_items_excluded": 0,
            # And so do issues we filed in upstream repositories.
            "upstream_issue_items_excluded": 0,
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


# #78 — kept in step with the copy in `test_fork_github_convergence_ll`. The
# default is the shape a round that RELEASES produces: the record on disk is
# run_tick's re-verification, so it POSTDATES the gate it authorized.
AI_CHECKED_AT_TICK_RAN = "2026-08-17T06:02:55+08:00"
AI_CHECKED_AT_TICK_NOT_RUN = "2026-08-17T06:02:00+08:00"


def _ai_authorization_record(*, status="authorized", run_id=None,
                             checked_at=AI_CHECKED_AT_TICK_RAN) -> dict:
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
        "checked_at": checked_at,
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
        "checked_at": "2026-08-17T06:04:30+08:00",
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


def _published_release_record(version="0.3.5") -> dict:
    manifest = "e" * 64
    return {
        "program": "daily_release", "schema_version": 2,
        "status": "published", "exit_code": 0,
        "started_at": "2026-08-17T06:03:30+08:00",
        "finished_at": "2026-08-17T06:04:00+08:00",
        "version": {"from": "0.3.4", "to": version},
        "version_candidate": {"from": "0.3.4", "to": version},
        "moved": [], "built": [],
        "compose_attempts": [{"attempt": 1, "exit_code": 0}],
        "registry_composed": True, "pushed": True,
        "smoke": {"executed": True, "expected": 16, "passed": 16,
                  "outcome": "natural", "failures": []},
        "remote_publication": {
            "verified": True,
            "manifests": {
                "version": {
                    "ref": f"ghcr.io/vibeic/vibeic-eda:{version}",
                    "state": "present", "source": "registry",
                    "identity": manifest, "detail": "fixture",
                },
                "latest": {
                    "ref": "ghcr.io/vibeic/vibeic-eda:latest",
                    "state": "present", "source": "registry",
                    "identity": manifest, "detail": "fixture",
                },
            },
        },
        "latest_publication": {
            "ref": "ghcr.io/vibeic/vibeic-eda:latest",
            "tagged": True, "pushed": True,
        },
        "release_record_publication": {
            "committed": True, "verified": True,
            "subject": "origin/main:RELEASED.json", "identity": _EDA_HEAD,
            "initial_state": "present", "repair_attempted": False,
        },
        "anchor_pr": {"ok": True, "note": f"anchor {version} verified"},
    }


def _noop_release_record(version="0.3.5") -> dict:
    release = _published_release_record(version)
    release.update({
        "status": "noop",
        "version": {"from": version, "to": version},
        "version_candidate": None,
        "compose_attempts": [],
        "registry_composed": None,
        "pushed": None,
        "smoke": {"executed": False, "expected": 16, "passed": 0,
                  "outcome": "not_applicable", "failures": []},
        "latest_publication": None,
        "release_record_publication": {
            "verified": True, "subject": "origin/main:RELEASED.json",
            "identity": _EDA_HEAD, "initial_state": "present",
            "repair_attempted": False,
        },
        "anchor_pr": {"ok": True, "note": f"anchor {version} verified"},
    })
    return release


def test_cron_does_not_invoke_release_without_all_green_controls():
    source = (Path(__file__).resolve().parent / "run_0530.sh").read_text()
    gate = source[source.index('TICK_STATE="ran"'):
                  source.index("# Step 6")]
    assert 'if [ "${TESTS}" -ne 0 ]' in gate
    assert 'elif [ "${CODE}" -ne 0 ]' in gate
    assert 'elif [ "${AI_AUTH}" -ne 0 ]' in gate
    assert gate.count('"${DIR}/run_tick.sh"') == 1
    assert 'TICK=2' in gate and "NOT CHECKED" in gate

    page_gate = source[source.index("# PUBLISH LAST"):
                       source.index("# 0 only when BOTH are clean")]
    for authority in (
        "TESTS", "CODE", "AI_AUTH", "TICK", "DISC", "GAP", "FORK_GH",
        "LL", "FRESH", "INPUT_CLEAR", "GAP_FATAL",
    ):
        assert f'[ "${{{authority}}}" -eq 0 ]' in page_gate
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
    ai_record = _ai_authorization_record(
        checked_at=AI_CHECKED_AT_TICK_NOT_RUN)   # this test's tick is NOT CHECKED
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
    blocked = _ai_authorization_record(status="blocked",
                                       checked_at=AI_CHECKED_AT_TICK_NOT_RUN)
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


@pytest.mark.parametrize("release_factory", [
    pytest.param(_published_release_record, id="published"),
    pytest.param(_noop_release_record, id="noop"),
])
def test_complete_release_terminal_proof_can_form_a_clean_ll(
        tmp_path, capsys, release_factory):
    rc, record = _capture_ran_tick(
        tmp_path, release_factory(),
        _semantic_record(rc=0, outcome="natural"))
    capsys.readouterr()

    assert rc == 0
    assert record["source_errors"] == []
    assert record["release"]["status"] in {"published", "noop"}


@pytest.mark.parametrize("mutate,needle", [
    # #78 — this fixture is a RAN tick, so the record on disk is run_tick's
    # re-verification and the out-of-order shape is the OTHER one: an
    # authorization older than the gate run it vouches for. Pushing the gate
    # past the authorization (06:02:55) is how that is expressed without
    # reaching into the AI record.
    pytest.param(
        lambda release, semantic: semantic.update(
            started_at="2026-08-17T06:03:00+08:00"),
        "AI authorization predates the semantic pytest run it authorized",
        id="ai-predates-selftest"),
    pytest.param(
        lambda release, _semantic: release.update(
            started_at="2026-08-17T06:02:30+08:00"),
        "daily release started before semantic pytest finished",
        id="release-before-selftest-finish"),
    pytest.param(
        lambda release, _semantic: release.update(
            finished_at="2026-08-17T06:05:00+08:00"),
        "release pins were checked before the release finished",
        id="pins-before-release-finish"),
])
def test_individually_current_but_out_of_order_sources_cannot_form_clean_ll(
        tmp_path, capsys, mutate, needle):
    release = _published_release_record()
    semantic = _semantic_record(rc=0, outcome="natural")
    mutate(release, semantic)

    rc, record = _capture_ran_tick(tmp_path, release, semantic)
    capsys.readouterr()

    assert rc == 1
    assert any(needle in error for error in record["source_errors"])
    assert record["convergence"]["round_converged"] is False


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda r: r.pop("smoke"), id="missing-smoke"),
    pytest.param(lambda r: r.update(schema_version=2.0),
                 id="float-schema"),
    pytest.param(lambda r: r["remote_publication"].update(verified=False),
                 id="unverified-manifests"),
    pytest.param(lambda r: r["release_record_publication"].pop("identity"),
                 id="record-without-identity"),
    pytest.param(lambda r: r.update(compose_attempts=[]),
                 id="no-compose"),
    pytest.param(lambda r: r.update(failure={"category": "push_failed"}),
                 id="success-with-failure"),
    pytest.param(lambda r: r.update(exit_code=1), id="nonzero-exit"),
    pytest.param(lambda r: r.update(exit_code=False), id="boolean-exit"),
    pytest.param(lambda r: r["compose_attempts"][0].update(attempt=True),
                 id="boolean-compose-attempt"),
    pytest.param(lambda r: r["anchor_pr"].update(ok=False),
                 id="anchor-failed"),
    pytest.param(lambda r: r["latest_publication"].update(pushed=False),
                 id="latest-not-pushed"),
])
def test_published_status_without_exact_terminal_proof_is_a_source_error(
        tmp_path, capsys, mutate):
    release = _published_release_record()
    mutate(release)

    rc, record = _capture_ran_tick(
        tmp_path, release, _semantic_record(rc=0, outcome="natural"))
    capsys.readouterr()

    assert rc == 1
    assert record["release"]["status"] == "unmeasured"
    assert any("terminal proof is invalid" in error
               for error in record["source_errors"])


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda s: s.update(schema=1.0), id="float-schema"),
    pytest.param(lambda s: s.update(exit_code=False), id="boolean-exit"),
    pytest.param(lambda s: s["progress"].update(stage="running"),
                 id="unfinished-progress"),
    pytest.param(lambda s: s["process_tree"].update(
        cleanup_survivors=[123], final_empty=False),
                 id="live-descendant"),
    pytest.param(lambda s: s.update(elapsed_seconds=float("inf")),
                 id="nonfinite-elapsed"),
])
def test_green_selftest_label_without_exact_terminal_proof_is_rejected(
        tmp_path, capsys, mutate):
    semantic = _semantic_record(rc=0, outcome="natural")
    mutate(semantic)

    rc, record = _capture_ran_tick(
        tmp_path, _published_release_record(), semantic)
    capsys.readouterr()

    assert rc == 1
    assert record["selftest"] is None
    assert any("semantic" in error for error in record["source_errors"])


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
