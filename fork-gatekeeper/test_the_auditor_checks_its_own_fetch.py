#!/usr/bin/env python3
"""`fork_gap_report`'s own fetch was never checked — the auditor did not audit itself.

    if fetch:
        _git(clone, "fetch", "-q", "--all", timeout=180)      # result discarded

Every number this program produces — `sync_lag`, `release_lag`, `image_behind` —
is a `rev-list` against `upstream/<branch>`, a REMOTE-TRACKING REF that only means
anything if the fetch refreshed it. A fetch that did not left the counts running
against whatever the clone last managed to fetch, and a stale ref counts FEWER
commits behind than there are. Small numbers read as health, and THIS program's
numbers are the ones the published page prints.

The same shape was fixed in `daily_0530` twice by 2b33719.
`discover_forks._local_compare` never had it. The program whose job is to check
the other two was the last to check itself.

WHY THESE TESTS FAIL BEHAVIOURALLY AGAINST THE OLD TREE
=======================================================
Every test below drives `analyse()` — which exists identically on both trees — over
REAL git repositories, and asserts on the REPORTED NUMBER. None of them names
`fetch_confirms_current`, `_run` or `_first_error_line`. Dropped onto the pristine
tree they fail because the old code PUBLISHES A CONFIDENT ZERO for a fork that is
three commits behind, which is the defect; they do not fail on an absent symbol,
which would prove only that a symbol is absent. (#83's control died that way, and a
peer caught the same thing in their own test the same day.)

The one exception is `test_the_remote_question_is_not_respelled`, which is a
STRUCTURAL claim about not writing a fourth copy of one rule and is honestly labelled
as such — it is not evidence that the defect exists.

BIDIRECTIONAL. `test_a_working_fetch_that_finds_nothing_is_still_measured` is the
control that matters for review: a guard that reported UNKNOWN for every fetch would
satisfy every other test here and take the round out entirely.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load():
    """The `fork_gap_report` sitting NEXT TO THIS FILE — so the same test file
    dropped into a pristine checkout exercises that checkout's program."""
    spec = importlib.util.spec_from_file_location(
        "fgr_under_test", HERE / "fork_gap_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fgr_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(cwd, *a, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *a],
                       capture_output=True, text=True, timeout=60)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} -> {r.returncode}: {r.stderr}")
    return r.stdout.strip()


def _commit(repo, name, msg):
    (repo / name).write_text(msg, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def fleet(tmp_path):
    """A REAL upstream, a REAL fork clone, and a REAL pinned Dockerfile.

    Nothing is stubbed: `analyse()` runs its own `ls-tree`, `show`, `rev-parse`,
    `rev-list` and `fetch` against these. A fixture that mocked git would prove the
    test's own arithmetic and nothing about the program.
    """
    up = tmp_path / "upstream"; up.mkdir()
    _git(up, "init", "-q", "-b", "master")
    base = _commit(up, "a.txt", "base")

    fork = tmp_path / "forks" / "toolx"; fork.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(up), str(fork))
    _git(fork, "remote", "add", "upstream", str(up))
    _git(fork, "fetch", "-q", "upstream")

    # The image's pin: the fork tip, i.e. level to begin with.
    pin = _git(fork, "rev-parse", "HEAD")

    repo = tmp_path / "eda"; repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "Dockerfile").write_text(f"FROM x\nARG TOOLX_REF={pin}\n", encoding="utf-8")
    _git(repo, "add", "Dockerfile")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pin")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    ledger = tmp_path / "ledger"; ledger.mkdir()
    (ledger / "toolx.json").write_text(json.dumps({
        "tool": "toolx", "integrated": True, "vibeic_branch": "master",
        "upstream_default_branch": "master", "pinned_ref_full": pin,
        "pin_kind": "pin", "ahead": 0}), encoding="utf-8")

    return {"up": up, "fork": fork, "repo": repo, "ledger": ledger,
            "forks_root": fork.parent, "pin": pin, "base": base}


def _row(fleet, fetch=True):
    m = _load()
    rep = m.analyse(fleet["repo"], fleet["forks_root"], fleet["ledger"], fetch)
    row = next(r for r in rep["rows"] if r["tool"] == "toolx")
    return rep, row


def _break_the_fetch(fleet):
    """Make the fetch FAIL while the tracking ref keeps its old, stale value."""
    _git(fleet["fork"], "remote", "set-url", "upstream",
         str(fleet["up"].parent / "does-not-exist"))
    _git(fleet["fork"], "remote", "set-url", "origin",
         str(fleet["up"].parent / "does-not-exist"))


# ── the control that must stay green ─────────────────────────────────────────
def test_a_working_fetch_that_finds_nothing_is_still_measured(fleet):
    """A guard that called every fetch UNKNOWN would pass every other test here
    and take the daily round out. Level with upstream must read 0, MEASURED."""
    rep, row = _row(fleet)
    assert row["note"] is None, row["note"]
    assert row["image_behind"] == 0, row
    assert rep["q1_unmeasured"] == [], rep["q1_unmeasured"]


def test_a_working_fetch_reports_the_real_distance(fleet):
    """And it still counts. Upstream +3 must read 3, not 0 and not unknown."""
    for i in range(3):
        _commit(fleet["up"], f"n{i}.txt", f"n{i}")
    rep, row = _row(fleet)
    assert row["note"] is None, row["note"]
    assert row["image_behind"] == 3, row
    assert rep["q1_unmeasured"] == []


# ── the defect ───────────────────────────────────────────────────────────────
def test_a_failed_fetch_is_not_reported_as_zero(fleet):
    """THE ONE THAT MATTERS. Upstream is 3 ahead; the fetch cannot run; the clone's
    tracking ref still equals the pin.

    Old tree: `image_behind == 0` — a confident, published, wrong number.
    Required: NOT MEASURED, with a cause.
    """
    for i in range(3):
        _commit(fleet["up"], f"n{i}.txt", f"n{i}")
    _break_the_fetch(fleet)
    rep, row = _row(fleet)
    assert row["image_behind"] is None, (
        f"a fetch that could not run left this reported as "
        f"image_behind={row['image_behind']} while upstream is 3 ahead. "
        f"An absent measurement rendered as the reassuring answer.")
    assert row["note"], "NOT MEASURED must say why"
    assert "toolx" in rep["q1_unmeasured"], rep["q1_unmeasured"]


def test_the_unmeasured_row_names_the_cause_not_the_boilerplate(fleet):
    """git puts the diagnosis first and `and the repository exists.` last."""
    _break_the_fetch(fleet)
    _, row = _row(fleet)
    assert row["note"], "no note at all"
    assert not row["note"].rstrip().endswith("and the repository exists."), row["note"]


def test_a_fetch_that_exits_zero_but_refreshes_nothing_is_not_current(fleet):
    """rc=0 IS NOT ENOUGH, which is why this is not a one-line rc check.

    The upstream remote is given a refspec that maps a branch it will never
    update, so `fetch --all` exits 0 and `upstream/master` never moves, while the
    remote's master really has advanced. Only asking the remote can separate
    "nothing new upstream" from "this fetch refreshed nothing".
    """
    _git(fleet["fork"], "config", "remote.upstream.fetch",
         "+refs/heads/nonexistent:refs/remotes/upstream/nonexistent")
    for i in range(3):
        _commit(fleet["up"], f"n{i}.txt", f"n{i}")
    rep, row = _row(fleet)
    assert row["image_behind"] is None, (
        f"the fetch exited 0 and refreshed nothing while upstream moved 3 ahead, "
        f"and this was reported as image_behind={row['image_behind']}")
    assert "toolx" in rep["q1_unmeasured"]


def test_no_fetch_mode_reaches_for_no_network(fleet):
    """`--no-fetch` asks about the clones EXACTLY AS THEY STAND, and must stay
    offline — including the confirmation.

    The defect is on the fetch path; `--no-fetch` does not take it. A guard that
    called a remote here would make the offline mode need a network. Pinned with
    the remote made unreachable: the answer must still come from the clone.
    """
    _break_the_fetch(fleet)          # no remote is reachable at all
    rep, row = _row(fleet, fetch=False)
    assert row["image_behind"] == 0, row
    assert rep["q1_unmeasured"] == [], (
        "`--no-fetch` reached for the network: an offline read must not depend on "
        "a remote being up")


# ── structural, and labelled as such ─────────────────────────────────────────
def test_the_remote_question_is_not_respelled():
    """NOT evidence of the defect — a claim about not writing a fourth copy.

    `daily_0530._remote_confirms` and `discover_forks._ls_remote_head` already ask
    a remote what a branch points at. Three programs need that answer now, and a
    fourth spelling of one rule is how this defect reached three files.

    PARSED, NOT GREPPED — twice now a substring search in this campaign has gone
    red on the prose EXPLAINING the rule. The docstring below says "one single-ref
    `ls-remote`"; a text search cannot tell that from code that runs one.
    """
    import ast
    src = (HERE / "fork_gap_report.py").read_text(encoding="utf-8")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "fetch_confirms_current"),
              None)
    if fn is None:
        pytest.skip("pre-fix tree: the guard does not exist yet")
    names = {getattr(n.func, "attr", getattr(n.func, "id", ""))
             for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "_ls_remote_head" in names, (
        "the guard asks a remote without reusing `discover_forks._ls_remote_head`, "
        "the function that already answers exactly this question")
    # A literal "ls-remote" argument would mean it shelled out itself. Constants
    # only — a mention inside the docstring is a string too, but not an argument.
    argv = [c.value for n in ast.walk(fn) if isinstance(n, ast.Call)
            for c in n.args if isinstance(c, ast.Constant) and isinstance(c.value, str)]
    assert "ls-remote" not in argv, (
        "`git ls-remote` is invoked directly inside the guard rather than through "
        "the function that already does it — that is the fourth copy of one rule")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
