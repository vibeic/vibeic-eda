"""Policy pins: elapsed wall time is never a long-work correctness verdict."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def _subprocess_timeout_lines(path: Path, function: str) -> list[int]:
    node = _function(path, function)
    lines = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        dotted = ast.unparse(call.func)
        if dotted not in {"subprocess.run", "subprocess.call", "subprocess.check_call"}:
            continue
        if any(keyword.arg == "timeout" for keyword in call.keywords):
            lines.append(call.lineno)
    return lines


def test_verdict_bearing_long_processes_do_not_use_subprocess_timeout():
    targets = (
        (HERE / "gatekeeper.py", "_run_harness"),
        (HERE / "daily_merge.py", "run_post_merge_checks"),
        (HERE / "capability_smoke.py", "_run"),
        (HERE / "capability_gate.py", "main"),
        (HERE / "semantic_pytest_gate.py", "main"),
        (HERE / "daily_release.py", "smoke_image"),
        (HERE / "check_no_capability_lost.py", "_sh"),
    )
    offenders = {
        f"{path.name}:{function}": _subprocess_timeout_lines(path, function)
        for path, function in targets
    }
    assert not {key: value for key, value in offenders.items() if value}, offenders


def test_pytest_lease_is_semantic_only_and_has_no_total_ceiling():
    source = (HERE / "semantic_pytest_gate.py").read_text(encoding="utf-8")
    assert "semantic_only=True" in source
    assert '"total_runtime_ceiling_seconds": None' in source
    assert "hard_ceiling" not in source
    assert "process_tree_cpu_progress=True" not in source


def test_legacy_and_release_paths_cannot_restore_the_old_runtime_budgets():
    regression = json.loads((HERE / "regression.json").read_text(encoding="utf-8"))
    assert "timeout" not in regression["image_build"]
    assert regression["image_build"]["stall_grace"] > 0

    release = (HERE / "daily_release.py").read_text(encoding="utf-8")
    assert "COMPOSE_TIMEOUT" not in release
    assert "TOOL_BUILD_TIMEOUT" not in release

    promote = (HERE / "build_and_regress.sh").read_text(encoding="utf-8")
    assert not re.search(r"(^|[;&|]\s*)timeout\s+", promote, re.MULTILINE)
    assert "legacy GK_MODE=promote is retired" in promote


def test_reachability_is_bounded_by_graph_shape_not_machine_speed():
    source = (HERE / "reachability.py").read_text(encoding="utf-8")
    assert "MAX_DEPTH" in source and "MAX_NODES" in source
    assert "MAX_SECONDS" not in source
    assert "deadline=" not in source


def test_0530_invokes_progress_leases_not_total_runtime_flags():
    source = (HERE / "run_0530.sh").read_text(encoding="utf-8")
    assert "--ai-timeout" not in source
    assert "--ai-stall-grace" in source
    semantic_arm = source[
        source.index("semantic_pytest_gate.py"):source.index("TESTS=$?")
    ]
    assert "--timeout" not in semantic_arm
    assert "--stall-grace" in semantic_arm

    daily = (HERE / "daily_0530.py").read_text(encoding="utf-8")
    assert "DEFAULT_TIMEOUT_S" not in daily
    fetch = ast.unparse(_function(HERE / "daily_0530.py", "_fetch"))
    assert "progress=True" in fetch
    assert "FETCH_TIMEOUT" not in fetch

    current = (HERE / "check_round_code_is_current.py").read_text(
        encoding="utf-8")
    assert '"fetch", "--progress", "origin"' in current
    assert "progress=True" in current

    auditor = ast.unparse(
        _function(HERE / "fork_gap_report.py", "fetch_confirms_current"))
    assert all(flag in auditor for flag in ("'fetch'", "'--progress'", "'--all'"))
    assert "progress=True" in auditor
    assert "FETCH_TIMEOUT" not in auditor


def test_atomic_smoke_uses_container_progress_not_a_runtime_ceiling():
    source = ast.unparse(_function(HERE / "daily_release.py", "smoke_image"))
    assert "run_container_probe" in source
    assert "timeout=300" not in source

    adapter = ast.unparse(
        _function(HERE / "capability_smoke.py", "run_container_probe"))
    assert "_run" in adapter
