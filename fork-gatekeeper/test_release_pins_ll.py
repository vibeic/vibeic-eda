"""The 05:30 LL retains the committed-release-pin postcondition exactly."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capture_release_ll as LL  # noqa: E402
import check_release_pins_committed as CHECK  # noqa: E402
from test_ai_handoff_progress_supervision import (  # noqa: E402
    _published_release_record,
)
from test_fork_github_convergence_ll import (  # noqa: E402
    ROUND_AT,
    _ai_authorization_record,
    _fork_record,
    _gap_record,
    _watch_reset_record,
)
from test_release_pins_committed import _repo  # noqa: E402


RUN_ID = "a" * 64
VERSION = "0.3.6"
HEAD_REVISION = subprocess.run(
    ["git", "-C", str(HERE.parent), "rev-parse", "HEAD^{commit}"],
    check=True, capture_output=True, text=True,
).stdout.strip()

ACTUAL_036_OUTSTANDING = {
    "iverilog": ("5d87083a0", "10a16b5d3"),
    "verilator": ("bd5b856c0", "095502531"),
    "xschem": ("5d5a4adbf", "064c923ca"),
    "yosys": ("7a41b1522", "798e7fdc2"),
}


def _pins(verdict: str = "PASS") -> dict:
    outstanding = (list(ACTUAL_036_OUTSTANDING)
                   if verdict == "OUTSTANDING" else [])
    findings = [
        f"{tool}: RELEASED.json={released}  HEAD={head}"
        for tool, (released, head) in ACTUAL_036_OUTSTANDING.items()
    ] if outstanding else []
    record = {
        "program": LL.RELEASE_PINS_PROGRAM,
        "tool": LL.RELEASE_PINS_PROGRAM,
        "schema_version": LL.RELEASE_PINS_SCHEMA_VERSION,
        "checked_at": "2026-08-17T06:04:30+08:00",
        "round_at": ROUND_AT,
        "run_id": RUN_ID,
        "verdict": verdict,
        "findings": findings,
        "version": VERSION,
        "rev": "HEAD",
        "revision": HEAD_REVISION,
        "recorded_count": 34,
        "committed_count": 34,
        "outstanding": outstanding,
    }
    if verdict == "COULD_NOT_TELL":
        record = {
            key: value for key, value in record.items()
            if key not in {"version", "rev", "revision", "recorded_count",
                           "committed_count", "outstanding"}
        }
        record["findings"] = ["RELEASED.json is absent"]
    return record


def _capture(tmp_path: Path, pins: dict | None, *, raw: str | None = None,
             tick_state: str = "ran",
             release_value: dict | None = None) -> tuple[int, dict, dict]:
    release = tmp_path / "daily-release.json"
    release.write_text(json.dumps(
        release_value or _published_release_record(VERSION)))
    gap = tmp_path / "gap.json"
    gap.write_text(json.dumps(_gap_record()))
    github = tmp_path / "fork-github.json"
    github.write_text(json.dumps(_fork_record()))
    ai = tmp_path / "ai.json"
    ai.write_text(json.dumps(_ai_authorization_record()))
    watch = _watch_reset_record(tmp_path / "daily_0530.json")
    pin_path = tmp_path / "release-pins.json"
    if raw is not None:
        pin_path.write_text(raw)
    elif pins is not None:
        pin_path.write_text(json.dumps(pins))
    state = tmp_path / "state"
    args = [
        "--state-dir", str(state),
        "--eda-root", str(HERE.parent),
        "--release-json", str(release),
        "--release-pins-json", str(pin_path),
        "--gap-json", str(gap),
        "--fork-github-json", str(github),
        "--ai-authorization-json", str(ai),
        "--watch-reset-json", str(watch),
        "--expected-github-actor", "reyerchu",
        "--round-at", ROUND_AT,
        "--tick-exit", "0" if tick_state == "ran" else "2",
        "--tick-state", tick_state,
    ]
    rc = LL.main(args)
    full = json.loads((state / "release-ll" / "latest.json").read_text())
    narrow = json.loads(
        (state / "release-ll" / "index.jsonl").read_text().splitlines()[-1])
    return rc, full, narrow


def test_outstanding_pins_are_exact_blocking_evidence_not_a_source_error(
        tmp_path, capsys):
    rc, full, narrow = _capture(tmp_path, _pins("OUTSTANDING"))
    capsys.readouterr()

    assert rc == 0, "a measured negative is complete evidence"
    assert full["source_errors"] == []
    assert full["release_pins"]["verdict"] == "OUTSTANDING"
    assert full["release_pins"]["outstanding"] == list(
        ACTUAL_036_OUTSTANDING)
    assert full["release_pins"]["findings"] == _pins("OUTSTANDING")[
        "findings"]
    assert narrow["release_pins"] == full["release_pins"]
    assert full["convergence"]["delivery_converged"] is True
    assert full["convergence"]["release_pins_committed"] is False
    assert full["convergence"]["converged"] is False
    assert full["lesson"]["trigger"] == "release_pins_uncommitted"
    assert full["lesson"]["outcome"] == (
        "published_with_uncommitted_release_pins")


def test_pass_is_current_exact_and_can_participate_in_convergence(
        tmp_path, capsys):
    rc, full, narrow = _capture(tmp_path, _pins("PASS"))
    capsys.readouterr()

    assert rc == 0
    assert full["release_pins"]["revision"] == HEAD_REVISION
    assert full["convergence"]["release_pins_committed"] is True
    assert narrow["release_pins_committed"] is True
    assert full["convergence"]["converged"] is True
    assert full["lesson"]["outcome"] == "converged"


def test_could_not_tell_is_retained_and_fails_closed(tmp_path, capsys):
    rc, full, narrow = _capture(tmp_path, _pins("COULD_NOT_TELL"))
    capsys.readouterr()

    assert rc == 1
    assert full["release_pins"]["verdict"] == "COULD_NOT_TELL"
    assert narrow["release_pins"]["findings"] == [
        "RELEASED.json is absent"]
    assert full["convergence"]["release_pins_committed"] is None
    assert full["convergence"]["converged"] is False
    assert any("could not tell whether release pins are committed" in error
               for error in full["source_errors"])


@pytest.mark.parametrize(("mutation", "needle"), [
    (lambda value: value.update(run_id="b" * 64), "run_id"),
    (lambda value: value.update(
        checked_at="2026-08-16T06:03:00+08:00"), "predates"),
    (lambda value: value.update(version="0.3.4"), "release target"),
    (lambda value: value.update(outstanding=["iverilog"]),
     "different tools"),
    (lambda value: value.update(rev="HEAD~1"), "rev is not HEAD"),
    (lambda value: value.update(revision="d" * 40),
     "independently resolved current HEAD"),
    (lambda value: value.update(revision="short"), "full commit id"),
])
def test_stale_malformed_or_contradictory_pin_evidence_is_not_accepted(
        tmp_path, capsys, mutation, needle):
    value = _pins("OUTSTANDING")
    mutation(value)
    rc, full, narrow = _capture(tmp_path, value)
    capsys.readouterr()

    assert rc == 1
    assert full["release_pins"] is None
    assert narrow["release_pins"] is None
    assert full["convergence"]["release_pins_committed"] is None
    assert any(needle in error for error in full["source_errors"])


def test_jointly_shuffled_findings_and_tools_are_not_canonical_evidence(
        tmp_path, capsys):
    value = _pins("OUTSTANDING")
    value["outstanding"].reverse()
    value["findings"].reverse()
    rc, full, _narrow = _capture(tmp_path, value)
    capsys.readouterr()

    assert rc == 1
    assert full["release_pins"] is None
    assert any("canonical sorted order" in error
               for error in full["source_errors"])


def test_failed_candidate_keeps_last_released_version_evidence(
        tmp_path, capsys):
    failed = {
        "program": "daily_release", "schema_version": 2,
        "status": "failed",
        "started_at": "2026-08-17T05:40:00+08:00",
        "finished_at": "2026-08-17T05:50:00+08:00",
        "version_candidate": {"from": VERSION, "to": "0.3.7"},
        "compose_attempts": [{"exit_code": 1}],
        "failure": {"stage": "compose", "category": "compose_failed",
                    "retryable": False},
    }
    rc, full, _narrow = _capture(
        tmp_path, _pins("PASS"), release_value=failed)
    capsys.readouterr()

    assert rc == 0
    assert full["source_errors"] == []
    assert full["release"]["target"] == "0.3.7"
    assert full["release_pins"]["version"] == VERSION
    assert full["lesson"]["outcome"] == "blocked_non_transient"


@pytest.mark.parametrize("pins,raw,needle", [
    (None, None, "missing release-pins.json"),
    (None, "{not json", "unreadable release-pins.json"),
])
def test_missing_or_unreadable_pin_sidecar_is_not_an_empty_pass(
        tmp_path, capsys, pins, raw, needle):
    rc, full, _narrow = _capture(tmp_path, pins, raw=raw)
    capsys.readouterr()
    assert rc == 1
    assert full["release_pins"] is None
    assert any(needle in error for error in full["source_errors"])


def test_skipped_tick_does_not_read_a_prior_pin_sidecar(tmp_path, capsys):
    stale = _pins("PASS")
    stale["run_id"] = "old" * 20
    rc, full, _narrow = _capture(
        tmp_path, stale, tick_state="not_checked_pytest_norecord")
    capsys.readouterr()

    assert rc == 0
    assert full["release_pins"]["verdict"] == "NOT_CHECKED"
    assert full["release_pins"]["reason"] == "not_checked_pytest_norecord"
    assert full["convergence"]["release_pins_committed"] is None
    assert full["source_errors"] == []


def test_checker_writes_an_immutable_current_round_envelope(tmp_path, capsys):
    repo = _repo(tmp_path)
    sidecar = tmp_path / "pins.json"
    rc = CHECK.main([
        "--eda-root", str(repo), "--json", str(sidecar),
        "--round-at", ROUND_AT, "--run-id", RUN_ID,
    ])
    capsys.readouterr()
    record = json.loads(sidecar.read_text())
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()

    assert rc == CHECK.RC_OK
    assert record["program"] == LL.RELEASE_PINS_PROGRAM
    assert record["schema_version"] == 1
    assert record["round_at"] == ROUND_AT and record["run_id"] == RUN_ID
    assert datetime.fromisoformat(record["checked_at"]).utcoffset() is not None
    assert record["rev"] == "HEAD" and record["revision"] == head
    assert not list(tmp_path.glob(".pins.json.*.tmp"))


def test_both_runners_wire_and_clear_the_exact_current_round_sidecar():
    outer = (HERE / "run_0530.sh").read_text()
    inner = (HERE / "run_tick.sh").read_text()

    assert 'RELPIN_JSON="${GK_STATE_DIR}/release-pins-committed.json"' in outer
    assert 'rm -f -- "${RELPIN_JSON}"' in outer
    ll_call = outer[outer.index('python3 "${DIR}/capture_release_ll.py"'):
                    outer.index("LL=$?")]
    assert '--release-pins-json "${RELPIN_JSON}"' in ll_call
    assert '--eda-root "${DIR}/.."' in ll_call

    start = inner.index('RELPIN_OUT="')
    block = inner[start:start + 2200]
    assert 'RELPIN_JSON="${LOG_DIR}/release-pins-committed.json"' in block
    assert block.index('rm -f -- "${RELPIN_JSON}"') < block.index(
        "check_release_pins_committed.py")
    assert '--round-at "${AI_ROUND_AT}" --run-id "${AI_RUN_ID}"' in block
    assert '--json "${RELPIN_JSON}"' in block
