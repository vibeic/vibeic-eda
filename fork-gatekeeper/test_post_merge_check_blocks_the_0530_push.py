#!/usr/bin/env python3
"""The 05:30 round's FIRST merger must obey the post-merge checks too (#89).

`run_0530.sh` runs two automatic paths from upstream onto a published branch:

    1. daily_0530.py --apply    step1_upstream / step2_ours, then `git push`
    2. run_tick.sh -> daily_merge.py

Wiring the checks into (2) alone would have produced a gate that is correct,
tested, and ABSENT FROM THE PATH THAT ACTUALLY MERGES EVERY MORNING — which is
the defect vibeic-eda#89 is about, reproduced by the fix for it. The fork tip
that carries our OpenROAD patches is advanced by (1): its merge commits are the
ones reading `Merge upstream into master (daily 05:30)`.

The two programs differ in one way this pins on purpose. `daily_merge` merges in
a throwaway worktree, so refusing leaves nothing behind. `daily_0530` merges into
the shared checkout's own mainline, so refusing leaves the REMOTE untouched and
the LOCAL branch holding an unpublished merge. Undoing that with `reset --hard`
on a tree other sessions share would risk discarding work this program did not
create, so it is reported rather than reverted — and the report has to say so,
or the next reader finds a local branch mysteriously ahead.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
_spec = importlib.util.spec_from_file_location("d0530_pmc", _HERE / "daily_0530.py")
D = importlib.util.module_from_spec(_spec)
sys.modules["d0530_pmc"] = D
_spec.loader.exec_module(D)


def _git(repo, *a):
    p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                       text=True, timeout=60)
    return p


FAILING = "#!/usr/bin/env python3\nimport sys\nprint('dup id 0524', file=sys.stderr)\nsys.exit(1)\n"
PASSING = "#!/usr/bin/env python3\nprint('clean')\n"

#: What the real `round_record.write` returns on success. Stubbed with the
#: same SHAPE, not with an empty container: a stub whose shape is wrong
#: makes the caller crash for a reason that has nothing to do with the test.
_RETAINED = {"stamp": "test", "rows": 0}


def _fleet(tmp_path: pathlib.Path, checker: str):
    """An upstream with a new commit, and our fork one behind it."""
    up = tmp_path / "up"
    up.mkdir()
    _git(up, "init", "-q", "-b", "master", ".")
    _git(up, "config", "user.email", "a@a")
    _git(up, "config", "user.name", "A")
    (up / "etc").mkdir()
    (up / "etc" / "check.py").write_text(checker)
    (up / "f.txt").write_text("base\n")
    _git(up, "add", "-A", ".")
    _git(up, "commit", "-q", "-m", "base")

    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "master", str(origin))
    _git(up, "remote", "add", "origin", str(origin))
    _git(up, "push", "-q", "origin", "master")

    forks = tmp_path / "forks"
    forks.mkdir()
    fork = forks / "OpenROAD"
    subprocess.run(["git", "clone", "-q", str(origin), str(fork)], timeout=120)
    _git(fork, "config", "user.email", "b@b")
    _git(fork, "config", "user.name", "B")
    _git(fork, "remote", "add", "upstream", str(up))

    # upstream moves ahead; the fork does not. The merge will be clean.
    (up / "g.txt").write_text("upstream's new file\n")
    _git(up, "add", "-A", ".")
    _git(up, "commit", "-q", "-m", "upstream: a clean, conflict-free change")
    return forks, fork, origin


def _run(tmp_path, monkeypatch, checker):
    forks, fork, origin = _fleet(tmp_path, checker)
    before = _git(origin, "rev-parse", "refs/heads/master").stdout.strip()

    monkeypatch.setattr(D, "FORKS", forks)
    monkeypatch.setattr(D, "image_pins", lambda *a, **k: {})
    # The declaration, injected rather than read from the shipped FORKS.json, so
    # the test states its own premise instead of depending on today's registry.
    monkeypatch.setattr(D, "post_merge_checks", lambda repo: [
        {"name": "dup-logger-ids", "path": "etc/check.py",
         "cmd": ["python3", "etc/check.py"]}])
    monkeypatch.setattr(D, "step2b_ai_decisions", lambda *a, **k: {})
    monkeypatch.setattr(D, "step4_prune", lambda *a, **k: None)
    monkeypatch.setenv("GK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(D.round_record, "write", lambda *a, **k: _RETAINED)

    rc = D.main_(["--apply", "--no-ai", "--skip-build"])
    after = _git(origin, "rev-parse", "refs/heads/master").stdout.strip()
    return rc, before, after, fork, origin


def test_a_failing_check_stops_the_0530_push(tmp_path, monkeypatch):
    rc, before, after, fork, origin = _run(tmp_path, monkeypatch, FAILING)
    assert after == before, \
        "origin advanced — the 05:30 merger published a tree the check rejected"
    assert rc == 1, f"the round reported clean (rc={rc}) after refusing a fork"


def test_a_clean_check_lets_the_0530_push_through(tmp_path, monkeypatch):
    """The control. Without it, the test above is satisfied by a program that
    has stopped pushing at all."""
    rc, before, after, fork, origin = _run(tmp_path, monkeypatch, PASSING)
    assert after != before, "a clean check must not stop the merge"
    assert rc == 0, f"rc={rc} on a clean round"


def test_the_refusal_says_the_local_branch_still_holds_the_merge(tmp_path, monkeypatch):
    """This merger works in the SHARED checkout, so 'not published' is not the
    same as 'nothing happened'. Anyone reading the report has to be told which
    of the two they are looking at."""
    forks, fork, origin = _fleet(tmp_path, FAILING)
    monkeypatch.setattr(D, "FORKS", forks)
    monkeypatch.setattr(D, "image_pins", lambda *a, **k: {})
    monkeypatch.setattr(D, "post_merge_checks", lambda repo: [
        {"name": "dup-logger-ids", "path": "etc/check.py",
         "cmd": ["python3", "etc/check.py"]}])
    monkeypatch.setattr(D, "step2b_ai_decisions", lambda *a, **k: {})
    monkeypatch.setattr(D, "step4_prune", lambda *a, **k: None)
    monkeypatch.setenv("GK_STATE_DIR", str(tmp_path / "state"))
    captured = {}
    monkeypatch.setattr(D.round_record, "write",
                        lambda report, *a, **k: (captured.update(report), _RETAINED)[1])
    D.main_(["--apply", "--no-ai", "--skip-build"])
    rep = captured["OpenROAD"]
    assert "BLOCKED by post-merge check" in rep["push"], rep
    assert "previous tip" in rep["push"] and "unpublished merge" in rep["push"], rep
    assert rep["needs_human"] is True
    assert rep["post_merge_check"][0]["ok"] is False
    # …and the claim in that sentence is TRUE, not just present.
    local = _git(fork, "rev-parse", "master").stdout.strip()
    remote = _git(origin, "rev-parse", "refs/heads/master").stdout.strip()
    assert local != remote, "the report claims a local merge that is not there"


def test_a_fork_declaring_nothing_is_unaffected(tmp_path, monkeypatch):
    forks, fork, origin = _fleet(tmp_path, PASSING)
    before = _git(origin, "rev-parse", "refs/heads/master").stdout.strip()
    monkeypatch.setattr(D, "FORKS", forks)
    monkeypatch.setattr(D, "image_pins", lambda *a, **k: {})
    monkeypatch.setattr(D, "post_merge_checks", lambda repo: [])
    monkeypatch.setattr(D, "step2b_ai_decisions", lambda *a, **k: {})
    monkeypatch.setattr(D, "step4_prune", lambda *a, **k: None)
    monkeypatch.setenv("GK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(D.round_record, "write", lambda *a, **k: _RETAINED)
    assert D.main_(["--apply", "--no-ai", "--skip-build"]) == 0
    assert _git(origin, "rev-parse", "refs/heads/master").stdout.strip() != before


def test_both_mergers_share_one_implementation():
    """Two copies of a gate is how two programs come to disagree about what a
    gate means — vibeic-eda#29 was exactly that, `branch_is_ours` implemented
    twice and answering differently about the same four pins."""
    src = (_HERE / "daily_0530.py").read_text()
    assert "from daily_merge import post_merge_checks, run_post_merge_checks" in src
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "def run_post_merge_checks" not in code, \
        "daily_0530 grew its own copy of the checker runner"
