"""An `advisory` post-merge check reports but does not veto the publish.

WHY THE CONCEPT EXISTS. A post-merge check earns its veto by reading the MERGED
TREE: if it fails, this merge is one the program is not entitled to publish.
klayout's `vendored-plugin-sync` does not read the merged tree at all -- it
compares the fork against a vibe-ic PLUGIN checkout located through
`~/vibe-ic/...` hints, i.e. another repository's working copy on whatever host
runs the round. Merging upstream into klayout cannot make that copy more or less
stale, and on a host with no plugin checkout the script exits SKIP=3, which is
non-zero. Wired as a hard gate it therefore blocked klayout's upstream sync
permanently, everywhere, for a defect living in a different repository.

Measured 2026-08-13, the 05:30 round:
    klayout  upstream: already current; BLOCKED by post-merge check:
             vendored-plugin-sync rc=1 ... -- NOT pushed
and after marking it advisory, on the same state:
    klayout  ALREADY_CURRENT  level with upstream/master        0 need a human

THE LINE THIS PINS. `advisory` must never quiet a check that DOES read the
merged tree -- that is the entire gate. So the test below asserts BOTH halves:
an advisory check stops blocking, AND a normal failing check still blocks.
"""
import json
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import daily_merge as M                                    # noqa: E402


@pytest.fixture
def failing_tree(tmp_path):
    s = tmp_path / "chk.sh"
    s.write_text("#!/bin/sh\nexit 1\n")
    s.chmod(0o755)
    return tmp_path


def _row(wt, **extra):
    chk = {"name": "c", "path": "chk.sh", "cmd": ["sh", "chk.sh"], **extra}
    return M.run_post_merge_checks(wt, [chk])[0]


def test_a_normal_failing_check_still_blocks(failing_tree):
    r = _row(failing_tree)
    assert r["ok"] is False, "the gate is gone: a failing check no longer blocks"
    assert r["rc"] != 0


def test_an_advisory_failing_check_does_not_block(failing_tree):
    r = _row(failing_tree, advisory=True)
    assert r["ok"] is True, "an advisory check still vetoes the publish"


def test_advisory_does_not_hide_the_real_result(failing_tree):
    """The point is to drop the veto, NOT to make the red disappear: the round's
    summary and the page must still show it."""
    r = _row(failing_tree, advisory=True)
    assert r["rc"] != 0, "advisory masked the real exit code"
    assert r["advisory"] is True
    assert "ADVISORY" in r["detail"], r["detail"]
    assert r["detail"] != "clean"


def test_selftest_report_does_not_call_advisory_red_pass(tmp_path):
    clone = tmp_path / "forks" / "Demo"
    (clone / ".git").mkdir(parents=True)
    (clone / "chk.sh").write_text("#!/bin/sh\nexit 1\n")
    registry = tmp_path / "FORKS.json"
    registry.write_text(json.dumps({"forks": [{"tool": "Demo", "post_merge_check": [{
        "name": "cross-repo", "path": "chk.sh", "cmd": ["sh", "chk.sh"],
        "advisory": True}]}]}))
    output = tmp_path / "selftests.json"
    cp = subprocess.run([sys.executable, str(HERE / "check_fork_selftests.py"),
        "--forks-dir", str(tmp_path / "forks"), "--forks-json", str(registry),
        "--ledger", str(tmp_path / "ledger"), "--json", str(output)],
        capture_output=True, text=True)
    assert cp.returncode == 0, cp.stdout + cp.stderr  # still advisory
    record = json.loads(output.read_text())
    assert record["results"][0]["state"] == "ADVISORY-FAIL", record
    assert record["results"][0]["rc"] == 1
    assert record["advisory_failed"] == 1 and record["failed"] == 0
    assert "0 pass" in cp.stdout and "1 ADVISORY-FAIL" in cp.stdout


def test_absent_script_still_blocks_even_when_advisory(tmp_path):
    """MISSING is decided before the command runs, and it stays a block: a
    declaration naming a path no revision carries is a typo, not a waiver."""
    r = _row(tmp_path, advisory=True)
    assert r["ok"] is False, "a missing script became a pass via advisory"
    assert "MISSING" in r["detail"]


@pytest.mark.parametrize("outcome,rc", [("stalled", 199), ("launch_error", 127),
                                      ("protocol_incomplete", 0)])
def test_advisory_noncompletion_cannot_authorize_a_merge(failing_tree, monkeypatch,
                                                        outcome, rc):
    monkeypatch.setattr(M.watchdog, "run_supervised", lambda *a, **k: SimpleNamespace(
        outcome=outcome, rc=rc, out="partial output", err="", elapsed_s=1.0))
    result = _row(failing_tree, advisory=True)
    assert result["ok"] is False, result
    assert result["outcome"] == outcome


def test_advisory_cannot_turn_a_stall_into_a_completed_record(tmp_path,
                                                              monkeypatch):
    """Dropping a veto is not permission to forge PASS from no completion."""
    import check_fork_selftests as S

    clone = tmp_path / "tool"
    (clone / ".git").mkdir(parents=True)
    (clone / "chk.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: type(
        "CP", (), {"stdout": "abc123\n"})())

    class DM:
        @staticmethod
        def run_post_merge_checks(_clone, _checks):
            return [{"name": "c", "ok": True, "rc": 199,
                     "advisory": True, "outcome": "stalled",
                     "elapsed_seconds": 30, "detail": "ADVISORY stalled"}]

    rows = S.check_fork(
        "tool", [{"name": "c", "path": "chk.sh", "cmd": ["sh", "chk.sh"],
                  "advisory": True}],
        tmp_path, DM, None,
    )
    assert rows[0]["state"] == S.UNKNOWN


def test_only_the_cross_repo_check_is_advisory_in_forks_json():
    """A guard on the concept itself: if a second check ever becomes advisory,
    that is a decision someone must make deliberately, not inherit."""
    d = json.loads((HERE / "FORKS.json").read_text())
    adv = [(f.get("tool"), c.get("name"))
           for f in d["forks"]
           for c in (f.get("post_merge_check") or [])
           if c.get("advisory")]
    assert adv == [("klayout", "vendored-plugin-sync")], (
        "the advisory set changed; each entry must be justified by NOT reading "
        f"the merged tree: {adv}")
    for f in d["forks"]:
        for c in f.get("post_merge_check") or []:
            if c.get("advisory"):
                assert c.get("why_advisory"), (
                    f"{f['tool']}:{c['name']} is advisory with no why_advisory")
