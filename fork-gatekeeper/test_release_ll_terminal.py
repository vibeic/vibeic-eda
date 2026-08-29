from __future__ import annotations

import json

import check_release_ll_terminal as C


ROUND = "2026-08-30T05:30:00+08:00"
RUN_ID = "a" * 32


def _record(*, converged=True, measured=True):
    return {
        "program": "release_ll",
        "schema_version": 3,
        "round_at": ROUND,
        "run_id": RUN_ID,
        "source_errors": [],
        "convergence": {
            "round_measured": measured,
            "round_converged": converged,
            "sync_lag_at_merge": 0,
            "release_lag": 0 if converged else 3,
            "image_behind_upstream": 0 if converged else 2,
            "ours_past_pin": 0 if converged else 1,
            "ours_past_pin_substantive": 0,
            "our_commits_stranded": 0,
        },
    }


def test_current_converged_round_authorizes_zero(tmp_path):
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(_record()), encoding="utf-8")
    rc, detail = C.assess(path, ROUND, RUN_ID)
    assert rc == C.RC_CONVERGED
    assert "converged" in detail


def test_valid_finished_with_gap_record_is_not_success(tmp_path):
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(_record(converged=False)), encoding="utf-8")
    rc, detail = C.assess(path, ROUND, RUN_ID)
    assert rc == C.RC_OPEN
    assert "release=3" in detail and "image=2" in detail and "past-pin=1" in detail


def test_stale_or_unmeasured_record_is_norecord(tmp_path):
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(_record(measured=False)), encoding="utf-8")
    assert C.assess(path, ROUND, RUN_ID)[0] == C.RC_NORECORD
    path.write_text(json.dumps(_record()), encoding="utf-8")
    assert C.assess(path, ROUND + "-other", RUN_ID)[0] == C.RC_NORECORD


def test_converged_boolean_cannot_hide_a_nonzero_counter(tmp_path):
    path = tmp_path / "latest.json"
    record = _record()
    record["convergence"]["release_lag"] = 1
    path.write_text(json.dumps(record), encoding="utf-8")
    rc, detail = C.assess(path, ROUND, RUN_ID)
    assert rc == C.RC_NORECORD
    assert "claims convergence" in detail
