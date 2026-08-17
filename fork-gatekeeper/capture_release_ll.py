#!/usr/bin/env python3
"""Capture the 05:30 release learning loop as retained, machine-readable facts.

The daily text log answered *what command failed* but not the operational
question that matters next morning: did the round recover by itself, and if it
did not, which delivery hop kept the published image behind?  Worse, the live
release JSON used to keep yesterday's success on failure.

This program runs after `fork_gap_report` on every 05:30 round.  It joins the
release program's terminal record to the measured convergence result and keeps:

* `release-ll/latest.json` — the latest complete learning-loop record;
* `release-ll/rounds/<stamp>.json` — full per-round evidence, 400-day default;
* `release-ll/index.jsonl` — a narrow append-only record, never pruned.

"LL" here is evidence, not generated prose: trigger, control applied, attempts,
outcome, and the final measured lag.  A future gate can therefore learn from a
class of failure without scraping human-oriented logs or inheriting a story.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Optional

import gk_state

KEEP_DAYS = int(os.environ.get("GK_RELEASE_LL_KEEP_DAYS", "400"))


def _read(path: Path) -> tuple[Optional[dict], Optional[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"missing {path.name}"
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"unreadable {path.name}: {type(exc).__name__}: {exc}"
    if not isinstance(value, dict):
        return None, f"{path.name} is not a JSON object"
    return value, None


def _parse_when(value: Optional[str]) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _strict_when(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _target(release: dict) -> Optional[str]:
    for key in ("version_candidate", "version"):
        value = release.get(key)
        if isinstance(value, dict) and value.get("to"):
            return str(value["to"])
    return None


def _selftest_summary(record: Optional[dict]) -> Optional[dict]:
    """Retain verdict and supervision outcome as two different facts.

    In particular, ``exit_code=2`` means NORECORD; ``outcome=stalled`` says
    why no complete record formed.  Folding those into one timeout/failure
    label would lose the distinction the semantic pytest gate was added for.
    """
    if record is None:
        return None
    rc = record.get("exit_code")
    if rc == 0:
        status = "passed"
    elif rc == 1:
        status = "red"
    elif rc == 2:
        status = "norecord"
    else:
        status = "cannot_ask"
    progress = record.get("progress")
    return {
        "status": status,
        "exit_code": rc,
        "outcome": record.get("outcome"),
        "reason": record.get("reason"),
        "mechanism": record.get("mechanism"),
        "elapsed_seconds": record.get("elapsed_seconds"),
        "stall_grace_seconds": record.get("stall_grace_seconds"),
        "total_runtime_ceiling_seconds": record.get(
            "total_runtime_ceiling_seconds"),
        "progress": progress if isinstance(progress, dict) else None,
    }


def _fork_github_summary(record: Optional[dict]) -> Optional[dict]:
    """Keep the checker's answer verbatim enough to audit every open item.

    Missing evidence stays ``None``. In particular, this function never builds
    an empty counts object or an empty item list on behalf of a checker that did
    not produce a valid current-round record.
    """
    if record is None:
        return None
    return {
        "program": record["program"],
        "schema_version": record["schema_version"],
        "status": record["status"],
        "started_at": record["started_at"],
        "completed_at": record["completed_at"],
        "actor": record["actor"],
        "expected_actor": record["expected_actor"],
        "round": dict(record["round"]),
        "counts": dict(record["counts"]),
        "items": [dict(item) for item in record["items"]],
        "source_errors": [dict(error) for error in record["source_errors"]],
    }


def _validate_fork_github(record: dict, path: Path, round_at: str
                          ) -> tuple[Optional[dict], list[str]]:
    """Validate provenance, shape and freshness before retaining the answer."""
    errors: list[str] = []
    observed_now = datetime.now(timezone.utc)
    if record.get("program") != "fork_github_convergence":
        errors.append("program is not fork_github_convergence")
    if (type(record.get("schema_version")) is not int
            or record["schema_version"] != 1):
        errors.append("schema_version is not 1")
    if record.get("status") not in {"converged", "open", "norecord"}:
        errors.append("status is not converged, open, or norecord")

    started = _strict_when(record.get("started_at"))
    completed = _strict_when(record.get("completed_at"))
    boundary = _strict_when(round_at)
    if boundary is None:
        errors.append("the 05:30 round boundary is malformed")
    elif boundary > observed_now:
        errors.append("the 05:30 round boundary is in the future")
    if started is None:
        errors.append("started_at is missing or malformed")
    elif boundary is not None and started < boundary:
        errors.append("started_at predates this 05:30 round")
    elif started > observed_now:
        errors.append("started_at is in the future")
    if completed is None:
        errors.append("completed_at is missing or malformed")
    elif started is not None and completed < started:
        errors.append("completed_at precedes started_at")
    elif completed > observed_now:
        errors.append("completed_at is in the future")

    actor = record.get("actor")
    expected_actor = record.get("expected_actor")
    status = record.get("status")
    if actor is not None and (not isinstance(actor, str) or not actor):
        errors.append("actor is malformed")
    if not isinstance(expected_actor, str) or not expected_actor:
        errors.append("expected_actor is missing or malformed")
    elif status in {"converged", "open"} and actor != expected_actor:
        errors.append("actor does not match expected_actor")
    round_source = record.get("round")
    if not isinstance(round_source, dict):
        errors.append("round is not an object")
    else:
        source_boundary = _strict_when(round_source.get("round_at"))
        source_started = _strict_when(round_source.get("started_at"))
        source_finished = _strict_when(round_source.get("finished_at"))
        run_id = round_source.get("run_id")
        if (type(round_source.get("schema_version")) is not int
                or round_source["schema_version"] != 1):
            errors.append("round.schema_version is not 1")
        if (not isinstance(run_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", run_id)):
            errors.append("round.run_id is missing or malformed")
        if source_boundary is None:
            errors.append("round.round_at is missing or malformed")
        elif boundary is not None and source_boundary != boundary:
            errors.append("round.round_at does not match this 05:30 round")
        if source_started is None or source_finished is None:
            errors.append("round started_at/finished_at is missing or malformed")
        else:
            if boundary is not None and source_started < boundary:
                errors.append("round.started_at predates this 05:30 round")
            if source_finished < source_started:
                errors.append("round.finished_at precedes round.started_at")
            if started is not None and source_finished > started:
                errors.append(
                    "round.finished_at is after GitHub collection started")

    def valid_count_tree(value: object) -> bool:
        if type(value) is int:
            return value >= 0
        return (isinstance(value, dict)
                and all(isinstance(key, str) and key
                        and valid_count_tree(child)
                        for key, child in value.items()))

    counts = record.get("counts")
    counts_valid = isinstance(counts, dict) and valid_count_tree(counts)
    if not counts_valid:
        errors.append("counts is not an object tree of non-negative integers")
    items = record.get("items")
    items_valid = isinstance(items, list)
    identities: set[tuple[str, str, int]] = set()
    if items_valid:
        for pos, item in enumerate(items):
            if not isinstance(item, dict):
                items_valid = False
                errors.append(f"items[{pos}] is not an object")
                continue
            repo = item.get("repository")
            kind = item.get("type")
            number = item.get("number")
            url = item.get("url")
            repo_parts = repo.split("/") if isinstance(repo, str) else []
            if (len(repo_parts) != 2 or not all(repo_parts)
                    or any(part.strip() != part for part in repo_parts)
                    or kind not in {"issue", "pull_request"}
                    or type(number) is not int or number <= 0
                    or not isinstance(url, str) or not url):
                items_valid = False
                errors.append(
                    f"items[{pos}] has no valid repository/type/number/url identity")
                continue
            identity = (repo.lower(), kind, number)
            if identity in identities:
                items_valid = False
                errors.append(f"items[{pos}] duplicates {repo} {kind} #{number}")
            identities.add(identity)
    else:
        errors.append("items is not a list")

    if counts_valid and items_valid:
        expected_counts = {
            "items": len(items),
            "issues": sum(item["type"] == "issue" for item in items),
            "pull_requests": sum(
                item["type"] == "pull_request" for item in items),
        }
        for name, expected in expected_counts.items():
            if counts.get(name) != expected:
                errors.append(
                    f"counts.{name}={counts.get(name)!r}, expected {expected}")

    upstream_errors = record.get("source_errors")
    source_errors_valid = (
        isinstance(upstream_errors, list)
        and all(isinstance(error, dict)
                and isinstance(error.get("source"), str)
                and bool(error["source"])
                and isinstance(error.get("error"), str)
                and bool(error["error"])
                for error in upstream_errors)
    )
    if not source_errors_valid:
        errors.append("source_errors is not a list of source/error objects")

    if (status in {"converged", "open", "norecord"}
            and items_valid and source_errors_valid):
        consistent = (
            (status == "converged" and not items and not upstream_errors)
            or (status == "open" and bool(items) and not upstream_errors)
            or (status == "norecord" and bool(upstream_errors))
        )
        if not consistent:
            errors.append(
                f"status={status} contradicts items/source_errors cardinality")

    if errors:
        return None, [f"{path.name} is invalid: " + "; ".join(errors)]

    summary = _fork_github_summary(record)
    assert summary is not None
    source_errors = [
        f"{path.name}: {error['source']}: {error['error']}"
        for error in upstream_errors
    ]
    return summary, source_errors


def _registered_fleet(path: Path) -> tuple[list[str], list[str]]:
    """Read the canonical fleet independently of the gap producer."""
    errors: list[str] = []
    tools: list[str] = []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [f"could not read registered fleet {path}: "
                    f"{type(exc).__name__}: {exc}"]
    entries = value.get("forks") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        return [], [f"registered fleet {path} has no forks list"]
    for pos, entry in enumerate(entries):
        tool = entry.get("tool") if isinstance(entry, dict) else None
        if (not isinstance(tool, str) or not tool
                or tool.strip() != tool):
            errors.append(
                f"registered fleet forks[{pos}].tool is malformed")
            continue
        tools.append(tool)
    duplicates = sorted(tool for tool in set(tools) if tools.count(tool) > 1)
    if duplicates:
        errors.append("registered fleet has duplicate tool(s): "
                      + ", ".join(duplicates))
    if not tools:
        errors.append("registered fleet is empty")
    return sorted(tools), errors


def _validate_gap(record: dict, path: Path, round_at: str,
                  forks_json: Path
                  ) -> tuple[Optional[dict], list[str]]:
    """Accept only a complete, current, round-scoped gap measurement.

    A numeric zero is not enough: an incomplete sweep can sum the rows it did
    answer to zero.  The producer schema, its own timestamps, both third-state
    lists, and the daily merge round it names are therefore one contract.
    """
    errors: list[str] = []
    observed_now = datetime.now(timezone.utc)
    if record.get("program") != "fork_gap_report":
        errors.append("program is not fork_gap_report")
    if (type(record.get("schema_version")) is not int
            or record["schema_version"] != 1):
        errors.append("schema_version is not 1")
    if record.get("status") != "complete":
        errors.append("status is not complete")

    boundary = _strict_when(round_at)
    started = _strict_when(record.get("started_at"))
    completed = _strict_when(record.get("completed_at"))
    if boundary is None:
        errors.append("the 05:30 round boundary is malformed")
    elif boundary > observed_now:
        errors.append("the 05:30 round boundary is in the future")
    if started is None:
        errors.append("started_at is missing or malformed")
    elif boundary is not None and started < boundary:
        errors.append("started_at predates this 05:30 round")
    elif started > observed_now:
        errors.append("started_at is in the future")
    if completed is None:
        errors.append("completed_at is missing or malformed")
    elif started is not None and completed < started:
        errors.append("completed_at precedes started_at")
    elif completed > observed_now:
        errors.append("completed_at is in the future")

    source = record.get("source")
    source_round = source.get("_round") if isinstance(source, dict) else None
    if not isinstance(source, dict):
        errors.append("source is not an object")
    else:
        if source.get("kind") != "daily_0530_merge_round":
            errors.append("source.kind is not daily_0530_merge_round")
        merge_path = source.get("merge_evidence")
        if not isinstance(merge_path, str) or not merge_path:
            errors.append("source.merge_evidence is missing")
        source_boundary = _strict_when(source.get("round_at"))
        if source_boundary is None:
            errors.append("source.round_at is missing or malformed")
        elif boundary is not None and source_boundary != boundary:
            errors.append("source.round_at does not match this 05:30 round")
    if not isinstance(source_round, dict):
        errors.append("source._round is not an object")
    else:
        if (type(source_round.get("schema_version")) is not int
                or source_round["schema_version"] != 1):
            errors.append("source._round.schema_version is not 1")
        run_id = source_round.get("run_id")
        if (not isinstance(run_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", run_id)):
            errors.append("source._round.run_id is missing or malformed")
        merge_started = _strict_when(source_round.get("started_at"))
        merge_finished = _strict_when(source_round.get("finished_at"))
        if merge_started is None or merge_finished is None:
            errors.append(
                "source._round started_at/finished_at is missing or malformed")
        else:
            if boundary is not None and merge_started < boundary:
                errors.append("source._round.started_at predates this round")
            if merge_started > observed_now:
                errors.append("source._round.started_at is in the future")
            if merge_finished < merge_started:
                errors.append(
                    "source._round.finished_at precedes its started_at")
            if merge_finished > observed_now:
                errors.append("source._round.finished_at is in the future")
            if started is not None and merge_finished > started:
                errors.append(
                    "source._round.finished_at is after gap collection started")
        if source_round.get("applied") is not True:
            errors.append("source._round.applied is not true")

    count_fields = (
        "q1_image_behind_upstream", "q1_forks_behind", "q1_sync_lag",
        "q1_sync_lag_at_merge", "q1_release_lag",
        "q2_our_commits_stranded", "q2_ours_past_the_pin",
        "q2_ours_past_the_pin_substantive",
    )
    for name in count_fields:
        value = record.get(name)
        if type(value) is not int or value < 0:
            errors.append(f"{name} is not a non-negative integer")
    total_past = record.get("q2_ours_past_the_pin")
    substantive = record.get("q2_ours_past_the_pin_substantive")
    if (type(total_past) is int and type(substantive) is int
            and substantive > total_past):
        errors.append(
            "q2_ours_past_the_pin_substantive exceeds q2_ours_past_the_pin")

    for name in ("q1_unmeasured", "q1_unmeasured_at_merge",
                 "q2_unmeasured_ship"):
        value = record.get(name)
        if not isinstance(value, list):
            errors.append(f"{name} is not a list")
        elif value:
            errors.append(f"{name} is not empty")
    for name in ("q2_forks_not_built_from_ours", "q2_unshipped_commits",
                 "rows", "tip_rejected", "tip_unverified"):
        if not isinstance(record.get(name), list):
            errors.append(f"{name} is not a list")

    # Repeat the producer's denominator proof from the canonical manifest.  A
    # status string and numeric zero are not evidence that all registered forks
    # were present; the LL record must reject a sidecar with one omitted row
    # even if somebody forged its status back to ``complete``.
    expected, fleet_manifest_errors = _registered_fleet(forks_json)
    errors.extend(fleet_manifest_errors)

    def exact_tool_list(name: str, value: object) -> None:
        if not isinstance(value, list):
            errors.append(f"{name} is not a list")
            return
        malformed = [pos for pos, tool in enumerate(value)
                     if (not isinstance(tool, str) or not tool
                         or tool.strip() != tool)]
        if malformed:
            errors.append(f"{name} has malformed identity at position(s): "
                          + ", ".join(map(str, malformed)))
            return
        duplicates = sorted(
            tool for tool in set(value) if value.count(tool) > 1)
        if duplicates:
            errors.append(f"{name} has duplicate tool(s): "
                          + ", ".join(duplicates))
        missing = sorted(set(expected) - set(value))
        unexpected = sorted(set(value) - set(expected))
        if missing:
            errors.append(f"{name} is missing registered tool(s): "
                          + ", ".join(missing))
        if unexpected:
            errors.append(f"{name} has unexpected tool(s): "
                          + ", ".join(unexpected))
        if len(value) != len(expected):
            errors.append(
                f"{name} count is {len(value)}, expected {len(expected)}")

    exact_tool_list("registered_forks", record.get("registered_forks"))
    exact_tool_list("ledger_forks", record.get("ledger_forks"))
    rows = record.get("rows")
    row_tools: object = rows
    if isinstance(rows, list):
        row_tools = [row.get("tool") if isinstance(row, dict) else None
                     for row in rows]
    exact_tool_list("rows", row_tools)
    merge_tips = record.get("merge_tips_seen")
    if not isinstance(merge_tips, dict):
        errors.append("merge_tips_seen is not an object")
    else:
        exact_tool_list("merge_tips_seen", list(merge_tips))
        malformed_tips = sorted(
            str(tool) for tool, tip in merge_tips.items()
            if (not isinstance(tool, str)
                or not isinstance(tip, str)
                or not re.fullmatch(r"[0-9a-f]{40}", tip)))
        if malformed_tips:
            errors.append("merge_tips_seen has malformed tip(s): "
                          + ", ".join(malformed_tips))
    if record.get("fleet_errors") != []:
        errors.append("fleet_errors is not an empty list")

    not_built = record.get("q2_forks_not_built_from_ours")
    if isinstance(not_built, list):
        bad_not_built = [tool for tool in not_built
                         if not isinstance(tool, str) or tool not in expected]
        if bad_not_built:
            errors.append(
                "q2_forks_not_built_from_ours contains malformed or "
                "unregistered identities")
        elif len(not_built) != len(set(not_built)):
            errors.append("q2_forks_not_built_from_ours contains duplicates")
    if isinstance(record.get("tip_rejected"), list) and record["tip_rejected"]:
        errors.append("tip_rejected is not empty")
    if record.get("measured_with_fetch") is not True:
        errors.append("measured_with_fetch is not true")
    if record.get("source_errors") != []:
        errors.append("source_errors is not an empty list")

    if errors:
        return None, [f"{path.name} is invalid: " + "; ".join(errors)]
    return record, []


def make_record(release: Optional[dict], gap: Optional[dict], round_at: str,
                tick_exit: Optional[int] = None,
                source_errors: Optional[list[str]] = None,
                semantic_pytest: Optional[dict] = None,
                tick_state: str = "ran",
                fork_github: Optional[dict] = None) -> dict:
    release = release or {}
    gap = gap or {}
    attempts = release.get("compose_attempts") or []
    failed_attempts = sum(1 for a in attempts
                          if isinstance(a, dict) and a.get("exit_code"))
    status = release.get("status") or "unmeasured"
    failure = (release.get("failure")
               if isinstance(release.get("failure"), dict) else {})
    anchor_pr = (release.get("anchor_pr")
                 if isinstance(release.get("anchor_pr"), dict) else None)
    sync_lag = gap.get("q1_sync_lag_at_merge")
    live_sync_lag = gap.get("q1_sync_lag")
    if sync_lag is None:
        sync_lag = live_sync_lag
    image_lag = gap.get("q1_image_behind_upstream")
    release_lag = gap.get("q1_release_lag")
    past_pin = gap.get("q2_ours_past_the_pin")
    stranded = gap.get("q2_our_commits_stranded")
    measured = all(isinstance(v, int) for v in (sync_lag, image_lag, past_pin))
    round_measured = all(isinstance(v, int)
                         for v in (sync_lag, release_lag, past_pin))
    terminal_ok = status in {"published", "noop", "completed", "recorded"}
    anchor_blocked = bool(terminal_ok and anchor_pr is not None
                          and anchor_pr.get("ok") is False)
    # Preserve the original image-delivery arithmetic under explicit names.
    # The generic verdict now means the whole fork programme, including its
    # GitHub issue/PR closure boundary; an absent/open GitHub answer can never
    # inherit a clean-looking delivery zero.
    delivery_converged = bool(measured and terminal_ok and sync_lag == 0
                              and image_lag == 0 and past_pin == 0)
    delivery_round_converged = bool(
        round_measured and terminal_ok and sync_lag == 0
        and release_lag == 0 and past_pin == 0)
    fork_github_status = (fork_github.get("status")
                          if isinstance(fork_github, dict) else None)
    github_converged = fork_github_status == "converged"
    inputs_authoritative = not source_errors
    tick_authorized = tick_state == "ran" and tick_exit == 0
    whole_round_clear = bool(
        tick_authorized and not anchor_blocked
        and isinstance(stranded, int) and stranded == 0)
    converged = bool(delivery_converged and github_converged
                     and inputs_authoritative and whole_round_clear)
    round_converged = bool(delivery_round_converged and github_converged
                           and inputs_authoritative and whole_round_clear)

    if source_errors:
        outcome = "could_not_measure_inputs"
    elif anchor_blocked:
        # Publishing and advancing the consumer's immutable anchor are two
        # delivery hops.  Keep the image convergence arithmetic intact, but do
        # not let a successful registry push erase a failed second hop from LL.
        outcome = "published_with_anchor_pr_blocked"
    elif terminal_ok and tick_state == "ran" and tick_exit != 0:
        outcome = "finished_with_nonzero_release_tick"
    elif terminal_ok and fork_github_status == "open":
        outcome = "finished_with_open_fork_work"
    elif terminal_ok and failed_attempts:
        outcome = "self_healed_after_retry"
    elif status == "failed" and failure.get("retryable"):
        outcome = "blocked_after_bounded_retries"
    elif status == "failed":
        outcome = "blocked_non_transient"
    elif status == "running":
        outcome = "interrupted_without_terminal_verdict"
    elif status == "not_checked":
        outcome = "release_not_checked"
    elif tick_state == "ran" and tick_exit != 0:
        outcome = "finished_with_nonzero_release_tick"
    elif converged:
        outcome = "converged"
    elif (round_converged and isinstance(live_sync_lag, int)
          and live_sync_lag > 0):
        outcome = "converged_at_boundary_with_late_upstream_drift"
    elif not measured:
        outcome = "could_not_measure_convergence"
    else:
        outcome = "finished_with_gap"

    selftest = _selftest_summary(semantic_pytest)
    category = (failure.get("category")
                or ("anchor_pr" if anchor_blocked else None))
    if tick_state == "ran" and tick_exit != 0 and category is None:
        category = "release_tick_nonzero"
    if tick_state != "ran":
        if tick_state == "not_checked_stale_round_code":
            category = "stale_round_code"
        elif tick_state == "not_checked_round_code_norecord":
            category = "round_code_norecord"
        elif selftest and selftest["status"] == "red":
            category = "pytest_red"
        elif selftest and selftest["status"] == "norecord":
            category = ("pytest_stalled" if selftest["outcome"] == "stalled"
                        else "pytest_norecord")
        else:
            category = "release_tick_not_checked"
    control = None
    if tick_state != "ran":
        control = (
            "do not invoke the release tick without current code and a "
            "complete green semantic pytest record")
    elif anchor_blocked:
        control = (
            "retain the published image; refuse to bypass the anchor "
            "repository push gate")
    elif failed_attempts:
        control = (
            "classified bounded retry; reuse pushed artefacts and BuildKit "
            "cache")
    elif attempts:
        control = "single compose attempt; no retry needed"
    elif failure:
        control = "fail closed; publish no version and retain the terminal cause"

    return {
        "program": "release_ll",
        "schema_version": 2,
        "round_at": round_at,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "release": {
            "status": status,
            "target": _target(release),
            "moved_tools": [r.get("repo") for r in release.get("moved", [])
                            if isinstance(r, dict) and r.get("repo")],
            "built_tools": list(release.get("built") or []),
            "compose_attempts": attempts,
            "failure": failure or None,
            "anchor_pr": anchor_pr,
            "tick_exit": tick_exit,
            "tick_state": tick_state,
        },
        "selftest": selftest,
        "fork_github": _fork_github_summary(fork_github),
        "convergence": {
            "measured": measured,
            "round_measured": round_measured,
            "sync_lag_at_merge": sync_lag,
            "live_sync_lag": live_sync_lag,
            "release_lag": release_lag,
            "image_behind_upstream": image_lag,
            "ours_past_pin": past_pin,
            "ours_past_pin_substantive": gap.get(
            "q2_ours_past_the_pin_substantive"),
            "our_commits_stranded": stranded,
            "delivery_converged": delivery_converged,
            "delivery_round_converged": delivery_round_converged,
            "tick_authorized": tick_authorized,
            "anchor_complete": not anchor_blocked,
            "converged": converged,
            "round_converged": round_converged,
        },
        "lesson": {
            "trigger": category,
            "control": control,
            "outcome": outcome,
            "principle": ("A fork sync is not delivery; the round converges "
                          "when every commit seen at its merge boundary reaches "
                          "the published image. Later upstream drift is retained "
                          "as live lag for the next round, never rewritten as "
                          "this round's failure or as zero."),
        },
        "source_errors": list(source_errors or []),
    }


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _stamp(when: datetime) -> str:
    return when.astimezone().strftime("%Y-%m-%dT%H%M%S%z")


def _prune(rounds: Path, now: datetime, keep_days: int) -> list[str]:
    if keep_days <= 0 or not rounds.is_dir():
        return []
    cutoff = now.timestamp() - keep_days * 86400
    removed = []
    for path in sorted(rounds.glob("*.json")):
        try:
            when = datetime.strptime(path.stem, "%Y-%m-%dT%H%M%S%z")
        except ValueError:
            continue
        if when.timestamp() < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


def retain(record: dict, state: Path, keep_days: int = KEEP_DAYS) -> dict:
    """Write the full record and append its narrow outcome; never infer facts."""
    root = Path(state) / "release-ll"
    gk_state.require_writable(root, "the retained 05:30 release learning loop")
    when = _parse_when(record.get("round_at"))
    stamp = _stamp(when)
    rounds = root / "rounds"
    full = rounds / f"{stamp}.json"
    _atomic(full, record)
    _atomic(root / "latest.json", record)
    fork_github = record.get("fork_github")
    narrow = {
        "round": stamp,
        "status": record["release"]["status"],
        "target": record["release"]["target"],
        "trigger": record["lesson"]["trigger"],
        "outcome": record["lesson"]["outcome"],
        "compose_attempts": len(record["release"]["compose_attempts"]),
        "anchor_pr_ok": (record["release"]["anchor_pr"] or {}).get("ok"),
        "tick_exit": record["release"].get("tick_exit"),
        "tick_state": record["release"].get("tick_state"),
        "selftest_status": (record.get("selftest") or {}).get("status"),
        "selftest_outcome": (record.get("selftest") or {}).get("outcome"),
        "sync_lag_at_merge": record["convergence"]["sync_lag_at_merge"],
        "live_sync_lag": record["convergence"]["live_sync_lag"],
        "release_lag": record["convergence"]["release_lag"],
        "image_behind_upstream": record["convergence"]["image_behind_upstream"],
        "ours_past_pin": record["convergence"]["ours_past_pin"],
        "our_commits_stranded": record["convergence"][
            "our_commits_stranded"],
        "delivery_converged": record["convergence"]["delivery_converged"],
        "delivery_round_converged": record["convergence"][
            "delivery_round_converged"],
        "tick_authorized": record["convergence"]["tick_authorized"],
        "anchor_complete": record["convergence"]["anchor_complete"],
        "converged": record["convergence"]["converged"],
        "round_converged": record["convergence"]["round_converged"],
        # Keep exact item references in the append-only record. This may be
        # larger than a count-only index, but it prevents a later zero from
        # erasing which issue/PR was open in this round.
        "fork_github": (None if not isinstance(fork_github, dict) else {
            "program": fork_github.get("program"),
            "schema_version": fork_github.get("schema_version"),
            "status": fork_github.get("status"),
            "started_at": fork_github.get("started_at"),
            "completed_at": fork_github.get("completed_at"),
            "actor": fork_github.get("actor"),
            "expected_actor": fork_github.get("expected_actor"),
            "round": fork_github.get("round"),
            "counts": fork_github.get("counts"),
            "items": fork_github.get("items"),
            "source_errors": fork_github.get("source_errors"),
        }),
        "source_errors": record["source_errors"],
    }
    index = root / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with index.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(narrow, sort_keys=True) + "\n")
    return {"stamp": stamp, "full": str(full), "index": str(index),
            "pruned": _prune(rounds, datetime.now(timezone.utc), keep_days)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", type=Path, default=gk_state.state_dir())
    ap.add_argument("--release-json", type=Path, required=True)
    ap.add_argument("--gap-json", type=Path, required=True)
    ap.add_argument("--fork-github-json", type=Path, required=True)
    ap.add_argument("--forks-json", type=Path,
                    default=Path(__file__).resolve().parent / "FORKS.json",
                    help="canonical registered fleet used to independently "
                         "validate gap row coverage")
    ap.add_argument("--semantic-pytest-json", type=Path)
    ap.add_argument("--round-at", required=True)
    ap.add_argument("--tick-exit", type=int, default=None)
    ap.add_argument("--tick-state", default="ran", choices=(
        "ran", "not_checked_pytest_red", "not_checked_pytest_norecord",
        "not_checked_stale_round_code", "not_checked_round_code_norecord",
    ))
    a = ap.parse_args(argv)
    observed_now = datetime.now(timezone.utc)

    semantic, semantic_err = ((None, None) if a.semantic_pytest_json is None
                              else _read(a.semantic_pytest_json))
    if semantic_err is None and semantic is not None:
        if (semantic.get("mechanism") != "validated_semantic_progress_lease"
                or semantic.get("exit_code") not in (0, 1, 2)):
            semantic_err = (
                f"{a.semantic_pytest_json.name} has no validated semantic "
                "pytest verdict")
            semantic = None

    # A skipped tick has no release record.  Do not even read the live path:
    # it necessarily still contains an earlier round and must not be allowed
    # to impersonate this one.  `not_checked` is evidence, not an error in the
    # LL capture itself.
    if a.tick_state == "ran":
        release, release_err = _read(a.release_json)
    else:
        release = {"program": "daily_release", "status": "not_checked",
                   "not_checked_reason": a.tick_state}
        release_err = None
    gap_raw, gap_read_err = _read(a.gap_json)
    if gap_read_err is not None:
        gap, gap_errors = None, [gap_read_err]
    else:
        assert gap_raw is not None
        gap, gap_errors = _validate_gap(
            gap_raw, a.gap_json, a.round_at, a.forks_json)
    fork_github_raw, fork_github_read_err = _read(a.fork_github_json)
    if fork_github_read_err is not None:
        fork_github, fork_github_errors = None, [fork_github_read_err]
    else:
        assert fork_github_raw is not None
        fork_github, fork_github_errors = _validate_fork_github(
            fork_github_raw, a.fork_github_json, a.round_at)
    provenance_errors: list[str] = []
    if gap is not None and fork_github is not None:
        gap_run_id = gap["source"]["_round"]["run_id"]
        github_run_id = fork_github["round"]["run_id"]
        if gap_run_id != github_run_id:
            provenance_errors.append(
                "fork_gap.json and fork-github-convergence.json name different "
                "daily_0530 run_id values")
    if release_err is None and (release.get("program") != "daily_release"
                                or not release.get("status")):
        release_err = ("daily-release.json has no current-run status; it is "
                       "a pre-schema or unrelated record, not this round's verdict")
        release = None
    if release_err is None and a.tick_state == "ran":
        started = _strict_when(release.get("started_at"))
        finished_value = release.get("finished_at")
        finished = (_strict_when(finished_value)
                    if finished_value is not None else None)
        boundary = _strict_when(a.round_at)
        if boundary is None or started is None or started < boundary:
            release_err = (
                "daily-release.json predates this 05:30 round; the release "
                "tick produced no current-run record")
            release = None
        elif boundary > observed_now or started > observed_now:
            release_err = (
                "daily-release.json is dated in the future; no current-run "
                "release verdict was recorded")
            release = None
        elif finished_value is not None and finished is None:
            release_err = (
                "daily-release.json finished_at is malformed; no current-run "
                "release verdict was recorded")
            release = None
        elif finished is not None and finished < started:
            release_err = (
                "daily-release.json finished_at precedes started_at; no "
                "current-run release verdict was recorded")
            release = None
        elif finished is not None and finished > observed_now:
            release_err = (
                "daily-release.json finished_at is in the future; no "
                "current-run release verdict was recorded")
            release = None
    if semantic_err is None and semantic is not None:
        semantic_started = _strict_when(semantic.get("started_at"))
        boundary = _strict_when(a.round_at)
        if (boundary is None or semantic_started is None
                or semantic_started < boundary):
            semantic_err = (
                f"{a.semantic_pytest_json.name} predates this 05:30 round; "
                "no current self-test verdict was recorded")
            semantic = None
        elif boundary > observed_now or semantic_started > observed_now:
            semantic_err = (
                f"{a.semantic_pytest_json.name} is dated in the future; "
                "no current self-test verdict was recorded")
            semantic = None
    if (semantic_err is None and semantic is not None
            and a.tick_state == "ran" and semantic.get("exit_code") != 0):
        semantic_err = (
            "release tick ran without a complete green semantic pytest "
            "record; cron gate wiring is inconsistent")
    errors = [e for e in (release_err, semantic_err) if e]
    errors.extend(gap_errors)
    errors.extend(fork_github_errors)
    errors.extend(provenance_errors)
    record = make_record(
        release, gap, a.round_at, a.tick_exit, errors,
        semantic_pytest=semantic, tick_state=a.tick_state,
        fork_github=fork_github,
    )
    written = retain(record, a.state_dir)
    print("release LL: "
          f"{record['lesson']['outcome']} · "
          f"image lag {record['convergence']['image_behind_upstream']} · "
          f"ours past pin {record['convergence']['ours_past_pin']} · "
          f"fork GitHub "
          f"{(record.get('fork_github') or {}).get('status', 'unmeasured')} · "
          f"retained {written['stamp']}")
    if errors:
        for err in errors:
            print(f"  source error: {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
