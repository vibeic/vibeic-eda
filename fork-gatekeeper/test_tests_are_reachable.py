"""The reachability gate must find the orphans, and must not invent them.

Both directions are load-bearing and both were measured while writing it.

THE FALSE-GREEN DIRECTION, which is the dangerous one. The first version matched
a test against its parent directory's BASENAME, so `test/crosstalk_eco_gate.cc`
looked covered because the word "test" appears in every CMakeLists in the repo.
OpenSTA and klayout each reported a confident ZERO while carrying 4 and 25
orphans the audit had already found by hand.

THE FALSE-RED DIRECTION. pytest collects `test_*.py` by convention, so a project
that invokes pytest reaches those files without naming them. Without that rule
cocotb-coverage and pyuvm reported 7 and 2 orphans whose tests demonstrably DO
run -- pyuvm's are the very tests the audit found turning the suite red on
Python 3.9, which they could not do if nothing ran them. And Trilinos reported
16, of which 14 were `doc/build_docs` and `doc/DoxyfileWeb` files that carry the
executable bit and are not tests.

THE HISTORICAL CONTROL, which is why this file can be trusted. sby carried 33
unreachable pytest tests until 2c2f04e wired them in. Both commits are in the
clone, so the gate can be run against a real before and a real after that were
not constructed for it:

    sby @1230919   pytest in the build surface: 0   ->  7 unreachable
    sby @2c2f04e   pytest in the build surface: 1   ->  0 unreachable

A fixture I built myself could be wrong in the same direction as the code it
tests. This one is a fact about the repository.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_tests_are_reachable", HERE / "check_tests_are_reachable.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_tests_are_reachable"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()

FORKS = pathlib.Path(os.environ.get("GK_FORKS_DIR") or "/home/reyerchu/vibe-ic-forks")


def _fork(name):
    p = FORKS / name
    if not (p / ".git").exists():
        import pytest
        pytest.skip(f"{name} is not cloned on this host")
    return p


# --------------------------------------------------------------------------
# The synthetic cases: exact control over what the tree contains.
# --------------------------------------------------------------------------

def _tree(tmp_path, files: dict, exe=()):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        if rel in exe:
            p.chmod(0o755)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "x"], check=True)
    return tmp_path


def test_a_parent_directory_NAME_is_not_evidence_of_reachability(tmp_path):
    """The false green that made OpenSTA and klayout report zero."""
    blob = "add_subdirectory(test)\nadd_executable(other test/other.cc)\n"
    assert "test" in blob                       # the bare name IS present...
    # ...and must not count. MIN_PARENT rejects a parent path this short.
    assert len("test") < M.MIN_PARENT, (
        "MIN_PARENT must exceed the length of the commonest directory name, or "
        "every file under test/ reads as covered")


def test_a_fixture_is_not_a_test(tmp_path):
    """Counting fixtures produced 90 netgen 'orphans' where 15 is the truth."""
    for fixture in ("a.lef", "b.spice", "waivers/cdc.json", "gold/x.txt"):
        assert not M.ENTRY.search(os.path.basename(fixture)), fixture
    for entry in ("run.sh", "run_all_the_things.sh", "check_enh11.sh",
                  "test_cdc.py", "coverage_test.py", "crosstalk_eco_gate.cc",
                  "job.sby"):
        assert M.ENTRY.search(os.path.basename(entry)), entry


def test_an_executable_outside_a_test_path_is_not_a_test(tmp_path):
    """Trilinos' doc/build_docs carries +x and is documentation."""
    assert not M.TESTISH_PATH.search("packages/amesos/doc/build_docs")
    assert M.TESTISH_PATH.search("test/blackbox/run.sh")
    assert M.TESTISH_PATH.search("vibeic_proofs/check_enh11.sh")
    assert M.TESTISH_PATH.search("packages/epetra/test/Foo/generateTarFile")


# --------------------------------------------------------------------------
# The historical control: a real before and a real after, from the repository.
# --------------------------------------------------------------------------

def test_sby_before_the_fix_is_measured_as_broken(tmp_path):
    """1230919 shipped 33 pytest tests no build system could reach."""
    clone = _fork("sby")
    wt = tmp_path / "before"
    r = subprocess.run(["git", "-C", str(clone), "worktree", "add", "--detach",
                        str(wt), "1230919"], capture_output=True, text=True)
    if r.returncode != 0:
        import pytest
        pytest.skip(f"cannot create worktree at 1230919: {r.stderr[:120]}")
    try:
        state, orphans, meta = M.unreachable(wt)
        assert state == M.FAIL, (state, meta)
        assert any(o.startswith("tests/formalapps_cdc/") for o in orphans), orphans
        assert len(orphans) >= 5, orphans
    finally:
        subprocess.run(["git", "-C", str(clone), "worktree", "remove", "--force",
                        str(wt)], capture_output=True)


def test_sby_after_the_fix_is_measured_as_clean():
    """The same fork, the same check, on the commit that wired the tests in."""
    clone = _fork("sby")
    state, orphans, meta = M.unreachable(clone)
    assert state == M.PASS, (state, orphans, meta)
    assert meta.get("entry_points", 0) > 0, (
        "a PASS with zero entry points would be vacuous — sby adds several")


# --------------------------------------------------------------------------
# The waiver contract.
# --------------------------------------------------------------------------

def test_the_waiver_is_names_and_never_a_count(tmp_path):
    """A count lets a NEW orphan hide behind a fixed one."""
    wpath = HERE / "TEST_REACH_WAIVERS.json"
    if not wpath.is_file():
        import pytest
        pytest.skip("no waiver file yet")
    data = json.loads(wpath.read_text())
    for tool, entry in data.items():
        if tool.startswith("_"):
            continue
        assert isinstance(entry.get("files"), list) and entry["files"], (
            f"{tool}: a waiver must NAME the files it excuses")
        assert entry.get("issue"), f"{tool}: a waiver must name the issue tracking it"
        for f in entry["files"]:
            assert isinstance(f, str) and "/" in f, (
                f"{tool}: {f!r} is not a path — a waiver names files, not counts")


def test_a_waiver_that_no_longer_applies_is_itself_a_failure(tmp_path):
    """It must not be possible for the list to outlive its truth."""
    src = (HERE / "check_tests_are_reachable.py").read_text()
    assert "waiver_no_longer_applies" in src
    assert "stale" in src, (
        "the program must detect and fail on a waiver whose file is now reachable")


def test_a_declared_post_merge_check_counts_as_reachable(tmp_path):
    """A FORKS.json check runs twice a day; leaving it out of the surface is a false red.

    Measured: OpenROAD's converter unit test was registered as a
    `post_merge_check` -- it is pure Python, needs no build, and passes in under
    a second -- and the gate went on calling it unreachable, because FORKS.json
    lives in vibeic-eda and `surface()` only read the fork's own tree.
    """
    fj = tmp_path / "FORKS.json"
    fj.write_text(json.dumps({"forks": [
        {"tool": "T", "upstream": "a/b", "role": "x",
         "post_merge_check": [{"name": "u", "path": "test/deep/it_runs.py",
                               "cmd": ["python3", "test/deep/it_runs.py"]}]},
        {"tool": "U", "upstream": "a/c", "role": "y"},
    ]}))
    assert "it_runs.py" in M.declared_checks("T", fj)
    assert M.declared_checks("U", fj) in ("", "[]")
    assert M.declared_checks("nosuch", fj) == ""


def test_the_declared_surface_cannot_manufacture_a_pass(tmp_path):
    """It must name the FILE, not merely exist."""
    fj = tmp_path / "FORKS.json"
    fj.write_text(json.dumps({"forks": [
        {"tool": "T", "upstream": "a/b", "role": "x",
         "post_merge_check": [{"name": "u", "path": "something/else.py",
                               "cmd": ["python3", "something/else.py"]}]}]}))
    declared = M.declared_checks("T", fj)
    assert "it_runs.py" not in declared, (
        "a declaration of a DIFFERENT check must not excuse this file")
