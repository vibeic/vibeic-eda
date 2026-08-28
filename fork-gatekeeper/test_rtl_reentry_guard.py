"""Regression coverage for the supplied-RTL validation provenance boundary."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "rtl_reentry_guard.py"
SPEC = importlib.util.spec_from_file_location("rtl_reentry_guard", SCRIPT)
assert SPEC and SPEC.loader
G = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = G
SPEC.loader.exec_module(G)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    rtl = project / "phase2" / "stage1" / "rtl"
    rtl.mkdir(parents=True)
    (rtl / "dut.sv").write_text(
        "module dut(input logic a, output logic y); assign y = a; endmodule\n")
    report = project / "reports" / "orchestrator"
    report.mkdir(parents=True)
    (report / "phase2_one_shot.json").write_text(json.dumps({
        "verdict": "PASS",
        "steps": [
            {"name": "rtl_gen", "status": "SKIPPED-BY-ENTRY",
             "detail": "run declared --entry-step 2 (site 'rtl_lint')"},
            {"name": "rtl_lint", "status": "PASS"},
            {"name": "yosys_synth", "status": "PASS"},
        ],
    }))
    return project


def test_supplied_rtl_survives_validation_reentry(tmp_path):
    project = _project(tmp_path)
    manifest = G.record(project, "AI_REPAIR", 2)
    result = G.verify(project, manifest)

    assert result["status"] == "VERIFIED"
    assert result["rtl_gen_status"] == "SKIPPED-BY-ENTRY"
    assert result["downstream_dispatched"] == 2


def test_changed_rtl_is_rejected_even_when_report_claims_skip(tmp_path):
    project = _project(tmp_path)
    manifest = G.record(project, "AI_BACKUP", 2)
    (project / "phase2" / "stage1" / "rtl" / "dut.sv").write_text(
        "module dut(input logic a, output logic y); assign y = ~a; endmodule\n")

    with pytest.raises(G.ContractError, match="bytes changed"):
        G.verify(project, manifest)


@pytest.mark.parametrize("status,detail", [
    ("PASS", "generator ran"),
    ("SKIPPED-BY-ENTRY", "run declared --entry-step 3"),
])
def test_report_must_prove_the_declared_validation_entry(
        tmp_path, status, detail):
    project = _project(tmp_path)
    manifest = G.record(project, "USER_SUPPLIED", 2)
    report_path = project / "reports" / "orchestrator" / "phase2_one_shot.json"
    report = json.loads(report_path.read_text())
    report["steps"][0].update(status=status, detail=detail)
    report_path.write_text(json.dumps(report))

    with pytest.raises(G.ContractError):
        G.verify(project, manifest)


def test_release_image_installs_the_guard():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "tools/rtl_reentry_guard.py" in dockerfile
    assert "/foss/tools/bin/vibeic-rtl-reentry-guard" in dockerfile
