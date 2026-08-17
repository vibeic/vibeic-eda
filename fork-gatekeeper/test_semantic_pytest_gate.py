"""The 05:30 pytest gate leases only validated pytest progress."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import semantic_pytest_gate as G  # noqa: E402


def _run(tmp_path: Path, body: str, *, grace: float = 0.18) -> tuple[int, dict]:
    test = tmp_path / "test_subject.py"
    test.write_text(body, encoding="utf-8")
    record = tmp_path / "record.json"
    rc = G.main([
        "--junit", str(tmp_path / "junit.xml"),
        "--json", str(record),
        "--stall-grace", str(grace),
        "--poll", "0.03",
        "--", sys.executable, "-m", "pytest", "-q", str(test),
    ])
    return rc, json.loads(record.read_text())


def test_complete_green_record_passes(tmp_path) -> None:
    rc, record = _run(tmp_path, "def test_green():\n    assert 2 + 2 == 4\n")
    assert rc == G.RC_OK
    assert record["outcome"] == "natural"
    assert record["junit_cases"] == 1
    assert record["progress"]["last_event"] == "session_finish"
    assert record["total_runtime_ceiling_seconds"] is None


def test_complete_red_record_is_red_not_norecord(tmp_path) -> None:
    rc, record = _run(tmp_path, "def test_red():\n    assert False\n")
    assert rc == G.RC_RED
    assert record["outcome"] == "natural"
    assert record["junit_red"] == 1


def test_chatty_hang_cannot_impersonate_pytest_progress(tmp_path) -> None:
    rc, record = _run(
        tmp_path,
        "def test_hangs():\n"
        "    import sys\n"
        "    while True:\n"
        "        print('noise', flush=True)\n",
    )
    assert rc == G.RC_NORECORD
    assert record["outcome"] == "stalled"
    assert record["progress"]["last_event"] == "collection_finish"


def test_cpu_busy_hang_cannot_impersonate_pytest_progress(tmp_path) -> None:
    rc, record = _run(
        tmp_path,
        "def test_hangs():\n"
        "    while True:\n"
        "        pass\n",
    )
    assert rc == G.RC_NORECORD
    assert record["outcome"] == "stalled"


def test_finite_domain_checkpoints_let_long_test_finish(tmp_path) -> None:
    rc, record = _run(
        tmp_path,
        "def test_long_but_measured():\n"
        "    import time\n"
        "    from _pytest_progress_plugin import domain_progress\n"
        "    for i in range(1, 7):\n"
        "        time.sleep(.08)\n"
        "        domain_progress('chunks', i, 6)\n",
        grace=0.25,
    )
    assert rc == G.RC_OK
    assert record["elapsed_seconds"] > 0.4
    assert record["progress"]["last_event"] == "session_finish"


def test_wrong_nonce_and_sequence_freeze_the_protocol(tmp_path) -> None:
    path = tmp_path / "events"
    path.write_text(
        json.dumps({
            "schema": 1, "nonce": "wrong", "pid": 7, "seq": 1,
            "event": "session_start", "monotonic_ns": 1,
        }) + "\n",
        encoding="utf-8",
    )
    probe = G.SemanticProgressProbe(path, "expected")
    assert probe.sample() == 0
    assert "nonce" in probe.error
    path.write_text("", encoding="utf-8")
    assert probe.sample() == 0, "a broken channel must stay frozen"


def test_green_junit_with_live_detached_child_is_norecord_and_cleaned(tmp_path) -> None:
    """Natural pytest exit is incomplete until its whole process tree is empty."""
    pid_file = tmp_path / "escaped.pid"
    late_file = tmp_path / "late-write"
    child = (
        "import pathlib,time; time.sleep(.8); "
        f"pathlib.Path({str(late_file)!r}).write_text('leaked')"
    )
    rc, record = _run(
        tmp_path,
        "def test_returns_while_work_is_live():\n"
        "    import pathlib,subprocess,sys\n"
        f"    p=subprocess.Popen([sys.executable,'-c',{child!r}], "
        "start_new_session=True)\n"
        f"    pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
        "    assert True\n",
        grace=1.0,
    )
    assert rc == G.RC_NORECORD, record
    assert record["outcome"] == "live_descendants"
    assert record["junit_cases"] == 1, "green XML exists but is not sufficient"
    assert record["process_tree"]["final_empty"] is True
    escaped_pid = int(pid_file.read_text())
    try:
        os.kill(escaped_pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError(f"detached child {escaped_pid} survived cleanup")
    time.sleep(0.9)
    assert not late_file.exists()
