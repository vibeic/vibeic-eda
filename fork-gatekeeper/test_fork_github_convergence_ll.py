"""The 05:30 LL retains GitHub fork convergence without inventing zero."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capture_release_ll as LL  # noqa: E402


ROUND_AT = "2026-08-17T05:30:00+08:00"
FORK_ENTRIES = json.loads(
    (HERE / "FORKS.json").read_text(encoding="utf-8"))["forks"]
REGISTERED_FORKS = sorted(entry["tool"] for entry in FORK_ENTRIES)
REGISTRY_SHA256 = hashlib.sha256(
    (HERE / "FORKS.json").read_bytes()).hexdigest()
REGISTERED_REPOSITORIES = sorted(
    f"vibeic/{entry['tool']}" for entry in FORK_ENTRIES)
REQUIRED_REPOSITORIES = sorted(
    REGISTERED_REPOSITORIES + ["vibeic/vibeic-eda"])
UPSTREAM_BY_TOOL = {entry["tool"]: entry["upstream"]
                    for entry in FORK_ENTRIES}
SOURCE_KEYS = (
    "exact_repository_issues", "exact_repository_pull_requests",
    "fork_delivery_pull_requests", "viewer_upstream_issues",
    "viewer_upstream_pull_requests", "round_upstream_pull_requests",
)


def _inventory_row(repository: str) -> dict:
    registered = repository in REGISTERED_REPOSITORIES
    return {
        "repository": repository,
        "scope": "registered_fork" if registered else "vibeic_eda",
        "status": "complete",
        "is_fork": registered,
        "is_archived": False,
        "has_issues_enabled": True,
        "issues": {"outcome": "complete", "open_count": 0},
        "pull_requests": {"outcome": "complete", "open_count": 0},
    }


def _recount(record: dict) -> None:
    rows = {row["repository"].lower(): row
            for row in record["inventory"]["repositories"]}
    round_rows = {row["repository"].lower(): row
                  for row in record["round_upstream"]["forks"]}
    for row in rows.values():
        row["issues"]["open_count"] = 0
        row["pull_requests"]["open_count"] = 0
    for row in round_rows.values():
        row["open_count"] = 0
    delivery = record["delivery"]["pull_requests"]
    delivery.update(open_count=0, matched_count=0, excluded_count=0,
                    excluded=[])
    actor = record["upstream_actor"]
    for field in ("issues", "pull_requests"):
        actor[field].update(seen_count=0, matched_count=0)

    by_source = {key: 0 for key in SOURCE_KEYS}
    for item in record["items"]:
        repo = item["repository"].lower()
        kind = item["type"]
        sources = item["sources"]
        if "exact_repository_issues" in sources:
            rows[repo]["issues"]["open_count"] += 1
            by_source["exact_repository_issues"] += 1
        if "exact_repository_pull_requests" in sources:
            rows[repo]["pull_requests"]["open_count"] += 1
            by_source["exact_repository_pull_requests"] += 1
        if "fork_delivery_pull_requests" in sources:
            delivery["open_count"] += 1
            delivery["matched_count"] += 1
            by_source["fork_delivery_pull_requests"] += 1
        if "viewer_upstream" in sources:
            actor[kind + "s"]["seen_count"] += 1
            actor[kind + "s"]["matched_count"] += 1
            by_source[f"viewer_upstream_{kind}s"] += 1
        if "round_upstream_pull_requests" in sources:
            round_rows[repo]["open_count"] += 1
            by_source["round_upstream_pull_requests"] += 1

    items = record["items"]
    record["counts"] = {
        "items": len(items),
        "issues": sum(item["type"] == "issue" for item in items),
        "pull_requests": sum(item["type"] == "pull_request" for item in items),
        "registered_fork_items": sum(
            "registered_fork" in item["scopes"] for item in items),
        "upstream_items": sum("upstream" in item["scopes"] for item in items),
        "vibeic_eda_items": sum(
            "vibeic_eda" in item["scopes"] for item in items),
        "fork_delivery_items": sum(
            "fork_delivery" in item["scopes"] for item in items),
        "by_source": by_source,
    }


def _fork_record(*, status="converged", started_at=None,
                 counts=None, items=None, source_errors=None) -> dict:
    catalog_repositories = [
        {"repository": repository,
         "is_fork": repository in REGISTERED_REPOSITORIES,
         "is_archived": False, "has_issues_enabled": True}
        for repository in sorted(
            REQUIRED_REPOSITORIES + ["vibeic/vibe-ic"])
    ]
    record = {
        "program": "fork_github_convergence",
        "schema_version": 2,
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
        "inventory": {
            "status": "complete",
            "catalog": {
                "outcome": "complete",
                "total_repositories": len(catalog_repositories),
                "repositories": catalog_repositories,
                "live_fork_repositories": list(REGISTERED_REPOSITORIES),
                "unregistered_live_fork_repositories": [],
            },
            "canonical_registered_repositories": list(REGISTERED_REPOSITORIES),
            "required_repositories": list(REQUIRED_REPOSITORIES),
            "required_repository_count": len(REQUIRED_REPOSITORIES),
            "completed_repository_count": len(REQUIRED_REPOSITORIES),
            "repositories": [_inventory_row(repo)
                             for repo in REQUIRED_REPOSITORIES],
        },
        "delivery": {
            "repository": "vibeic/vibe-ic", "status": "complete",
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
            "issues": {"outcome": "complete", "seen_count": 0,
                       "matched_count": 0},
            "pull_requests": {"outcome": "complete", "seen_count": 0,
                              "matched_count": 0},
        },
        "round_upstream": {
            "status": "complete", "required_fork_count": len(FORK_ENTRIES),
            "completed_fork_count": len(FORK_ENTRIES),
            "forks": [{"tool": entry["tool"],
                       "repository": entry["upstream"],
                       "outcome": "complete", "open_count": 0}
                      for entry in FORK_ENTRIES],
        },
        "status": status,
        "items": [] if items is None else items,
        "source_errors": [] if source_errors is None else source_errors,
    }
    _recount(record)
    if counts is not None:
        record["counts"] = counts
    return record


def _item(number=1) -> dict:
    return {
        "repository": "vibeic/yosys",
        "type": "pull_request",
        "number": number,
        "title": "fix yosys",
        "url": f"https://github.com/vibeic/yosys/pull/{number}",
        "author": "reyerchu", "head_owner": "vibeic",
        "head_ref": f"fix-{number}", "base_ref": "main", "draft": False,
        "updated_at": "2026-08-17T05:31:00+08:00",
        "sources": ["exact_repository_pull_requests"],
        "scopes": ["registered_fork"],
    }


def _delivery_item(number=91, **updates) -> dict:
    item = {
        "repository": "vibeic/vibe-ic",
        "type": "pull_request",
        "number": number,
        "title": "chore(image-anchor): adopt vibeic-eda 0.3.5",
        "url": f"https://github.com/vibeic/vibe-ic/pull/{number}",
        "author": "reyerchu",
        "head_owner": "vibeic",
        "head_ref": "eda-anchor-0.3.5",
        "base_ref": "main",
        "draft": False,
        "updated_at": "2026-08-17T05:31:00+08:00",
        "sources": ["fork_delivery_pull_requests"],
        "scopes": ["fork_delivery"],
        "delivery_kind": "anchor",
        "delivery_anomalies": [],
    }
    item.update(updates)
    kind = LL._delivery_kind(item["head_ref"])
    if kind is not None and "delivery_anomalies" not in updates:
        item["delivery_anomalies"] = LL._delivery_anomalies(item, kind)
    return item


def _upstream_item(number=1455) -> dict:
    return {
        "repository": "steveicarus/iverilog",
        "type": "pull_request",
        "number": number,
        "title": "upstream pull request from the fork owner",
        "url": f"https://github.com/steveicarus/iverilog/pull/{number}",
        "author": "reyerchu",
        "head_owner": "vibeic",
        "head_ref": "upstream-fix",
        "base_ref": "master",
        "draft": False,
        "updated_at": "2026-08-17T05:31:00+08:00",
        "sources": ["round_upstream_pull_requests"],
        "scopes": ["upstream"],
    }


def _excluded_delivery_item(number=92, **updates) -> dict:
    item = {
        "repository": "vibeic/vibe-ic",
        "type": "pull_request",
        "number": number,
        "title": "unrelated pull request",
        "url": f"https://github.com/vibeic/vibe-ic/pull/{number}",
        "author": None,
        "head_owner": "somebody-else",
        "head_ref": "eda-anchor-0.3.5",
        "base_ref": "main",
        "draft": False,
        "updated_at": "2026-08-17T05:31:00+08:00",
        "reasons": ["head_owner_is_not_vibeic"],
    }
    item.update(updates)
    return item


def _validate_record(record: dict) -> tuple[dict | None, list[str]]:
    return LL._validate_fork_github(
        record, HERE / "fixture-fork-github.json", ROUND_AT,
        HERE / "FORKS.json", "reyerchu")


def _gap_record(**updates) -> dict:
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
        "assertions": [],
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
        "q2_unmeasured_stranded": [],
        "tip_rejected": [],
        "tip_unverified": [],
        "registered_forks": REGISTERED_FORKS,
        "ledger_forks": REGISTERED_FORKS,
        "merge_tips_seen": {tool: "b" * 40 for tool in REGISTERED_FORKS},
        "fleet_errors": [],
        "rows": [gap_row(tool) for tool in REGISTERED_FORKS],
        "measured_with_fetch": True,
    }
    record.update(updates)
    return record


def _ai_authorization_record() -> dict:
    return {
        "program": "check_ai_release_authorization",
        "schema_version": 1,
        "checked_at": "2026-08-17T06:02:00+08:00",
        "round_at": ROUND_AT,
        "source": "/state/daily_0530.json",
        "registered_forks": REGISTERED_FORKS,
        "registered_count": len(REGISTERED_FORKS),
        "registry": {
            "source": str(HERE / "FORKS.json"),
            "sha256": REGISTRY_SHA256,
        },
        "round": {
            "schema_version": 1, "run_id": "a" * 64,
            "started_at": "2026-08-17T05:30:01+08:00",
            "finished_at": "2026-08-17T05:59:00+08:00",
            "applied": True,
        },
        "status": "authorized",
        "case_count": 0, "verified_count": 0, "unverified_count": 0,
        "transport": {"invoked": False, "outcome": None, "rc": None},
        "case_results": [], "reasons": [], "source_errors": [],
    }


def _capture(tmp_path: Path, fork_path: Path, *, gap_record=None
             ) -> tuple[int, dict, dict]:
    gap = tmp_path / "gap.json"
    gap.write_text(json.dumps(_gap_record() if gap_record is None else gap_record))
    ai = tmp_path / "ai-authorization.json"
    ai.write_text(json.dumps(_ai_authorization_record()))
    state = tmp_path / "state"
    rc = LL.main([
        "--state-dir", str(state),
        "--release-json", str(tmp_path / "release-not-read.json"),
        "--gap-json", str(gap),
        "--fork-github-json", str(fork_path),
        "--ai-authorization-json", str(ai),
        "--expected-github-actor", "reyerchu",
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
    assert '--ai-authorization-json "${AI_AUTH_JSON}"' in ll_arm
    assert '--forks-json "${DIR}/FORKS.json"' in ll_arm
    assert ('--expected-github-actor "${GK_GITHUB_ACTOR:-reyerchu}"'
            in ll_arm)
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
    item = _item(17)
    fork = tmp_path / "fork.json"
    fork.write_text(json.dumps(_fork_record(
        status="open", items=[item],
    )))

    rc, full, narrow = _capture(tmp_path, fork)
    capsys.readouterr()

    assert rc == 0, "open is a valid measurement; FORK_GH=1 gates the cron"
    assert full["fork_github"]["program"] == "fork_github_convergence"
    assert full["fork_github"]["schema_version"] == 2
    assert full["fork_github"]["status"] == "open"
    assert full["fork_github"]["counts"]["pull_requests"] == 1
    assert full["fork_github"]["actor"] == "reyerchu"
    assert full["fork_github"]["expected_actor"] == "reyerchu"
    assert full["fork_github"]["round"]["run_id"] == "a" * 64
    assert full["fork_github"]["items"] == [item]
    assert narrow["fork_github"]["items"] == [item]
    assert narrow["fork_github"]["round"]["run_id"] == "a" * 64
    assert narrow["fork_github"]["inventory"][
        "required_repository_count"] == 38
    assert narrow["fork_github"]["delivery"]["status"] == "complete"
    assert narrow["fork_github"]["round_upstream"][
        "required_fork_count"] == 37
    assert narrow["fork_github"]["started_at"] >= ROUND_AT
    assert full["source_errors"] == []


def test_open_github_cannot_contradict_a_generic_converged_lesson():
    item = _item(7)
    github = _fork_record(
        status="open", items=[item],
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
    (lambda gap: gap.update(q2_unmeasured_stranded=["yosys"]),
     "q2_unmeasured_stranded is not empty"),
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
        rows=gap["rows"][:-1]),
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


def test_gap_row_nonzero_cannot_be_laundered_by_zero_headline(tmp_path, capsys):
    fork = tmp_path / "fork.json"
    fork.write_text(json.dumps(_fork_record()))
    gap = _gap_record()
    gap["rows"][0]["sync_lag_at_merge"] = 1

    rc, full, narrow = _capture(tmp_path, fork, gap_record=gap)
    capsys.readouterr()

    assert rc == 1
    assert any("q1_sync_lag_at_merge contradicts retained per-fork rows"
               in error for error in full["source_errors"])
    assert full["convergence"]["sync_lag_at_merge"] is None
    assert full["convergence"]["converged"] is False
    assert narrow["converged"] is False


def test_gap_nonzero_headline_cannot_override_zero_rows(tmp_path, capsys):
    fork = tmp_path / "fork.json"
    fork.write_text(json.dumps(_fork_record()))
    gap = _gap_record(q1_sync_lag_at_merge=1)

    rc, full, narrow = _capture(tmp_path, fork, gap_record=gap)
    capsys.readouterr()

    assert rc == 1
    assert any("q1_sync_lag_at_merge contradicts retained per-fork rows"
               in error for error in full["source_errors"])
    assert full["convergence"]["sync_lag_at_merge"] is None
    assert full["convergence"]["converged"] is False
    assert narrow["converged"] is False


@pytest.mark.parametrize(("headline", "forged"), [
    ("assertions", [{"tool": REGISTERED_FORKS[0], "contents": "d" * 40}]),
    ("q1_image_behind_upstream", 1),
    ("q1_forks_behind", 1),
    ("q1_sync_lag", 1),
    ("q1_release_lag", 1),
    ("q2_our_commits_stranded", 1),
    ("q2_ours_past_the_pin", 1),
    ("q2_ours_past_the_pin_substantive", 1),
    ("q2_forks_not_built_from_ours", [REGISTERED_FORKS[0]]),
    ("q2_unshipped_commits", [
        {"tool": REGISTERED_FORKS[0], "sha": "d" * 40, "merge": False},
    ]),
    ("tip_unverified", [
        {"tool": REGISTERED_FORKS[0], "why": "forged unknown"},
    ]),
])
def test_gap_consumer_recomputes_each_retained_headline(headline, forged):
    gap = _gap_record()
    gap[headline] = forged

    accepted, errors = LL._validate_gap(
        gap, Path("fixture-gap.json"), ROUND_AT, HERE / "FORKS.json")

    assert accepted is None
    assert any(f"{headline} contradicts retained per-fork rows" in error
               for error in errors)


def test_legitimate_not_built_from_ours_row_keeps_producer_semantics():
    gap = _gap_record()
    row = gap["rows"][0]
    row.update(
        integrated=False,
        pin=None,
        release_lag=None,
        image_behind=None,
        ours_unshipped=None,
        ours_unshipped_substantive=None,
    )
    gap["q2_forks_not_built_from_ours"] = [row["tool"]]

    accepted, errors = LL._validate_gap(
        gap, Path("fixture-gap.json"), ROUND_AT, HERE / "FORKS.json")

    assert errors == []
    assert accepted == gap


def test_unmeasured_ahead_on_not_built_row_cannot_launder_stranded_work():
    gap = _gap_record()
    row = gap["rows"][0]
    row.update(
        integrated=False,
        ahead=None,
        pin=None,
        release_lag=None,
        image_behind=None,
        ours_unshipped=None,
        ours_unshipped_substantive=None,
    )
    gap["q2_forks_not_built_from_ours"] = [row["tool"]]
    # This is the historical icsprout55-pdk shape: its unknown ahead count was
    # silently summed as zero and no third-state field admitted the omission.
    gap["q2_unmeasured_stranded"] = []

    accepted, errors = LL._validate_gap(
        gap, Path("fixture-gap.json"), ROUND_AT, HERE / "FORKS.json")

    assert accepted is None
    assert any("q2_unmeasured_stranded contradicts retained per-fork rows"
               in error for error in errors)


def test_contents_assertion_is_not_misclassified_as_unmeasured_or_not_built():
    gap = _gap_record()
    row = gap["rows"][0]
    row.update(
        kind="contents_assertion",
        asserted_contents="d" * 64,
        pin=None,
        sync_lag=None,
        sync_lag_at_merge=None,
        release_lag=None,
        image_behind=None,
        ours_unshipped=None,
        ours_unshipped_substantive=None,
        tip_state=None,
        tip_note=None,
    )
    row.pop("merge_ref_seen")
    gap["assertions"] = [{"tool": row["tool"], "contents": "d" * 64}]

    accepted, errors = LL._validate_gap(
        gap, Path("fixture-gap.json"), ROUND_AT, HERE / "FORKS.json")

    assert errors == []
    assert accepted == gap


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


def test_self_consistent_forged_sidecar_actor_cannot_replace_cli_authority(
        tmp_path, capsys):
    fork = tmp_path / "fork.json"
    record = _fork_record()
    record.update(actor="attacker", expected_actor="attacker")
    fork.write_text(json.dumps(record))

    rc, full, narrow = _capture(tmp_path, fork)
    capsys.readouterr()

    assert rc == 1
    assert full["fork_github"] is None
    assert narrow["fork_github"] is None
    assert any("expected_actor does not match configured GitHub actor" in error
               for error in full["source_errors"])
    assert any("actor does not match configured GitHub actor" in error
               for error in full["source_errors"])


def test_ll_accepts_exact_v2_disabled_issue_evidence(tmp_path, capsys):
    actual = _fork_record()
    repo = "vibeic/yosys"
    catalog = next(row for row in actual["inventory"]["catalog"]["repositories"]
                   if row["repository"] == repo)
    inventory = next(row for row in actual["inventory"]["repositories"]
                     if row["repository"] == repo)
    catalog["has_issues_enabled"] = False
    inventory["has_issues_enabled"] = False
    inventory["issues"] = {"outcome": "disabled", "open_count": 0}
    fork = tmp_path / "fork.json"
    fork.write_text(json.dumps(actual))

    rc, full, narrow = _capture(tmp_path, fork)
    capsys.readouterr()
    assert rc == 0
    assert full["fork_github"]["counts"] == actual["counts"]
    assert narrow["fork_github"]["inventory"] == actual["inventory"]


def test_ll_accepts_and_retains_exact_delivery_anomalies():
    item = _delivery_item(
        base_ref="release", draft=True, title="manually changed title")
    record = _fork_record(status="open", items=[item])

    accepted, errors = _validate_record(record)

    assert errors == []
    assert accepted is not None
    assert accepted["items"][0]["delivery_anomalies"] == [
        "base_ref_is_not_main",
        "draft_is_not_false",
        "title_does_not_match_anchor_producer",
    ]


@pytest.mark.parametrize("anomalies", [
    [],
    ["base_ref_is_not_main", "draft_is_not_false"],
    ["base_ref_is_not_main", "draft_is_not_false",
     "title_does_not_match_anchor_producer", "invented"],
])
def test_ll_rejects_delivery_anomaly_drift(anomalies):
    item = _delivery_item(
        base_ref="release", draft=True, title="manually changed title",
        delivery_anomalies=anomalies)
    accepted, errors = _validate_record(
        _fork_record(status="open", items=[item]))

    assert accepted is None
    assert any("violates producer identity" in error for error in errors)


def test_ll_accepts_exact_audited_delivery_exclusion():
    record = _fork_record()
    excluded = _excluded_delivery_item()
    record["delivery"]["pull_requests"].update(
        open_count=1, matched_count=0, excluded_count=1,
        excluded=[excluded])

    accepted, errors = _validate_record(record)

    assert errors == []
    assert accepted is not None
    assert accepted["delivery"]["pull_requests"]["excluded"] == [excluded]


@pytest.mark.parametrize("updates", [
    {"reasons": ["head_ref_is_not_a_delivery_producer_branch"]},
    {"title": ""},
    {"updated_at": "2026-08-17T05:31:00"},
    {"type": "issue"},
    {"base_ref": None},
    {"draft": None},
    {"author": ""},
])
def test_ll_rejects_malformed_or_misclassified_delivery_exclusion(updates):
    record = _fork_record()
    record["delivery"]["pull_requests"].update(
        open_count=1, matched_count=0, excluded_count=1,
        excluded=[_excluded_delivery_item(**updates)])

    accepted, errors = _validate_record(record)

    assert accepted is None
    assert errors


def _drop_inventory_row(record: dict) -> None:
    record["inventory"]["repositories"].pop()


def _duplicate_inventory_row(record: dict) -> None:
    record["inventory"]["repositories"][-1] = copy.deepcopy(
        record["inventory"]["repositories"][0])


def _drop_required_identity(record: dict) -> None:
    record["inventory"]["required_repositories"].pop()


def _wrong_inventory_denominator(record: dict) -> None:
    record["inventory"]["required_repository_count"] -= 1


def _consistent_but_unregistered_live_fork(record: dict) -> None:
    catalog = record["inventory"]["catalog"]
    row = next(row for row in catalog["repositories"]
               if row["repository"] == "vibeic/vibe-ic")
    row["is_fork"] = True
    catalog["live_fork_repositories"].append("vibeic/vibe-ic")
    catalog["unregistered_live_fork_repositories"] = ["vibeic/vibe-ic"]


def _disabled_issue_count_is_not_zero(record: dict) -> None:
    row = record["inventory"]["repositories"][0]
    catalog = next(
        item for item in record["inventory"]["catalog"]["repositories"]
        if item["repository"] == row["repository"])
    row["has_issues_enabled"] = False
    catalog["has_issues_enabled"] = False
    row["issues"] = {"outcome": "disabled", "open_count": 1}


def _drop_round_upstream_row(record: dict) -> None:
    record["round_upstream"]["forks"].pop()


def _shift_round_upstream_count(record: dict) -> None:
    record["round_upstream"]["forks"][0]["open_count"] = 1
    record["round_upstream"]["forks"][1]["open_count"] = 0
    record["counts"]["by_source"]["round_upstream_pull_requests"] = 1


def _actor_seen_less_than_matched(record: dict) -> None:
    record["upstream_actor"]["pull_requests"].update(
        seen_count=0, matched_count=1)
    record["counts"]["by_source"]["viewer_upstream_pull_requests"] = 1


def _source_count_drift(record: dict) -> None:
    record["counts"]["by_source"]["exact_repository_issues"] = 1


@pytest.mark.parametrize("mutation", [
    _drop_inventory_row,
    _duplicate_inventory_row,
    _drop_required_identity,
    _wrong_inventory_denominator,
    _consistent_but_unregistered_live_fork,
    _disabled_issue_count_is_not_zero,
    _drop_round_upstream_row,
    _shift_round_upstream_count,
    _actor_seen_less_than_matched,
    _source_count_drift,
])
def test_ll_rejects_incomplete_or_contradictory_v2_source_contract(mutation):
    record = _fork_record()
    mutation(record)

    accepted, errors = _validate_record(record)

    assert accepted is None
    assert errors


def test_ll_rejects_round_count_shifted_between_upstream_repositories():
    record = _fork_record(status="open", items=[_upstream_item()])
    right = next(
        row for row in record["round_upstream"]["forks"]
        if row["repository"].lower() == "steveicarus/iverilog")
    wrong = next(
        row for row in record["round_upstream"]["forks"]
        if row["repository"].lower() != "steveicarus/iverilog")
    right["open_count"] = 0
    wrong["open_count"] = 1

    accepted, errors = _validate_record(record)

    assert accepted is None
    assert any("retained items=" in error for error in errors)


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
    lambda record: record.update(schema_version=1),
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
