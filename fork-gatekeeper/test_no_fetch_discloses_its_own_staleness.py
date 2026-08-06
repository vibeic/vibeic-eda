#!/usr/bin/env python3
"""`--no-fetch` must say so, next to the numbers it qualifies.

WHY THIS EXISTS (measured 2026-08-07)
======================================
`fetch_confirms_current`'s `--no-fetch` branch is a deliberate, documented
design choice (see its own docstring): offline mode answers "what do the
clones show AS THEY STAND", and reaching for the network there would make an
offline flag need one. That part is correct and this file does not touch it.

What was missing is DISCLOSURE. The CLI's Q1/Q2 summary and the JSON `rep`
looked IDENTICAL whether the run fetched or not — nothing in the printed
report said which. Measured the same afternoon: `--no-fetch` printed

    Q1  image behind upstream : 0 across 0 fork(s)   [sync 0 · release 0]

against a ledger where a fetching run of the IDENTICAL data read

    Q1  image behind upstream : 117 across 7 fork(s)  [sync 117 · release 0]

Trilinos's clone had not been fetched in over 8 hours. The `--no-fetch` mode
was not wrong about what it measured — it was silent about the fact that what
it measured was hours-old, and a "0" with no caveat reads as a live "0".

THE THIRD STATE, RESTATED FOR THIS FILE
----------------------------------------
"We did not check upstream" is not "upstream agrees with us". The fix is not
to make `--no-fetch` fetch (that would defeat the flag) and it is not to
refuse to run without a fetch (that would take the offline mode out entirely,
the same overcorrection `test_a_working_fetch_that_finds_nothing_is_still_measured`
in the sibling file guards against). It is to LABEL the output with the mode
it was produced under, so nobody — human or agent — can mistake one for the
other by reading the report alone.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), *a],
                          capture_output=True, text=True).stdout.strip()


def _repo(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    _git(d, "init", "-q", "-b", "master")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    return d


def _commit(d: Path, msg: str) -> str:
    (d / "f.txt").write_text(msg)
    _git(d, "add", "f.txt")
    _git(d, "commit", "-qm", msg)
    return _git(d, "rev-parse", "HEAD")


@pytest.fixture()
def one_tool_fleet(tmp_path):
    """A fork, an upstream, a ledger and a Dockerfile pin -- the minimum
    `analyse()` needs to produce one real row, so the report under test is the
    REAL one, not a hand-built stand-in for it."""
    forks_root = tmp_path / "forks"
    up = _repo(forks_root / "_upstream_scratch", "up")
    base = _commit(up, "base")

    fork = _repo(forks_root, "tool")
    _git(fork, "remote", "add", "origin", str(up))
    _git(fork, "remote", "add", "upstream", str(up))
    _git(fork, "fetch", "origin", "-q")
    _git(fork, "fetch", "upstream", "-q")
    _git(fork, "reset", "--hard", base)

    # upstream moves three commits past what the fork's clone last fetched
    _commit(up, "upstream work 1")
    _commit(up, "upstream work 2")
    tip = _commit(up, "upstream work 3")

    repo_root = tmp_path / "repo"
    (repo_root).mkdir()
    (repo_root / "Dockerfile").write_text(f"ARG TOOL_REF={base}\n")

    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "tool.json").write_text(json.dumps({
        "tool": "tool", "integrated": True, "pinned_ref_full": base,
        "ahead": 0,
    }))
    return {"forks_root": forks_root, "repo_root": repo_root, "ledger": ledger,
            "fork": fork, "tip": tip, "base": base}


def _run_report(fleet, no_fetch: bool):
    mod_path = HERE / "fork_gap_report.py"
    spec = importlib.util.spec_from_file_location("fork_gap_report_under_test",
                                                    mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.analyse(fleet["repo_root"], fleet["forks_root"], fleet["ledger"],
                        not no_fetch)


def test_no_fetch_marks_the_report_as_such(one_tool_fleet):
    """THE REGRESSION. `rep["measured_with_fetch"]` must exist and be False."""
    rep = _run_report(one_tool_fleet, no_fetch=True)
    assert rep.get("measured_with_fetch") is False, (
        "a --no-fetch run does not record that it skipped the network; a "
        "reader of the JSON has no way to tell a live number from a stale one")


def test_a_fetching_run_marks_itself_too(one_tool_fleet):
    """BIDIRECTIONAL CONTROL. A fix that hardcodes False would pass the test
    above and lie about every normal run."""
    rep = _run_report(one_tool_fleet, no_fetch=False)
    assert rep.get("measured_with_fetch") is True, (
        "a fetching run does not record that it fetched -- either the field "
        "is missing, or it is stuck at one value regardless of the flag")


def test_the_cli_prints_the_caveat_on_no_fetch(one_tool_fleet, capsys):
    """The disclosure must reach the thing a human actually reads -- the
    printed report -- not live only in a JSON field nobody opens."""
    import fork_gap_report as mod
    rc = mod.main(["--repo", str(one_tool_fleet["repo_root"]),
                   "--forks-root", str(one_tool_fleet["forks_root"]),
                   "--ledger", str(one_tool_fleet["ledger"]),
                   "--no-fetch"])
    out = capsys.readouterr().out
    assert "--no-fetch" in out and ("stale" in out.lower() or "AS THEY" in out), (
        f"the CLI output does not warn that this run skipped the fetch; a "
        f"Q1 number with no caveat reads as a live one. Got:\n{out[:500]}")


def test_the_cli_does_not_print_the_caveat_when_it_fetched(one_tool_fleet, capsys):
    """BIDIRECTIONAL CONTROL. The caveat must not appear on every run --
    that would train readers to ignore it, which is its own failure mode."""
    import fork_gap_report as mod
    mod.main(["--repo", str(one_tool_fleet["repo_root"]),
              "--forks-root", str(one_tool_fleet["forks_root"]),
              "--ledger", str(one_tool_fleet["ledger"])])
    out = capsys.readouterr().out
    assert "--no-fetch" not in out.split("\n")[0], (
        "the staleness caveat is printed even on a fetching run, which would "
        "teach a reader to ignore it")
