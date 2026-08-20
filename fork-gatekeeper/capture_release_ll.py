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
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
from typing import Optional

import gk_state
import watch_only_reset

KEEP_DAYS = int(os.environ.get("GK_RELEASE_LL_KEEP_DAYS", "400"))
GITHUB_ORG = "vibeic"
VIBEIC_EDA_REPOSITORY = "vibeic/vibeic-eda"
VIBEIC_DELIVERY_REPOSITORY = "vibeic/vibe-ic"
DELIVERY_HEAD_PATTERNS = (
    re.compile(r"^eda-anchor-(\d+\.\d+\.\d+)$"),
    re.compile(r"^eda-fork-sync-(\d{4}-\d{2}-\d{2})$"),
    re.compile(r"^eda-fork-assess-(\d{4}-\d{2}-\d{2})$"),
)
DELIVERY_HEAD_PATTERN_TEXT = {
    r"^eda-anchor-(\d+\.\d+\.\d+)$",
    r"^eda-fork-sync-(\d{4}-\d{2}-\d{2})$",
    r"^eda-fork-assess-(\d{4}-\d{2}-\d{2})$",
}
AI_AUTHORIZATION_PROGRAM = "check_ai_release_authorization"
AI_AUTHORIZATION_SCHEMA_VERSION = 1
RELEASE_PINS_PROGRAM = "check_release_pins_committed"
RELEASE_PINS_SCHEMA_VERSION = 1
FULL_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
MANIFEST_ID = re.compile(r"[0-9a-f]{64}")
RELEASE_IMAGE = "ghcr.io/vibeic/vibeic-eda"
EDA_ROOT = Path(__file__).resolve().parent.parent

# Every Git read used by the retained certificate goes through one fixed,
# hermetic entry point.  In particular, do not inherit a caller's GIT_DIR,
# worktree/index, object alternates, replace-ref namespace, or config injection:
# any of those can make a read-only-looking ``rev-parse``/``show`` answer a
# different repository question.  Global/system config is disabled and hooks
# are pinned off as defense in depth for future commands added to this helper.
_GIT = "/usr/bin/git"
_GIT_PREFIX = (
    _GIT,
    "--no-replace-objects",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
)


def _git_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    })
    return env


def _git_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Run fixed Git with no ambient repository/config authority."""
    if "env" in kwargs:
        raise TypeError("_git_run owns its sanitized environment")
    return subprocess.run(
        [*_GIT_PREFIX, *args], env=_git_environment(), **kwargs)


def _repository_clean(root: Path) -> tuple[bool, str | None]:
    """Prove that a checkout has no staged, unstaged, or untracked state.

    A commit identity is not the identity of the Python and JSON bytes this
    process executed/read when its worktree is dirty.  Porcelain v1 with ``-z``
    is a stable, filename-safe census; fixed Git and the sanitized environment
    keep ambient repository/config authority out of the question.
    """
    try:
        process = _git_run(
            ["-C", str(root), "status", "--porcelain=v1", "-z",
             "--untracked-files=all", "--ignore-submodules=none"],
            capture_output=True, text=True,
        )
    except OSError as exc:
        return False, f"could not prove repository cleanliness: {exc}"
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()[:160]
        return False, ("could not prove repository cleanliness"
                       + (f": {detail}" if detail else ""))
    if process.stdout:
        entries = len([row for row in process.stdout.split("\0") if row])
        return False, (
            "repository is dirty at the page publication boundary "
            f"({entries} staged, unstaged, or untracked path(s))")
    return True, None


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number {value!r}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number {value!r}")
    return parsed


def _read(path: Path) -> tuple[Optional[dict], Optional[str]]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object,
            parse_float=_finite_float,
            parse_constant=_reject_json_constant,
        )
    except FileNotFoundError:
        return None, f"missing {path.name}"
    except (OSError, ValueError) as exc:
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


def _round_provenance_errors(left: object, right: object, *,
                             left_name: str, right_name: str) -> list[str]:
    """Require two consumers to name the same immutable daily round.

    A nonce is necessary but not sufficient provenance.  A copied nonce beside
    different source timestamps can otherwise splice two executions into one
    apparently current certificate.  Compare the fields all daily-round
    consumers retain; ``applied`` is deliberately excluded because the GitHub
    collector's normalized envelope does not carry it (its source validators
    independently require it on the gap/AI records that do).
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return [f"{left_name} and {right_name} do not both name a round object"]
    errors: list[str] = []
    for field in ("schema_version", "run_id"):
        if left.get(field) != right.get(field):
            label = ("run_id" if field == "run_id"
                     else f"round {field}")
            errors.append(
                f"{left_name} and {right_name} name different daily_0530 "
                f"{label} values")
    for field in ("started_at", "finished_at"):
        left_when = _strict_when(left.get(field))
        right_when = _strict_when(right.get(field))
        if (left_when is None or right_when is None
                or left_when != right_when):
            errors.append(
                f"{left_name} and {right_name} name different daily_0530 "
                f"round {field} values")
    return errors


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
    process_tree = record.get("process_tree")
    return {
        "schema": record.get("schema"),
        "status": status,
        "started_at": record.get("started_at"),
        "exit_code": rc,
        "process_rc": record.get("process_rc"),
        "outcome": record.get("outcome"),
        "reason": record.get("reason"),
        "mechanism": record.get("mechanism"),
        "elapsed_seconds": record.get("elapsed_seconds"),
        "stall_grace_seconds": record.get("stall_grace_seconds"),
        "total_runtime_ceiling_seconds": record.get(
            "total_runtime_ceiling_seconds"),
        "junit": record.get("junit"),
        "junit_cases": record.get("junit_cases"),
        "junit_red": record.get("junit_red"),
        "progress": progress if isinstance(progress, dict) else None,
        "process_tree": (process_tree
                         if isinstance(process_tree, dict) else None),
    }


def _validate_selftest(record: object, *, round_at: str,
                       observed_until: datetime, summary: bool = False
                       ) -> list[str]:
    """Re-derive a semantic pytest verdict from its retained finite evidence."""
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["semantic pytest evidence is not an object"]
    if type(record.get("schema")) is not int or record.get("schema") != 1:
        errors.append("semantic pytest schema is not 1")
    if record.get("mechanism") != "validated_semantic_progress_lease":
        errors.append("semantic pytest mechanism is invalid")
    started = _strict_when(record.get("started_at"))
    boundary = _strict_when(round_at)
    if (started is None or boundary is None or started < boundary
            or started > observed_until):
        errors.append("semantic pytest started_at is stale, malformed, or future")
    elapsed = record.get("elapsed_seconds")
    if (not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool)
            or not math.isfinite(float(elapsed)) or elapsed < 0):
        errors.append("semantic pytest elapsed_seconds is not finite/non-negative")
    elif started is not None \
            and started + timedelta(seconds=float(elapsed)) > observed_until:
        errors.append("semantic pytest elapsed interval ends after its observer")
    grace = record.get("stall_grace_seconds")
    if (not isinstance(grace, (int, float)) or isinstance(grace, bool)
            or not math.isfinite(float(grace)) or grace <= 0):
        errors.append("semantic pytest stall_grace_seconds is not finite/positive")
    if record.get("total_runtime_ceiling_seconds") is not None:
        errors.append("semantic pytest carries a forbidden total runtime ceiling")

    rc = record.get("exit_code")
    process_rc = record.get("process_rc")
    red = record.get("junit_red")
    cases = record.get("junit_cases")
    progress = record.get("progress")
    tree = record.get("process_tree")
    if type(rc) is not int or rc not in {0, 1, 2}:
        errors.append("semantic pytest exit_code is not an exact supported integer")
    green = type(rc) is int and rc == 0
    if summary:
        expected_status = ("passed" if rc == 0 else
                           "red" if rc == 1 else
                           "norecord" if rc == 2 else "cannot_ask")
        if record.get("status") != expected_status:
            errors.append("semantic pytest status contradicts exit_code")
    if green:
        if (record.get("outcome") != "natural"
                or type(process_rc) is not int or process_rc != 0):
            errors.append("semantic pytest did not end naturally with process rc 0")
        if (type(cases) is not int or cases <= 0
                or type(red) is not int or red != 0):
            errors.append("semantic pytest has no complete all-green JUnit census")
        if not isinstance(record.get("junit"), str) or not record["junit"]:
            errors.append("semantic pytest does not name its JUnit evidence")
        if (not isinstance(progress, dict)
                or progress.get("stage") != "finished"
                or progress.get("last_event") != "session_finish"
                or type(progress.get("selected")) is not int
                or progress["selected"] <= 0
                or type(progress.get("finished")) is not int
                or progress.get("finished") != progress["selected"]
                or progress["selected"] != cases
                or type(progress.get("exitstatus")) is not int
                or progress.get("exitstatus") != 0
                or progress.get("protocol_error") is not None
                or type(progress.get("events")) is not int
                or progress["events"] <= 0):
            errors.append("semantic pytest progress protocol is not a complete green run")
        if (not isinstance(tree, dict)
                or tree.get("subreaper") is not True
                or tree.get("census_complete") is not True
                or tree.get("live_descendants_observed") != []
                or tree.get("cleanup_survivors") != []
                or tree.get("final_empty") is not True):
            errors.append("semantic pytest process-tree terminal census is not empty")
    return errors


def _string_identities(value: object, name: str, errors: list[str]) -> list[str]:
    if (not isinstance(value, list)
            or any(not isinstance(item, str) or not item
                   or item.strip() != item for item in value)):
        errors.append(f"{name} is not a list of non-empty identities")
        return []
    if len({item.casefold() for item in value}) != len(value):
        errors.append(f"{name} contains duplicate identities")
    return list(value)


def _validate_remote_publication(value: object, target: str,
                                 errors: list[str]) -> None:
    if not isinstance(value, dict) or value.get("verified") is not True:
        errors.append("remote release publication is not verified")
        return
    manifests = value.get("manifests")
    if not isinstance(manifests, dict) or set(manifests) != {"version", "latest"}:
        errors.append("remote publication has no exact version/latest manifests")
        return
    identities = []
    expected_refs = {
        "version": f"{RELEASE_IMAGE}:{target}",
        "latest": f"{RELEASE_IMAGE}:latest",
    }
    for name in ("version", "latest"):
        row = manifests.get(name)
        if (not isinstance(row, dict)
                or row.get("ref") != expected_refs[name]
                or row.get("state") != "present"
                or row.get("source") != "registry"
                or not isinstance(row.get("detail"), str)
                or not isinstance(row.get("identity"), str)
                or MANIFEST_ID.fullmatch(row["identity"]) is None):
            errors.append(f"remote publication {name} manifest is invalid")
            continue
        identities.append(row["identity"])
    if len(identities) == 2 and identities[0] != identities[1]:
        errors.append("immutable version and latest name different manifests")


def _validate_record_publication(value: object, errors: list[str],
                                 *, published: bool) -> None:
    if not isinstance(value, dict) or value.get("verified") is not True:
        errors.append("release-record publication is not verified")
        return
    if value.get("subject") != "origin/main:RELEASED.json":
        errors.append("release-record publication names the wrong remote subject")
    identity = value.get("identity")
    if (not isinstance(identity, str)
            or FULL_OBJECT_ID.fullmatch(identity.lower()) is None):
        errors.append("release-record publication has no exact commit identity")
    initial = value.get("initial_state")
    attempted = value.get("repair_attempted")
    if initial not in {"present", "absent"} or type(attempted) is not bool:
        errors.append("release-record initial/repair evidence is malformed")
    elif initial == "present" and attempted:
        errors.append("present release record claims an unnecessary repair")
    elif initial == "absent" and not (
            attempted and value.get("final_state") == "present"
            and isinstance(value.get("repair"), dict)
            and value["repair"].get("ok") is True):
        errors.append("absent release record has no verified successful repair")
    if published and value.get("committed") is not True:
        errors.append("published release record was not committed")


def _validate_release_terminal(record: object, *, round_at: str,
                               observed_until: datetime,
                               summary: bool = False) -> list[str]:
    """Validate the complete successful release, not its status label."""
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["release terminal evidence is not an object"]
    if record.get("program") != "daily_release":
        errors.append("release program is not daily_release")
    if (type(record.get("schema_version")) is not int
            or record.get("schema_version") != 2):
        errors.append("release schema_version is not 2")
    status = record.get("status")
    if status not in {
            "published", "noop", "failed", "running", "dry_run",
            "recorded", "smoke_passed"}:
        errors.append("release status is unsupported")
    started = _strict_when(record.get("started_at"))
    finished = _strict_when(record.get("finished_at"))
    boundary = _strict_when(round_at)
    if (started is None or boundary is None or started < boundary
            or started > observed_until):
        errors.append("release started_at is stale, malformed, or future")
    if (finished is None or started is None or finished < started
            or finished > observed_until):
        errors.append("release finished_at is malformed, reversed, or future")

    # Failed/unmeasured terminals remain useful LL evidence.  Only a source that
    # claims success must prove the full publication chain below.
    if status not in {"published", "noop"}:
        return errors
    if type(record.get("exit_code")) is not int \
            or record.get("exit_code") != 0:
        errors.append("successful release does not carry exit_code 0")
    if record.get("failure") is not None:
        errors.append("successful release carries a failure")
    if summary:
        if (type(record.get("tick_exit")) is not int
                or record.get("tick_exit") != 0
                or record.get("tick_state") != "ran"):
            errors.append("release LL does not prove a successful current tick")
        if record.get("noop") is not (status == "noop"):
            errors.append("release noop flag contradicts its terminal status")

    version = record.get("version")
    if (not isinstance(version, dict)
            or set(version) != {"from", "to"}
            or any(not isinstance(version.get(name), str)
                   or SEMVER.fullmatch(version[name]) is None
                   for name in ("from", "to"))):
        errors.append("release version is not an exact from/to semver pair")
        target = None
    else:
        target = version["to"]
        if status == "noop" and version["from"] != target:
            errors.append("release noop changes the version")
        if status == "published" and version["from"] == target:
            errors.append("published release does not advance the version")
    if summary and target is not None and record.get("target") != target:
        errors.append("release LL target differs from release version.to")

    moved_name = "moved_tools" if summary else "moved"
    built_name = "built_tools" if summary else "built"
    moved_raw = record.get(moved_name)
    if not summary and isinstance(moved_raw, list):
        moved_raw = [item.get("repo") if isinstance(item, dict) else None
                     for item in moved_raw]
    moved = _string_identities(moved_raw, moved_name, errors)
    built = _string_identities(record.get(built_name), built_name, errors)
    attempts = record.get("compose_attempts")
    if not isinstance(attempts, list):
        errors.append("release compose_attempts is not a list")
        attempts = []

    smoke = record.get("smoke")
    if not isinstance(smoke, dict) or smoke.get("failures") != []:
        errors.append("release smoke evidence is missing or carries failures")
    elif status == "published":
        expected = smoke.get("expected")
        if (smoke.get("executed") is not True
                or type(expected) is not int or expected <= 0
                or type(smoke.get("passed")) is not int
                or smoke.get("passed") != expected
                or smoke.get("outcome") != "natural"):
            errors.append("published image has no complete natural smoke census")
    elif (smoke.get("executed") is not False
          or smoke.get("outcome") != "not_applicable"
          or type(smoke.get("expected")) is not int
          or smoke["expected"] <= 0
          or type(smoke.get("passed")) is not int
          or smoke.get("passed") != 0):
        errors.append("release noop has contradictory smoke evidence")

    if target is not None:
        _validate_remote_publication(record.get("remote_publication"),
                                     target, errors)
    _validate_record_publication(
        record.get("release_record_publication"), errors,
        published=status == "published")

    anchor = record.get("anchor_pr")
    if status == "noop":
        if moved or built or attempts:
            errors.append("release noop carries moved/built/compose work")
        if (not isinstance(anchor, dict) or anchor.get("ok") is not True
                or not isinstance(anchor.get("note"), str)
                or not anchor["note"]):
            errors.append("release noop has no successful anchor verification")
    else:
        candidate = record.get("version_candidate")
        if (not isinstance(candidate, dict) or not isinstance(version, dict)
                or candidate.get("from") != version.get("from")
                or candidate.get("to") != version.get("to")):
            errors.append("published version candidate differs from terminal version")
        if not attempts:
            errors.append("published release has no compose attempt")
        else:
            for pos, attempt in enumerate(attempts, 1):
                if (not isinstance(attempt, dict)
                        or type(attempt.get("attempt")) is not int
                        or attempt.get("attempt") != pos
                        or type(attempt.get("exit_code")) is not int):
                    errors.append("published compose attempt evidence is malformed")
                    break
            if isinstance(attempts[-1], dict) \
                    and attempts[-1].get("exit_code") != 0:
                errors.append("published compose did not finish successfully")
        if record.get("registry_composed") is not True:
            errors.append("published release did not prove registry composition")
        if record.get("pushed") is not True:
            errors.append("published immutable version was not pushed")
        latest = record.get("latest_publication")
        expected_latest = f"{RELEASE_IMAGE}:latest"
        if (not isinstance(latest, dict)
                or latest.get("ref") != expected_latest
                or latest.get("tagged") is not True
                or latest.get("pushed") is not True):
            errors.append("published latest tag was not pushed")
        # NO ANCHOR TO PROVE. A release used to move vibe-ic's pinned image
        # version and this demanded evidence that it had. It no longer does:
        # the pin claimed to be "what the plugin was verified against" and
        # nothing ever ran that verification, so the anchor was removed
        # (vibeic-eda 8ed5f33) and vibe-ic resolves the current image instead.
        #
        # A RELEASE THAT CARRIES ONE IS STILL CHECKED. Absence is now the
        # expected shape; a present-but-failed anchor would mean something
        # re-wired that path without updating this file, and saying nothing
        # about it is how a stale consumer becomes a silent one.
        if anchor is not None:
            if (not isinstance(anchor, dict) or anchor.get("ok") is not True
                    or not isinstance(anchor.get("note"), str)
                    or not anchor["note"]):
                errors.append("release carries an anchor action that did not "
                              "succeed — the release path no longer anchors, so "
                              "something re-wired it")
    return errors


def _current_head(root: Path) -> tuple[Optional[str], Optional[str]]:
    """Resolve the checkout independently of the release-pin producer.

    This reads only the HEAD ref/object identity.  It deliberately does not
    inspect mutable Dockerfiles or recompute pins: the checker owns that
    measurement, while LL independently proves which immutable commit it
    measured.
    """
    try:
        process = _git_run(
            ["-C", str(root), "rev-parse", "--verify",
             "HEAD^{commit}"],
            capture_output=True, text=True,
        )
    except OSError as exc:
        return None, f"could not independently resolve release-pin HEAD: {exc}"
    revision = process.stdout.strip().lower()
    if (process.returncode != 0
            or FULL_OBJECT_ID.fullmatch(revision) is None):
        detail = (process.stderr or process.stdout).strip()[:160]
        return None, (
            "could not independently resolve release-pin HEAD"
            + (f": {detail}" if detail else ""))
    return revision, None


def _validate_release_pins(record: dict, path: Path, round_at: str,
                           expected_run_id: Optional[str],
                           release_target: Optional[str],
                           expected_revision: str,
                           ) -> tuple[Optional[dict], list[str]]:
    """Validate and normalize the current round's committed-pin verdict.

    ``OUTSTANDING`` is valid negative evidence, just as an open PR is: it is
    retained and blocks generic convergence, but is not mislabeled a source
    error.  ``COULD_NOT_TELL`` and malformed/stale input are source failures;
    neither may inherit a prior round's PASS or disappear from LL.
    """
    errors: list[str] = []
    now = datetime.now(timezone.utc)
    if record.get("program") != RELEASE_PINS_PROGRAM:
        errors.append(f"program is not {RELEASE_PINS_PROGRAM}")
    if record.get("tool") != RELEASE_PINS_PROGRAM:
        errors.append(f"tool is not {RELEASE_PINS_PROGRAM}")
    if (type(record.get("schema_version")) is not int
            or record.get("schema_version") != RELEASE_PINS_SCHEMA_VERSION):
        errors.append(
            f"schema_version is not {RELEASE_PINS_SCHEMA_VERSION}")

    checked = _strict_when(record.get("checked_at"))
    boundary = _strict_when(round_at)
    if checked is None:
        errors.append("checked_at is missing or malformed")
    elif boundary is None or checked < boundary:
        errors.append("checked_at predates this 05:30 round")
    elif checked > now:
        errors.append("checked_at is in the future")
    if record.get("round_at") != round_at:
        errors.append("round_at does not match this 05:30 round")
    run_id = record.get("run_id")
    if (not isinstance(expected_run_id, str) or not expected_run_id
            or run_id != expected_run_id):
        errors.append("run_id does not match current daily_0530 evidence")

    verdict = record.get("verdict")
    if verdict not in {"PASS", "OUTSTANDING", "COULD_NOT_TELL"}:
        errors.append("verdict is not PASS, OUTSTANDING, or COULD_NOT_TELL")
    findings = record.get("findings")
    findings_valid = (isinstance(findings, list)
                      and all(isinstance(item, str) and item
                              for item in findings)
                      and len(findings) == len(set(findings)))
    if not findings_valid:
        errors.append("findings is not a duplicate-free list of strings")
        findings = []

    version = record.get("version")
    rev = record.get("rev")
    revision = record.get("revision")
    recorded_count = record.get("recorded_count")
    committed_count = record.get("committed_count")
    outstanding = record.get("outstanding")
    if verdict in {"PASS", "OUTSTANDING"}:
        if not isinstance(version, str) or not version:
            errors.append("version is missing")
        elif release_target is not None and version != release_target:
            errors.append(
                f"version {version!r} does not match release target "
                f"{release_target!r}")
        if rev != "HEAD":
            errors.append("rev is not HEAD")
        if (not isinstance(revision, str)
                or FULL_OBJECT_ID.fullmatch(revision.lower()) is None):
            errors.append("revision is not one full commit id")
        elif revision.lower() != expected_revision:
            errors.append(
                "revision does not match independently resolved current HEAD")
        for name, value in (("recorded_count", recorded_count),
                            ("committed_count", committed_count)):
            if type(value) is not int or value < 0:
                errors.append(f"{name} is not a non-negative integer")
        if (not isinstance(outstanding, list)
                or any(not isinstance(item, str) or not item
                       for item in outstanding)
                or len(outstanding) != len(set(outstanding))):
            errors.append(
                "outstanding is not a duplicate-free list of tool names")
            outstanding = []
        elif outstanding != sorted(outstanding):
            errors.append("outstanding tools are not in canonical sorted order")
        finding_tools = []
        for finding in findings:
            tool, separator, _detail = finding.partition(":")
            if not separator or not tool:
                errors.append("a finding does not name its tool before ':'")
                break
            finding_tools.append(tool)
        if finding_tools != outstanding:
            errors.append("findings and outstanding name different tools")
        if verdict == "PASS":
            if findings or outstanding:
                errors.append("PASS carries findings/outstanding tools")
            if (type(recorded_count) is int
                    and type(committed_count) is int
                    and recorded_count != committed_count):
                errors.append("PASS carries unequal pin counts")
        elif not findings or not outstanding:
            errors.append("OUTSTANDING names no outstanding pin review")
    elif verdict == "COULD_NOT_TELL" and not findings:
        errors.append("COULD_NOT_TELL carries no cause")

    if errors:
        return None, [f"{path.name} is invalid: " + "; ".join(errors)]

    normalized = {
        "program": RELEASE_PINS_PROGRAM,
        "tool": RELEASE_PINS_PROGRAM,
        "schema_version": RELEASE_PINS_SCHEMA_VERSION,
        "checked_at": record["checked_at"],
        "round_at": record["round_at"],
        "run_id": run_id,
        "verdict": verdict,
        "committed": (True if verdict == "PASS" else
                      False if verdict == "OUTSTANDING" else None),
        "version": (version if verdict != "COULD_NOT_TELL"
                    and isinstance(version, str) else None),
        "rev": (rev if verdict != "COULD_NOT_TELL"
                and isinstance(rev, str) else None),
        "requested_revision": (rev if verdict != "COULD_NOT_TELL"
                               and isinstance(rev, str) else None),
        "revision": (revision.lower()
                     if verdict != "COULD_NOT_TELL"
                     and isinstance(revision, str) else None),
        "recorded_count": (recorded_count
                           if verdict != "COULD_NOT_TELL"
                           and type(recorded_count) is int else None),
        "committed_count": (committed_count
                            if verdict != "COULD_NOT_TELL"
                            and type(committed_count) is int else None),
        "outstanding": (list(outstanding or [])
                        if verdict != "COULD_NOT_TELL" else []),
        "findings": list(findings),
    }
    if verdict == "COULD_NOT_TELL":
        return normalized, [
            f"{path.name} could not tell whether release pins are committed: "
            + "; ".join(findings)
        ]
    return normalized, []


def _validate_ai_authorization(record: dict, path: Path, round_at: str,
                               forks_json: Path
                               ) -> tuple[Optional[dict], list[str]]:
    """Validate the exact pre-release AI postcondition used by this round.

    A valid ``blocked`` record is evidence, not a source error.  ``norecord``
    and malformed records remain explicit third states and make LL capture
    non-clean; neither may inherit an earlier authorization.
    """
    errors: list[str] = []
    now = datetime.now(timezone.utc)
    if record.get("program") != AI_AUTHORIZATION_PROGRAM:
        errors.append(f"program is not {AI_AUTHORIZATION_PROGRAM}")
    if (type(record.get("schema_version")) is not int
            or record["schema_version"] != AI_AUTHORIZATION_SCHEMA_VERSION):
        errors.append(
            f"schema_version is not {AI_AUTHORIZATION_SCHEMA_VERSION}")
    status = record.get("status")
    if status not in {"authorized", "blocked", "norecord"}:
        errors.append("status is not authorized, blocked, or norecord")
    boundary = _strict_when(round_at)
    checked = _strict_when(record.get("checked_at"))
    source_boundary = _strict_when(record.get("round_at"))
    if boundary is None:
        errors.append("the 05:30 round boundary is malformed")
    if checked is None:
        errors.append("checked_at is missing or malformed")
    elif boundary is not None and checked < boundary:
        errors.append("checked_at predates this 05:30 round")
    elif checked > now:
        errors.append("checked_at is in the future")
    if source_boundary is None:
        errors.append("round_at is missing or malformed")
    elif boundary is not None and source_boundary != boundary:
        errors.append("round_at does not match this 05:30 round")
    if not isinstance(record.get("source"), str) or not record["source"]:
        errors.append("source is missing")

    registered, registry_hash, fleet_errors = _registered_fleet(forks_json)
    errors.extend(fleet_errors)
    claimed_forks = record.get("registered_forks")
    claimed_count = record.get("registered_count")
    if claimed_forks != registered:
        errors.append("registered_forks does not exactly match canonical fleet")
    if type(claimed_count) is not int or claimed_count != len(registered):
        errors.append("registered_count does not match canonical fleet")
    registry = record.get("registry")
    if not isinstance(registry, dict):
        errors.append("registry is not an object")
    else:
        if registry.get("source") != str(forks_json):
            errors.append("registry.source does not name canonical --forks-json")
        if registry_hash is None or registry.get("sha256") != registry_hash:
            errors.append("registry.sha256 does not match canonical --forks-json")

    source_errors = record.get("source_errors")
    reasons = record.get("reasons")
    if (not isinstance(source_errors, list)
            or any(not isinstance(item, str) or not item
                   for item in source_errors)):
        errors.append("source_errors is not a list of non-empty strings")
        source_errors = []
    if (not isinstance(reasons, list)
            or any(not isinstance(item, str) or not item for item in reasons)):
        errors.append("reasons is not a list of non-empty strings")
        reasons = []

    # NORECORD is itself a retained fact. Its missing round fields must not be
    # laundered into malformed-record noise, but it still makes capture fail.
    if status == "norecord":
        if not source_errors:
            errors.append("norecord has no source error")
        return record, errors + [
            f"{path.name} reports NORECORD: "
            + "; ".join(source_errors or reasons or ["unknown cause"])
        ]

    meta = record.get("round")
    if not isinstance(meta, dict):
        errors.append("round is not an object")
        meta = {}
    if type(meta.get("schema_version")) is not int or meta.get("schema_version") != 1:
        errors.append("round.schema_version is not 1")
    run_id = meta.get("run_id")
    if (not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", run_id)):
        errors.append("round.run_id is missing or malformed")
    started = _strict_when(meta.get("started_at"))
    finished = _strict_when(meta.get("finished_at"))
    if started is None or finished is None:
        errors.append("round started_at/finished_at is missing or malformed")
    else:
        if boundary is not None and started < boundary:
            errors.append("round.started_at predates this 05:30 round")
        if finished < started:
            errors.append("round.finished_at precedes started_at")
        if checked is not None and finished > checked:
            errors.append("round.finished_at is after authorization check")
    if meta.get("applied") is not True:
        errors.append("round.applied is not true")

    count = record.get("case_count")
    verified_count = record.get("verified_count")
    unverified_count = record.get("unverified_count")
    for name, value in (("case_count", count),
                        ("verified_count", verified_count),
                        ("unverified_count", unverified_count)):
        if type(value) is not int or value < 0:
            errors.append(f"{name} is not a non-negative integer")
    results = record.get("case_results")
    if not isinstance(results, list):
        errors.append("case_results is not a list")
        results = []
    identities: list[tuple[str, str]] = []
    true_count = 0
    for pos, result in enumerate(results):
        if not isinstance(result, dict):
            errors.append(f"case_results[{pos}] is not an object")
            continue
        fork, branch = result.get("fork"), result.get("branch")
        if (not isinstance(fork, str) or not fork or fork.strip() != fork
                or not isinstance(branch, str) or not branch
                or branch.strip() != branch):
            errors.append(f"case_results[{pos}] has malformed identity")
            continue
        if type(result.get("verified")) is not bool:
            errors.append(f"case_results[{pos}].verified is not boolean")
            continue
        mainline = result.get("mainline")
        source_tip = result.get("source_tip")
        published_tip = result.get("published_tip")
        reason = result.get("reason")
        if (not isinstance(mainline, str) or not mainline
                or mainline.strip() != mainline):
            errors.append(f"case_results[{pos}].mainline is malformed")
        if (not isinstance(source_tip, str)
                or not re.fullmatch(r"[0-9a-f]{40}", source_tip)):
            errors.append(f"case_results[{pos}].source_tip is malformed")
        if (result["verified"] and
                (not isinstance(published_tip, str)
                 or not re.fullmatch(r"[0-9a-f]{40}", published_tip))):
            errors.append(f"case_results[{pos}].published_tip is malformed")
        if not isinstance(reason, str) or not reason:
            errors.append(f"case_results[{pos}].reason is malformed")
        identities.append((fork, branch))
        true_count += int(result["verified"])
    duplicates = sorted(
        key for key in set(identities) if identities.count(key) > 1)
    if duplicates:
        errors.append("case_results has duplicate identity")
    if type(count) is int and count != len(results):
        errors.append("case_count does not equal case_results length")
    if type(verified_count) is int and verified_count != true_count:
        errors.append("verified_count contradicts case_results")
    false_count = len(results) - true_count
    if type(unverified_count) is int and unverified_count != false_count:
        errors.append("unverified_count contradicts case_results")

    fully_verified = bool(
        type(count) is int and count == len(results)
        and true_count == count and false_count == 0)
    transport = record.get("transport")
    transport_authorized = bool(
        count == 0
        or (isinstance(transport, dict)
            and transport.get("invoked") is True
            and transport.get("outcome") == "natural"
            and type(transport.get("rc")) is int
            and transport.get("rc") == 0))
    fully_authorized = fully_verified and transport_authorized
    if status == "authorized":
        if not fully_authorized:
            errors.append(
                "authorized status contradicts case_results or AI transport")
        if reasons or source_errors:
            errors.append("authorized status carries reasons/source_errors")
    elif status == "blocked":
        if fully_authorized:
            errors.append("blocked status contradicts fully authorized cases")
        if not reasons:
            errors.append("blocked status has no reason")
        if source_errors:
            errors.append("blocked status carries source_errors")
    if errors:
        return None, [f"{path.name} is invalid: " + "; ".join(errors)]
    return record, []


def _valid_repository(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    return (len(parts) == 2 and all(parts)
            and all(part.strip() == part for part in parts)
            and not any(any(char.isspace() for char in part)
                        for part in parts))


def _github_registry(path: Path) -> tuple[Optional[dict], list[str]]:
    """Independently derive the GitHub repository denominators from FORKS."""
    errors: list[str] = []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"could not read GitHub fleet registry {path}: "
                      f"{type(exc).__name__}: {exc}"]
    entries = value.get("forks") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("org") != GITHUB_ORG:
        errors.append(f"GitHub fleet registry org is not {GITHUB_ORG}")
    if not isinstance(entries, list):
        return None, errors + [f"GitHub fleet registry {path} has no forks list"]
    registered: list[str] = []
    upstream: list[str] = []
    tools: list[str] = []
    for pos, entry in enumerate(entries):
        tool = entry.get("tool") if isinstance(entry, dict) else None
        parent = entry.get("upstream") if isinstance(entry, dict) else None
        if (not isinstance(tool, str) or not tool or tool.strip() != tool
                or "/" in tool or any(char.isspace() for char in tool)):
            errors.append(f"GitHub fleet forks[{pos}].tool is malformed")
            continue
        if not _valid_repository(parent):
            errors.append(f"GitHub fleet forks[{pos}].upstream is malformed")
            continue
        tools.append(tool)
        registered.append(f"{GITHUB_ORG}/{tool}")
        upstream.append(parent)
    for label, values in (("tool", tools), ("registered repository", registered)):
        duplicates = sorted(
            value for value in set(item.lower() for item in values)
            if sum(item.lower() == value for item in values) > 1)
        if duplicates:
            errors.append(f"GitHub fleet has duplicate {label}(s): "
                          + ", ".join(duplicates))
    if not registered:
        errors.append("GitHub fleet registry is empty")
    if errors:
        return None, errors
    canonical = sorted(registered, key=str.lower)
    return {
        "tools": sorted(tools, key=str.lower),
        "registered": canonical,
        "required": sorted(canonical + [VIBEIC_EDA_REPOSITORY], key=str.lower),
        "upstream": sorted(set(upstream), key=str.lower),
        "upstream_by_tool": dict(zip(tools, upstream)),
    }, []


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
        "inventory": record["inventory"],
        "delivery": record["delivery"],
        "upstream_actor": record["upstream_actor"],
        "round_upstream": record["round_upstream"],
        "counts": dict(record["counts"]),
        "items": [dict(item) for item in record["items"]],
        "source_errors": [dict(error) for error in record["source_errors"]],
    }


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _exact_string_identities(name: str, value: object, expected: list[str],
                             errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{name} is not a list")
        return []
    if any(not isinstance(item, str) or not item or item.strip() != item
           for item in value):
        errors.append(f"{name} contains a malformed identity")
        return []
    lowered = [item.lower() for item in value]
    duplicates = sorted(item for item in set(lowered)
                        if lowered.count(item) > 1)
    if duplicates:
        errors.append(f"{name} contains duplicate identities: "
                      + ", ".join(duplicates))
    actual_set = set(lowered)
    expected_set = {item.lower() for item in expected}
    missing = sorted(expected_set - actual_set)
    unexpected = sorted(actual_set - expected_set)
    if missing:
        errors.append(f"{name} is missing: " + ", ".join(missing))
    if unexpected:
        errors.append(f"{name} has unexpected identities: "
                      + ", ".join(unexpected))
    if len(value) != len(expected):
        errors.append(f"{name} count is {len(value)}, expected {len(expected)}")
    return value


def _connection_evidence(value: object, name: str, errors: list[str], *,
                         allow_disabled: bool = False
                         ) -> tuple[str | None, Optional[int]]:
    if not isinstance(value, dict):
        errors.append(f"{name} is not an object")
        return None, None
    outcome = value.get("outcome")
    count = value.get("open_count")
    allowed = {"complete", "norecord"}
    if allow_disabled:
        allowed.add("disabled")
    if outcome not in allowed:
        errors.append(f"{name}.outcome={outcome!r} is malformed")
        return None, None
    if outcome == "norecord":
        if count is not None:
            errors.append(f"{name}.open_count must be null for norecord")
        return outcome, None
    if not _nonnegative_int(count):
        errors.append(f"{name}.open_count is not a non-negative integer")
        return outcome, None
    if outcome == "disabled" and count != 0:
        errors.append(f"{name}.disabled must have open_count=0")
    return outcome, count


def _delivery_kind(head_ref: object) -> Optional[str]:
    if not isinstance(head_ref, str):
        return None
    names = ("anchor", "sync", "assessment")
    for name, pattern in zip(names, DELIVERY_HEAD_PATTERNS):
        if pattern.fullmatch(head_ref):
            return name
    return None


def _delivery_exclusion_reasons(item: dict) -> list[str]:
    if item.get("head_owner") != GITHUB_ORG:
        return ["head_owner_is_not_vibeic"]
    if not isinstance(item.get("head_ref"), str):
        return ["head_ref_is_missing"]
    if _delivery_kind(item["head_ref"]) is None:
        return ["head_ref_is_not_a_delivery_producer_branch"]
    return []


def _delivery_anomalies(item: dict, kind: str) -> list[str]:
    anomalies: list[str] = []
    if item.get("base_ref") != "main":
        anomalies.append("base_ref_is_not_main")
    if item.get("draft") is not False:
        anomalies.append("draft_is_not_false")
    head_ref = item.get("head_ref")
    pattern = DELIVERY_HEAD_PATTERNS[("anchor", "sync", "assessment").index(kind)]
    match = pattern.fullmatch(head_ref) if isinstance(head_ref, str) else None
    token = match.group(1) if match is not None else ""
    title = item.get("title")
    if kind == "anchor":
        if title != f"chore(image-anchor): adopt vibeic-eda {token}":
            anomalies.append("title_does_not_match_anchor_producer")
    elif kind == "sync":
        if (not isinstance(title, str)
                or not title.startswith(f"[eda-fork] {token}: ")):
            anomalies.append("title_does_not_match_sync_producer")
    elif (not isinstance(title, str)
          or not title.startswith(
              f"[eda-fork] {token}: upstream release assessment — ")):
        anomalies.append("title_does_not_match_assessment_producer")
    return anomalies


def _validate_github_v2_sources(record: dict, items: list[dict],
                                registry: dict, errors: list[str]) -> bool:
    """Recompute every v2 denominator and source count from retained facts."""
    canonical = registry["registered"]
    required = registry["required"]
    canonical_keys = {repo.lower() for repo in canonical}
    required_keys = {repo.lower() for repo in required}
    upstream_keys = {repo.lower() for repo in registry["upstream"]}
    expected_by_tool = {tool.lower(): repo for tool, repo in
                        registry["upstream_by_tool"].items()}

    item_by_repo_type: dict[tuple[str, str], list[dict]] = {}
    for item in items:
        repository = item.get("repository")
        kind = item.get("type")
        if isinstance(repository, str) and isinstance(kind, str):
            item_by_repo_type.setdefault((repository.lower(), kind), []).append(item)

    inventory = record.get("inventory")
    inventory_complete = False
    inventory_issue_total = 0
    inventory_pr_total = 0
    if not isinstance(inventory, dict):
        errors.append("inventory is not an object")
    else:
        _exact_string_identities(
            "inventory.canonical_registered_repositories",
            inventory.get("canonical_registered_repositories"), canonical,
            errors)
        _exact_string_identities(
            "inventory.required_repositories",
            inventory.get("required_repositories"), required, errors)
        if inventory.get("required_repository_count") != len(required):
            errors.append(
                "inventory.required_repository_count does not equal the "
                "canonical required denominator")

        catalog = inventory.get("catalog")
        catalog_complete = False
        catalog_unregistered: list[str] = []
        catalog_by_repo: dict[str, dict] = {}
        if not isinstance(catalog, dict):
            errors.append("inventory.catalog is not an object")
        elif catalog.get("outcome") == "norecord":
            for field in ("total_repositories", "repositories",
                          "live_fork_repositories",
                          "unregistered_live_fork_repositories"):
                if catalog.get(field) is not None:
                    errors.append(
                        f"inventory.catalog.{field} must be null for norecord")
        elif catalog.get("outcome") != "complete":
            errors.append("inventory.catalog.outcome is malformed")
        else:
            catalog_rows = catalog.get("repositories")
            if not isinstance(catalog_rows, list):
                errors.append("inventory.catalog.repositories is not a list")
            else:
                for pos, row in enumerate(catalog_rows):
                    repo = row.get("repository") if isinstance(row, dict) else None
                    if (not _valid_repository(repo)
                            or not isinstance(row.get("is_fork"), bool)
                            or not isinstance(row.get("is_archived"), bool)
                            or not isinstance(
                                row.get("has_issues_enabled"), bool)):
                        errors.append(
                            f"inventory.catalog.repositories[{pos}] is malformed")
                        continue
                    key = repo.lower()
                    if key in catalog_by_repo:
                        errors.append(
                            f"inventory.catalog.repositories duplicates {repo}")
                    catalog_by_repo[key] = row
                if catalog.get("total_repositories") != len(catalog_rows):
                    errors.append(
                        "inventory.catalog.total_repositories does not equal "
                        "the catalog row count")
                catalog_required = required_keys | {VIBEIC_DELIVERY_REPOSITORY}
                missing = sorted(catalog_required - set(catalog_by_repo))
                if missing:
                    errors.append("inventory.catalog misses required repositories: "
                                  + ", ".join(missing))
                derived_live = sorted(
                    row["repository"] for row in catalog_by_repo.values()
                    if row["is_fork"] and not row["is_archived"])
                _exact_string_identities(
                    "inventory.catalog.live_fork_repositories",
                    catalog.get("live_fork_repositories"), derived_live, errors)
                derived_unregistered = sorted(
                    repo for repo in derived_live
                    if repo.lower() not in canonical_keys)
                catalog_unregistered = _exact_string_identities(
                    "inventory.catalog.unregistered_live_fork_repositories",
                    catalog.get("unregistered_live_fork_repositories"),
                    derived_unregistered, errors)
                catalog_complete = True

        rows = inventory.get("repositories")
        row_by_repo: dict[str, dict] = {}
        completed = 0
        if not isinstance(rows, list):
            errors.append("inventory.repositories is not a list")
        else:
            identities = [row.get("repository") if isinstance(row, dict) else None
                          for row in rows]
            if all(isinstance(repo, str) for repo in identities):
                _exact_string_identities(
                    "inventory.repositories", identities, required, errors)
            else:
                errors.append("inventory.repositories has malformed identity")
            for pos, row in enumerate(rows):
                if not isinstance(row, dict):
                    errors.append(f"inventory.repositories[{pos}] is not an object")
                    continue
                repo = row.get("repository")
                if not isinstance(repo, str):
                    continue
                key = repo.lower()
                if key in row_by_repo:
                    continue
                row_by_repo[key] = row
                expected_scope = ("registered_fork" if key in canonical_keys
                                  else "vibeic_eda")
                if row.get("scope") != expected_scope:
                    errors.append(f"inventory row {repo} has wrong scope")
                status = row.get("status")
                if status not in {"complete", "norecord"}:
                    errors.append(f"inventory row {repo} status is malformed")
                issue_outcome, issue_count = _connection_evidence(
                    row.get("issues"), f"inventory row {repo}.issues", errors,
                    allow_disabled=True)
                pr_outcome, pr_count = _connection_evidence(
                    row.get("pull_requests"),
                    f"inventory row {repo}.pull_requests", errors)
                if status == "complete":
                    completed += 1
                    for boolean in ("is_fork", "is_archived",
                                    "has_issues_enabled"):
                        if not isinstance(row.get(boolean), bool):
                            errors.append(
                                f"inventory row {repo}.{boolean} is malformed")
                    expected_issue_outcome = (
                        "complete" if row.get("has_issues_enabled") is True
                        else "disabled")
                    if issue_outcome != expected_issue_outcome:
                        errors.append(
                            f"inventory row {repo} issue outcome contradicts "
                            "has_issues_enabled")
                    if pr_outcome != "complete":
                        errors.append(
                            f"inventory row {repo} PR connection is incomplete")
                    catalog_row = catalog_by_repo.get(key)
                    if catalog_row is not None:
                        for field in ("is_fork", "is_archived",
                                      "has_issues_enabled"):
                            if row.get(field) != catalog_row.get(field):
                                errors.append(
                                    f"inventory row {repo}.{field} contradicts "
                                    "the exact catalog")
                    actual_issues = sum(
                        "exact_repository_issues" in item.get("sources", [])
                        for item in item_by_repo_type.get((key, "issue"), []))
                    actual_prs = sum(
                        "exact_repository_pull_requests"
                        in item.get("sources", [])
                        for item in item_by_repo_type.get(
                            (key, "pull_request"), []))
                    if issue_count != actual_issues:
                        errors.append(
                            f"inventory row {repo} issue count={issue_count!r}, "
                            f"retained items={actual_issues}")
                    # ALARMS ARE SPLIT OUT OF `items`, SO ADD THEM BACK HERE.
                    # This check exists to catch an item vanishing between the
                    # catalog and the retained list, and it did its job: the
                    # publish-silence exclusion showed up as "PR count=1,
                    # retained items=0". The exclusion is legitimate — a notice
                    # is not a debt — but it must be ACCOUNTED FOR, not waived.
                    # `alarm_items` carries the excluded rows, so the catalog
                    # still has to equal retained + excluded, and anything that
                    # disappears for any OTHER reason still fails here.
                    alarms_here = sum(
                        item.get("repository") == repo
                        and item.get("type") == "pull_request"
                        for item in (record.get("alarm_items") or []))
                    if pr_count != actual_prs + alarms_here:
                        errors.append(
                            f"inventory row {repo} PR count={pr_count!r}, "
                            f"retained items={actual_prs}"
                            + (f" + {alarms_here} excluded alarm(s)"
                               if alarms_here else ""))
                    inventory_issue_total += issue_count or 0
                    inventory_pr_total += pr_count or 0
                else:
                    if issue_outcome != "norecord" or pr_outcome != "norecord":
                        errors.append(
                            f"inventory row {repo} norecord has a measured connection")
                    for field in ("is_fork", "is_archived",
                                  "has_issues_enabled"):
                        if row.get(field) is not None:
                            errors.append(
                                f"inventory row {repo}.{field} must be null "
                                "for norecord")
        if inventory.get("completed_repository_count") != completed:
            errors.append(
                "inventory.completed_repository_count contradicts rows")
        derived_inventory_complete = (
            catalog_complete and not catalog_unregistered
            and len(row_by_repo) == len(required) and completed == len(required))
        expected_status = "complete" if derived_inventory_complete else "norecord"
        if inventory.get("status") != expected_status:
            errors.append(
                f"inventory.status={inventory.get('status')!r}, expected "
                f"{expected_status}")
        inventory_complete = derived_inventory_complete

    delivery = record.get("delivery")
    delivery_complete = False
    delivery_matched = 0
    if not isinstance(delivery, dict):
        errors.append("delivery is not an object")
    else:
        if delivery.get("repository") != VIBEIC_DELIVERY_REPOSITORY:
            errors.append("delivery.repository is not vibeic/vibe-ic")
        patterns = delivery.get("pr_head_patterns")
        if (not isinstance(patterns, list)
                or any(not isinstance(pattern, str) for pattern in patterns)
                or set(patterns) != DELIVERY_HEAD_PATTERN_TEXT
                or len(patterns) != len(DELIVERY_HEAD_PATTERN_TEXT)):
            errors.append("delivery.pr_head_patterns is not the exact producer set")
        if delivery.get("issues") != {
                "outcome": "excluded_no_durable_producer_marker"}:
            errors.append("delivery.issues is not explicitly excluded")
        pulls = delivery.get("pull_requests")
        pulls_complete = False
        if not isinstance(pulls, dict):
            errors.append("delivery.pull_requests is not an object")
        elif pulls.get("outcome") == "norecord":
            for field in ("open_count", "matched_count", "excluded_count",
                          "excluded"):
                if pulls.get(field) is not None:
                    errors.append(
                        f"delivery.pull_requests.{field} must be null for norecord")
        elif pulls.get("outcome") != "complete":
            errors.append("delivery.pull_requests.outcome is malformed")
        else:
            open_count = pulls.get("open_count")
            matched_count = pulls.get("matched_count")
            excluded_count = pulls.get("excluded_count")
            excluded = pulls.get("excluded")
            if not all(_nonnegative_int(value) for value in
                       (open_count, matched_count, excluded_count)):
                errors.append("delivery PR counts are malformed")
            if not isinstance(excluded, list):
                errors.append("delivery.pull_requests.excluded is not a list")
                excluded = []
            if excluded_count != len(excluded):
                errors.append("delivery excluded_count contradicts excluded rows")
            if (_nonnegative_int(open_count) and _nonnegative_int(matched_count)
                    and _nonnegative_int(excluded_count)
                    and open_count != matched_count + excluded_count):
                errors.append(
                    "delivery included+excluded does not equal total open PRs")
            excluded_ids: set[tuple[str, int]] = set()
            for pos, item in enumerate(excluded):
                repo = item.get("repository") if isinstance(item, dict) else None
                number = item.get("number") if isinstance(item, dict) else None
                reasons = item.get("reasons") if isinstance(item, dict) else None
                if (repo != VIBEIC_DELIVERY_REPOSITORY
                        or not _nonnegative_int(number) or number == 0
                        or not isinstance(reasons, list) or not reasons
                        or any(not isinstance(reason, str) or not reason
                               for reason in reasons)):
                    errors.append(
                        f"delivery excluded row {pos} is malformed")
                    continue
                identity = (repo.lower(), number)
                if identity in excluded_ids:
                    errors.append("delivery excluded rows contain a duplicate")
                excluded_ids.add(identity)
                if (not isinstance(item.get("title"), str)
                        or not item["title"]
                        or not isinstance(item.get("url"), str)
                        or not item["url"]
                        or item.get("type") != "pull_request"
                        or (item.get("author") is not None
                            and (not isinstance(item.get("author"), str)
                                 or not item["author"]))
                        or (item.get("head_owner") is not None
                            and (not isinstance(item.get("head_owner"), str)
                                 or not item["head_owner"]))
                        or (item.get("head_ref") is not None
                            and (not isinstance(item.get("head_ref"), str)
                                 or not item["head_ref"]))
                        or not isinstance(item.get("base_ref"), str)
                        or not item["base_ref"]
                        or not isinstance(item.get("draft"), bool)
                        or _strict_when(item.get("updated_at")) is None):
                    errors.append(
                        f"delivery excluded PR #{number} is not normalized")
                expected_reasons = _delivery_exclusion_reasons(item)
                if reasons != expected_reasons:
                    errors.append(
                        f"delivery excluded PR #{number} reasons contradict "
                        "producer identity")
            matched_items = [item for item in items
                             if "fork_delivery" in item.get("scopes", [])]
            delivery_matched = len(matched_items)
            if matched_count != delivery_matched:
                errors.append(
                    "delivery matched_count contradicts retained delivery items")
            matched_ids: set[tuple[str, int]] = set()
            for item in matched_items:
                repo, number = item.get("repository"), item.get("number")
                identity = (str(repo).lower(), number)
                if identity in excluded_ids:
                    errors.append("delivery PR appears in both matched and excluded")
                matched_ids.add(identity)
                kind = _delivery_kind(item.get("head_ref"))
                if (repo != VIBEIC_DELIVERY_REPOSITORY
                        or item.get("type") != "pull_request"
                        or item.get("head_owner") != GITHUB_ORG or kind is None
                        or item.get("delivery_kind") != kind
                        or item.get("delivery_anomalies")
                        != _delivery_anomalies(item, kind or "anchor")):
                    errors.append(
                        f"delivery matched PR #{number!r} violates producer identity")
            pulls_complete = True
        delivery_complete = pulls_complete
        expected_delivery_status = (
            "complete" if pulls_complete else "norecord")
        if delivery.get("status") != expected_delivery_status:
            errors.append(
                f"delivery.status={delivery.get('status')!r}, expected "
                f"{expected_delivery_status}")

    actor = record.get("upstream_actor")
    actor_complete = False
    actor_counts = {"issues": 0, "pull_requests": 0}
    if not isinstance(actor, dict):
        errors.append("upstream_actor is not an object")
    else:
        part_complete = []
        for field in ("issues", "pull_requests"):
            value = actor.get(field)
            if not isinstance(value, dict):
                errors.append(f"upstream_actor.{field} is not an object")
                part_complete.append(False)
                continue
            outcome = value.get("outcome")
            seen, matched = value.get("seen_count"), value.get("matched_count")
            if outcome == "complete":
                okay = (_nonnegative_int(seen) and _nonnegative_int(matched)
                        and matched <= seen)
                if not okay:
                    errors.append(f"upstream_actor.{field} counts are malformed")
                actor_counts[field] = matched if _nonnegative_int(matched) else 0
                part_complete.append(okay)
            elif outcome == "norecord":
                if seen is not None or matched is not None:
                    errors.append(
                        f"upstream_actor.{field} norecord counts must be null")
                part_complete.append(False)
            else:
                errors.append(f"upstream_actor.{field}.outcome is malformed")
                part_complete.append(False)
        actor_complete = all(part_complete) and len(part_complete) == 2
        expected_actor_status = "complete" if actor_complete else "norecord"
        if actor.get("status") != expected_actor_status:
            errors.append("upstream_actor.status contradicts connections")

    round_upstream = record.get("round_upstream")
    round_complete = False
    round_total = 0
    if not isinstance(round_upstream, dict):
        errors.append("round_upstream is not an object")
    else:
        if round_upstream.get("required_fork_count") != len(canonical):
            errors.append("round_upstream.required_fork_count is wrong")
        rows = round_upstream.get("forks")
        row_tools: list[str] = []
        complete_rows = 0
        if not isinstance(rows, list):
            errors.append("round_upstream.forks is not a list")
        else:
            for pos, row in enumerate(rows):
                tool = row.get("tool") if isinstance(row, dict) else None
                if not isinstance(tool, str):
                    errors.append(f"round_upstream.forks[{pos}] has no tool")
                    continue
                row_tools.append(tool)
                expected_repo = expected_by_tool.get(tool.lower())
                if (not isinstance(row.get("repository"), str)
                        or expected_repo is None
                        or row["repository"].lower() != expected_repo.lower()):
                    errors.append(f"round_upstream row {tool} repository is wrong")
                outcome, count = _connection_evidence(
                    {"outcome": row.get("outcome"),
                     "open_count": row.get("open_count")},
                    f"round_upstream row {tool}", errors)
                if outcome == "complete":
                    complete_rows += 1
                    round_total += count or 0
                    retained = sum(
                        item.get("repository", "").lower()
                        == str(expected_repo).lower()
                        and "round_upstream_pull_requests"
                        in item.get("sources", [])
                        for item in items
                    )
                    if count != retained:
                        errors.append(
                            f"round_upstream row {tool} count={count!r}, "
                            f"retained items={retained}")
            _exact_string_identities(
                "round_upstream.forks", row_tools, registry["tools"], errors)
        if round_upstream.get("completed_fork_count") != complete_rows:
            errors.append("round_upstream.completed_fork_count contradicts rows")
        round_complete = complete_rows == len(canonical)
        expected_round_status = "complete" if round_complete else "norecord"
        if round_upstream.get("status") != expected_round_status:
            errors.append("round_upstream.status contradicts rows")

    counts = record.get("counts")
    if isinstance(counts, dict):
        by_source = counts.get("by_source")
        expected_sources = {
            "exact_repository_issues": inventory_issue_total,
            "exact_repository_pull_requests": inventory_pr_total,
            "fork_delivery_pull_requests": delivery_matched,
            "viewer_upstream_issues": actor_counts["issues"],
            "viewer_upstream_pull_requests": actor_counts["pull_requests"],
            "round_upstream_pull_requests": round_total,
        }
        if by_source != expected_sources:
            errors.append("counts.by_source contradicts exact source evidence")
        expected_scope_counts = {
            "registered_fork_items": sum(
                "registered_fork" in item.get("scopes", []) for item in items),
            "upstream_items": sum(
                "upstream" in item.get("scopes", []) for item in items),
            "vibeic_eda_items": sum(
                "vibeic_eda" in item.get("scopes", []) for item in items),
            "fork_delivery_items": sum(
                "fork_delivery" in item.get("scopes", []) for item in items),
        }
        for field, expected in expected_scope_counts.items():
            if counts.get(field) != expected:
                errors.append(f"counts.{field}={counts.get(field)!r}, "
                              f"expected {expected}")
        # COUNT THE EXCLUDED ALARMS TOO — `by_source` never dropped them.
        #
        # The producer removes publish-silence alarms from `items` (a notice is
        # not a debt) but `counts.by_source` is derived before that split, so
        # comparing per-source tallies against `items` alone is comparing two
        # different populations. This is the SECOND place that reconciliation
        # lives; I fixed the inventory one last round and stopped there, and
        # this one surfaced as
        # "exact_repository_pull_requests retained item count contradicts
        # source evidence" the very next round.
        #
        # Counted over items + alarms, so the check keeps its whole force:
        # anything vanishing for any OTHER reason still fails here.
        items = list(items) + [
            a for a in (record.get("alarm_items") or [])
            if isinstance(a, dict)]
        item_source_counts = {
            "exact_repository_issues": sum(
                item.get("type") == "issue"
                and "exact_repository_issues" in item.get("sources", [])
                for item in items),
            "exact_repository_pull_requests": sum(
                item.get("type") == "pull_request"
                and "exact_repository_pull_requests" in item.get("sources", [])
                for item in items),
            "fork_delivery_pull_requests": sum(
                "fork_delivery_pull_requests" in item.get("sources", [])
                for item in items),
            "viewer_upstream_issues": sum(
                item.get("type") == "issue"
                and "viewer_upstream" in item.get("sources", [])
                for item in items),
            "viewer_upstream_pull_requests": sum(
                item.get("type") == "pull_request"
                and "viewer_upstream" in item.get("sources", [])
                for item in items),
            "round_upstream_pull_requests": sum(
                "round_upstream_pull_requests" in item.get("sources", [])
                for item in items),
        }
        for field in ("exact_repository_issues",
                      "exact_repository_pull_requests",
                      "fork_delivery_pull_requests",
                      "viewer_upstream_issues",
                      "viewer_upstream_pull_requests"):
            if item_source_counts[field] != expected_sources[field]:
                errors.append(
                    f"{field} retained item count contradicts source evidence")
        if len(registry["upstream"]) != len(registry["tools"]):
            errors.append(
                "round_upstream schema cannot prove per-tool counts for a "
                "shared upstream repository")
        elif (item_source_counts["round_upstream_pull_requests"]
              != expected_sources["round_upstream_pull_requests"]):
            errors.append(
                "round_upstream retained item count contradicts source evidence")

    # Every retained item must itself name a source and scope consistent with
    # the independently derived repository sets.
    allowed_sources = {
        "exact_repository_issues", "exact_repository_pull_requests",
        "fork_delivery_pull_requests", "viewer_upstream",
        "round_upstream_pull_requests",
    }
    allowed_scopes = {
        "registered_fork", "upstream", "vibeic_eda", "fork_delivery",
    }
    for item in items:
        repo = str(item.get("repository", "")).lower()
        sources, scopes = item.get("sources"), item.get("scopes")
        if (not isinstance(sources, list) or not sources
                or any(not isinstance(source, str) for source in sources)
                or len(sources) != len(set(sources))
                or any(source not in allowed_sources for source in sources)):
            errors.append(
                f"item {repo} #{item.get('number')} has malformed sources")
            continue
        if (not isinstance(scopes, list) or not scopes
                or any(not isinstance(scope, str) for scope in scopes)
                or len(scopes) != len(set(scopes))
                or any(scope not in allowed_scopes for scope in scopes)):
            errors.append(
                f"item {repo} #{item.get('number')} has malformed scopes")
            continue
        membership = set()
        if repo in canonical_keys:
            membership.add("registered_fork")
        if repo in upstream_keys:
            membership.add("upstream")
        if repo == VIBEIC_EDA_REPOSITORY:
            membership.add("vibeic_eda")
        if repo == VIBEIC_DELIVERY_REPOSITORY:
            membership.add("fork_delivery")
        if set(scopes) != membership:
            errors.append(
                f"item {repo} #{item.get('number')} scopes contradict repository")
        if "exact_repository_issues" in sources and item.get("type") != "issue":
            errors.append("exact_repository_issues contains a non-issue")
        if ("exact_repository_pull_requests" in sources
                and item.get("type") != "pull_request"):
            errors.append("exact_repository_pull_requests contains a non-PR")
        if "fork_delivery_pull_requests" in sources \
                and "fork_delivery" not in scopes:
            errors.append("fork delivery source lacks fork_delivery scope")
        if "viewer_upstream" in sources and "upstream" not in scopes:
            errors.append("viewer upstream source lacks upstream scope")
        if "round_upstream_pull_requests" in sources \
                and "upstream" not in scopes:
            errors.append("round upstream source lacks upstream scope")

    return (inventory_complete and delivery_complete and actor_complete
            and round_complete)


def _validate_fork_github(record: dict, path: Path, round_at: str,
                          forks_json: Path, configured_actor: str
                          ) -> tuple[Optional[dict], list[str]]:
    """Validate provenance, shape and freshness before retaining the answer."""
    errors: list[str] = []
    registry, registry_errors = _github_registry(forks_json)
    errors.extend(registry_errors)
    observed_now = datetime.now(timezone.utc)
    if record.get("program") != "fork_github_convergence":
        errors.append("program is not fork_github_convergence")
    if (type(record.get("schema_version")) is not int
            or record["schema_version"] != 2):
        errors.append("schema_version is not 2")
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
    configured_identity = (
        configured_actor.casefold()
        if isinstance(configured_actor, str)
        and configured_actor
        and configured_actor.strip() == configured_actor
        and re.fullmatch(r"[A-Za-z0-9-]{1,39}", configured_actor)
        else None
    )
    if configured_identity is None:
        errors.append("configured GitHub actor is malformed")
    if actor is not None and (not isinstance(actor, str) or not actor):
        errors.append("actor is malformed")
    if not isinstance(expected_actor, str) or not expected_actor:
        errors.append("expected_actor is missing or malformed")
    elif (configured_identity is not None
          and expected_actor.casefold() != configured_identity):
        errors.append(
            "expected_actor does not match configured GitHub actor")
    if isinstance(actor, str) and configured_identity is not None \
            and actor.casefold() != configured_identity:
        errors.append("actor does not match configured GitHub actor")
    elif status in {"converged", "open"} and not isinstance(actor, str):
        errors.append("actor is missing for a measured GitHub result")
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
    elif set(counts) != {
            "items", "issues", "pull_requests", "registered_fork_items",
            "upstream_items", "vibeic_eda_items", "fork_delivery_items",
            "by_source",
            # Publish-silence alarms, split out of `items` because a notice is
            # not a debt (see fork_github_convergence). The EXACT set is the
            # point of this check — a producer that grows a key must say so
            # here, which is what caught this one.
            "alarm_items_excluded"}:
        counts_valid = False
        errors.append("counts does not have the exact schema-v2 key set")
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
            title = item.get("title")
            updated_at = _strict_when(item.get("updated_at"))
            author = item.get("author")
            repo_parts = repo.split("/") if isinstance(repo, str) else []
            if (len(repo_parts) != 2 or not all(repo_parts)
                    or any(part.strip() != part for part in repo_parts)
                    or kind not in {"issue", "pull_request"}
                    or type(number) is not int or number <= 0
                    or not isinstance(url, str) or not url
                    or not isinstance(title, str) or not title
                    or updated_at is None
                    or (author is not None
                        and (not isinstance(author, str) or not author))):
                items_valid = False
                errors.append(
                    f"items[{pos}] has no valid normalized identity")
                continue
            if kind == "pull_request":
                if (not isinstance(item.get("head_owner"), str)
                        or not item["head_owner"]
                        or not isinstance(item.get("head_ref"), str)
                        or not item["head_ref"]
                        or not isinstance(item.get("base_ref"), str)
                        or not item["base_ref"]
                        or not isinstance(item.get("draft"), bool)):
                    items_valid = False
                    errors.append(f"items[{pos}] has malformed PR details")
            elif any(item.get(field) is not None for field in
                     ("head_owner", "head_ref", "base_ref", "draft")):
                items_valid = False
                errors.append(f"items[{pos}] issue carries PR-only details")
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

    sources_complete = False
    if registry is not None and items_valid:
        sources_complete = _validate_github_v2_sources(
            record, items, registry, errors)

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
            (status == "converged" and not items and not upstream_errors
             and sources_complete)
            or (status == "open" and bool(items) and not upstream_errors
                and sources_complete)
            or (status == "norecord"
                and (bool(upstream_errors) or not sources_complete))
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


def _registered_fleet(
        path: Path) -> tuple[list[str], Optional[str], list[str]]:
    """Read the canonical fleet independently of the gap producer."""
    errors: list[str] = []
    tools: list[str] = []
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return [], None, [f"could not read registered fleet {path}: "
                          f"{type(exc).__name__}: {exc}"]
    digest = hashlib.sha256(raw).hexdigest()
    entries = value.get("forks") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        return [], digest, [f"registered fleet {path} has no forks list"]
    for pos, entry in enumerate(entries):
        tool = entry.get("tool") if isinstance(entry, dict) else None
        if (not isinstance(tool, str) or not tool or tool.strip() != tool
                or "/" in tool or any(char.isspace() for char in tool)):
            errors.append(
                f"registered fleet forks[{pos}].tool is malformed")
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
    return sorted(tools), digest, errors


def _validate_gap_arithmetic(record: dict, merge_tips: object,
                             errors: list[str]) -> None:
    """Recompute every gap headline from its retained per-fork rows.

    The gap producer intentionally retains both its rows and its aggregates.
    The rows are the evidence; a self-asserted zero in an aggregate is not.
    Keep this implementation aligned with ``fork_gap_report.analyse`` so the
    LL consumer independently catches omitted, unknown, or rewritten work.
    """
    rows = record.get("rows")
    if not isinstance(rows, list):
        return

    numeric_fields = (
        "sync_lag", "sync_lag_at_merge", "release_lag", "image_behind",
        "ours_unshipped", "ours_unshipped_substantive",
    )
    clean_rows: list[dict] = []
    for pos, row in enumerate(rows):
        if not isinstance(row, dict):
            # The denominator check reports this too, but arithmetic must not
            # try to derive a plausible zero from an unusable row.
            errors.append(f"rows[{pos}] is not an object")
            continue
        tool = row.get("tool")
        label = tool if isinstance(tool, str) and tool else f"rows[{pos}]"
        required_fields = {
            "integrated", "ahead", "pin", "pin_disagreement", "kind",
            "asserted_contents", "sync_lag", "sync_lag_at_merge",
            "release_lag", "image_behind", "ours_unshipped",
            "ours_unshipped_substantive", "unshipped_commits", "tip_state",
            "tip_note",
        }
        missing_fields = sorted(required_fields - set(row))
        if missing_fields:
            errors.append(
                f"{label} is missing arithmetic evidence field(s): "
                + ", ".join(missing_fields))
        if type(row.get("integrated")) is not bool:
            errors.append(f"{label}.integrated is not a boolean")
        ahead = row.get("ahead")
        if ahead is not None and (type(ahead) is not int or ahead < 0):
            errors.append(
                f"{label}.ahead is not null or a non-negative integer")

        kind = row.get("kind")
        if kind not in {"pin", "contents_assertion"}:
            errors.append(f"{label}.kind is malformed")
        pin = row.get("pin")
        if pin is not None and (
                not isinstance(pin, str) or not pin
                or pin.strip() != pin):
            errors.append(f"{label}.pin is malformed")
        if row.get("pin_disagreement") is not None:
            errors.append(f"{label}.pin_disagreement is not empty")

        for field in numeric_fields:
            value = row.get(field)
            if value is not None and (type(value) is not int or value < 0):
                errors.append(
                    f"{label}.{field} is not null or a non-negative integer")

        tip_state = row.get("tip_state")
        if tip_state not in {None, "current", "behind", "undetermined",
                             "no_claim"}:
            errors.append(f"{label}.tip_state is malformed")
        tip_note = row.get("tip_note")
        if tip_note is not None and not isinstance(tip_note, str):
            errors.append(f"{label}.tip_note is malformed")

        asserted_contents = row.get("asserted_contents")
        if kind == "contents_assertion":
            if (not isinstance(asserted_contents, str)
                    or not asserted_contents):
                errors.append(f"{label}.asserted_contents is malformed")
            if pin is not None:
                errors.append(f"{label} contents assertion unexpectedly has a pin")
            if (any(row.get(field) is not None for field in numeric_fields)
                    or row.get("unshipped_commits") != []
                    or tip_state is not None or tip_note is not None):
                errors.append(
                    f"{label} contents assertion carries measurement evidence")
        elif asserted_contents is not None:
            errors.append(f"{label} pin row carries asserted_contents")

        commits = row.get("unshipped_commits")
        commits_valid = isinstance(commits, list)
        if not commits_valid:
            errors.append(f"{label}.unshipped_commits is not a list")
            commits = []
        else:
            for commit_pos, commit in enumerate(commits):
                if not isinstance(commit, dict):
                    commits_valid = False
                    errors.append(
                        f"{label}.unshipped_commits[{commit_pos}] is not an object")
                elif commit.get("merge") is not False:
                    commits_valid = False
                    errors.append(
                        f"{label}.unshipped_commits[{commit_pos}] is not a "
                        "substantive non-merge commit")

        ours = row.get("ours_unshipped")
        substantive = row.get("ours_unshipped_substantive")
        if ours is None:
            if substantive is not None or commits:
                errors.append(
                    f"{label} has unshipped detail without a measured total")
        elif type(ours) is int and ours >= 0:
            if type(substantive) is not int or substantive < 0:
                errors.append(
                    f"{label}.ours_unshipped_substantive is not measured")
            else:
                if substantive > ours:
                    errors.append(
                        f"{label}.ours_unshipped_substantive exceeds "
                        "ours_unshipped")
                if commits_valid and substantive != len(commits):
                    errors.append(
                        f"{label}.ours_unshipped_substantive contradicts "
                        "unshipped_commits")

        image_behind = row.get("image_behind")
        if type(image_behind) is int and image_behind >= 0:
            if not isinstance(pin, str) or not pin:
                errors.append(f"{label} measured image lag without a pin")
            for field in ("sync_lag", "sync_lag_at_merge", "release_lag",
                          "ours_unshipped", "ours_unshipped_substantive"):
                value = row.get(field)
                if type(value) is not int or value < 0:
                    errors.append(f"{label}.{field} is not measured")
            merge_ref = row.get("merge_ref_seen")
            expected_tip = (merge_tips.get(tool)
                            if isinstance(merge_tips, dict) else None)
            if merge_ref != expected_tip:
                errors.append(
                    f"{label}.merge_ref_seen does not match merge_tips_seen")

        clean_rows.append(row)

    measurable = [row for row in clean_rows
                  if type(row.get("image_behind")) is int
                  and row["image_behind"] >= 0]
    asserted = [row for row in clean_rows
                if row.get("kind") == "contents_assertion"]
    unmeasured = [row for row in clean_rows
                  if row.get("image_behind") is None
                  and row.get("integrated") is True
                  and row.get("kind") != "contents_assertion"]
    not_built = [row for row in clean_rows
                 if row.get("integrated") is False
                 and row.get("kind") != "contents_assertion"]
    stranded = [row for row in not_built
                if type(row.get("ahead")) is int and row["ahead"] > 0]

    unshipped_commits: list[dict] = []
    for row in clean_rows:
        commits = row.get("unshipped_commits")
        if not isinstance(commits, list):
            continue
        for commit in commits:
            if isinstance(commit, dict):
                unshipped_commits.append(dict(commit, tool=row.get("tool")))

    def sum_field(source_rows: list[dict], field: str) -> int:
        return sum(row.get(field) or 0 for row in source_rows
                   if (row.get(field) is None
                       or type(row.get(field)) is int))

    derived = {
        "assertions": [
            {"tool": row.get("tool"),
             "contents": row.get("asserted_contents")}
            for row in asserted
        ],
        "q1_image_behind_upstream": sum_field(measurable, "image_behind"),
        "q1_forks_behind": sum(bool(row.get("image_behind"))
                               for row in measurable),
        "q1_sync_lag": sum_field(measurable, "sync_lag"),
        "q1_sync_lag_at_merge": sum_field(
            measurable, "sync_lag_at_merge"),
        "q1_unmeasured_at_merge": [
            row.get("tool") for row in measurable
            if row.get("sync_lag_at_merge") is None
        ],
        "q1_release_lag": sum_field(measurable, "release_lag"),
        "q1_unmeasured": [row.get("tool") for row in unmeasured],
        "q2_forks_not_built_from_ours": [
            row.get("tool") for row in not_built
        ],
        "q2_unmeasured_stranded": [
            row.get("tool") for row in not_built
            if (type(row.get("ahead")) is not int
                or row["ahead"] < 0)
        ],
        "q2_our_commits_stranded": sum_field(stranded, "ahead"),
        "q2_ours_past_the_pin": sum_field(clean_rows, "ours_unshipped"),
        "q2_ours_past_the_pin_substantive": sum_field(
            clean_rows, "ours_unshipped_substantive"),
        "q2_unshipped_commits": unshipped_commits,
        "q2_unmeasured_ship": [
            row.get("tool") for row in clean_rows
            if row.get("integrated") is True and row.get("pin")
            and row.get("ours_unshipped") is None
        ],
        "tip_rejected": [
            {"tool": row.get("tool"), "why": row.get("tip_note")}
            for row in clean_rows if row.get("tip_state") == "behind"
        ],
        "tip_unverified": [
            {"tool": row.get("tool"), "why": row.get("tip_note")}
            for row in clean_rows if row.get("tip_state") == "undetermined"
        ],
    }
    for name, expected_value in derived.items():
        if record.get(name) != expected_value:
            errors.append(f"{name} contradicts retained per-fork rows")


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
                 "q2_unmeasured_ship", "q2_unmeasured_stranded"):
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
    expected, _registry_hash, fleet_manifest_errors = _registered_fleet(
        forks_json)
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

    _validate_gap_arithmetic(record, merge_tips, errors)

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
                fork_github: Optional[dict] = None,
                ai_authorization: Optional[dict] = None,
                release_pins: Optional[dict] = None,
                watch_reset: Optional[dict] = None) -> dict:
    release = release or {}
    gap = gap or {}
    attempts = release.get("compose_attempts") or []
    failed_attempts = sum(
        1 for attempt in attempts
        if (isinstance(attempt, dict)
            and type(attempt.get("exit_code")) is int
            and attempt["exit_code"] != 0))
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
    # JSON booleans are Python integers.  A forged ``false`` zero must not turn
    # an unmeasured gap or tick into an exact numeric success.
    measured = all(type(v) is int for v in (sync_lag, image_lag, past_pin))
    round_measured = all(type(v) is int
                         for v in (sync_lag, release_lag, past_pin))
    convergence_metrics_exact = all(type(v) is int for v in (
        sync_lag, live_sync_lag, image_lag, release_lag, past_pin,
        gap.get("q2_ours_past_the_pin_substantive"), stranded,
    ))
    # A release mode that merely wrote RELEASED.json (``recorded``), completed
    # local work without a push (``completed``), or carries only a successful
    # status label is not delivery.  Re-derive the complete terminal proof here
    # even though the production CLI validates the source before calling this
    # pure formatter: direct callers must not be able to manufacture convergence
    # from ``status=published`` or ``exit_code=0`` alone.
    release_terminal_errors = _validate_release_terminal(
        release, round_at=round_at,
        observed_until=datetime.now(timezone.utc),
    )
    terminal_ok = bool(
        status in {"published", "noop"} and not release_terminal_errors)
    # Keep the terminal cause visible even though a failed anchor necessarily
    # prevents the complete-terminal predicate above from authorizing delivery.
    anchor_blocked = bool(status in {"published", "noop"}
                          and anchor_pr is not None
                          and anchor_pr.get("ok") is False)
    # Preserve the original image-delivery arithmetic under explicit names.
    # The generic verdict now means the whole fork programme, including its
    # GitHub issue/PR closure boundary; an absent/open GitHub answer can never
    # inherit a clean-looking delivery zero.
    delivery_converged = bool(
        measured and convergence_metrics_exact and terminal_ok
        and sync_lag == 0 and image_lag == 0 and past_pin == 0)
    delivery_round_converged = bool(
        round_measured and convergence_metrics_exact and terminal_ok
        and sync_lag == 0
        and release_lag == 0 and past_pin == 0)
    fork_github_status = (fork_github.get("status")
                          if isinstance(fork_github, dict) else None)
    github_converged = fork_github_status == "converged"
    # ``None`` keeps the pure helper backwards-compatible for callers that are
    # testing unrelated LL arithmetic. Production always passes a validated
    # sidecar (including an explicit ``norecord`` on read failure).
    ai_status = (ai_authorization.get("status")
                 if isinstance(ai_authorization, dict) else None)
    ai_authorized = ai_authorization is None or ai_status == "authorized"
    release_pins_verdict = (release_pins.get("verdict")
                            if isinstance(release_pins, dict) else None)
    # ``None`` preserves the pure helper's historical semantics. Production
    # always supplies either this round's validated verdict or explicit
    # NOT_CHECKED evidence when the release-capable tick did not run.
    release_pins_clear = (release_pins is None
                          or release_pins_verdict == "PASS")
    watch_reset_status = (watch_reset.get("status")
                          if isinstance(watch_reset, dict) else None)
    # None preserves helper compatibility. Production supplies independently
    # validated current-round reset/noop/refusal evidence.
    watch_reset_clear = watch_reset is None or watch_reset_status == "pass"
    inputs_authoritative = not source_errors
    tick_authorized = (tick_state == "ran" and type(tick_exit) is int
                       and tick_exit == 0)
    tick_ran_unsuccessfully = tick_state == "ran" and not tick_authorized
    whole_round_clear = bool(
        tick_authorized and not anchor_blocked
        and ai_authorized and release_pins_clear and watch_reset_clear
        and type(stranded) is int and stranded == 0)
    converged = bool(delivery_converged and github_converged
                     and inputs_authoritative and whole_round_clear)
    round_converged = bool(delivery_round_converged and github_converged
                           and inputs_authoritative and whole_round_clear)

    if watch_reset_status in {"blocked", "norecord"}:
        outcome = f"watch_only_reset_{watch_reset_status}"
    elif source_errors:
        outcome = "could_not_measure_inputs"
    elif ai_status == "blocked" and status == "not_checked":
        outcome = "release_blocked_by_unverified_ai_decision"
    elif ai_status == "blocked":
        outcome = "finished_without_ai_authorization"
    elif anchor_blocked:
        # Publishing and advancing the consumer's immutable anchor are two
        # delivery hops.  Keep the image convergence arithmetic intact, but do
        # not let a successful registry push erase a failed second hop from LL.
        outcome = "published_with_anchor_pr_blocked"
    elif terminal_ok and tick_ran_unsuccessfully:
        outcome = "finished_with_nonzero_release_tick"
    elif terminal_ok and release_pins_verdict == "OUTSTANDING":
        outcome = "published_with_uncommitted_release_pins"
    elif terminal_ok and fork_github_status == "open":
        outcome = "finished_with_open_fork_work"
    elif terminal_ok and failed_attempts:
        outcome = "self_healed_after_retry"
    elif status == "recorded":
        outcome = "release_record_only"
    elif status == "failed" and failure.get("retryable"):
        outcome = "blocked_after_bounded_retries"
    elif status == "failed":
        outcome = "blocked_non_transient"
    elif status == "running":
        outcome = "interrupted_without_terminal_verdict"
    elif status == "not_checked":
        outcome = "release_not_checked"
    elif tick_ran_unsuccessfully:
        outcome = "finished_with_nonzero_release_tick"
    elif converged:
        outcome = "converged"
    elif (round_converged and type(live_sync_lag) is int
          and live_sync_lag > 0):
        outcome = "converged_at_boundary_with_late_upstream_drift"
    elif not measured:
        outcome = "could_not_measure_convergence"
    else:
        outcome = "finished_with_gap"

    selftest = _selftest_summary(semantic_pytest)
    category = (failure.get("category")
                or ("anchor_pr" if anchor_blocked else None))
    if watch_reset_status in {"blocked", "norecord"}:
        category = f"watch_only_reset_{watch_reset_status}"
    elif ai_status == "blocked" and category is None:
        category = "ai_postcondition_unverified"
    elif ai_status == "norecord" and category is None:
        category = "ai_authorization_norecord"
    elif status == "recorded" and category is None:
        category = "release_record_only"
    if tick_ran_unsuccessfully and category is None:
        category = "release_tick_nonzero"
    elif (terminal_ok and release_pins_verdict == "OUTSTANDING"
          and category is None):
        category = "release_pins_uncommitted"
    if (tick_state != "ran"
            and watch_reset_status not in {"blocked", "norecord"}):
        if tick_state == "not_checked_stale_round_code":
            category = "stale_round_code"
        elif tick_state == "not_checked_round_code_norecord":
            category = "round_code_norecord"
        elif tick_state == "not_checked_ai_unverified":
            category = "ai_postcondition_unverified"
        elif tick_state == "not_checked_ai_norecord":
            category = "ai_authorization_norecord"
        elif selftest and selftest["status"] == "red":
            category = "pytest_red"
        elif selftest and selftest["status"] == "norecord":
            category = ("pytest_stalled" if selftest["outcome"] == "stalled"
                        else "pytest_norecord")
        else:
            category = "release_tick_not_checked"
    control = None
    if tick_state != "ran":
        if tick_state.startswith("not_checked_ai_"):
            control = (
                "do not invoke the release tick until every AI conflict has "
                "a current verified published-mainline postcondition")
        else:
            control = (
                "do not invoke the release tick without current code and a "
                "complete green semantic pytest record")
    elif status == "recorded":
        control = (
            "retain the release record as bookkeeping only; require a canonical "
            "published or noop terminal with complete remote, smoke, record and "
            "anchor proof before declaring delivery")
    elif anchor_blocked:
        control = (
            "retain the published image; refuse to bypass the anchor "
            "repository push gate")
    elif terminal_ok and release_pins_verdict == "OUTSTANDING":
        control = (
            "review and commit only the named pin files by explicit path; "
            "do not let a fresh clone describe different sources from the "
            "published release")
    elif failed_attempts:
        control = (
            "classified bounded retry; reuse pushed artefacts and BuildKit "
            "cache")
    elif attempts:
        control = "single compose attempt; no retry needed"
    elif failure:
        control = "fail closed; publish no version and retain the terminal cause"

    # Bind the retained certificate to one immutable daily_0530 nonce and one
    # fleet denominator.  Prior LL records carried the nonce only inside several
    # nested sources; a consumer could therefore read a forged top-level green
    # while never proving that gap, GitHub, AI and release pins named one round.
    run_ids = []
    for value in (
        ((gap.get("source") or {}).get("_round") or {}).get("run_id"),
        ((fork_github or {}).get("round") or {}).get("run_id"),
        ((ai_authorization or {}).get("round") or {}).get("run_id"),
        (release_pins or {}).get("run_id"),
        (watch_reset or {}).get("run_id"),
    ):
        if isinstance(value, str) and value:
            run_ids.append(value)
    run_id = run_ids[0] if run_ids and len(set(run_ids)) == 1 else None
    registered_forks = gap.get("registered_forks")
    if not isinstance(registered_forks, list):
        registered_forks = []

    return {
        "program": "release_ll",
        "schema_version": 3,
        "round_at": round_at,
        "run_id": run_id,
        "registered_forks": list(registered_forks),
        "registered_count": len(registered_forks),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "release": {
            "program": release.get("program"),
            "schema_version": release.get("schema_version"),
            "started_at": release.get("started_at"),
            "finished_at": release.get("finished_at"),
            "status": status,
            "noop": status == "noop",
            "target": _target(release),
            "exit_code": release.get("exit_code"),
            "version": (dict(release["version"])
                        if isinstance(release.get("version"), dict) else None),
            "version_candidate": (
                dict(release["version_candidate"])
                if isinstance(release.get("version_candidate"), dict) else None),
            "moved_tools": [r.get("repo") for r in release.get("moved", [])
                            if isinstance(r, dict) and r.get("repo")],
            "built_tools": list(release.get("built") or []),
            "compose_attempts": attempts,
            "registry_composed": release.get("registry_composed"),
            "pushed": release.get("pushed"),
            "smoke": (dict(release["smoke"])
                      if isinstance(release.get("smoke"), dict) else None),
            "remote_publication": (
                dict(release["remote_publication"])
                if isinstance(release.get("remote_publication"), dict)
                else None),
            "latest_publication": (
                dict(release["latest_publication"])
                if isinstance(release.get("latest_publication"), dict)
                else None),
            "release_record_publication": (
                dict(release["release_record_publication"])
                if isinstance(release.get("release_record_publication"), dict)
                else None),
            "failure": failure or None,
            "anchor_pr": anchor_pr,
            "tick_exit": tick_exit,
            "tick_state": tick_state,
        },
        "selftest": selftest,
        "ai_authorization": ai_authorization,
        "watch_only_reset": watch_reset,
        "release_pins": release_pins,
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
            "ai_authorized": ai_authorized,
            "release_pins_committed": (
                True if release_pins_verdict == "PASS" else
                False if release_pins_verdict == "OUTSTANDING" else None),
            "watch_only_reset_clear": watch_reset_clear,
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
        "run_id": record.get("run_id"),
        "registered_count": record.get("registered_count"),
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
        # Retain the exact normalized postconditions, not only a boolean. A
        # later clean round must not erase which source tip was still pending.
        "ai_authorization": record.get("ai_authorization"),
        # Keep exact old/target/recovery refs and the CAS result in the
        # append-only index; a later noop must not erase what this round reset.
        "watch_only_reset": record.get("watch_only_reset"),
        # Same retention rule for the post-release source declaration: keep
        # every named tool and both abbreviated SHAs, not merely a boolean a
        # later PASS could erase.
        "release_pins": record.get("release_pins"),
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
        "ai_authorized": record["convergence"]["ai_authorized"],
        "release_pins_committed": record["convergence"][
            "release_pins_committed"],
        "watch_only_reset_clear": record["convergence"][
            "watch_only_reset_clear"],
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
            "inventory": fork_github.get("inventory"),
            "delivery": fork_github.get("delivery"),
            "upstream_actor": fork_github.get("upstream_actor"),
            "round_upstream": fork_github.get("round_upstream"),
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
    ap.add_argument("--eda-root", type=Path, default=EDA_ROOT,
                    help="checkout whose immutable HEAD the pin checker must "
                         "have measured")
    ap.add_argument("--release-json", type=Path, required=True)
    ap.add_argument("--release-pins-json", type=Path, required=True)
    ap.add_argument("--gap-json", type=Path, required=True)
    ap.add_argument("--fork-github-json", type=Path, required=True)
    ap.add_argument("--ai-authorization-json", type=Path, required=True)
    ap.add_argument("--watch-reset-json", type=Path,
                    help="daily_0530.json carrying current-round reset evidence")
    ap.add_argument(
        "--expected-github-actor", required=True,
        help="independently configured GitHub login that both actor fields "
             "in the convergence sidecar must match")
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
        "not_checked_ai_unverified", "not_checked_ai_norecord",
    ))
    a = ap.parse_args(argv)
    observed_now = datetime.now(timezone.utc)

    reset_registry = watch_only_reset.load_registry(a.forks_json)
    if reset_registry.get("errors"):
        watch_reset, watch_reset_errors = None, [
            "watch-reset registry is invalid: "
            + "; ".join(reset_registry["errors"])]
    elif a.watch_reset_json is None and reset_registry.get("policies"):
        watch_reset, watch_reset_errors = None, [
            "--watch-reset-json is required because FORKS.json declares "
            "watch-only reset policy"]
    elif a.watch_reset_json is None:
        watch_reset, watch_reset_errors = None, []
    else:
        try:
            watch_raw = watch_only_reset.read_json(a.watch_reset_json)
        except (OSError, ValueError) as exc:
            watch_reset, watch_reset_errors = None, [
                f"strict watch-reset source read failed: "
                f"{type(exc).__name__}: {exc}"]
        else:
            watch_reset, watch_reset_errors = (
                watch_only_reset.validate_round_evidence(
                    watch_raw, round_at=a.round_at,
                    forks_json=a.forks_json, now=observed_now))

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
        release_pins_raw, release_pins_read_err = _read(
            a.release_pins_json)
    else:
        release = {"program": "daily_release", "status": "not_checked",
                   "not_checked_reason": a.tick_state}
        release_err = None
        release_pins_raw = None
        release_pins_read_err = None
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
            fork_github_raw, a.fork_github_json, a.round_at, a.forks_json,
            a.expected_github_actor)
    ai_raw, ai_read_err = _read(a.ai_authorization_json)
    if ai_read_err is not None:
        ai_authorization = {
            "program": AI_AUTHORIZATION_PROGRAM,
            "schema_version": AI_AUTHORIZATION_SCHEMA_VERSION,
            "status": "norecord",
            "round_at": a.round_at,
            "source_errors": [ai_read_err],
            "reasons": [ai_read_err],
            "case_results": [],
        }
        ai_errors = [ai_read_err]
    else:
        assert ai_raw is not None
        ai_authorization, ai_errors = _validate_ai_authorization(
            ai_raw, a.ai_authorization_json, a.round_at, a.forks_json)
        if ai_authorization is None:
            ai_authorization = {
                "program": AI_AUTHORIZATION_PROGRAM,
                "schema_version": AI_AUTHORIZATION_SCHEMA_VERSION,
                "status": "norecord",
                "round_at": a.round_at,
                "source_errors": list(ai_errors),
                "reasons": list(ai_errors),
                "case_results": [],
            }
    provenance_errors: list[str] = []
    if gap is not None and fork_github is not None:
        provenance_errors.extend(_round_provenance_errors(
            gap["source"]["_round"], fork_github["round"],
            left_name="fork_gap.json",
            right_name="fork-github-convergence.json"))
    if (gap is not None and isinstance(ai_authorization.get("round"), dict)):
        provenance_errors.extend(_round_provenance_errors(
            gap["source"]["_round"], ai_authorization["round"],
            left_name="fork_gap.json",
            right_name="ai-release-authorization.json"))
    if (fork_github is not None
            and isinstance(ai_authorization.get("round"), dict)):
        provenance_errors.extend(_round_provenance_errors(
            fork_github["round"], ai_authorization["round"],
            left_name="fork-github-convergence.json",
            right_name="ai-release-authorization.json"))
    if watch_reset is not None:
        watch_run_id = watch_reset.get("run_id")
        if gap is not None and watch_run_id != gap["source"]["_round"]["run_id"]:
            provenance_errors.append(
                "watch-reset evidence and fork_gap.json name different "
                "daily_0530 run_id values")
        if (isinstance(ai_authorization.get("round"), dict)
                and watch_run_id != ai_authorization["round"].get("run_id")):
            provenance_errors.append(
                "watch-reset evidence and ai-release-authorization.json name "
                "different daily_0530 run_id values")
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
        if release_err is None and release is not None:
            terminal_errors = _validate_release_terminal(
                release, round_at=a.round_at, observed_until=observed_now)
            if terminal_errors:
                release_err = (
                    "daily-release.json terminal proof is invalid: "
                    + "; ".join(terminal_errors))
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
        if semantic_err is None and semantic is not None:
            semantic_errors = _validate_selftest(
                semantic, round_at=a.round_at, observed_until=observed_now)
            if semantic_errors:
                semantic_err = (
                    f"{a.semantic_pytest_json.name} terminal proof is invalid: "
                    + "; ".join(semantic_errors))
                semantic = None
    if (semantic_err is None and semantic is not None
            and a.tick_state == "ran" and semantic.get("exit_code") != 0):
        semantic_err = (
            "release tick ran without a complete green semantic pytest "
            "record; cron gate wiring is inconsistent")

    if a.tick_state == "ran":
        expected_revision, revision_err = _current_head(a.eda_root)
        if release_pins_read_err is not None or revision_err is not None:
            release_pins = None
            release_pins_errors = [
                error for error in (release_pins_read_err, revision_err)
                if error is not None
            ]
        else:
            assert release_pins_raw is not None
            assert expected_revision is not None
            expected_run_id = None
            if gap is not None:
                expected_run_id = gap["source"]["_round"]["run_id"]
            release_status = ((release or {}).get("status")
                              if isinstance(release, dict) else None)
            release_target = (_target(release or {})
                              if release_status in {"published", "recorded"}
                              else None)
            release_pins, release_pins_errors = _validate_release_pins(
                release_pins_raw, a.release_pins_json, a.round_at,
                expected_run_id, release_target, expected_revision)
    else:
        expected_run_id = None
        if gap is not None:
            expected_run_id = gap["source"]["_round"]["run_id"]
        release_pins = {
            "program": RELEASE_PINS_PROGRAM,
            "schema_version": RELEASE_PINS_SCHEMA_VERSION,
            "checked_at": None,
            "round_at": a.round_at,
            "run_id": expected_run_id,
            "verdict": "NOT_CHECKED",
            "committed": None,
            "version": None,
            "requested_revision": None,
            "revision": None,
            "recorded_count": None,
            "committed_count": None,
            "outstanding": [],
            "findings": [],
            "reason": a.tick_state,
        }
        release_pins_errors = []
    ordering_errors: list[str] = []
    ai_checked = (_strict_when(ai_authorization.get("checked_at"))
                  if isinstance(ai_authorization, dict) else None)
    semantic_started = (_strict_when(semantic.get("started_at"))
                        if isinstance(semantic, dict) else None)
    semantic_elapsed = (semantic.get("elapsed_seconds")
                        if isinstance(semantic, dict) else None)
    release_started = (_strict_when(release.get("started_at"))
                       if isinstance(release, dict) else None)
    release_finished = (_strict_when(release.get("finished_at"))
                        if isinstance(release, dict) else None)
    pins_checked = (_strict_when(release_pins.get("checked_at"))
                    if isinstance(release_pins, dict) else None)
    if (ai_checked is not None and semantic_started is not None
            and ai_checked > semantic_started):
        ordering_errors.append(
            "semantic pytest started before AI authorization finished")
    if (semantic_started is not None
            and isinstance(semantic_elapsed, (int, float))
            and not isinstance(semantic_elapsed, bool)
            and math.isfinite(float(semantic_elapsed))
            and release_started is not None
            and semantic_started + timedelta(
                seconds=float(semantic_elapsed)) > release_started):
        ordering_errors.append(
            "daily release started before semantic pytest finished")
    if (release_finished is not None and pins_checked is not None
            and release_finished > pins_checked):
        ordering_errors.append(
            "release pins were checked before the release finished")
    if gap is not None and fork_github is not None:
        gap_completed = _strict_when(gap.get("completed_at"))
        github_started = _strict_when(fork_github.get("started_at"))
        if (gap_completed is not None and github_started is not None
                and gap_completed > github_started):
            ordering_errors.append(
                "GitHub convergence started before fork-gap collection finished")
    errors = [e for e in (release_err, semantic_err) if e]
    errors.extend(release_pins_errors)
    errors.extend(gap_errors)
    errors.extend(fork_github_errors)
    errors.extend(ai_errors)
    errors.extend(watch_reset_errors)
    errors.extend(provenance_errors)
    errors.extend(ordering_errors)
    record = make_record(
        release, gap, a.round_at, a.tick_exit, errors,
        semantic_pytest=semantic, tick_state=a.tick_state,
        fork_github=fork_github, ai_authorization=ai_authorization,
        release_pins=release_pins,
        watch_reset=watch_reset,
    )
    written = retain(record, a.state_dir)
    print("release LL: "
          f"{record['lesson']['outcome']} · "
          f"image lag {record['convergence']['image_behind_upstream']} · "
          f"ours past pin {record['convergence']['ours_past_pin']} · "
          f"fork GitHub "
          f"{(record.get('fork_github') or {}).get('status', 'unmeasured')} · "
          f"AI authorization "
          f"{(record.get('ai_authorization') or {}).get('status', 'unmeasured')} · "
          f"retained {written['stamp']}")
    if errors:
        for err in errors:
            print(f"  source error: {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
