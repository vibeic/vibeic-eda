"""The 05:30 LL retains GitHub fork convergence without inventing zero."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capture_release_ll as LL  # noqa: E402
import fork_github_convergence as C  # noqa: E402


ROUND_AT = "2026-08-17T05:30:00+08:00"
REGISTERED_FORKS = sorted(
    entry["tool"] for entry in
    json.loads((HERE / "FORKS.json").read_text(encoding="utf-8"))["forks"])


def _fork_record(*, status="converged", started_at=None,
                 counts=None, items=None, source_errors=None) -> dict:
    return {
        "program": "fork_github_convergence",
        "schema_version": 1,
        "started_at": started_at or "2026-08-17T05:34:00+08:00",
        "completed_at": "2026-08-17T05:35:00+08:00",
        "actor": "reyerchu",
        "expected_actor": "reyerchu",
        "round": {
            "round_at": ROUND_AT,
            "schema_version": 1,
            "run_id": "a" * 64,
            "started_at": "2026-08-17T05:30:01+08:00",
            "finished_at": "2026-08-17T05:33:00+08:00",
        },
        "status": status,
        "counts": ({"items": 0, "issues": 0, "pull_requests": 0,
                    "by_source": {"org_search_issues": 0}}
                   if counts is None else counts),
        "items": [] if items is None else items,
        "source_errors": [] if source_errors is None else source_errors,
    }


def _item(number=1) -> dict:
    return {
        "repository": "vibeic/yosys",
        "type": "pull_request",
        "number": number,
        "url": f"https://github.com/vibeic/yosys/pull/{number}",
    }


def _gap_record(**updates) -> dict:
    record = {
        "program": "fork_gap_report",
        "schema_version": 1,
        "started_at": "2026-08-17T06:00:00+08:00",
        "completed_at": "2026-08-17T06:01:00+08:00",
        "status": "complete",
        "source": {
            "kind": "daily_0530_merge_round",
            "merge_evidence": "/state/daily_0530.json",
            "round_at": ROUND_AT,
            "_round": {
                "schema_version": 1,
                "run_id": "a" * 64,
                "started_at": "2026-08-17T05:30:01+08:00",
                "finished_at": "2026-08-17T05:59:00+08:00",
                "applied": True,
            },
        },
        "source_errors": [],
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
        "tip_rejected": [],
        "tip_unverified": [],
        "registered_forks": REGISTERED_FORKS,
        "ledger_forks": REGISTERED_FORKS,
        "merge_tips_seen": {tool: "b" * 40 for tool in REGISTERED_FORKS},
        "fleet_errors": [],
        "rows": [{"tool": tool} for tool in REGISTERED_FORKS],
        "measured_with_fetch": True,
    }
    record.update(updates)
    return record


def _capture(tmp_path: Path, fork_path: Path, *, gap_record=None
             ) -> tuple[int, dict, dict]:
    gap = tmp_path / "gap.json"
    gap.write_text(json.dumps(_gap_record() if gap_record is None else gap_record))
    state = tmp_path / "state"
    rc = LL.main([
        "--state-dir", str(state),
        "--release-json", str(tmp_path / "release-not-read.json"),
        "--gap-json", str(gap),
        "--fork-github-json", str(fork_path),
        "--round-at", ROUND_AT,
        "--tick-exit", "2",
        "--tick-state", "not_checked_pytest_norecord",
    ])
    full = json.loads((state / "release-ll" / "latest.json").read_text())
    narrow = json.loads(
        (state / "release-ll" / "index.jsonl").read_text().splitlines()[-1])
    return rc, full, narrow


def test_cron_runs_checker_between_gap_and_ll_and_gates_on_its_rc():
    source = (HERE / "run_0530.sh").read_text()
    gap = source.index('python3 "${DIR}/fork_gap_report.py"')
    github = source.index('python3 "${DIR}/fork_github_convergence.py"')
    ll = source.index('python3 "${DIR}/capture_release_ll.py"')
    assert gap < github < ll

    arm = source[github:ll]
    assert '--forks-json "${DIR}/FORKS.json"' in arm
    assert '--round-json "${GK_STATE_DIR}/daily_0530.json"' in arm
    assert '--round-at "${ROUND_AT}"' in arm
    assert '--expected-actor "${GK_GITHUB_ACTOR:-reyerchu}"' in arm
    assert '--json "${FORK_GH_JSON}"' in arm
    assert "FORK_GH=$?" in arm
    assert "0=exact-zero, 1=open work, 2=NORECORD" in arm
    assert 'rounds/${PYTEST_STAMP}/fork-github-convergence.json' in arm

    ll_arm = source[ll:source.index("LL=$?")]
    assert '--fork-github-json "${FORK_GH_JSON}"' in ll_arm
    final = source[source.index("# 0 only when BOTH are clean"):]
    assert '[ "${FORK_GH}" -ne 0 ]' in final


def test_cron_clears_round_inputs_and_retention_cannot_fail_clean():
    source = (HERE / "run_0530.sh").read_text()
    daily = source.index('python3 "${DIR}/daily_0530.py"')
    clear_daily = 'if ! rm -f "${GK_STATE_DIR}/daily_0530.json"'
    assert source.index(clear_daily) < daily

    gap = source.index('python3 "${DIR}/fork_gap_report.py"')
    gap_setup = source[source.rindex('GAP_JSON=', 0, gap):gap]
    assert 'GAP_JSON="${GK_STATE_DIR}/fork_gap.json"' in gap_setup
    assert 'if ! rm -f "${GAP_JSON}"' in gap_setup
    assert '--round-at "${ROUND_AT}"' in source[gap:source.index("GAP=$?", gap)]
    assert '--json "${GAP_JSON}"' in source[gap:source.index("GAP=$?", gap)]
    assert '--gap-json "${GAP_JSON}"' in source
    assert "open('${GAP_JSON}')" in source

    retained = source[source.index('FORK_GH_RETAINED='):
                      source.index("# CAPTURE THE LEARNING LOOP")]
    assert 'cp "${FORK_GH_JSON}" "${FORK_GH_RETAIN_TMP}"' in retained
    assert 'mv "${FORK_GH_RETAIN_TMP}" "${FORK_GH_RETAINED}"' in retained
    assert retained.count("FORK_GH=2") >= 2  # copy failure and missing sidecar


def test_current_open_work_is_retained_with_exact_items(tmp_path, capsys):
    item = {
        "repository": "vibeic/yosys", "type": "pull_request", "number": 17,
        "url": "https://github.com/vibeic/yosys/pull/17",
    }
    fork = tmp_path / "fork.json"
    fork.write_text(json.dumps(_fork_record(
        status="open",
        counts={"items": 1, "issues": 0, "pull_requests": 1,
                "by_source": {"org_search_pull_requests": 1}},
        items=[item],
    )))

    rc, full, narrow = _capture(tmp_path, fork)
    capsys.readouterr()

    assert rc == 0, "open is a valid measurement; FORK_GH=1 gates the cron"
    assert full["fork_github"]["program"] == "fork_github_convergence"
    assert full["fork_github"]["schema_version"] == 1
    assert full["fork_github"]["status"] == "open"
    assert full["fork_github"]["counts"]["pull_requests"] == 1
    assert full["fork_github"]["actor"] == "reyerchu"
    assert full["fork_github"]["expected_actor"] == "reyerchu"
    assert full["fork_github"]["round"]["run_id"] == "a" * 64
    assert full["fork_github"]["items"] == [item]
    assert narrow["fork_github"]["items"] == [item]
    assert narrow["fork_github"]["round"]["run_id"] == "a" * 64
    assert narrow["fork_github"]["started_at"] >= ROUND_AT
    assert full["source_errors"] == []


def test_open_github_cannot_contradict_a_generic_converged_lesson():
    item = _item(7)
    github = _fork_record(
        status="open", items=[item],
        counts={"items": 1, "issues": 0, "pull_requests": 1},
    )
    record = LL.make_record(
        {"status": "published", "compose_attempts": [{"exit_code": 0}]},
        _gap_record(), ROUND_AT, 0, fork_github=github,
    )
    convergence = record["convergence"]
    assert convergence["delivery_converged"] is True
    assert convergence["delivery_round_converged"] is True
    assert convergence["converged"] is False
    assert convergence["round_converged"] is False
    assert record["lesson"]["outcome"] == "finished_with_open_fork_work"


@pytest.mark.parametrize("mutation,needle", [
    (lambda gap: gap.update(status="incomplete"), "status is not complete"),
    (lambda gap: gap.update(schema_version=True), "schema_version is not 1"),
    (lambda gap: gap.update(q1_unmeasured=["yosys"]),
     "q1_unmeasured is not empty"),
    (lambda gap: gap.update(q2_unmeasured_ship=["yosys"]),
     "q2_unmeasured_ship is not empty"),
    (lambda gap: gap.update(q1_sync_lag_at_merge=None),
     "q1_sync_lag_at_merge is not a non-negative integer"),
    (lambda gap: gap.update(started_at="2026-08-16T06:00:00+08:00"),
     "started_at predates this 05:30 round"),
    (lambda gap: gap.update(started_at="2026-08-17T06:00:00"),
     "started_at is missing or malformed"),
    (lambda gap: gap.update(
        started_at="2099-08-17T06:00:00+08:00",
        completed_at="2099-08-17T06:01:00+08:00"),
     "started_at is in the future"),
    (lambda gap: gap.update(completed_at="2099-08-17T06:01:00+08:00"),
     "completed_at is in the future"),
    (lambda gap: gap.update(source={}), "source.kind"),
    (lambda gap: gap["source"]["_round"].update(schema_version=2),
     "source._round.schema_version"),
    (lambda gap: gap["source"]["_round"].update(run_id="too-short"),
     "source._round.run_id"),
    (lambda gap: gap["source"]["_round"].update(
        started_at="2099-08-17T05:30:01+08:00",
        finished_at="2099-08-17T05:59:00+08:00"),
     "source._round.started_at is in the future"),
    (lambda gap: gap["source"]["_round"].update(
        finished_at="2099-08-17T05:59:00+08:00"),
     "source._round.finished_at is in the future"),
    (lambda gap: gap.update(
        registered_forks=[], ledger_forks=[], rows=[]),
     "registered_forks is missing registered tool"),
    (lambda gap: gap.update(
        ledger_forks=REGISTERED_FORKS[:-1],
        rows=[{"tool": tool} for tool in REGISTERED_FORKS[:-1]]),
     "ledger_forks is missing registered tool"),
])
def test_partial_stale_or_unproven_gap_zero_is_not_accepted(
        tmp_path, capsys, mutation, needle):
    fork = tmp_path / "fork.json"
    fork.write_text(json.dumps(_fork_record()))
    gap = _gap_record()
    mutation(gap)

    rc, full, narrow = _capture(tmp_path, fork, gap_record=gap)
    capsys.readouterr()

    assert rc == 1
    assert full["convergence"]["sync_lag_at_merge"] is None
    assert full["convergence"]["delivery_converged"] is False
    assert full["convergence"]["converged"] is False
    assert any(needle in error for error in full["source_errors"])
    assert narrow["delivery_converged"] is False


def test_gap_and_github_must_name_the_same_daily_round_nonce(tmp_path, capsys):
    fork = tmp_path / "fork.json"
    record = _fork_record()
    record["round"]["run_id"] = "b" * 64
    fork.write_text(json.dumps(record))

    rc, full, _narrow = _capture(tmp_path, fork)
    capsys.readouterr()

    assert rc == 1
    assert full["fork_github"]["round"]["run_id"] == "b" * 64
    assert any("different daily_0530 run_id" in error
               for error in full["source_errors"])
    assert full["convergence"]["converged"] is False


def test_ll_accepts_the_real_checkers_nested_count_schema(tmp_path, capsys):
    forks = tmp_path / "FORKS.json"
    forks.write_text(json.dumps({
        "org": "vibeic",
        "forks": [{"tool": "yosys", "upstream": "YosysHQ/yosys"}],
    }))
    round_json = tmp_path / "round.json"
    round_json.write_text(json.dumps({
        "_round": {
            "schema_version": 1,
            "run_id": "a" * 64,
            "started_at": "2026-08-17T05:30:01+08:00",
            "finished_at": "2026-08-17T05:33:00+08:00",
        },
        "forks": [{
            "fork": "yosys",
            "upstream_pull_requests": {
                "outcome": "complete",
                "upstream_repository": "YosysHQ/yosys",
                "fork_owner": "vibeic",
                "open_count": 0,
                "items": [],
            },
        }],
    }))

    def empty_runner(operation, _query, _variables):
        page = {
            "totalCount": 0, "nodes": [],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
        if operation.startswith("org_"):
            page = dict(page, issueCount=page.pop("totalCount"))
            return {"data": {"search": page}}
        key = "issues" if operation == "viewer_issues" else "pullRequests"
        return {"data": {"viewer": {"login": "reyerchu", key: page}}}

    actual, checker_rc = C.collect(
        forks, round_json, round_at=ROUND_AT, expected_actor="reyerchu",
        runner=empty_runner,
        now=lambda: datetime.fromisoformat("2026-08-17T05:34:00+08:00"),
    )
    assert checker_rc == 0
    fork = tmp_path / "fork.json"
    fork.write_text(json.dumps(actual))

    rc, full, narrow = _capture(tmp_path, fork)
    capsys.readouterr()
    assert rc == 0
    assert full["fork_github"]["counts"] == actual["counts"]
    assert narrow["fork_github"]["counts"]["by_source"] == \
        actual["counts"]["by_source"]


def test_stale_record_is_source_error_and_never_zero(tmp_path, capsys):
    fork = tmp_path / "fork.json"
    record = _fork_record(started_at="2026-08-16T05:34:00+08:00")
    record["completed_at"] = "2026-08-16T05:35:00+08:00"
    fork.write_text(json.dumps(record))

    rc, full, narrow = _capture(tmp_path, fork)
    capsys.readouterr()

    assert rc == 1
    assert full["fork_github"] is None
    assert narrow["fork_github"] is None
    assert any("predates this 05:30 round" in error
               for error in full["source_errors"])
    assert "items" not in narrow, "missing evidence became a zero count"


@pytest.mark.parametrize("mutation", [
    lambda record: record.update(program="something_else"),
    lambda record: record.update(schema_version=2),
    lambda record: record.update(schema_version=True),
    lambda record: record.update(status="clean"),
    lambda record: record.update(actor="somebody-else"),
    lambda record: record["round"].update(run_id="too-short"),
    lambda record: record["round"].update(
        started_at="2026-08-16T05:30:01+08:00"),
    lambda record: record["round"].update(
        finished_at="2026-08-17T05:40:00+08:00"),
    lambda record: record.update(
        started_at="2099-08-17T05:34:00+08:00",
        completed_at="2099-08-17T05:35:00+08:00"),
    lambda record: record.update(started_at="2026-08-17T05:34:00"),
    lambda record: record.update(counts={"open_issues": False}),
    lambda record: record.update(items=["not-an-item"]),
])
def test_malformed_envelope_is_retained_as_source_error(
        tmp_path, capsys, mutation):
    fork = tmp_path / "fork.json"
    record = _fork_record()
    mutation(record)
    fork.write_text(json.dumps(record))

    rc, full, narrow = _capture(tmp_path, fork)
    capsys.readouterr()

    assert rc == 1
    assert full["fork_github"] is None
    assert narrow["fork_github"] is None
    assert full["source_errors"]


@pytest.mark.parametrize("mutation", [
    lambda record: record.update(
        status="converged", items=[_item()],
        counts={"items": 1, "issues": 0, "pull_requests": 1}),
    lambda record: record.update(status="open"),
    lambda record: record.update(status="norecord"),
    lambda record: record["counts"].update(items=7),
    lambda record: record["counts"].update(issues=1, pull_requests=1),
    lambda record: record.update(
        status="open", items=[dict(_item(), url="")],
        counts={"items": 1, "issues": 0, "pull_requests": 1}),
    lambda record: record.update(
        status="open", items=[_item(), _item()],
        counts={"items": 2, "issues": 0, "pull_requests": 2}),
])
def test_contradictory_status_counts_or_identity_is_never_accepted_as_zero(
        tmp_path, capsys, mutation):
    fork = tmp_path / "fork.json"
    record = _fork_record()
    mutation(record)
    fork.write_text(json.dumps(record))

    rc, full, narrow = _capture(tmp_path, fork)
    capsys.readouterr()

    assert rc == 1
    assert full["fork_github"] is None
    assert narrow["fork_github"] is None
    assert full["source_errors"]


def test_missing_json_is_source_error_not_empty_convergence(tmp_path, capsys):
    rc, full, narrow = _capture(tmp_path, tmp_path / "missing.json")
    capsys.readouterr()
    assert rc == 1
    assert full["fork_github"] is None
    assert narrow["fork_github"] is None
    assert any("missing missing.json" in error for error in full["source_errors"])


def test_checker_norecord_and_its_source_errors_are_preserved(tmp_path, capsys):
    fork = tmp_path / "fork.json"
    fork.write_text(json.dumps(_fork_record(
        status="norecord", items=[],
        source_errors=[{"source": "org_pull_requests",
                        "error": "GitHub API did not answer"}],
    )))

    rc, full, narrow = _capture(tmp_path, fork)
    capsys.readouterr()

    assert rc == 1
    assert full["fork_github"]["status"] == "norecord"
    assert full["fork_github"]["source_errors"] == [{
        "source": "org_pull_requests", "error": "GitHub API did not answer",
    }]
    assert narrow["fork_github"]["status"] == "norecord"
    assert any("GitHub API did not answer" in error
               for error in full["source_errors"])
