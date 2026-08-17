#!/usr/bin/env python3
"""Run one pytest selection with a validated semantic progress lease.

There is no total-runtime limit. Only valid, strictly ordered pytest lifecycle
events renew the lease; stdout chatter and CPU activity do not. A stalled,
malformed, incomplete, empty, or missing record returns 2 (NORECORD), never a
false pass or a synthetic test failure.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

import progress_watchdog as watchdog


RC_OK = 0
RC_RED = 1
RC_NORECORD = 2
RC_CANNOT_ASK = 3
DEFAULT_STALL_GRACE_S = 300.0
DEFAULT_POLL_S = 2.0
SCHEMA = 1
MAX_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 1_000_000
MAX_LINE = 64 * 1024
MAX_DOMAIN_TOTAL = 10_000
MAX_DOMAIN_SCOPES = 64
HERE = Path(__file__).resolve().parent
_PR_SET_CHILD_SUBREAPER = 36
_ACTIVE_JOB: Optional[tuple[int, set[tuple[int, int]]]] = None


class SemanticProgressProbe:
    """Validate the nonce-bound append-only pytest lifecycle protocol."""

    FIELDS = {
        "session_start": set(),
        "collect_report": {"nodeid", "outcome"},
        "item_collected": {"nodeid"},
        "collection_finish": {"selected_items"},
        "domain_progress": {"nodeid", "scope", "completed", "total"},
        "test_finish": {"nodeid"},
        "session_finish": {"exitstatus"},
    }
    COMMON = {"schema", "nonce", "pid", "seq", "event", "monotonic_ns"}

    def __init__(self, path: Path, nonce: str,
                 pid_fn: Optional[Callable[[], Optional[int]]] = None) -> None:
        self.path = path
        self.nonce = nonce
        self.pid_fn = pid_fn
        self.offset = 0
        self.tail = b""
        self.inode: Optional[tuple[int, int]] = None
        self.pid: Optional[int] = None
        self.seq = 0
        self.monotonic_ns = 0
        self.stage = "new"
        self.items: set[str] = set()
        self.collect_reports: set[str] = set()
        self.finished: set[str] = set()
        self.selected: Optional[int] = None
        self.exitstatus: Optional[int] = None
        self.domain: dict[tuple[str, str], tuple[int, int]] = {}
        self.score = 0.0
        self.error = ""
        self.last_event: Optional[str] = None

    def _fail(self, reason: str) -> None:
        if not self.error:
            self.error = reason

    @staticmethod
    def _is_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _accept(self, record: dict) -> None:
        event = record.get("event")
        if event not in self.FIELDS:
            self._fail(f"unknown event {event!r}")
            return
        if set(record) != self.COMMON | self.FIELDS[event]:
            self._fail(f"wrong fields for {event}")
            return
        expected_pid = self.pid_fn() if self.pid_fn is not None else None
        if (record.get("schema") != SCHEMA or record.get("nonce") != self.nonce
                or (expected_pid is not None and record.get("pid") != expected_pid)):
            self._fail("schema, nonce, or writer pid mismatch")
            return
        pid = record.get("pid")
        seq = record.get("seq")
        stamp = record.get("monotonic_ns")
        if not all(self._is_int(v) for v in (pid, seq, stamp)):
            self._fail("pid/seq/timestamp is not an integer")
            return
        if pid <= 0 or seq != self.seq + 1 or stamp <= self.monotonic_ns:
            self._fail("pid/sequence/timestamp is not strictly monotonic")
            return
        if self.pid is not None and pid != self.pid:
            self._fail("writer pid changed")
            return
        if event == "session_start":
            if self.stage != "new" or seq != 1:
                self._fail("session_start out of order")
                return
            self.stage = "collecting"
        elif event == "collect_report":
            nodeid = record.get("nodeid")
            if (self.stage != "collecting" or not isinstance(nodeid, str)
                    or nodeid in self.collect_reports
                    or record.get("outcome") not in {"passed", "failed", "skipped"}):
                self._fail("invalid collect_report")
                return
            self.collect_reports.add(nodeid)
        elif event == "item_collected":
            nodeid = record.get("nodeid")
            if (self.stage != "collecting" or not isinstance(nodeid, str)
                    or not nodeid or nodeid in self.items):
                self._fail("invalid or duplicate item_collected")
                return
            self.items.add(nodeid)
        elif event == "collection_finish":
            selected = record.get("selected_items")
            if (self.stage != "collecting" or not self._is_int(selected)
                    or selected < 0 or selected > len(self.items)):
                self._fail("invalid collection_finish")
                return
            self.selected = selected
            self.stage = "running"
        elif event == "domain_progress":
            nodeid = record.get("nodeid")
            scope = record.get("scope")
            completed = record.get("completed")
            total = record.get("total")
            key = (nodeid, scope)
            previous = self.domain.get(key)
            if (self.stage != "running" or nodeid not in self.items
                    or nodeid in self.finished or not isinstance(scope, str)
                    or not scope or len(scope) > 160
                    or not self._is_int(completed) or not self._is_int(total)
                    or total < 1 or total > MAX_DOMAIN_TOTAL
                    or completed < 1 or completed > total):
                self._fail("invalid/out-of-order domain_progress")
                return
            if previous is None and len(self.domain) >= MAX_DOMAIN_SCOPES:
                self._fail("domain progress scope limit exceeded")
                return
            if ((previous is None and completed != 1)
                    or (previous is not None
                        and (total != previous[1] or completed != previous[0] + 1))):
                self._fail("non-monotonic domain_progress")
                return
            self.domain[key] = (completed, total)
        elif event == "test_finish":
            nodeid = record.get("nodeid")
            if (self.stage != "running" or nodeid not in self.items
                    or nodeid in self.finished):
                self._fail("invalid or duplicate test_finish")
                return
            self.finished.add(nodeid)
            if self.selected is not None and len(self.finished) > self.selected:
                self._fail("more tests finished than selected")
                return
        elif event == "session_finish":
            exitstatus = record.get("exitstatus")
            if (self.stage != "running" or not self._is_int(exitstatus)
                    or self.selected is None
                    or len(self.finished) != self.selected):
                self._fail("incomplete or invalid session_finish")
                return
            self.exitstatus = exitstatus
            self.stage = "finished"

        self.pid = pid
        self.seq = seq
        self.monotonic_ns = stamp
        self.last_event = event
        self.score += 1.0

    def sample(self) -> float:
        if self.error:
            return self.score
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return self.score
        except OSError as exc:
            self._fail(f"progress sidecar unreadable: {exc}")
            return self.score
        identity = (stat.st_dev, stat.st_ino)
        if self.inode is None:
            self.inode = identity
        elif identity != self.inode:
            self._fail("progress sidecar was replaced")
            return self.score
        if stat.st_size < self.offset or stat.st_size > MAX_BYTES:
            self._fail("progress sidecar was truncated or exceeded its limit")
            return self.score
        if stat.st_size == self.offset:
            return self.score
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(stat.st_size - self.offset)
        except OSError as exc:
            self._fail(f"progress sidecar read failed: {exc}")
            return self.score
        self.offset += len(chunk)
        data = self.tail + chunk
        lines = data.split(b"\n")
        self.tail = lines.pop()
        if len(self.tail) > MAX_LINE:
            self._fail("progress event exceeded its line limit")
            return self.score
        for raw in lines:
            if self.seq >= MAX_EVENTS:
                self._fail("progress event count exceeded its limit")
                break
            if not raw or len(raw) > MAX_LINE:
                self._fail("empty or oversized progress event")
                break
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._fail("malformed progress JSON")
                break
            if not isinstance(record, dict):
                self._fail("progress event is not an object")
                break
            self._accept(record)
            if self.error:
                break
        return self.score

    def complete(self) -> tuple[bool, str]:
        self.sample()
        if not self.error and self.tail:
            self._fail("truncated final event")
        if not self.error and self.stage != "finished":
            self._fail(f"terminal event missing (stage={self.stage})")
        if self.error:
            return False, self.error
        return True, ""

    def summary(self) -> dict:
        return {
            "events": self.seq,
            "last_event": self.last_event,
            "stage": self.stage,
            "selected": self.selected,
            "finished": len(self.finished),
            "exitstatus": self.exitstatus,
            "protocol_error": self.error or None,
        }


def _enable_subreaper() -> bool:
    """Make detached pytest descendants attributable to this gate process."""
    if os.name != "posix" or not Path("/proc").is_dir():
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (AttributeError, OSError):
        return False


def _proc_snapshot_checked() -> tuple[dict[int, tuple[int, int]], bool]:
    """Return ``pid -> (ppid,starttime)`` and whether the census is complete."""
    out: dict[int, tuple[int, int]] = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return {}, False
    complete = True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(errors="replace")
            fields = raw[raw.rfind(")") + 2:].split()
            out[int(entry.name)] = (int(fields[1]), int(fields[19]))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            try:
                if entry.exists():
                    complete = False
            except OSError:
                complete = False
    return out, complete


def _descendants(snapshot: dict[int, tuple[int, int]], root: int) -> set[int]:
    found: set[int] = set()
    frontier = {root}
    while frontier:
        children = {
            pid for pid, (ppid, _start) in snapshot.items()
            if ppid in frontier and pid not in found
        }
        found.update(children)
        frontier = children
    return found


def _job_processes_checked(
    root_pid: int,
    baseline: set[tuple[int, int]],
) -> tuple[dict[int, int], bool]:
    """Return exact pid/starttime identities attributable to this pytest run."""
    snapshot, complete = _proc_snapshot_checked()
    pids = _descendants(snapshot, root_pid)
    if root_pid in snapshot:
        pids.add(root_pid)
    # A child that double-forks or starts a new session is adopted by this
    # subreaper after its parent exits. Anything newly below us belongs to this
    # sequential semantic run; baseline identities prevent old children from
    # being mistaken for this job.
    for pid in _descendants(snapshot, os.getpid()):
        identity = (pid, snapshot[pid][1])
        if identity not in baseline:
            pids.add(pid)
    return ({pid: snapshot[pid][1] for pid in pids if pid in snapshot}, complete)


def _signal_identities(identities: dict[int, int], sig: int) -> None:
    snapshot, _ = _proc_snapshot_checked()
    for pid, starttime in identities.items():
        if (pid == os.getpid() or pid not in snapshot
                or snapshot[pid][1] != starttime):
            continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _reap_adopted() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid <= 0:
            return


@dataclass(frozen=True)
class CleanupResult:
    observed: set[int]
    survivors: set[int]
    census_ok: bool


def _cleanup_job(root_pid: int, baseline: set[tuple[int, int]],
                 *, term_grace_s: float = 2.0) -> CleanupResult:
    """Terminate all attributable identities and prove a final empty census."""
    identities, census_ok = _job_processes_checked(root_pid, baseline)
    observed = set(identities)
    if identities:
        _signal_identities(identities, signal.SIGTERM)
    deadline = time.monotonic() + term_grace_s
    while identities and time.monotonic() < deadline:
        time.sleep(0.05)
        _reap_adopted()
        identities, scan_ok = _job_processes_checked(root_pid, baseline)
        census_ok = census_ok and scan_ok
        observed.update(identities)
    if identities:
        _signal_identities(identities, signal.SIGKILL)
        deadline = time.monotonic() + term_grace_s
        while identities and time.monotonic() < deadline:
            time.sleep(0.05)
            _reap_adopted()
            identities, scan_ok = _job_processes_checked(root_pid, baseline)
            census_ok = census_ok and scan_ok
            observed.update(identities)
            if identities:
                _signal_identities(identities, signal.SIGKILL)
    _reap_adopted()
    final, final_ok = _job_processes_checked(root_pid, baseline)
    observed.update(final)
    return CleanupResult(observed, set(final), census_ok and final_ok)


def _junit_cases(path: Path) -> tuple[int, int]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return 0, 0
    cases = red = 0
    for case in root.iter("testcase"):
        cases += 1
        if any(child.tag.rsplit("}", 1)[-1] in {"failure", "error"}
               for child in case):
            red += 1
    return cases, red


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--junit", required=True)
    parser.add_argument("--json", action="append", default=[])
    parser.add_argument("--cwd")
    parser.add_argument("--stall-grace", type=float, default=DEFAULT_STALL_GRACE_S)
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_S)
    parser.add_argument("pytest_argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.pytest_argv)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("PYTEST_CANNOT_ASK: no command after --", file=sys.stderr)
        return RC_CANNOT_ASK
    if args.stall_grace <= 0 or args.poll <= 0:
        parser.error("stall-grace and poll must be positive")

    junit = Path(args.junit).resolve()
    junit.parent.mkdir(parents=True, exist_ok=True)
    try:
        junit.unlink()
    except FileNotFoundError:
        pass
    temporary = Path(tempfile.mkdtemp(prefix="vibeic-eda-pytest-progress-"))
    progress = temporary / "events.jsonl"
    progress.touch(mode=0o600)
    nonce = secrets.token_hex(24)
    holder: dict[str, subprocess.Popen] = {}
    snapshot, census_ok = _proc_snapshot_checked()
    subreaper_ok = _enable_subreaper()
    baseline = {
        (pid, snapshot[pid][1])
        for pid in _descendants(snapshot, os.getpid())
    }
    probe = SemanticProgressProbe(
        progress, nonce,
        lambda: holder["proc"].pid if "proc" in holder else None,
    )
    env = dict(os.environ)
    env["VIBEIC_PYTEST_PROGRESS_FILE"] = str(progress)
    env["VIBEIC_PYTEST_PROGRESS_NONCE"] = nonce
    old_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(HERE) + (os.pathsep + old_path if old_path else "")
    full = command + [
        "-p", "_pytest_progress_plugin", "-o", "junit_family=xunit1",
        f"--junitxml={junit}",
    ]
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        cleanup_observed: set[int] = set()
        cleanup_survivors: set[int] = set()
        cleanup_census_ok = census_ok

        def popen_factory(argv, **kwargs):
            proc = subprocess.Popen(argv, **kwargs)
            holder["proc"] = proc
            return proc

        def kill_job(proc, _reason: str) -> None:
            nonlocal cleanup_census_ok
            cleanup = _cleanup_job(proc.pid, baseline)
            cleanup_observed.update(cleanup.observed)
            cleanup_survivors.update(cleanup.survivors)
            cleanup_census_ok = cleanup_census_ok and cleanup.census_ok

        if not subreaper_ok or not census_ok:
            why = ("SUBREAPER_UNAVAILABLE" if not subreaper_ok
                   else "PROCESS_CENSUS_UNAVAILABLE")
            run = watchdog.SupervisedResult(
                RC_NORECORD, "", f"{why}: descendant cleanup is not provable\n",
                "process_census_unavailable", 0.0,
            )
        else:
            run = watchdog.run_supervised(
                full,
                cwd=args.cwd,
                env=env,
                stall_grace_s=args.stall_grace,
                poll_s=args.poll,
                domain_progress_probe=probe.sample,
                semantic_only=True,
                kill=kill_job,
                popen_factory=popen_factory,
            )
        sys.stdout.write(run.out)
        sys.stderr.write(run.err)
        leaked: set[int] = set()
        proc = holder.get("proc")
        if proc is not None:
            # A complete pytest/JUnit pair is still not a complete run when a
            # fixture leaves asynchronous work behind. Reap dead adoptees,
            # census live identities, clean them, then require a verified empty
            # final census before any verdict may form.
            _reap_adopted()
            live, live_census_ok = _job_processes_checked(proc.pid, baseline)
            leaked = set(live)
            cleanup_census_ok = cleanup_census_ok and live_census_ok
            if live or not live_census_ok:
                cleanup = _cleanup_job(proc.pid, baseline)
                cleanup_observed.update(cleanup.observed)
                cleanup_survivors.update(cleanup.survivors)
                cleanup_census_ok = cleanup_census_ok and cleanup.census_ok
        protocol_complete, protocol_reason = probe.complete()
        cases, red = _junit_cases(junit)
        if (protocol_complete and run.outcome == "natural"
                and probe.exitstatus != run.rc):
            protocol_complete = False
            protocol_reason = (
                f"session_finish exitstatus {probe.exitstatus} disagrees with "
                f"OS rc {run.rc}"
            )
        if (protocol_complete and probe.selected is not None
                and cases < probe.selected):
            protocol_complete = False
            protocol_reason = (
                f"JUnit has {cases} case(s), fewer than the "
                f"{probe.selected} selected item(s)"
            )
        final_cleanup_ok = cleanup_census_ok and not cleanup_survivors
        if run.outcome != "natural":
            effective_outcome = run.outcome
        elif leaked:
            effective_outcome = "live_descendants"
        elif not final_cleanup_ok:
            effective_outcome = "descendant_cleanup_incomplete"
        elif not protocol_complete:
            effective_outcome = "protocol_incomplete"
        else:
            effective_outcome = "natural"

        if effective_outcome != "natural":
            rc = RC_NORECORD
            reason = f"{effective_outcome}: no complete pytest run was proved"
        elif not protocol_complete:
            rc = RC_NORECORD
            reason = f"progress protocol incomplete: {protocol_reason}"
        elif run.rc not in (0, 1) or cases == 0:
            rc = RC_NORECORD
            reason = f"incomplete pytest evidence: rc={run.rc}, junit cases={cases}"
        elif run.rc != 0 or red:
            rc = RC_RED
            reason = f"complete red pytest record: rc={run.rc}, red cases={red}"
        else:
            rc = RC_OK
            reason = f"complete green pytest record: {cases} cases"
        record = {
            "schema": 1,
            "mechanism": "validated_semantic_progress_lease",
            "started_at": started_at,
            "total_runtime_ceiling_seconds": None,
            "stall_grace_seconds": args.stall_grace,
            "outcome": effective_outcome,
            "process_rc": run.rc,
            "exit_code": rc,
            "elapsed_seconds": round(run.elapsed_s, 3),
            "junit": str(junit),
            "junit_cases": cases,
            "junit_red": red,
            "reason": reason,
            "progress": probe.summary(),
            "process_tree": {
                "subreaper": subreaper_ok,
                "census_complete": cleanup_census_ok,
                "live_descendants_observed": sorted(leaked),
                "cleanup_observed": sorted(cleanup_observed),
                "cleanup_survivors": sorted(cleanup_survivors),
                "final_empty": final_cleanup_ok,
            },
        }
        for json_path in args.json:
            _atomic_json(Path(json_path), record)
        label = "PASS" if rc == 0 else ("RED" if rc == 1 else "NORECORD")
        print(f"SEMANTIC_PYTEST_{label}: {reason}")
        return rc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
