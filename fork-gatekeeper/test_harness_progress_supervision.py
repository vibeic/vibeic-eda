"""The legacy integration harness cannot use elapsed time as a verdict."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gatekeeper as G  # noqa: E402


def test_a_stalled_run_cannot_reuse_yesterdays_green_result(tmp_path, monkeypatch):
    result = tmp_path / "result.json"
    result.write_text(json.dumps([
        {"tool": "demo", "status": "built_green", "detail": "old"},
    ]))
    monkeypatch.setattr(
        G.watchdog,
        "run_supervised",
        lambda *a, **k: G.watchdog.SupervisedResult(
            G.watchdog.RC_STALLED, "", "WATCHDOG_STALLED", "stalled", 8.0
        ),
    )
    got = G._run_harness(
        {"cmd": "anything", "result": str(result), "stall_grace": 10},
        [{"tool": "demo"}],
    )
    assert got["demo"]["status"] == "harness_stalled"
    assert not result.exists(), "the stale green result survived the new run"


def test_natural_run_reads_only_the_new_terminal_result(tmp_path, monkeypatch):
    result = tmp_path / "result.json"

    def run(*args, **kwargs):
        result.write_text(json.dumps([
            {"tool": "demo", "status": "built_green", "detail": "new"},
        ]))
        assert kwargs["stall_grace_s"] == 7
        assert kwargs["process_tree_cpu_progress"] is True
        return G.watchdog.SupervisedResult(0, "", "", "natural", 99.0)

    monkeypatch.setattr(G.watchdog, "run_supervised", run)
    got = G._run_harness(
        {"cmd": "anything", "result": str(result), "stall_grace": 7},
        [{"tool": "demo"}],
    )
    assert got["demo"]["status"] == "built_green"
    assert got["demo"]["detail"] == "new"


def test_checked_in_harness_declares_a_stall_lease_not_a_runtime_limit():
    cfg = json.loads((Path(G.__file__).parent / "regression.json").read_text())
    build = cfg["image_build"]
    assert build["stall_grace"] > 0
    assert "timeout" not in build
