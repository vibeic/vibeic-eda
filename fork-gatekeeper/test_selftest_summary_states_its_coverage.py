"""`3 declared, 3 pass` must not be readable as `the forks are verified`.

MEASURED 2026-08-12, on the tree that produced image 0.2.84. The tick's last
word on fork health was:

    fork self-checks: 3 declared, 3 pass, 0 FAIL, 0 COULD-NOT-CHECK

Every number in that line is true. What it does not say is the denominator:

    OpenROAD          102 of our commits    3 declared checks
    yosys              67                   0
    klayout            46                   0
    Trilinos           47                   0
    iverilog           30                   0
    ngspice            26                   0
    magic              26                   0
    sby                23                   0
    pyuvm              22                   0
    verilator          21                   0
    netgen             15                   0
    cocotb-coverage    11                   0
    OpenSTA            10                   0
    cocotb              6                   0
    ... and four more carrying 1-2 each

One fork of twenty declares anything. 102 of our 465 own commits are covered;
**363 are not**. And the three that exist on OpenROAD were not foresight -- each
was written AFTER a defect of that class had already shipped:

    dup-logger-ids            after one warning ID was renumbered by upstream
                              FOUR times through textually-clean merges
    test-registration-parity  after 27 of our own tests stopped being reachable
                              by the build that makes the binary
    severity-golden-parity    after a severity promotion left four upstream
                              tests red for weeks

So the classes are known to be real, and nineteen forks are exposed to them with
nothing watching. The summary line is what a reader consults to learn otherwise,
and it is silent.

THE DEFECT IS THE UNSTATED DENOMINATOR, NOT THE MISSING CHECKS.
Writing nineteen checks is a campaign. Refusing to let one line imply a coverage
it does not have is one measurement, available today, and it is the thing that
keeps the campaign from being forgotten.

WHY THIS DOES NOT CHANGE THE EXIT CODE
--------------------------------------
The program's question is "did the DECLARED checks pass", and its rc answers
exactly that: 0 clean, 1 red, 2 could-not-run. Coverage is a different question
and folding it into rc would either make the tick permanently rc=2 -- retiring a
signal that currently means something specific and rare (a clone vanished, a
fork deleted its checker) -- or make it red every day for a condition no tick
can clear. Both trade a working instrument for a louder one. The fix is that
the report states the fact; the rc keeps answering its own question.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_fork_selftests", HERE / "check_fork_selftests.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_fork_selftests"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()


def _forks_json(tmp_path, entries):
    p = tmp_path / "FORKS.json"
    p.write_text(json.dumps({"org": "vibeic", "forks": entries}, indent=2))
    return p


def _ledger(tmp_path, rows):
    d = tmp_path / "ledger"
    d.mkdir(exist_ok=True)
    for tool, ahead in rows.items():
        (d / f"{tool}.json").write_text(json.dumps({"tool": tool, "ahead": ahead}))
    return d


CHECK = [{"name": "noop", "path": "true.py", "cmd": ["true"], "why": "x"}]


def test_it_names_the_patched_forks_that_declare_nothing(tmp_path):
    """The regression: silence about nineteen unwatched forks."""
    fj = _forks_json(tmp_path, [
        {"tool": "OpenROAD", "upstream": "a/b", "role": "pnr", "post_merge_check": CHECK},
        {"tool": "yosys", "upstream": "a/c", "role": "synth"},
        {"tool": "klayout", "upstream": "a/d", "role": "drc"},
    ])
    led = _ledger(tmp_path, {"OpenROAD": 102, "yosys": 67, "klayout": 46})

    gap, covered, unmeasurable = M.coverage(fj, led)

    assert {t for t, _ in gap} == {"yosys", "klayout"}, (
        "a fork carrying our patches with no declared check must be named")
    assert dict(gap)["yosys"] == 67
    assert covered == [("OpenROAD", 102)]
    assert unmeasurable == []


def test_a_fork_with_no_patches_of_ours_is_not_a_gap(tmp_path):
    """Obverse. We do not want a check on a fork we have never touched.

    Without this, `coverage` could pass the first test by reporting every
    undeclared fork, which would drown the nineteen that matter in eighteen
    that do not.
    """
    fj = _forks_json(tmp_path, [
        {"tool": "slang", "upstream": "a/b", "role": "sv frontend"},
        {"tool": "Xyce", "upstream": "a/c", "role": "spice"},
    ])
    led = _ledger(tmp_path, {"slang": 0, "Xyce": 0})

    gap, covered, unmeasurable = M.coverage(fj, led)
    assert gap == [] and covered == []


def test_a_fork_with_no_ledger_row_is_unmeasurable_not_clean(tmp_path):
    """Absence of a measurement is never a measurement of zero."""
    fj = _forks_json(tmp_path, [{"tool": "ngspice", "upstream": "a/b", "role": "spice"}])
    led = _ledger(tmp_path, {})

    gap, covered, unmeasurable = M.coverage(fj, led)
    assert gap == [] and covered == []
    assert unmeasurable == ["ngspice"], (
        "a fork with no ledger row must be reported as unmeasured, never "
        "silently counted as carrying no patches")


def test_the_summary_the_tick_prints_carries_the_denominator(tmp_path, capsys):
    """End to end: the line a human reads must state coverage.

    This is the one that pins the actual regression -- `coverage()` could be
    perfect and never called.
    """
    fj = _forks_json(tmp_path, [
        {"tool": "OpenROAD", "upstream": "a/b", "role": "pnr", "post_merge_check": CHECK},
        {"tool": "yosys", "upstream": "a/c", "role": "synth"},
        {"tool": "klayout", "upstream": "a/d", "role": "drc"},
    ])
    led = _ledger(tmp_path, {"OpenROAD": 102, "yosys": 67, "klayout": 46})
    clone = tmp_path / "OpenROAD"
    clone.mkdir()
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    # The declared checker must EXIST in the tree, or the run is
    # COULD-NOT-CHECK (rc=2) and says nothing about coverage.
    (clone / "true.py").write_text("")

    M.main(["--forks-dir", str(tmp_path), "--forks-json", str(fj),
            "--ledger", str(led)])
    out = capsys.readouterr().out

    assert "yosys" in out and "klayout" in out, (
        "the unwatched forks must be named in the output a human reads")
    assert "113" in out, (
        "the number of our commits with no declared check (67+46) must appear; "
        f"got:\n{out}")


def test_coverage_reporting_does_not_change_the_exit_code(tmp_path):
    """The rc still answers 'did the declared checks pass', and nothing else."""
    fj = _forks_json(tmp_path, [
        {"tool": "OpenROAD", "upstream": "a/b", "role": "pnr", "post_merge_check": CHECK},
        {"tool": "yosys", "upstream": "a/c", "role": "synth"},
    ])
    led = _ledger(tmp_path, {"OpenROAD": 102, "yosys": 67})
    clone = tmp_path / "OpenROAD"
    clone.mkdir()
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    # The declared checker must EXIST in the tree, or the run is
    # COULD-NOT-CHECK (rc=2) and says nothing about coverage.
    (clone / "true.py").write_text("")

    rc = M.main(["--forks-dir", str(tmp_path), "--forks-json", str(fj),
                 "--ledger", str(led)])
    assert rc == 0, (
        "a passing declared check with an uncovered fork alongside it is rc=0: "
        "coverage is reported, not conflated into the verdict")
