"""The map must be able to tell "ships no test" from "broke an existing one".

vibeic-eda#93. `ee778e7ced` (drt post-route min-area repair, 494 adds) was filed
NO-ORACLE — "substantive code, no test at all". It has an oracle:
`drt:top_level_term2`, an upstream golden-diff test registered in BOTH CMake and
bazel. Our patch changed the routed DEF, the `.defok` was never regenerated, and
the test has been permanently red since. The map could not see that, because the
only question it asked was "does this commit SHIP a test", and a commit that
BREAKS one ships nothing.

The two conditions have OPPOSITE remedies — write a test vs regenerate a golden —
so putting them in one bucket is not a rounding error, it sends the reader to the
wrong repair.

MEASURED HERE, all four directions on a synthetic fork, because a bucket that
cannot be shown to change with the evidence is decoration:

    pre-existing test measured FAIL   BROKE-EXISTING-ORACLE, rc=1
    same commit, test measured PASS   NO-ORACLE,             rc=0
    same commit, nothing measured     COULD-NOT-MEASURE,     never a pass
    commit that adds a test           SHIPS-ORACLE

The third row is the load-bearing one. "NO-ORACLE" asserts *breaks nothing*,
which is a claim about a test run — so without a run it may not be made. A test
that is permanently red and a test that never executes are indistinguishable
from outside; both must refuse to render as a pass.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_spec = importlib.util.spec_from_file_location("oracle_map", _HERE / "oracle_map.py")
M = importlib.util.module_from_spec(_spec)
sys.modules["oracle_map"] = M
_spec.loader.exec_module(M)


def _git(repo, *args, **env):
    e = {"GIT_AUTHOR_NAME": env.get("who", "Upstream Dev"),
         "GIT_AUTHOR_EMAIL": "dev@example.com",
         "GIT_COMMITTER_NAME": "Upstream Dev",
         "GIT_COMMITTER_EMAIL": "dev@example.com",
         "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
         "HOME": str(repo), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, env=e, check=True)


def _write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _fork(tmp_path):
    """An upstream module with a registered golden test, then two of ours."""
    repo = tmp_path / "fork"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")

    # --- upstream: module `foo`, one test, its golden, both registrations ---
    _write(repo, "src/foo/src/foo.cpp", "int foo() { return 1; }\n")
    _write(repo, "src/foo/test/t1.tcl", "run_foo\n")
    _write(repo, "src/foo/test/t1.defok", "GOLDEN v1\n")
    _write(repo, "src/foo/test/CMakeLists.txt",
           'or_integration_tests(\n  "foo"\n  TESTS\n    t1\n)\n')
    _write(repo, "src/foo/test/BUILD", 'COMPULSORY_TESTS = [\n    "t1",\n]\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "foo: the module and its golden test")
    up = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/remotes/upstream/master", up)

    # --- ours: changes foo's source, ships no test ---
    _write(repo, "src/foo/src/foo.cpp", "int foo() { return 2; }  // vibeic\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "foo: change the output", who="reyerchu")
    breaker = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # --- ours: a different module, WITH a test ---
    _write(repo, "src/bar/src/bar.cpp", "int bar() { return 1; }\n")
    _write(repo, "src/bar/test/t2.tcl", "run_bar\n")
    _write(repo, "src/bar/test/CMakeLists.txt",
           'or_integration_tests(\n  "bar"\n  TESTS\n    t2\n)\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bar: a module that ships its test",
         who="reyerchu")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, up, breaker, head


def _ledger(tmp_path, name, sha, verdicts):
    p = tmp_path / name
    p.write_text(json.dumps({
        "tool": "synthetic", "measured_at_sha": sha,
        "measured_by": "unit test", "measured_on": "2026-08-05",
        "verdicts": verdicts}))
    return str(p)


def _row(m, sha):
    return next(r for r in m["commits"] if sha.startswith(r["sha"]))


def test_a_commit_that_breaks_an_existing_test_is_not_no_oracle(tmp_path):
    """THE RED PROOF. Same commit, same code — only the measurement moves."""
    repo, up, breaker, head = _fork(tmp_path)

    fail = _ledger(tmp_path, "fail.json", head, {"foo:t1": "FAIL"})
    red = M.build_map(repo, "refs/remotes/upstream/master", head,
                      M.load_ledgers([fail]))
    assert _row(red, breaker)["bucket"] == M.BROKE, _row(red, breaker)
    assert _row(red, breaker)["broke"] == ["foo:t1"]
    assert M.main(["--repo", str(repo), "--upstream-ref",
                   "refs/remotes/upstream/master", "--head", head,
                   "--verdicts", fail]) == 1

    ok = _ledger(tmp_path, "pass.json", head, {"foo:t1": "PASS"})
    green = M.build_map(repo, "refs/remotes/upstream/master", head,
                        M.load_ledgers([ok]))
    assert _row(green, breaker)["bucket"] == M.NO_ORACLE
    assert M.main(["--repo", str(repo), "--upstream-ref",
                   "refs/remotes/upstream/master", "--head", head,
                   "--verdicts", ok]) == 0


def test_an_unmeasured_module_never_renders_as_a_pass(tmp_path):
    """No verdict for foo ⇒ "breaks nothing" is unavailable, not true."""
    repo, up, breaker, head = _fork(tmp_path)
    other = _ledger(tmp_path, "other.json", head, {"bar:t2": "PASS"})
    m = M.build_map(repo, "refs/remotes/upstream/master", head,
                    M.load_ledgers([other]))
    assert _row(m, breaker)["bucket"] == M.UNMEASURED
    assert _row(m, breaker)["bucket"] != M.NO_ORACLE

    # and with no ledger at all, NOTHING may claim "breaks nothing" — every
    # source commit is either COULD-NOT-MEASURE or a git-decidable SHIPS-ORACLE
    # that also carries the COULD-NOT-MEASURE flag. NO-ORACLE appears nowhere.
    none = M.build_map(repo, "refs/remotes/upstream/master", head, [])
    assert M.NO_ORACLE not in {r["bucket"] for r in none["commits"]}
    assert all(M.UNMEASURED in r["flags"]
               for r in none["commits"] if r["modules"])


def test_a_partial_ledger_does_not_promote_a_module_out_of_unmeasured(tmp_path):
    """§5 — one test is a guess, the full set is a measurement.

    `foo` registers two tests here. A ledger carrying only one of them says
    nothing about the other, so the commit stays COULD-NOT-MEASURE. Without this
    rule the defect returns one level down: sampling a module would read as
    having cleared it.
    """
    repo, up, breaker, head = _fork(tmp_path)
    _write(repo, "src/foo/test/t3.tcl", "run_foo_again\n")
    _write(repo, "src/foo/test/CMakeLists.txt",
           'or_integration_tests(\n  "foo"\n  TESTS\n    t1\n    t3\n)\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "foo: a second test upstream owns")

    partial = _ledger(tmp_path, "partial.json", head, {"foo:t1": "PASS"})
    m = M.build_map(repo, "refs/remotes/upstream/master", head,
                    M.load_ledgers([partial]))
    assert _row(m, breaker)["bucket"] == M.UNMEASURED

    whole = _ledger(tmp_path, "whole.json", head,
                    {"foo:t1": "PASS", "foo:t3": "PASS"})
    m2 = M.build_map(repo, "refs/remotes/upstream/master", head,
                     M.load_ledgers([whole]))
    assert _row(m2, breaker)["bucket"] == M.NO_ORACLE

    # ...but a FAIL is positive evidence and needs no completeness at all.
    one_red = _ledger(tmp_path, "onered.json", head, {"foo:t1": "FAIL"})
    m3 = M.build_map(repo, "refs/remotes/upstream/master", head,
                     M.load_ledgers([one_red]))
    assert _row(m3, breaker)["bucket"] == M.BROKE


def test_source_that_maps_to_no_module_is_unmeasured_not_uncovered(tmp_path):
    """A slice the layout cannot place has no oracle set to have been run.

    Found on the real fork: `1bade74e7` changes only `.gitmodules`, repointing
    src/sta at another OpenSTA. It fell out of the module test with an empty
    module set and landed in NO-ORACLE — "breaks nothing" — about a submodule
    bump that can change anything the timer touches.
    """
    repo, up, breaker, head = _fork(tmp_path)
    _write(repo, ".gitmodules", '[submodule "sta"]\n  path = src/sta\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "submodule: repoint src/sta", who="reyerchu")
    bump = _git(repo, "rev-parse", "HEAD").stdout.strip()

    whole = _ledger(tmp_path, "whole.json", bump,
                    {"foo:t1": "PASS", "bar:t2": "PASS"})
    m = M.build_map(repo, "refs/remotes/upstream/master", bump,
                    M.load_ledgers([whole]))
    assert _row(m, bump)["bucket"] == M.UNMEASURED, _row(m, bump)


def test_a_commit_that_ships_a_test_is_still_ships_oracle(tmp_path):
    repo, up, breaker, head = _fork(tmp_path)
    led = _ledger(tmp_path, "l.json", head, {"foo:t1": "FAIL", "bar:t2": "PASS"})
    m = M.build_map(repo, "refs/remotes/upstream/master", head,
                    M.load_ledgers([led]))
    assert _row(m, head)["bucket"] == M.SHIPS
    assert "src/bar/test/t2.tcl" in _row(m, head)["ships_tests"]

    # ...and it stays SHIPS-ORACLE when its module was NOT measured, because
    # "this commit contains a test" is answered by git, not by a test run. Only
    # NO-ORACLE — "breaks nothing" — yields to COULD-NOT-MEASURE. The flags carry
    # both so the unmeasured half is not lost.
    thin = _ledger(tmp_path, "thin.json", head, {"foo:t1": "FAIL"})
    m2 = M.build_map(repo, "refs/remotes/upstream/master", head,
                     M.load_ledgers([thin]))
    row = _row(m2, head)
    assert row["bucket"] == M.SHIPS, row
    assert M.UNMEASURED in row["flags"], row


def test_the_dead_oracle_names_the_golden_and_whose_it_is(tmp_path):
    """The remedy depends on who owns the golden, so the map must say."""
    repo, up, breaker, head = _fork(tmp_path)
    led = _ledger(tmp_path, "l.json", head, {"foo:t1": "FAIL"})
    m = M.build_map(repo, "refs/remotes/upstream/master", head,
                    M.load_ledgers([led]))
    dead = m["dead_oracles"][0]
    assert dead["test"] == "foo:t1"
    assert dead["golden"]["path"] == "src/foo/test/t1.defok"
    assert dead["golden_is_upstreams"] is True
    assert dead["registered"] == {"cmake": True, "bazel": True}
    assert [s["sha"] for s in dead["suspects"]] == [breaker[:9]]


def test_a_measured_flip_narrows_the_suspects(tmp_path):
    """Two ledgers bracketing the change beat one ledger plus the golden date."""
    repo, up, breaker, head = _fork(tmp_path)
    before = _ledger(tmp_path, "before.json", up, {"foo:t1": "PASS"})
    after = _ledger(tmp_path, "after.json", head, {"foo:t1": "FAIL"})
    m = M.build_map(repo, "refs/remotes/upstream/master", head,
                    M.load_ledgers([before, after]))
    dead = m["dead_oracles"][0]
    assert dead["attribution"] == "measured-flip"
    assert dead["flip_range"] == {"passing_at": up, "failing_at": head}

    only_after = M.build_map(repo, "refs/remotes/upstream/master", head,
                             M.load_ledgers([after]))
    assert only_after["dead_oracles"][0]["attribution"] == "golden-relative"


def test_an_incomplete_ledger_is_reported_as_a_floor(tmp_path):
    """A corrected number presented as complete is the same defect one layer up."""
    repo, up, breaker, head = _fork(tmp_path)
    partial = _ledger(tmp_path, "p.json", head, {"foo:t1": "FAIL"})
    m = M.build_map(repo, "refs/remotes/upstream/master", head,
                    M.load_ledgers([partial]))
    assert m["counting"] == "FLOOR"

    full = _ledger(tmp_path, "f.json", head,
                   {"foo:t1": "FAIL", "bar:t2": "PASS"})
    m2 = M.build_map(repo, "refs/remotes/upstream/master", head,
                     M.load_ledgers([full]))
    assert m2["counting"] == "TOTAL"


def test_registered_tests_reads_both_build_systems(tmp_path):
    """§0.1 — enumerate from the mechanism. A CMake-only test is the #813 gap."""
    repo, up, breaker, head = _fork(tmp_path)
    assert M.registered_tests(repo, "foo") == {"cmake": {"t1"}, "bazel": {"t1"}}
    assert M.registered_tests(repo, "bar") == {"cmake": {"t2"}, "bazel": set()}


def test_a_ledger_without_measurements_is_refused(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"tool": "x"}))
    try:
        M.load_ledgers([str(p)])
    except ValueError:
        pass
    else:
        raise AssertionError("a file with no verdicts was accepted as a ledger")


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
