#!/usr/bin/env python3
"""Opt-in, recoverable reset of a watch-only mirror to authoritative upstream.

This is deliberately narrower than the ordinary fork merger.  A repository is
eligible only when its ``FORKS.json`` row explicitly selects the one supported
policy.  Before the published branch is changed, its exact old object is made
reachable through a deterministic remote tag.  The branch update itself is a
compare-and-swap protected by that same old object id.

The returned record is verdict evidence, not a log summary.  Consumers validate
it again against the registry and the enclosing 05:30 round before allowing it
to supersede an older ledger observation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import progress_watchdog as watchdog


PROGRAM = "watch_only_reset_round"
SCHEMA_VERSION = 1
POLICY_KEY = "watch_only_reset"
MODE = "reset_to_authoritative_upstream"
PRESERVE = "deterministic_remote_tag"
RECOVERY_PREFIX = "refs/tags/vibeic-watch-reset"
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{32,256}")
_NAME = re.compile(r"[A-Za-z0-9._+-]+")
_BRANCH = re.compile(r"(?!/)(?!.*(?:\.\.|//|@\{|\\))[A-Za-z0-9._/+\-]+(?<![/.])")
LS_REMOTE_TIMEOUT_S = 120
GIT_STALL_GRACE_S = float(os.environ.get("GK_GIT_STALL_GRACE", "300"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number {value!r}")


def read_json(path: Path) -> object:
    return json.loads(
        Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=_json_object, parse_constant=_reject_json_constant)


def _parse_when(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _first_error(cp: subprocess.CompletedProcess) -> str:
    for line in ((cp.stderr or "") + "\n" + (cp.stdout or "")).splitlines():
        if line.strip():
            return line.strip()[:300]
    return "no error text"


def _git(repo: Path, *args: str, progress: bool = False,
         timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    command = ["git", "-C", str(repo), *args]
    if progress:
        run = watchdog.run_supervised(
            command, stall_grace_s=GIT_STALL_GRACE_S,
            output_progress=True, process_tree_cpu_progress=True)
        return subprocess.CompletedProcess(command, run.rc, run.out, run.err)
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 124, "",
                                           f"{type(exc).__name__}: {exc}")


def _output(repo: Path, *args: str) -> Optional[str]:
    cp = _git(repo, *args)
    value = (cp.stdout or "").strip()
    return value if cp.returncode == 0 and value else None


def _canonical_policy(entry: dict) -> tuple[Optional[dict], list[str]]:
    tool = entry.get("tool")
    policy = entry.get(POLICY_KEY)
    errors: list[str] = []
    if policy is None:
        return None, []
    if not isinstance(policy, dict):
        return None, [f"{tool or '?'}: {POLICY_KEY} is not an object"]
    if set(policy) != {"mode", "preserve"}:
        errors.append(
            f"{tool or '?'}: {POLICY_KEY} keys must be exactly mode,preserve")
    if policy.get("mode") != MODE:
        errors.append(f"{tool or '?'}: unsupported watch-only reset mode")
    if policy.get("preserve") != PRESERVE:
        errors.append(f"{tool or '?'}: unsupported watch-only preservation mode")
    if not isinstance(tool, str) or not _NAME.fullmatch(tool):
        errors.append("watch-only row has an invalid tool name")
    branch = entry.get("branch")
    if not isinstance(branch, str) or not _BRANCH.fullmatch(branch):
        errors.append(f"{tool or '?'}: watch-only branch is invalid")
    upstream = entry.get("upstream")
    if (not isinstance(upstream, str) or upstream.count("/") != 1
            or any(not _NAME.fullmatch(part) for part in upstream.split("/"))):
        errors.append(f"{tool or '?'}: authoritative upstream repository is invalid")
    reason = entry.get("not_built_from")
    if not isinstance(reason, str) or len(reason.strip()) < 40:
        errors.append(
            f"{tool or '?'}: reset policy requires a substantive not_built_from reason")
    if errors:
        return None, errors
    return {
        "tool": tool,
        "branch": branch,
        "upstream": upstream,
        "mode": MODE,
        "preserve": PRESERVE,
        # The digest must bind the product decision which authorizes dropping
        # mirror-only commits, not merely the mechanical reset spelling.
        "reason": reason.strip(),
    }, []


def load_registry(path: Path) -> dict:
    """Return normalized policies plus fail-closed parse errors."""
    try:
        raw = read_json(path)
    except (OSError, ValueError) as exc:
        return {"policies": {}, "invalid_tools": {},
                "errors": [f"could not read registry: {type(exc).__name__}: {exc}"],
                "digest": None}
    rows = raw.get("forks") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {"policies": {}, "invalid_tools": {},
                "errors": ["registry forks is not a list"], "digest": None}
    policies: dict[str, dict] = {}
    invalid: dict[str, list[str]] = {}
    errors: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            errors.append("registry contains a non-object fork row")
            continue
        tool = row.get("tool")
        if not isinstance(tool, str) or not tool:
            errors.append("registry contains a fork row without a tool")
            continue
        if tool in seen:
            errors.append(f"registry contains duplicate tool {tool}")
            continue
        seen.add(tool)
        normalized, row_errors = _canonical_policy(row)
        if row_errors:
            invalid[tool] = row_errors
            errors.extend(row_errors)
        elif normalized is not None:
            policies[tool] = normalized
    canonical = json.dumps(
        [policies[name] for name in sorted(policies)],
        sort_keys=True, separators=(",", ":")).encode()
    return {"policies": policies, "invalid_tools": invalid,
            "errors": errors,
            "digest": hashlib.sha256(canonical).hexdigest()}


def _github_slug(url: str) -> Optional[str]:
    value = url.strip()
    match = re.fullmatch(
        r"(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)"
        r"([^/]+/[^/]+?)(?:\.git)?", value, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _remote_head(repo: Path, remote: str, branch: str
                 ) -> tuple[Optional[str], Optional[str]]:
    ref = f"refs/heads/{branch}"
    cp = _git(repo, "ls-remote", "--heads", remote, ref,
              timeout=LS_REMOTE_TIMEOUT_S)
    if cp.returncode != 0:
        return None, f"ls-remote {remote} failed: {_first_error(cp)}"
    rows = [line.split() for line in (cp.stdout or "").splitlines()
            if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref \
            or not _SHA.fullmatch(rows[0][0].lower()):
        return None, f"{remote} did not return exactly one valid {ref}"
    return rows[0][0].lower(), None


def _remote_ref(repo: Path, remote: str, ref: str
                ) -> tuple[Optional[str], Optional[str], bool]:
    cp = _git(repo, "ls-remote", "--refs", remote, ref,
              timeout=LS_REMOTE_TIMEOUT_S)
    if cp.returncode != 0:
        return None, f"ls-remote {remote} {ref} failed: {_first_error(cp)}", False
    rows = [line.split() for line in (cp.stdout or "").splitlines()
            if line.strip()]
    if not rows:
        return None, None, True
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref \
            or not _SHA.fullmatch(rows[0][0].lower()):
        return None, f"{remote} returned malformed or ambiguous {ref}", False
    return rows[0][0].lower(), None, True


def _fetch_branch(repo: Path, remote: str, branch: str, expected: str
                  ) -> Optional[str]:
    # Do not read FETCH_HEAD: it is process-shared mutable state.  Fetch the
    # advertised ref's objects without writing FETCH_HEAD or any destination
    # ref, then address the expected immutable object directly.
    cp = _git(repo, "fetch", "--no-tags", "--no-write-fetch-head",
              "--progress", remote, expected, progress=True)
    if cp.returncode != 0:
        return f"fetch {remote}/{branch} failed: {_first_error(cp)}"
    present = _git(repo, "cat-file", "-e", f"{expected}^{{commit}}")
    if present.returncode != 0:
        return (f"fetch {remote}/{branch} did not make measured object "
                f"{expected} available")
    return None


def _measure_without_mutation(origin_url: str, upstream_url: str,
                              old: str, target: str
                              ) -> tuple[Optional[tuple[int, int]], Optional[str]]:
    """Measure divergence in a disposable object database for a true dry run."""
    with tempfile.TemporaryDirectory(prefix="vibeic-watch-reset-") as raw:
        probe = Path(raw)
        initialized = _git(probe, "init", "--bare")
        if initialized.returncode != 0:
            return None, f"dry-run probe init failed: {_first_error(initialized)}"
        for label, url, sha in (("origin", origin_url, old),
                                ("upstream", upstream_url, target)):
            fetched = _git(
                probe, "fetch", "--no-tags", "--no-write-fetch-head",
                "--progress", url, sha, progress=True)
            if fetched.returncode != 0:
                return None, (f"dry-run probe fetch {label} failed: "
                              f"{_first_error(fetched)}")
        relation = _git(probe, "rev-list", "--left-right", "--count",
                        f"{target}...{old}")
        parts = (relation.stdout or "").split()
        if (relation.returncode != 0 or len(parts) != 2
                or not all(part.isdigit() for part in parts)):
            return None, "dry-run probe could not measure divergence"
        behind, ahead = map(int, parts)
        return (behind, ahead), None


def _blocked(result: dict, status: str, reason: str) -> dict:
    result.update(status=status, outcome=status, reason=reason,
                  finished_at=_now())
    return result


def refusal_record(policy: dict, *, apply: bool, round_at: str, run_id: str,
                   status: str, reason: str) -> dict:
    """Structured non-mutating result for a prerequisite refused by caller."""
    result = {
        "program": "watch_only_reset", "schema_version": SCHEMA_VERSION,
        "tool": policy.get("tool"), "round_at": round_at, "run_id": run_id,
        "started_at": _now(), "finished_at": None, "applied": bool(apply),
        "status": "norecord", "outcome": "norecord", "reason": None,
        "branch": policy.get("branch"),
        "upstream_repository": policy.get("upstream"),
        "policy": {"mode": policy.get("mode"),
                   "preserve": policy.get("preserve")},
        "old_tip": None, "upstream_tip": None, "local_old_tip": None,
        "ahead": None, "behind": None,
        "recovery_ref": None, "recovery_tip": None,
        "recovery_action": None, "remote_tip_after": None,
        "upstream_tip_after": None, "local_tip_after": None,
        "branch_cas_guard": None,
        "cas": {"applied": False, "expected_old": None,
                "target": None, "verified": False},
    }
    return _blocked(result, status, reason)


def apply_policy(repo: Path, policy: Optional[dict], *, apply: bool,
                 round_at: str, run_id: str,
                 allow_non_github_remotes: bool = False,
                 destructive_guard: Optional[
                     Callable[[], tuple[bool, dict, str]]] = None) -> dict:
    """Measure and, when authorized, reset one mirror with two remote leases."""
    started = _now()
    tool = policy.get("tool") if isinstance(policy, dict) else None
    result: dict[str, Any] = {
        "program": "watch_only_reset", "schema_version": SCHEMA_VERSION,
        "tool": tool, "round_at": round_at, "run_id": run_id,
        "started_at": started, "finished_at": None, "applied": bool(apply),
        "status": "norecord", "outcome": "norecord", "reason": None,
        "branch": policy.get("branch") if isinstance(policy, dict) else None,
        "upstream_repository": (
            policy.get("upstream") if isinstance(policy, dict) else None),
        "policy": ({"mode": policy.get("mode"),
                    "preserve": policy.get("preserve")}
                   if isinstance(policy, dict) else None),
        "old_tip": None, "upstream_tip": None, "local_old_tip": None,
        "ahead": None, "behind": None,
        "recovery_ref": None, "recovery_tip": None,
        "recovery_action": None, "remote_tip_after": None,
        "upstream_tip_after": None, "local_tip_after": None,
        "branch_cas_guard": None,
        "cas": {"applied": False, "expected_old": None,
                "target": None, "verified": False},
    }
    if (not isinstance(policy, dict) or policy.get("mode") != MODE
            or policy.get("preserve") != PRESERVE):
        return _blocked(result, "blocked", "no valid opt-in watch-only reset policy")
    branch = policy["branch"]
    old, error = _remote_head(repo, "origin", branch)
    if error:
        return _blocked(result, "norecord", error)
    target, error = _remote_head(repo, "upstream", branch)
    if error:
        return _blocked(result, "norecord", error)
    assert old is not None and target is not None
    result.update(old_tip=old, upstream_tip=target)
    result["cas"].update(expected_old=old, target=target)

    urls: dict[str, str] = {}
    for remote, expected_slug in (("origin", f"vibeic/{tool}"),
                                  ("upstream", policy["upstream"])):
        url = _output(repo, "remote", "get-url", remote)
        if not url:
            return _blocked(result, "norecord", f"{remote} remote has no URL")
        slug = _github_slug(url)
        if slug is None and not allow_non_github_remotes:
            return _blocked(
                result, "blocked",
                f"{remote} URL is not an exact GitHub repository identity")
        if slug is not None and slug.lower() != expected_slug.lower():
            return _blocked(
                result, "blocked",
                f"{remote} URL names {slug}, expected {expected_slug}")
        urls[remote] = url

    if apply:
        for remote, expected in (("origin", old), ("upstream", target)):
            error = _fetch_branch(repo, remote, branch, expected)
            if error:
                return _blocked(result, "norecord", error)

    local = _output(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=all",
                 "--ignored=matching", "--ignore-submodules=all")
    if dirty.returncode != 0:
        return _blocked(result, "norecord", f"worktree status failed: {_first_error(dirty)}")
    if (dirty.stdout or "").strip():
        return _blocked(
            result, "blocked",
            "worktree has tracked, untracked, or ignored content; reset refused")

    if local != old:
        # A prior remote CAS can succeed just before the local ref/worktree
        # update fails.  Repair that partial success only when the arbitrary
        # local tip is itself preserved by the exact deterministic remote tag.
        if old != target or local is None:
            return _blocked(
                result, "blocked",
                f"local {branch} is {local or 'unreadable'}, not measured origin {old}")
        recovery_ref = f"{RECOVERY_PREFIX}/{local}"
        preserved, error, measured = _remote_ref(repo, "origin", recovery_ref)
        if not measured or error:
            return _blocked(result, "norecord", error or "local recovery ref unmeasured")
        if preserved != local:
            return _blocked(
                result, "blocked",
                "aligned remote has a different local tip without its exact "
                "deterministic recovery tag; local repair refused")
        result.update(local_old_tip=local, recovery_ref=recovery_ref,
                      recovery_tip=preserved, recovery_action="reused",
                      behind=0, ahead=0, remote_tip_after=old,
                      upstream_tip_after=target)
        if not apply:
            result.update(status="dry_run", outcome="would_repair_local",
                          reason=None, local_tip_after=local,
                          finished_at=_now())
            return result
        updated = _git(repo, "update-ref", f"refs/heads/{branch}", target, local)
        if updated.returncode != 0:
            return _blocked(
                result, "blocked", f"local repair CAS failed: {_first_error(updated)}")
        current = _output(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        if current == branch:
            reset = _git(repo, "reset", "--hard", target, progress=True)
            if reset.returncode != 0:
                return _blocked(
                    result, "blocked", f"local repair reset failed: {_first_error(reset)}")
        origin_after, oerr = _remote_head(repo, "origin", branch)
        upstream_after, uerr = _remote_head(repo, "upstream", branch)
        tag_after, terr, tag_measured = _remote_ref(repo, "origin", recovery_ref)
        local_after = _output(repo, "rev-parse", "--verify",
                              f"refs/heads/{branch}^{{commit}}")
        if oerr or uerr or terr or not tag_measured:
            return _blocked(result, "norecord", oerr or uerr or terr
                            or "local repair postcondition unmeasured")
        result.update(remote_tip_after=origin_after,
                      upstream_tip_after=upstream_after,
                      recovery_tip=tag_after, local_tip_after=local_after)
        if (origin_after != target or upstream_after != target
                or tag_after != local or local_after != target):
            return _blocked(result, "blocked", "local repair postcondition failed")
        result.update(status="pass", outcome="repaired_local", reason=None,
                      finished_at=_now())
        result["cas"]["verified"] = True
        return result

    if not apply and old != target:
        measured, error = _measure_without_mutation(
            urls["origin"], urls["upstream"], old, target)
        if error or measured is None:
            return _blocked(result, "norecord", error or "dry-run measurement failed")
        result["behind"], result["ahead"] = measured
    else:
        relation = _git(repo, "rev-list", "--left-right", "--count",
                        f"{target}...{old}")
        parts = (relation.stdout or "").split()
        if (relation.returncode != 0 or len(parts) != 2
                or not all(p.isdigit() for p in parts)):
            return _blocked(
                result, "norecord", "could not measure fork/upstream divergence")
        # target-only is how far our mirror is behind; old-only is what reset drops.
        result["behind"], result["ahead"] = map(int, parts)

    if old == target:
        # Re-ask both remotes.  An old tracking ref is not current evidence.
        after, aerr = _remote_head(repo, "origin", branch)
        upstream_after, uerr = _remote_head(repo, "upstream", branch)
        if aerr or uerr or after != target or upstream_after != target:
            return _blocked(
                result, "norecord",
                aerr or uerr or "remote moved during already-aligned verification")
        result.update(status="pass", outcome="already_aligned", reason=None,
                      remote_tip_after=after,
                      upstream_tip_after=upstream_after,
                      local_tip_after=local, finished_at=_now())
        result["cas"]["verified"] = True
        return result

    recovery_ref = f"{RECOVERY_PREFIX}/{old}"
    result["recovery_ref"] = recovery_ref
    check = _git(repo, "check-ref-format", recovery_ref)
    if check.returncode != 0:
        return _blocked(result, "blocked", "deterministic recovery ref is invalid")
    tag_tip, error, measured = _remote_ref(repo, "origin", recovery_ref)
    if not measured:
        return _blocked(result, "norecord", error or "recovery ref unmeasured")
    if tag_tip is not None and tag_tip != old:
        return _blocked(
            result, "blocked",
            f"recovery tag collision: {recovery_ref} points to {tag_tip}, not {old}")

    if not apply:
        result.update(status="dry_run", outcome="would_reset", reason=None,
                      recovery_tip=tag_tip, recovery_action=(
                          "would_reuse" if tag_tip == old else "would_create"),
                      remote_tip_after=old, upstream_tip_after=target,
                      local_tip_after=local, finished_at=_now())
        return result

    if tag_tip is None:
        pushed = _git(
            repo, "push", "--progress",
            f"--force-with-lease={recovery_ref}:", "origin",
            f"{old}:{recovery_ref}", progress=True)
        if pushed.returncode != 0:
            return _blocked(
                result, "blocked", f"recovery tag push failed: {_first_error(pushed)}")
        result["recovery_action"] = "created"
    else:
        result["recovery_action"] = "reused"
    tag_tip, error, measured = _remote_ref(repo, "origin", recovery_ref)
    if not measured or error or tag_tip != old:
        return _blocked(
            result, "norecord",
            error or "recovery tag did not preserve the exact old tip")
    result["recovery_tip"] = tag_tip

    # Re-prove all destructive predicates immediately before the CAS push.
    local_now = _output(repo, "rev-parse", "--verify",
                        f"refs/heads/{branch}^{{commit}}")
    dirty_now = _git(repo, "status", "--porcelain=v1", "--untracked-files=all",
                     "--ignored=matching", "--ignore-submodules=all")
    origin_now, oerr = _remote_head(repo, "origin", branch)
    upstream_now, uerr = _remote_head(repo, "upstream", branch)
    tag_now, terr, tag_measured = _remote_ref(repo, "origin", recovery_ref)
    if dirty_now.returncode != 0 or oerr or uerr or terr or not tag_measured:
        return _blocked(
            result, "norecord",
            oerr or uerr or terr or "destructive-boundary measurement failed")
    if (local_now != old or (dirty_now.stdout or "").strip()
            or origin_now != old or upstream_now != target or tag_now != old):
        return _blocked(result, "blocked", "state changed before reset CAS; reset refused")

    if destructive_guard is None:
        return _blocked(
            result, "norecord",
            "branch CAS has no current upstream-PR evidence guard")
    else:
        try:
            guard_ok, guard_evidence, guard_reason = destructive_guard()
        except Exception as exc:
            return _blocked(
                result, "norecord",
                f"branch-CAS guard failed: {type(exc).__name__}: {exc}")
    result["branch_cas_guard"] = guard_evidence
    guard_keys = {"outcome", "upstream_repository", "fork_owner",
                  "open_count", "items"}
    guard_well_formed = bool(
        isinstance(guard_evidence, dict)
        and set(guard_evidence) == guard_keys
        and guard_evidence.get("outcome") == "complete"
        and guard_evidence.get("upstream_repository") == policy["upstream"]
        and isinstance(guard_evidence.get("fork_owner"), str)
        and guard_evidence["fork_owner"].casefold() == "vibeic"
        and type(guard_evidence.get("open_count")) is int
        and guard_evidence["open_count"] >= 0
        and isinstance(guard_evidence.get("items"), list)
        and guard_evidence["open_count"] == len(guard_evidence["items"]))
    if not guard_well_formed:
        return _blocked(
            result, "norecord",
            guard_reason or "branch-CAS upstream-PR evidence is malformed")
    if (guard_ok is not True or guard_evidence["open_count"] != 0
            or guard_evidence["items"] != []):
        return _blocked(
            result, "blocked",
            guard_reason or "upstream-PR evidence refused branch CAS")

    pushed = _git(
        repo, "push", "--progress",
        f"--force-with-lease=refs/heads/{branch}:{old}", "origin",
        f"{target}:refs/heads/{branch}", progress=True)
    result["cas"]["applied"] = True
    if pushed.returncode != 0:
        return _blocked(result, "blocked", f"branch CAS push failed: {_first_error(pushed)}")

    origin_after, oerr = _remote_head(repo, "origin", branch)
    upstream_after, uerr = _remote_head(repo, "upstream", branch)
    tag_after, terr, tag_measured = _remote_ref(repo, "origin", recovery_ref)
    if oerr or uerr or terr or not tag_measured:
        return _blocked(
            result, "norecord",
            oerr or uerr or terr or "post-reset remote verification failed")
    result.update(remote_tip_after=origin_after,
                  upstream_tip_after=upstream_after,
                  recovery_tip=tag_after)
    if origin_after != target or upstream_after != target or tag_after != old:
        return _blocked(result, "blocked", "post-reset exact-tip verification failed")

    # Update the local branch with its own expected-old CAS.  If it is checked
    # out, refresh index/worktree only after proving it is still clean.
    updated = _git(repo, "update-ref", f"refs/heads/{branch}", target, old)
    if updated.returncode != 0:
        return _blocked(result, "blocked", f"local branch CAS failed: {_first_error(updated)}")
    current = _output(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if current == branch:
        reset = _git(repo, "reset", "--hard", target, progress=True)
        if reset.returncode != 0:
            return _blocked(result, "blocked", f"local reset failed: {_first_error(reset)}")
    local_after = _output(repo, "rev-parse", "--verify",
                          f"refs/heads/{branch}^{{commit}}")
    result["local_tip_after"] = local_after
    if local_after != target:
        return _blocked(result, "blocked", "local branch does not match reset target")

    result.update(status="pass", outcome="reset", reason=None,
                  finished_at=_now())
    result["cas"]["verified"] = True
    return result


def make_round_evidence(*, round_at: str, run_id: str, applied: bool,
                        registry: dict, results: list[dict]) -> dict:
    statuses = [row.get("status") for row in results]
    if registry.get("errors") or any(s == "norecord" for s in statuses):
        status = "norecord"
    elif any(s == "blocked" for s in statuses):
        status = "blocked"
    elif not applied and any(s == "dry_run" for s in statuses):
        status = "dry_run"
    elif all(s == "pass" for s in statuses):
        status = "pass"
    else:
        status = "norecord"
    return {
        "program": PROGRAM, "schema_version": SCHEMA_VERSION,
        "round_at": round_at, "run_id": run_id, "applied": bool(applied),
        "policy_digest": registry.get("digest"),
        "policy_tools": sorted(registry.get("policies", {})),
        "status": status, "registry_errors": list(registry.get("errors") or []),
        "results": sorted(results, key=lambda row: str(row.get("tool"))),
    }


def validate_round_evidence(record: object, *, round_at: str,
                            forks_json: Path,
                            now: Optional[datetime] = None
                            ) -> tuple[Optional[dict], list[str]]:
    """Independently validate a daily report's exact reset/noop evidence."""
    errors: list[str] = []
    registry = load_registry(forks_json)
    errors.extend(registry.get("errors") or [])
    if not isinstance(record, dict):
        return None, errors + ["watch-reset source is not an object"]
    meta = record.get("_round")
    evidence = record.get("_watch_only_resets")
    if not isinstance(meta, dict):
        errors.append("watch-reset source has no _round object")
        meta = {}
    if not isinstance(evidence, dict):
        return None, errors + ["watch-reset source has no evidence object"]
    boundary = _parse_when(round_at)
    started = _parse_when(meta.get("started_at"))
    finished = _parse_when(meta.get("finished_at"))
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    run_id = meta.get("run_id")
    if meta.get("schema_version") != 1:
        errors.append("watch-reset source _round schema is not 1")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        errors.append("watch-reset source run_id is invalid")
    if boundary is None:
        errors.append("requested round_at is malformed")
    if started is None or finished is None:
        errors.append("watch-reset source timestamps are malformed")
    elif boundary is not None and (started < boundary or finished < started
                                   or finished > observed):
        errors.append("watch-reset source is stale, reversed, or future-dated")
    if meta.get("applied") is not True:
        errors.append("watch-reset source is not an applied round")
    if evidence.get("program") != PROGRAM or evidence.get("schema_version") != 1:
        errors.append("watch-reset evidence program/schema is invalid")
    if evidence.get("round_at") != round_at:
        errors.append("watch-reset evidence round_at does not match this round")
    if evidence.get("run_id") != run_id:
        errors.append("watch-reset evidence run_id does not match its source")
    if evidence.get("applied") is not True:
        errors.append("watch-reset evidence is not applied")
    if evidence.get("policy_digest") != registry.get("digest"):
        errors.append("watch-reset policy digest does not match FORKS.json")
    expected_tools = sorted(registry.get("policies", {}))
    if evidence.get("policy_tools") != expected_tools:
        errors.append("watch-reset policy tool coverage is incomplete")
    rows = evidence.get("results")
    if not isinstance(rows, list):
        errors.append("watch-reset results is not a list")
        rows = []
    by_tool: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("tool"), str):
            errors.append("watch-reset result is malformed")
            continue
        tool = row["tool"]
        if tool in by_tool:
            errors.append(f"duplicate watch-reset result for {tool}")
            continue
        by_tool[tool] = row
    if sorted(by_tool) != expected_tools:
        errors.append("watch-reset results do not exactly cover policy tools")

    for tool in expected_tools:
        row = by_tool.get(tool)
        if row is None:
            continue
        policy = registry["policies"][tool]
        if (row.get("program") != "watch_only_reset"
                or row.get("schema_version") != 1
                or row.get("round_at") != round_at
                or row.get("run_id") != run_id
                or row.get("applied") is not True):
            errors.append(f"{tool}: result envelope is invalid")
        if (row.get("branch") != policy["branch"]
                or row.get("upstream_repository") != policy["upstream"]
                or row.get("policy") != {"mode": MODE, "preserve": PRESERVE}):
            errors.append(f"{tool}: result does not match policy")
        row_started = _parse_when(row.get("started_at"))
        row_finished = _parse_when(row.get("finished_at"))
        if row_started is None or row_finished is None:
            errors.append(f"{tool}: result timestamps are malformed")
        elif (boundary is None or started is None or finished is None
              or row_started < boundary or row_started < started
              or row_finished < row_started or row_finished > finished
              or row_finished > observed):
            errors.append(f"{tool}: result timestamps fall outside this round")
        status = row.get("status")
        if status not in {"pass", "blocked", "norecord"}:
            errors.append(f"{tool}: applied result status is unsupported")
        if status != "pass":
            # A refusal/failure is first-class LL evidence, but never permits
            # semantic reconciliation. Validate every object id it does claim
            # without inventing postconditions a failed probe cannot provide.
            if not isinstance(row.get("reason"), str) or not row["reason"].strip():
                errors.append(f"{tool}: negative result has no reason")
            for field in ("old_tip", "upstream_tip", "remote_tip_after",
                          "upstream_tip_after", "local_tip_after",
                          "recovery_tip"):
                value = row.get(field)
                if value is not None and (not isinstance(value, str)
                                          or not _SHA.fullmatch(value)):
                    errors.append(f"{tool}: {field} is not an exact object id")
            continue
        old, target = row.get("old_tip"), row.get("upstream_tip")
        after, upstream_after = (row.get("remote_tip_after"),
                                 row.get("upstream_tip_after"))
        local_after = row.get("local_tip_after")
        if not all(isinstance(v, str) and _SHA.fullmatch(v)
                   for v in (old, target, after, upstream_after, local_after)):
            errors.append(f"{tool}: result has invalid exact object ids")
            continue
        ahead, behind = row.get("ahead"), row.get("behind")
        if (type(ahead) is not int or ahead < 0
                or type(behind) is not int or behind < 0):
            errors.append(f"{tool}: divergence counts are invalid")
        cas = row.get("cas")
        if (not isinstance(cas, dict) or cas.get("expected_old") != old
                or cas.get("target") != target or cas.get("verified") is not True):
            errors.append(f"{tool}: CAS evidence is invalid")
        if row.get("reason") is not None:
            errors.append(f"{tool}: passing result carries a failure reason")
        if after != target or upstream_after != target or local_after != target:
            errors.append(f"{tool}: successful result tips do not converge")
        outcome = row.get("outcome")
        if outcome == "already_aligned":
            if old != target or cas.get("applied") is not False \
                    or row.get("recovery_ref") is not None \
                    or row.get("recovery_tip") is not None \
                    or row.get("local_old_tip") is not None \
                    or ahead != 0 or behind != 0:
                errors.append(f"{tool}: already-aligned proof is contradictory")
        elif outcome == "reset":
            expected_ref = f"{RECOVERY_PREFIX}/{old}"
            guard = row.get("branch_cas_guard")
            if (old == target or cas.get("applied") is not True
                    or row.get("recovery_ref") != expected_ref
                    or row.get("recovery_tip") != old
                    or row.get("recovery_action") not in {"created", "reused"}
                    or row.get("local_old_tip") is not None
                    or ahead + behind == 0):
                errors.append(f"{tool}: reset/recovery proof is contradictory")
            if (not isinstance(guard, dict)
                    or set(guard) != {"outcome", "upstream_repository",
                                     "fork_owner", "open_count", "items"}
                    or guard.get("outcome") != "complete"
                    or guard.get("upstream_repository") != policy["upstream"]
                    or not isinstance(guard.get("fork_owner"), str)
                    or guard["fork_owner"].casefold() != "vibeic"
                    or type(guard.get("open_count")) is not int
                    or guard.get("open_count") != 0
                    or guard.get("items") != []):
                errors.append(f"{tool}: branch-CAS PR recheck is invalid")
        elif outcome == "repaired_local":
            local_old = row.get("local_old_tip")
            expected_ref = f"{RECOVERY_PREFIX}/{local_old}"
            if (old != target or not isinstance(local_old, str)
                    or not _SHA.fullmatch(local_old) or local_old == target
                    or cas.get("applied") is not False
                    or row.get("recovery_ref") != expected_ref
                    or row.get("recovery_tip") != local_old
                    or row.get("recovery_action") != "reused"
                    or ahead != 0 or behind != 0):
                errors.append(f"{tool}: local-repair proof is contradictory")
        else:
            errors.append(f"{tool}: passing outcome is unsupported")
    derived = ("norecord" if errors or registry.get("errors")
               or any(row.get("status") == "norecord" for row in rows
                      if isinstance(row, dict))
               else "blocked" if any(row.get("status") == "blocked" for row in rows
                                     if isinstance(row, dict))
               else "pass" if rows and all(row.get("status") == "pass" for row in rows)
               else "norecord")
    if evidence.get("status") != derived:
        errors.append("watch-reset aggregate status contradicts its results")
    summary = {
        "program": PROGRAM, "schema_version": SCHEMA_VERSION,
        "round_at": round_at, "run_id": run_id,
        "status": evidence.get("status"),
        "policy_digest": evidence.get("policy_digest"),
        "policy_tools": expected_tools,
        "results": rows,
    }
    return summary, errors
