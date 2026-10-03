"""The container transport must preserve semantic verdicts and source identity."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fork_pytest_container as C


def _receipt(tmp_path, body="", rc=0):
    xml = tmp_path / "junit.xml"
    xml.write_text(f'<testsuites><testsuite><testcase file="test_subject.py" name="test_value">{body}</testcase></testsuite></testsuites>')
    return xml, {
        "mechanism": "validated_semantic_progress_lease", "outcome": "natural",
        "exit_code": rc, "process_rc": rc, "junit_cases": 1,
        "junit_red": int("failure" in body or "error" in body),
        "progress": {"selected": 1, "finished": 1, "stage": "finished",
                     "exitstatus": rc, "protocol_error": None},
        "process_tree": {"final_empty": True},
    }


@pytest.mark.parametrize("body,rc,expected", [
    ("", 0, 0), ('<skipped message="missing runtime"/>', 0, 1),
    ('<failure message="wrong value"/>', 1, 1), ('<error message="setup failed"/>', 1, 1),
])
def test_every_junit_outcome_is_consumed(tmp_path, body, rc, expected):
    xml, semantic = _receipt(tmp_path, body, rc)
    actual, record = C.grade_receipts(rc, semantic, xml)
    assert actual == expected
    assert record["case_ids"] == ["test_subject.py::test_value"]


@pytest.mark.parametrize("second_class,expected", [("TestRight", 0), ("TestLeft", 2)])
def test_class_names_are_part_of_exact_case_identity(tmp_path, second_class, expected):
    xml, semantic = _receipt(tmp_path)
    xml.write_text('<testsuites><testsuite>' + ''.join(
        f'<testcase file="test_subject.py" classname="test_subject.{cls}" name="test_value"/>'
        for cls in ("TestLeft", second_class)) + '</testsuite></testsuites>')
    semantic["junit_cases"] = 2
    semantic["progress"].update(selected=2, finished=2)
    rc, record = C.grade_receipts(0, semantic, xml)
    assert rc == expected
    assert record["case_ids"][0] == "test_subject.py::TestLeft::test_value"


@pytest.mark.parametrize("breakage", ["missing_junit", "empty", "missing_semantic", "exitstatus", "protocol_error", "red_count", "unfinished", "stalled"])
def test_incomplete_or_inconsistent_receipts_never_pass(tmp_path, breakage):
    xml, semantic = _receipt(tmp_path)
    if breakage == "missing_junit":
        xml.unlink()
    elif breakage == "empty":
        xml.write_text("<testsuites/>")
    elif breakage == "missing_semantic":
        semantic = {}
    elif breakage == "exitstatus":
        semantic["progress"]["exitstatus"] = 1
    elif breakage == "protocol_error":
        semantic["progress"]["protocol_error"] = "writer pid mismatch"
    elif breakage == "red_count":
        semantic["junit_red"] = 1
    elif breakage == "unfinished":
        semantic["progress"]["finished"] = 0
    else:
        semantic["outcome"] = "stalled"
    rc, record = C.grade_receipts(0, semantic, xml)
    assert rc == 2
    assert record["outcome"] == ("stalled" if breakage == "stalled" else "protocol_incomplete")


@pytest.mark.parametrize("change", ["unchanged", "changed", "dirty"])
def test_source_recheck_after_green_cannot_fail_open(tmp_path, monkeypatch, change):
    monkeypatch.setenv("GK_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(C.subprocess, "check_output", lambda *a, **kw: "sha256:" + "a" * 64)
    identities = iter([{"head": "one", "tree": "one"}, {"head": "two", "tree": "two"}])
    calls = []
    def identity(wt):
        calls.append(wt)
        if change == "dirty" and len(calls) > 1:
            raise ValueError("candidate checkout has tracked modifications")
        return next(identities) if change == "changed" else {"head": "one", "tree": "one"}
    monkeypatch.setattr(C, "_source_identity", identity)
    monkeypatch.setattr(C, "_snapshot", lambda wt, head, evidence: (wt, "archive-sha"))
    def run(argv, evidence, grace):
        xml, semantic = _receipt(evidence)
        (evidence / "semantic.json").write_text(json.dumps(semantic))
        assert "--init" in argv
        assert "sha256:" + "a" * 64 in argv
        assert f"type=bind,src={tmp_path},dst=/candidate,readonly" in argv
        assert f"type=bind,src={evidence},dst=/evidence" in argv
        assert evidence.name == "records"
        assert "PYTHONPATH=/candidate:/gatekeeper" in argv
        return subprocess.CompletedProcess(argv, 0, "clean", "")
    monkeypatch.setattr(C, "_run_until_started", run)
    rc, _, _, record = C.run_container_pytest(
        ["python3", "-m", "pytest"], tmp_path, {"image": "released"}, 30, 1)
    assert rc == (0 if change == "unchanged" else 2)
    assert record["outcome"] == ("natural" if change == "unchanged" else
                                  "source_changed" if change == "changed" else "container_incomplete")
    assert json.loads((Path(record["evidence"]) / "result.json").read_text())["exit_code"] == rc


def test_startup_lease_does_not_wait_forever(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        C._run_until_started([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, .1)


def test_snapshot_excludes_untracked_and_ignored_source_overlays(tmp_path):
    repo, evidence = tmp_path / "repo", tmp_path / "evidence"
    repo.mkdir()
    evidence.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "tracked.py").write_text("VALUE = 7\n")
    (repo / ".gitignore").write_text("ignored.py\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fixture"], check=True)
    (repo / "untracked.py").write_text("VALUE = 1\n")
    (repo / "ignored.py").write_text("VALUE = 2\n")
    identity = C._source_identity(repo)
    snapshot, digest = C._snapshot(repo, identity["head"], evidence)
    assert (snapshot / "tracked.py").read_text() == "VALUE = 7\n"
    assert not (snapshot / "untracked.py").exists()
    assert not (snapshot / "ignored.py").exists()
    assert len(digest) == 64
