#!/usr/bin/env python3
"""Capability measurement uses progress leases, never elapsed-time verdicts."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import capability_gate as G                                      # noqa: E402
import capability_smoke as S                                     # noqa: E402


def _execution(output: str = "", outcome: str = "natural", rc: int = 0):
    return S.ProbeExecution(output=output, outcome=outcome, rc=rc)


def _supervised(*, outcome="natural", rc=0, out="", err=""):
    return SimpleNamespace(
        outcome=outcome, rc=rc, out=out, err=err, elapsed_s=99999.0)


def test_probe_runner_has_a_stall_lease_and_no_total_runtime_cap(monkeypatch):
    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return _supervised(out="[INFO] Final banner\nMEASURED\n")

    monkeypatch.setattr(S.W, "run_supervised", run)
    got = S._run("example:image", "do-real-work", stall_after=17)

    assert got == _execution("MEASURED", "natural", 0)
    assert seen["kwargs"]["stall_grace_s"] == 17
    assert "timeout" not in seen["kwargs"]
    assert "hard_ceiling_s" not in seen["kwargs"]
    assert callable(seen["kwargs"]["cpu_probe"])
    assert callable(seen["kwargs"]["kill"])
    assert seen["cmd"][:3] == ["docker", "run", "--rm"]
    assert "--cidfile" in seen["cmd"]


def test_inner_watchdog_sampling_advances_the_private_parent_journal(
        tmp_path, monkeypatch):
    journal_path = tmp_path / "events"
    journal = S._ProgressJournal(journal_path)
    cid_file = tmp_path / "missing.cid"
    sample = S._container_cpu_probe(cid_file, journal)

    assert sample(object()) is None
    assert journal_path.read_text(encoding="utf-8") == (
        "inner-watchdog-sample\n")


def test_stalled_subject_is_inconclusive_even_when_control_passes(
        tmp_path, monkeypatch, capsys):
    probe = S.Probe(
        "example/capability", "subject", lambda _out: "feature absent",
        "control", lambda _out: None)
    answers = iter([
        _execution("IMAGE_OK"),
        _execution("WATCHDOG_STALLED", "stalled", S.W.RC_STALLED),
        _execution("CONTROL_OK"),
    ])
    monkeypatch.setattr(S, "PROBES", [probe])
    monkeypatch.setattr(S, "_run", lambda *_a, **_k: next(answers))
    report = tmp_path / "capabilities.json"
    monkeypatch.setattr(
        sys, "argv", ["capability_smoke.py", "image", "--json", str(report)])

    rc = S.main()
    rows = json.loads(report.read_text(encoding="utf-8"))
    output = capsys.readouterr().out

    assert rc == S.RC_OK, output  # smoke measures; capability_gate blocks UNKNOWN
    assert rows[0]["verdict"] == "INCONCLUSIVE"
    assert rows[0]["subject_outcome"] == "stalled"
    assert rows[0]["control_outcome"] == "natural"
    assert "STALLED" in rows[0]["reason"]
    assert "BROKEN" not in output


def test_a_measured_red_subject_with_green_control_is_still_broken(
        tmp_path, monkeypatch):
    """Control proving the migration did not weaken real capability findings."""
    probe = S.Probe(
        "example/capability", "subject", lambda _out: "measured mismatch",
        "control", lambda _out: None)
    answers = iter([_execution("IMAGE_OK"), _execution("BAD"),
                    _execution("CONTROL_OK")])
    monkeypatch.setattr(S, "PROBES", [probe])
    monkeypatch.setattr(S, "_run", lambda *_a, **_k: next(answers))
    report = tmp_path / "capabilities.json"
    monkeypatch.setattr(
        sys, "argv", ["capability_smoke.py", "image", "--json", str(report)])

    assert S.main() == S.RC_BROKEN
    assert json.loads(report.read_text(encoding="utf-8"))[0]["verdict"] == "BROKEN"


def test_outer_sweep_stall_is_no_measurement_not_a_broken_capability(
        tmp_path, monkeypatch, capsys):
    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return _supervised(
            outcome="stalled", rc=199,
            err="WATCHDOG_STALLED: no completed probe arm")

    monkeypatch.setattr(G.W, "run_supervised", run)
    waivers = tmp_path / "empty-waivers.txt"
    waivers.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "capability_gate.py", "image", "--waivers", str(waivers),
        "--json", str(tmp_path / "report.json"), "--stall-after", "7",
    ])

    rc = G.main()
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert rc == G.RC_NOIMAGE
    assert "STALLED" in output
    assert "BROKEN" not in output
    assert "timeout" not in seen["kwargs"]
    assert "hard_ceiling_s" not in seen["kwargs"]
    assert seen["kwargs"]["stall_grace_s"] >= (
        7 + G._OUTER_CLEANUP_HEADROOM)
    assert seen["cmd"][1:3] == ["-u", G.SMOKE]
    assert seen["cmd"][seen["cmd"].index("--stall-after") + 1] == "7.0"
    assert "--progress-file" in seen["cmd"]
    assert seen["kwargs"]["semantic_only"] is True
    assert callable(seen["kwargs"]["domain_progress_probe"])
    assert "cpu_probe" not in seen["kwargs"]


def test_gate_blocks_an_individual_stall_record_as_inconclusive(
        tmp_path, monkeypatch, capsys):
    waivers = tmp_path / "empty-waivers.txt"
    waivers.write_text("", encoding="utf-8")
    report = tmp_path / "report.json"

    def run(cmd, **_kwargs):
        target = Path(cmd[cmd.index("--json") + 1])
        target.write_text(json.dumps([{
            "capability": "example/capability",
            "verdict": "INCONCLUSIVE",
            "reason": "STALLED (stalled): no semantic result",
            "control": "ok",
            "subject_outcome": "stalled",
            "control_outcome": "natural",
        }]), encoding="utf-8")
        return _supervised(out="completed one probe\n")

    monkeypatch.setattr(G.W, "run_supervised", run)
    monkeypatch.setattr(sys, "argv", [
        "capability_gate.py", "image", "--waivers", str(waivers),
        "--json", str(report),
    ])

    assert G.main() == G.RC_FAIL
    output = capsys.readouterr().out
    assert "[INCONCLUSIVE]" in output
    assert "STALLED" in output
    assert "capability-gate: FAIL" in output


def test_interrupted_sweep_cannot_reuse_a_stale_green_table(
        tmp_path, monkeypatch, capsys):
    waivers = tmp_path / "empty-waivers.txt"
    waivers.write_text("", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps([{
        "capability": "yesterdays/result",
        "verdict": "WORKS",
        "reason": None,
        "control": "ok",
    }]), encoding="utf-8")
    monkeypatch.setattr(
        G.W, "run_supervised",
        lambda *_a, **_k: _supervised(rc=70, err="smoke crashed\n"),
    )
    monkeypatch.setattr(sys, "argv", [
        "capability_gate.py", "image", "--waivers", str(waivers),
        "--json", str(report),
    ])

    assert G.main() == G.RC_NOIMAGE
    output = capsys.readouterr().out
    assert "capability-gate: PASS" not in output
    assert not report.exists()
