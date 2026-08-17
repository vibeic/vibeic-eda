"""Private pytest liveness plugin: append completed lifecycle events.

The parent watches this structured sidecar. Console output and CPU activity are
not pytest progress; JUnit plus the natural process return code remain verdict
evidence. This is the same protocol used by the vibe-ic plugin landing gate.
"""
from __future__ import annotations

import json
import os
import threading
import time


PATH_ENV = "VIBEIC_PYTEST_PROGRESS_FILE"
NONCE_ENV = "VIBEIC_PYTEST_PROGRESS_NONCE"
SCHEMA = 1
_sequence = 0
_current_nodeid = None
_lock = threading.Lock()


def _emit(event: str, **fields) -> None:
    global _sequence
    path = os.environ.get(PATH_ENV)
    nonce = os.environ.get(NONCE_ENV)
    if not path or not nonce:
        return
    with _lock:
        _sequence += 1
        record = {
            "schema": SCHEMA,
            "nonce": nonce,
            "pid": os.getpid(),
            "seq": _sequence,
            "event": event,
            "monotonic_ns": time.monotonic_ns(),
            **fields,
        }
        payload = (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        except OSError:
            # The parent will report an incomplete protocol. Do not prevent
            # pytest teardown from preserving whatever JUnit it can write.
            pass


def pytest_sessionstart(session) -> None:
    _emit("session_start")


def pytest_collectreport(report) -> None:
    _emit("collect_report", nodeid=str(report.nodeid), outcome=str(report.outcome))


def pytest_itemcollected(item) -> None:
    _emit("item_collected", nodeid=str(item.nodeid))


def pytest_collection_finish(session) -> None:
    _emit("collection_finish", selected_items=len(session.items))


def pytest_runtest_logstart(nodeid, location) -> None:
    global _current_nodeid
    _current_nodeid = str(nodeid)


def domain_progress(scope: str, completed: int, total: int) -> None:
    """Emit one finite, exact +1 checkpoint inside a long pytest item."""
    if _current_nodeid is not None:
        _emit(
            "domain_progress",
            nodeid=_current_nodeid,
            scope=str(scope),
            completed=int(completed),
            total=int(total),
        )


def pytest_runtest_logfinish(nodeid, location) -> None:
    global _current_nodeid
    _emit("test_finish", nodeid=str(nodeid))
    _current_nodeid = None


def pytest_sessionfinish(session, exitstatus) -> None:
    _emit("session_finish", exitstatus=int(exitstatus))
