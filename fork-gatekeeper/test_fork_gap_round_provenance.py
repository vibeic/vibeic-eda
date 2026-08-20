"""The gap sidecar proves which applied merge round its zero belongs to."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fork_gap_report as G  # noqa: E402


TOOLS = [f"tool-{number:02d}" for number in range(37)]


def _payload(*, tools=TOOLS, unmeasured=False) -> dict:
    return {
        "assertions": [],
        "q1_image_behind_upstream": 0,
        "q1_forks_behind": 0,
        "q1_sync_lag": 0,
        "q1_sync_lag_at_merge": 0,
        "q1_unmeasured_at_merge": [],
        # `_verdict` reads this and fails closed when it is absent, so a stub
        # without it would be exercising "measurement missing", not this test.
        "q1_image_behind_upstream_at_merge": 0,
        "q1_unmeasured_image_behind_at_merge": [],
        "q1_release_lag": 0,
        "q1_unmeasured": ["yosys"] if unmeasured else [],
        "q2_forks_not_built_from_ours": [],
        "q2_our_commits_stranded": 0,
        "q2_unmeasured_stranded": [],
        "q2_ours_past_the_pin": 0,
        "q2_ours_past_the_pin_substantive": 0,
        "q2_unshipped_commits": [],
        "q2_unmeasured_ship": [],
        "tip_rejected": [],
        "tip_unverified": [],
        "rows": [{
            "tool": tool,
            "integrated": True,
            "ahead": 0,
            "image_behind": 0,
            "note": None,
            "pin_disagreement": None,
            "tip_state": None,
            "tip_note": None,
        } for tool in tools],
        "measured_with_fetch": True,
    }


def _fleet(tmp_path: Path, *, ledger_tools=TOOLS) -> tuple[Path, Path]:
    forks = tmp_path / "FORKS.json"
    forks.write_text(json.dumps({
        "forks": [{"tool": tool} for tool in TOOLS],
    }), encoding="utf-8")
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    for tool in ledger_tools:
        (ledger / f"{tool}.json").write_text(json.dumps({
            "tool": tool, "integrated": True,
        }), encoding="utf-8")
    return forks, ledger


def _merge_evidence(round_meta: dict, *, tip_tools=TOOLS) -> dict:
    evidence = {"_round": round_meta}
    evidence.update({
        tool: {"upstream_evidence": {"tip_seen": "b" * 40}}
        for tool in tip_tools
    })
    return evidence


def _round_times():
    boundary = datetime.now(timezone.utc) - timedelta(seconds=10)
    return boundary, {
        "schema_version": 1,
        "run_id": "a" * 64,
        "started_at": (boundary + timedelta(seconds=1)).isoformat(),
        "finished_at": (boundary + timedelta(seconds=2)).isoformat(),
        "applied": True,
    }


def test_round_timestamp_without_timezone_is_malformed():
    assert G._strict_when("2026-08-17T05:30:00") is None


def test_current_applied_round_is_atomically_enveloped(tmp_path, monkeypatch):
    boundary, round_meta = _round_times()
    evidence = tmp_path / "daily_0530.json"
    evidence.write_text(json.dumps(_merge_evidence(round_meta)),
                        encoding="utf-8")
    forks, ledger = _fleet(tmp_path)
    out = tmp_path / "fork_gap.json"
    replacements = []
    real_replace = G.os.replace

    def replace(src, dst):
        replacements.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(G, "analyse", lambda *a, **k: _payload())
    monkeypatch.setattr(G.os, "replace", replace)
    rc = G.main([
        "--repo", str(tmp_path), "--forks-root", str(tmp_path / "forks"),
        "--ledger", str(ledger), "--forks-json", str(forks),
        "--merge-evidence", str(evidence),
        "--round-at", boundary.isoformat(), "--json", str(out),
    ])
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 0
    assert replacements and replacements[-1][1] == out
    assert record["program"] == "fork_gap_report"
    assert record["schema_version"] == 1
    assert record["status"] == "complete"
    assert record["started_at"] >= boundary.isoformat()
    assert record["completed_at"] >= record["started_at"]
    assert record["source"] == {
        "kind": "daily_0530_merge_round",
        "merge_evidence": str(evidence),
        "round_at": boundary.isoformat(),
        "_round": round_meta,
    }


@pytest.mark.parametrize("round_meta", [
    None,
    {
        "schema_version": 1, "run_id": "too-short",
        "started_at": "2026-08-17T05:31:00+00:00",
        "finished_at": "2026-08-17T05:32:00+00:00", "applied": True,
    },
    {
        "schema_version": 1, "run_id": "a" * 64,
        "started_at": "2000-01-01T05:31:00+00:00",
        "finished_at": "2000-01-01T05:32:00+00:00", "applied": True,
    },
])
def test_missing_malformed_or_stale_round_is_norecord_not_zero(
        tmp_path, monkeypatch, round_meta):
    boundary, _ = _round_times()
    evidence = tmp_path / "daily_0530.json"
    body = {} if round_meta is None else _merge_evidence(round_meta)
    evidence.write_text(json.dumps(body), encoding="utf-8")
    forks, ledger = _fleet(tmp_path)
    out = tmp_path / "fork_gap.json"
    out.write_text('{"q1_sync_lag_at_merge": 0}\n', encoding="utf-8")

    def must_not_measure(*_args, **_kwargs):
        raise AssertionError("invalid round provenance reached analyse")

    monkeypatch.setattr(G, "analyse", must_not_measure)
    rc = G.main([
        "--repo", str(tmp_path), "--forks-root", str(tmp_path / "forks"),
        "--ledger", str(ledger), "--forks-json", str(forks),
        "--merge-evidence", str(evidence),
        "--round-at", boundary.isoformat(), "--json", str(out),
    ])
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 2
    assert record["status"] == "norecord"
    assert record["source_errors"]
    assert "q1_sync_lag_at_merge" not in record


def test_partial_numeric_zero_is_written_incomplete_and_exits_two(
        tmp_path, monkeypatch):
    boundary, round_meta = _round_times()
    evidence = tmp_path / "daily_0530.json"
    evidence.write_text(json.dumps(_merge_evidence(round_meta)),
                        encoding="utf-8")
    forks, ledger = _fleet(tmp_path)
    out = tmp_path / "fork_gap.json"
    monkeypatch.setattr(G, "analyse", lambda *a, **k: _payload(unmeasured=True))

    rc = G.main([
        "--repo", str(tmp_path), "--forks-root", str(tmp_path / "forks"),
        "--ledger", str(ledger), "--forks-json", str(forks),
        "--merge-evidence", str(evidence),
        "--round-at", boundary.isoformat(), "--json", str(out),
    ])
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 2
    assert record["q1_sync_lag_at_merge"] == 0
    assert record["status"] == "incomplete"
    assert record["q1_unmeasured"] == ["yosys"]


@pytest.mark.parametrize("ledger_tools", [[], TOOLS[:-1]], ids=(
    "empty-ledger", "one-of-37-missing"))
def test_partial_fleet_numeric_zero_is_incomplete_and_exits_two(
        tmp_path, monkeypatch, ledger_tools):
    boundary, round_meta = _round_times()
    evidence = tmp_path / "daily_0530.json"
    evidence.write_text(json.dumps(_merge_evidence(round_meta)),
                        encoding="utf-8")
    forks, ledger = _fleet(tmp_path, ledger_tools=ledger_tools)
    out = tmp_path / "fork_gap.json"
    monkeypatch.setattr(
        G, "analyse", lambda *a, **k: _payload(tools=ledger_tools))

    rc = G.main([
        "--repo", str(tmp_path), "--forks-root", str(tmp_path / "forks"),
        "--ledger", str(ledger), "--forks-json", str(forks),
        "--merge-evidence", str(evidence),
        "--round-at", boundary.isoformat(), "--json", str(out),
    ])
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 2
    assert record["q1_image_behind_upstream"] == 0
    assert record["status"] == "incomplete"
    assert record["registered_forks"] == sorted(TOOLS)
    assert record["ledger_forks"] == sorted(ledger_tools)
    assert record["fleet_errors"]
    assert any("count is" in error for error in record["fleet_errors"])


def test_one_of_37_missing_round_tip_cannot_fallback_to_live_zero(
        tmp_path, monkeypatch):
    boundary, round_meta = _round_times()
    evidence = tmp_path / "daily_0530.json"
    evidence.write_text(
        json.dumps(_merge_evidence(round_meta, tip_tools=TOOLS[:-1])),
        encoding="utf-8")
    forks, ledger = _fleet(tmp_path)
    out = tmp_path / "fork_gap.json"
    monkeypatch.setattr(G, "analyse", lambda *a, **k: _payload())

    rc = G.main([
        "--repo", str(tmp_path), "--forks-root", str(tmp_path / "forks"),
        "--ledger", str(ledger), "--forks-json", str(forks),
        "--merge-evidence", str(evidence),
        "--round-at", boundary.isoformat(), "--json", str(out),
    ])
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 2
    assert record["q1_sync_lag_at_merge"] == 0
    assert record["status"] == "incomplete"
    assert TOOLS[-1] not in record["merge_tips_seen"]
    assert any("tip_seen" in error and TOOLS[-1] in error
               for error in record["fleet_errors"])
