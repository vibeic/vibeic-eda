#!/usr/bin/env python3
"""Authorize the release half only after this round's AI cases are delivered.

``daily_0530.py`` may exit 1 for useful, non-release reasons, so its process
status is not the authorization.  The release boundary needs the narrower
postcondition: every conflict handed to the gatekeeper in *this* applied round
has a verified published-mainline result.  Missing or malformed evidence is a
third state, never permission to publish.

Exit: 0 authorized, 1 complete evidence says blocked, 2 no valid record.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Optional


RC_AUTHORIZED = 0
RC_BLOCKED = 1
RC_NORECORD = 2
PROGRAM = "check_ai_release_authorization"
SCHEMA_VERSION = 1


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number {value!r}")


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


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read(path: Path) -> tuple[Optional[dict], Optional[str]]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except FileNotFoundError:
        return None, f"missing {path.name}"
    except (OSError, ValueError) as exc:
        return None, f"unreadable {path.name}: {type(exc).__name__}: {exc}"
    if not isinstance(value, dict):
        return None, f"{path.name} is not a JSON object"
    return value, None


def _case_key(value: object) -> Optional[tuple[str, str]]:
    if not isinstance(value, dict):
        return None
    fork, branch = value.get("fork"), value.get("branch")
    if not isinstance(fork, str) or not fork or fork.strip() != fork:
        return None
    if not isinstance(branch, str) or not branch or branch.strip() != branch:
        return None
    return fork, branch


def _registered_fleet(path: Path) -> tuple[list[str], Optional[str], list[str]]:
    errors: list[str] = []
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant)
    except (OSError, UnicodeError, ValueError) as exc:
        return [], None, [
            f"could not read registered fleet {path}: "
            f"{type(exc).__name__}: {exc}"]
    entries = value.get("forks") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        return [], hashlib.sha256(raw).hexdigest(), [
            f"registered fleet {path} has no forks list"]
    tools: list[str] = []
    for pos, entry in enumerate(entries):
        tool = entry.get("tool") if isinstance(entry, dict) else None
        if (not isinstance(tool, str) or not tool or tool.strip() != tool
                or "/" in tool or any(char.isspace() for char in tool)):
            errors.append(f"registered fleet forks[{pos}].tool is malformed")
            continue
        tools.append(tool)
    folded: dict[str, list[str]] = {}
    for tool in tools:
        folded.setdefault(tool.casefold(), []).append(tool)
    duplicates = sorted(
        names[0] for names in folded.values() if len(names) > 1)
    if duplicates:
        errors.append("registered fleet has duplicate tool(s): "
                      + ", ".join(duplicates))
    if not tools:
        errors.append("registered fleet is empty")
    return sorted(tools), hashlib.sha256(raw).hexdigest(), errors


def _conflicted_cases(round_record: dict, registered_forks: list[str]
                      ) -> tuple[list[tuple[str, str]], list[str]]:
    keys: list[tuple[str, str]] = []
    errors: list[str] = []
    expected_top = set(registered_forks) | {"_ai_decisions", "_round"}
    allowed_top = expected_top | {"_watch_only_resets"}
    observed_top = set(round_record)
    missing = sorted(expected_top - observed_top)
    extra = sorted(observed_top - allowed_top)
    if missing:
        errors.append("daily_0530 evidence is missing top-level row(s): "
                      + ", ".join(missing))
    if extra:
        errors.append("daily_0530 evidence has unexpected top-level row(s): "
                      + ", ".join(extra))
    for fork in registered_forks:
        row = round_record.get(fork)
        if not isinstance(row, dict):
            errors.append(f"fork row {fork!r} is not an object")
            continue
        # The absence of a recorded conflict is authoritative only after both
        # conflict-discovery passes completed.  `daily_0530` deliberately writes
        # an empty `conflicted` list when origin branch enumeration is unknown;
        # treating that shape as zero cases would authorize a release from a
        # denominator the round never measured.
        if "error" in row:
            errors.append(f"{fork}.error means conflict discovery is incomplete")
        if "ours_skipped" in row:
            errors.append(
                f"{fork}.ours_skipped means branch conflict discovery is incomplete")
        if "merge_unmeasurable" in row:
            errors.append(
                f"{fork}.merge_unmeasurable means branch conflict discovery "
                "is incomplete")
        upstream_outcome = row.get("upstream_merge_outcome")
        if upstream_outcome == "conflict_resolved":
            # A CONFLICT THAT WAS RESOLVED, LANDED, VERIFIED, AND WHOSE DEFERRED
            # OURS-SIDE DISCOVERY THEN RAN is not an incomplete measurement — it
            # is a complete one that took a detour. Refusing it cost three days
            # of a frozen public page on 2026-08-20 (OpenROAD `src/sta`): every
            # upstream commit was in and the published tip verified, but the
            # round was judged on a label written before the AI ran.
            #
            # This is NOT a relaxation. The outcome alone authorizes nothing:
            # `daily_0530.resume_ours_after_ai_landing` writes it ONLY after
            # `step2_ours` actually ran to completion, and the positive evidence
            # is demanded here rather than assumed. A row that claims the outcome
            # without the record is a forgery and is refused as loudly as an
            # unresolved conflict. `ours_skipped` is checked above and still
            # applies: if the ours side could not be measured, this never passes.
            landing = row.get("upstream_landing")
            published = landing.get("published_tip") if isinstance(landing, dict) else None
            if not (isinstance(landing, dict)
                    and landing.get("verified") is True
                    and landing.get("ours_resumed") is True
                    and isinstance(published, str) and published.strip()):
                errors.append(
                    f"{fork}.upstream_merge_outcome='conflict_resolved' without a "
                    "verified upstream_landing record (verified, ours_resumed, "
                    "published_tip) does not prove complete conflict discovery")
        elif upstream_outcome not in {"natural_success", "not_applicable"}:
            errors.append(
                f"{fork}.upstream_merge_outcome={upstream_outcome!r} does not "
                "prove complete conflict discovery")
        mainline = row.get("main")
        if (not isinstance(mainline, str) or not mainline
                or mainline.strip() != mainline):
            errors.append(f"{fork}.main is missing or malformed")
        conflicts = row.get("conflicted")
        if not isinstance(conflicts, list):
            errors.append(f"{fork}.conflicted is missing or not a list")
            continue
        for pos, case in enumerate(conflicts):
            if not isinstance(case, dict):
                errors.append(f"{fork}.conflicted[{pos}] is not an object")
                continue
            branch = case.get("branch")
            if (not isinstance(branch, str) or not branch
                    or branch.strip() != branch):
                errors.append(f"{fork}.conflicted[{pos}].branch is malformed")
                continue
            keys.append((fork, branch))
    duplicates = sorted(key for key in set(keys) if keys.count(key) > 1)
    if duplicates:
        errors.append("duplicate conflict case(s): " + ", ".join(
            f"{fork}:{branch}" for fork, branch in duplicates))
    return keys, errors


def assess(round_record: dict, round_at: str, *, registered_forks: list[str],
           source: str = "", registry_source: str = "",
           registry_sha256: Optional[str] = None,
           watch_reset: Optional[dict] = None,
           watch_reset_errors: Optional[list[str]] = None) -> tuple[int, dict]:
    """Validate and normalize one release-authorization record."""
    registered_forks = sorted(registered_forks)
    checked_at = datetime.now(timezone.utc)
    errors: list[str] = []
    errors.extend(watch_reset_errors or [])
    boundary = _strict_when(round_at)
    if boundary is None:
        errors.append("--round-at is missing a timezone-aware ISO-8601 timestamp")

    meta = round_record.get("_round")
    if not isinstance(meta, dict):
        errors.append("daily_0530 evidence has no _round object")
        meta = {}
    if type(meta.get("schema_version")) is not int or meta.get("schema_version") != 1:
        errors.append("_round.schema_version is not 1")
    run_id = meta.get("run_id")
    if (not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", run_id)):
        errors.append("_round.run_id is missing or malformed")
    started = _strict_when(meta.get("started_at"))
    finished = _strict_when(meta.get("finished_at"))
    if started is None or finished is None:
        errors.append("_round started_at/finished_at is missing or malformed")
    else:
        if boundary is not None and started < boundary:
            errors.append("daily_0530 evidence predates this 05:30 round")
        if finished < started:
            errors.append("_round.finished_at precedes started_at")
        if started > checked_at or finished > checked_at:
            errors.append("daily_0530 evidence is dated in the future")
    if meta.get("applied") is not True:
        errors.append("_round.applied is not true")

    expected_keys, conflict_errors = _conflicted_cases(
        round_record, registered_forks)
    errors.extend(conflict_errors)
    ai = round_record.get("_ai_decisions")
    if not isinstance(ai, dict):
        errors.append("daily_0530 evidence has no _ai_decisions object")
        ai = {}

    cases = ai.get("cases")
    if type(cases) is not int or cases < 0:
        errors.append("_ai_decisions.cases is not a non-negative integer")
        cases = None
    elif cases != len(expected_keys):
        errors.append(
            f"_ai_decisions.cases={cases} but the round contains "
            f"{len(expected_keys)} conflicted case(s)")

    verification = ai.get("verification", [])
    if not isinstance(verification, list):
        errors.append("_ai_decisions.verification is not a list")
        verification = []
    normalized: list[dict] = []
    proof_keys: list[tuple[str, str]] = []
    for pos, proof in enumerate(verification):
        key = _case_key(proof)
        if key is None:
            errors.append(f"_ai_decisions.verification[{pos}] has malformed identity")
            continue
        if type(proof.get("verified")) is not bool:
            errors.append(
                f"_ai_decisions.verification[{pos}].verified is not boolean")
            continue
        mainline = proof.get("mainline")
        source_tip = proof.get("source_tip")
        published_tip = proof.get("published_tip")
        reason = proof.get("reason")
        if (not isinstance(mainline, str) or not mainline
                or mainline.strip() != mainline):
            errors.append(
                f"_ai_decisions.verification[{pos}].mainline is malformed")
        if (not isinstance(source_tip, str)
                or not re.fullmatch(r"[0-9a-f]{40}", source_tip)):
            errors.append(
                f"_ai_decisions.verification[{pos}].source_tip is malformed")
        if (proof["verified"] and
                (not isinstance(published_tip, str)
                 or not re.fullmatch(r"[0-9a-f]{40}", published_tip))):
            errors.append(
                f"_ai_decisions.verification[{pos}].published_tip is malformed")
        if not isinstance(reason, str) or not reason:
            errors.append(
                f"_ai_decisions.verification[{pos}].reason is malformed")
        proof_keys.append(key)
        normalized.append({
            "fork": key[0],
            "branch": key[1],
            "mainline": mainline,
            "source_tip": source_tip,
            "published_tip": published_tip,
            "verified": proof["verified"],
            "reason": reason,
        })
    duplicate_proofs = sorted(
        key for key in set(proof_keys) if proof_keys.count(key) > 1)
    if duplicate_proofs:
        errors.append("duplicate verification result(s): " + ", ".join(
            f"{fork}:{branch}" for fork, branch in duplicate_proofs))
    if sorted(proof_keys) != sorted(expected_keys):
        errors.append("verification identities do not exactly match conflicted cases")

    unverified_cases = ai.get("unverified_cases", [])
    if not isinstance(unverified_cases, list):
        errors.append("_ai_decisions.unverified_cases is not a list")
        unverified_cases = []
    unresolved_keys: list[tuple[str, str]] = []
    for pos, case in enumerate(unverified_cases):
        key = _case_key(case)
        if key is None:
            errors.append(
                f"_ai_decisions.unverified_cases[{pos}] has malformed identity")
        else:
            unresolved_keys.append(key)
    false_keys = [(row["fork"], row["branch"])
                  for row in normalized if not row["verified"]]
    if sorted(unresolved_keys) != sorted(false_keys):
        errors.append("unverified_cases does not exactly match failed verification")

    verified_count = ai.get("verified_cases", 0 if cases == 0 else None)
    actual_verified = sum(1 for row in normalized if row["verified"])
    if type(verified_count) is not int or verified_count < 0:
        errors.append("_ai_decisions.verified_cases is not a non-negative integer")
    elif verified_count != actual_verified:
        errors.append(
            f"_ai_decisions.verified_cases={verified_count} but "
            f"{actual_verified} verification result(s) are true")

    declared_verified = ai.get("verified")
    if type(declared_verified) is not bool:
        errors.append("_ai_decisions.verified is not boolean")
    transport_rc = ai.get("rc")
    transport_rc_ok = type(transport_rc) is int and transport_rc == 0
    if cases and (ai.get("invoked") is not True
                  or ai.get("outcome") != "natural"
                  or not transport_rc_ok):
        # This is valid blocked evidence, not a malformed record. It is retained
        # below as the reason authorization was withheld.
        pass

    all_delivered = bool(
        cases is not None
        and len(normalized) == cases
        and actual_verified == cases
        and not unresolved_keys
        and declared_verified is True
        and (cases == 0 or (ai.get("invoked") is True
                            and ai.get("outcome") == "natural"
                            and transport_rc_ok))
    )
    watch_status = (watch_reset.get("status")
                    if isinstance(watch_reset, dict) else None)
    if watch_status == "norecord" and not errors:
        errors.append("current-round watch-only reset produced NORECORD")
    if errors:
        status, rc = "norecord", RC_NORECORD
        reasons = list(errors)
    elif watch_status == "blocked":
        status, rc = "blocked", RC_BLOCKED
        reasons = ["current-round watch-only reset is blocked"]
    elif all_delivered:
        status, rc = "authorized", RC_AUTHORIZED
        reasons = []
    else:
        status, rc = "blocked", RC_BLOCKED
        reasons = []
        if declared_verified is not True:
            reasons.append("_ai_decisions.verified is not true")
        if unresolved_keys:
            reasons.append(
                f"{len(unresolved_keys)} AI conflict case(s) lack a verified "
                "published-mainline postcondition")
        if cases and ai.get("invoked") is not True:
            reasons.append("gatekeeper AI was not invoked")
        if cases and ai.get("outcome") != "natural":
            reasons.append(
                f"gatekeeper AI outcome is {ai.get('outcome')!r}, not natural")
        if cases and not transport_rc_ok:
            reasons.append(
                f"gatekeeper AI exit is {transport_rc!r}, not integer 0")
        if not reasons:
            reasons.append("AI conflict delivery was not fully verified")

    record = {
        "program": PROGRAM,
        "schema_version": SCHEMA_VERSION,
        "checked_at": checked_at.isoformat(),
        "round_at": round_at,
        "source": source,
        "registered_forks": list(registered_forks),
        "registered_count": len(registered_forks),
        "registry": {
            "source": registry_source,
            "sha256": registry_sha256,
        },
        "round": {
            "schema_version": meta.get("schema_version"),
            "run_id": run_id,
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "applied": meta.get("applied"),
        },
        "status": status,
        "case_count": cases,
        "verified_count": actual_verified,
        "unverified_count": len(false_keys),
        "transport": {
            "invoked": ai.get("invoked"),
            "outcome": ai.get("outcome"),
            "rc": ai.get("rc"),
        },
        "case_results": normalized,
        "watch_only_reset": watch_reset,
        "reasons": reasons,
        "source_errors": list(errors),
    }
    return rc, record


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--round-json", type=Path, required=True)
    ap.add_argument("--forks-json", type=Path, required=True)
    ap.add_argument("--round-at", required=True)
    ap.add_argument("--json", type=Path, required=True)
    args = ap.parse_args(argv)

    registered_forks, registry_sha256, registry_errors = _registered_fleet(
        args.forks_json)
    value, read_error = _read(args.round_json)
    input_errors = list(registry_errors)
    if read_error is not None:
        input_errors.append(read_error)
    if input_errors:
        checked_at = datetime.now(timezone.utc).isoformat()
        record = {
            "program": PROGRAM, "schema_version": SCHEMA_VERSION,
            "checked_at": checked_at, "round_at": args.round_at,
            "source": str(args.round_json), "round": None,
            "registered_forks": registered_forks,
            "registered_count": len(registered_forks),
            "registry": {
                "source": str(args.forks_json), "sha256": registry_sha256},
            "status": "norecord", "case_count": None,
            "verified_count": 0, "unverified_count": None,
            "transport": None, "case_results": [],
            "reasons": input_errors, "source_errors": input_errors,
        }
        rc = RC_NORECORD
    else:
        assert value is not None
        registry_value, registry_read_error = _read(args.forks_json)
        has_reset_policy = bool(
            registry_read_error is None and registry_value
            and any(isinstance(row, dict) and "watch_only_reset" in row
                    for row in registry_value.get("forks", [])))
        if has_reset_policy:
            # Lazy on purpose: isolated consumers with no reset policy do not
            # need to vendor the reset implementation merely to validate AI.
            import watch_only_reset  # noqa: PLC0415
            try:
                strict_watch_source = watch_only_reset.read_json(args.round_json)
            except (OSError, ValueError) as exc:
                watch_summary, watch_errors = None, [
                    "strict watch-reset source read failed: "
                    f"{type(exc).__name__}: {exc}"]
            else:
                watch_summary, watch_errors = (
                    watch_only_reset.validate_round_evidence(
                        strict_watch_source, round_at=args.round_at,
                        forks_json=args.forks_json))
        else:
            watch_summary, watch_errors = None, []
        rc, record = assess(
            value, args.round_at, registered_forks=registered_forks,
            source=str(args.round_json),
            registry_source=str(args.forks_json),
            registry_sha256=registry_sha256,
            watch_reset=watch_summary,
            watch_reset_errors=watch_errors)
    try:
        _atomic_json(args.json, record)
    except OSError as exc:
        print(f"[NOT CHECKED] AI release authorization could not retain its "
              f"record: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_NORECORD
    if rc == RC_AUTHORIZED:
        print(f"[PASS] AI release authorization: {record['case_count']} "
              "conflict case(s), every published postcondition verified")
    elif rc == RC_BLOCKED:
        print("[BLOCKED] AI release authorization: "
              + "; ".join(record["reasons"]), file=sys.stderr)
    else:
        print("[NOT CHECKED] AI release authorization: "
              + "; ".join(record["source_errors"]),
              file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
