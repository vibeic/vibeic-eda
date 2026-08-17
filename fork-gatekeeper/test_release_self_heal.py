#!/usr/bin/env python3
"""Regression coverage for the 2026-08-17 non-converging 05:30 release.

The real round merged three forks and built all three artefacts, then BuildKit
could not resolve github.com during the final compose.  One retry failed, no
0.3.3 was cut, and `daily-release.json` continued to describe 0.3.2's success.
The public page could show the lag but not its cause, and no retained record
joined that cause to the final 31/3 gap.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_page as B                                          # noqa: E402
import capture_release_ll as LL                                 # noqa: E402
import daily_release as R                                       # noqa: E402
from test_fork_github_convergence_ll import (                   # noqa: E402
    _fork_record as _valid_fork_github_record,
)


def test_dns_failure_retries_until_success_without_changing_the_command(tmp_path):
    answers = iter([
        (1, "", "fatal: Could not resolve host: github.com"),
        (1, "", "Timeout while contacting DNS servers"),
        (0, "", ""),
    ])
    calls = []
    slept = []

    def run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return next(answers)

    cmd = ["docker", "buildx", "bake", "eda"]
    rc, err, history = R.compose_with_retries(
        cmd, tmp_path, run=run, sleep=slept.append,
        max_transient_attempts=5, delays=(1, 2, 3, 4))

    assert rc == 0 and err == ""
    assert [h["category"] for h in history] == ["dns", "dns", None]
    assert slept == [1, 2]
    assert len(calls) == 3 and all(c[0] == cmd for c in calls)
    assert all(c[1]["stream"] and c[1]["env"] == R.BUILD_ENV for c in calls)
    assert all(c[1]["stall_grace"] == R.BUILD_STALL_GRACE_S for c in calls)


def test_deterministic_compose_failure_gets_one_retry_not_five(tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return 1, "", "Dockerfile:99: syntax error"

    rc, _, history = R.compose_with_retries(
        ["docker", "buildx", "bake", "eda"], tmp_path,
        run=run, sleep=lambda _: None, max_transient_attempts=5, delays=(0,))
    assert rc == 1
    assert len(calls) == len(history) == 2
    assert all(h["category"] is None for h in history)


def test_progress_stall_is_named_and_not_a_network_retry_class():
    assert R.transient_failure_category("WATCHDOG_STALLED: flat") == \
        "progress_stall"
    assert "progress_stall" not in R._EXTENDED_RETRY_CATEGORIES

    release = {
        "status": "failed",
        "failure": {"stage": "compose", "category": "progress_stall",
                    "retryable": False},
        "compose_attempts": [
            {"exit_code": R.watchdog.RC_STALLED,
             "category": "progress_stall"},
        ],
    }
    record = LL.make_record(
        release, _gap(), "2026-08-17T05:30:00+08:00", 1)
    assert record["lesson"]["trigger"] == "progress_stall"
    assert record["lesson"]["outcome"] == "blocked_non_transient"


def test_streaming_command_keeps_a_classifiable_tail(capsys):
    rc, _, tail = R._sh(
        [sys.executable, "-c",
         "print('fatal: Could not resolve host: github.com'); raise SystemExit(7)"],
        stream=True, timeout=10)
    assert rc == 7
    assert "Could not resolve host" in tail
    assert "Could not resolve host" in capsys.readouterr().out
    assert R.transient_failure_category(tail) == "dns"


def test_terminal_failure_atomically_replaces_yesterdays_success(tmp_path):
    sidecar = tmp_path / "daily-release.json"
    sidecar.write_text('{"program":"daily_release","status":"published"}\n')
    now = {"program": "daily_release", "started_at": "today"}
    rc = R._finish(now, str(sidecar), R.RC_NEEDS_HUMAN, "failed", {
        "stage": "compose", "category": "dns", "retryable": True,
    })
    got = json.loads(sidecar.read_text())
    assert rc == R.RC_NEEDS_HUMAN
    assert got["status"] == "failed" and got["failure"]["category"] == "dns"
    assert got["exit_code"] == R.RC_NEEDS_HUMAN and got.get("finished_at")


def test_real_entrypoint_overwrites_stale_json_on_an_early_guard_return(tmp_path):
    root = tmp_path / "empty-eda"
    (root / "tools").mkdir(parents=True)
    sidecar = tmp_path / "daily-release.json"
    sidecar.write_text('{"program":"daily_release","status":"published"}\n')
    rc = R.main(["--eda-root", str(root), "--json", str(sidecar)])
    got = json.loads(sidecar.read_text())
    assert rc == R.RC_NEEDS_HUMAN
    assert got["status"] == "failed"
    assert got["failure"]["stage"] == "source_guard"
    assert got["started_at"] and got["finished_at"]


def test_page_reads_the_release_programs_own_blocker(tmp_path, monkeypatch):
    state = tmp_path / "daily-release.json"
    state.write_text(json.dumps({
        "program": "daily_release", "schema_version": 2, "status": "failed",
        "version_candidate": {"to": "0.3.3"},
        "failure": {"stage": "compose", "category": "dns"},
        "compose_attempts": [{"exit_code": 1}, {"exit_code": 1}],
    }))
    monkeypatch.setattr(B, "RELEASE_STATE", state)
    assert B._release_state()["failure"]["category"] == "dns"
    assert 'id="forkRelease"' in B.PAGE
    assert "Release ${target} is blocked at ${stage}" in B.PAGE
    assert ".replace(\"__RELEASE__\"" in Path(B.__file__).read_text()


def _gap(image=31, past=3):
    return {"q1_sync_lag_at_merge": 0,
            "q1_sync_lag": 0,
            "q1_release_lag": image,
            "q1_image_behind_upstream": image,
            "q2_our_commits_stranded": 0,
            "q2_ours_past_the_pin": past,
            "q2_ours_past_the_pin_substantive": 0}


def _fork_github_zero():
    # Reuse the exact schema-v2 denominator fixture.  A hand-written three-count
    # stub is precisely the false-zero shape this release/LL regression now
    # exists to reject.
    return _valid_fork_github_record()


def test_ll_retains_failure_and_later_self_healed_outcome(tmp_path):
    failed = {
        "status": "failed", "version_candidate": {"to": "0.3.3"},
        "moved": [{"repo": "iverilog"}], "built": ["iverilog"],
        "compose_attempts": [
            {"exit_code": 1, "category": "dns"},
            {"exit_code": 1, "category": "dns"},
        ],
        "failure": {"stage": "compose", "category": "dns",
                    "retryable": True},
    }
    first = LL.make_record(failed, _gap(), "2026-08-17T05:30:00+08:00", 5)
    LL.retain(first, tmp_path)
    assert first["lesson"]["outcome"] == "blocked_after_bounded_retries"
    assert first["convergence"]["image_behind_upstream"] == 31

    healed = dict(failed, status="published", failure=None,
                  compose_attempts=[{"exit_code": 1, "category": "dns"},
                                    {"exit_code": 0, "category": None}])
    second = LL.make_record(healed, _gap(0, 0),
                            "2026-08-18T05:30:00+08:00", 0,
                            fork_github=_fork_github_zero())
    LL.retain(second, tmp_path)
    assert second["lesson"]["outcome"] == "self_healed_after_retry"
    assert second["convergence"]["converged"] is True

    lines = [json.loads(x) for x in
             (tmp_path / "release-ll" / "index.jsonl").read_text().splitlines()]
    assert [x["outcome"] for x in lines] == [
        "blocked_after_bounded_retries", "self_healed_after_retry"]
    assert json.loads((tmp_path / "release-ll" / "latest.json").read_text()) \
        ["convergence"]["converged"] is True


def test_ll_separates_late_upstream_drift_from_a_failed_round():
    release = {
        "status": "published", "version": {"to": "0.3.4"},
        "compose_attempts": [{"exit_code": 0}],
    }
    gap = {
        "q1_sync_lag_at_merge": 0,
        "q1_sync_lag": 4,
        "q1_release_lag": 0,
        "q1_image_behind_upstream": 4,
        "q2_our_commits_stranded": 0,
        "q2_ours_past_the_pin": 0,
        "q2_ours_past_the_pin_substantive": 0,
    }
    record = LL.make_record(
        release, gap, "2026-08-17T09:12:00+08:00", 0,
        fork_github=_fork_github_zero())
    assert record["lesson"]["outcome"] == \
        "converged_at_boundary_with_late_upstream_drift"
    assert record["convergence"]["delivery_round_converged"] is True
    assert record["convergence"]["round_converged"] is True
    assert record["convergence"]["converged"] is False
    assert record["convergence"]["live_sync_lag"] == 4
    assert record["lesson"]["control"] == \
        "single compose attempt; no retry needed"


def test_ll_keeps_a_failed_consumer_anchor_as_a_delivery_outcome(tmp_path):
    """A registry publish is real, but it is not the whole delivery when the
    consumer repo still points at the preceding immutable tag."""
    release = {
        "status": "published", "version": {"to": "0.3.4"},
        "compose_attempts": [{"exit_code": 0}],
        "anchor_pr": {
            "ok": False,
            "note": "branch push failed: benchmark gate could not run",
        },
    }
    record = LL.make_record(
        release, _gap(0, 0), "2026-08-17T09:12:00+08:00", 0,
        fork_github=_fork_github_zero())
    assert record["schema_version"] == 2
    assert record["convergence"]["delivery_round_converged"] is True
    assert record["convergence"]["round_converged"] is False
    assert record["release"]["anchor_pr"] == release["anchor_pr"]
    assert record["lesson"]["trigger"] == "anchor_pr"
    assert record["lesson"]["outcome"] == \
        "published_with_anchor_pr_blocked"

    LL.retain(record, tmp_path)
    narrow = json.loads(
        (tmp_path / "release-ll" / "index.jsonl").read_text().splitlines()[0])
    assert narrow["anchor_pr_ok"] is False
    assert "delivery_complete" not in narrow


def test_ll_never_calls_a_nonzero_tick_or_stranded_work_converged():
    release = {"status": "published", "compose_attempts": [{"exit_code": 0}]}
    nonzero = LL.make_record(
        release, _gap(0, 0), "2026-08-17T09:12:00+08:00", 7,
        fork_github=_fork_github_zero())
    assert nonzero["convergence"]["delivery_converged"] is True
    assert nonzero["convergence"]["converged"] is False
    assert nonzero["convergence"]["tick_authorized"] is False
    assert nonzero["lesson"]["outcome"] == \
        "finished_with_nonzero_release_tick"

    gap = _gap(0, 0)
    gap["q2_our_commits_stranded"] = 1
    stranded = LL.make_record(
        release, gap, "2026-08-17T09:12:00+08:00", 0,
        fork_github=_fork_github_zero())
    assert stranded["convergence"]["delivery_converged"] is True
    assert stranded["convergence"]["our_commits_stranded"] == 1
    assert stranded["convergence"]["converged"] is False


def test_0530_captures_ll_after_gap_and_makes_capture_failure_fatal():
    src = (HERE / "run_0530.sh").read_text()
    assert src.index("fork_gap_report.py") < src.index("capture_release_ll.py")
    assert 'capture_release_ll exit ${LL}' in src
    final = src[src.index("# 0 only when BOTH are clean"):]
    assert '[ "${LL}" -ne 0 ]' in final
