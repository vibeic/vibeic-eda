#!/usr/bin/env python3
"""A clean merge is not a correct merge — `daily_merge` must be able to refuse one.

WHAT THIS PINS
==============
`git merge` refuses only on TEXTUAL conflict. The failure that actually keeps
happening to this fleet has no conflict at all: our resistance-clamp warning in
`src/rcx/src/extmain_v2.cpp` has been renumbered THREE times in six days because
upstream grew a logger message id underneath it —

    515 -> 519   2f9fbcd47e   upstream took RCX 515 in multiChipExtractor.cpp
    519 -> 524   5bb6ca31ee   upstream took RCX 519 in OpenRCX.tcl
    524 -> 527   724a389026   upstream took RCX 524 in ext.i

— and every one of those merges was clean, because the two call sites live in
different files. `etc/find_messages.py` sees it; nothing ran it at merge time
(vibeic-eda#89).

The tests below are in two layers, on purpose.

  * The MECHANISM tests are hermetic: two bare repos and a checker the test
    writes itself. They prove the four behaviours that make the gate a gate —
    non-zero blocks the push, missing blocks the push, malformed blocks the
    push, and clean PUBLISHES. The last one is not padding: a gate that blocks
    everything is not a gate either, and without it three of these tests pass
    against a `merge_one` that never pushes at all.

  * The FIDELITY test drives the real `etc/find_messages.py` against a real
    reproduction of the 524 collision. It is what makes the mechanism tests
    mean something: they prove the wiring, this proves the wiring is attached
    to the detector the three historical fixes actually used.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_merge as D  # noqa: E402

BRANCH = "vibeic/openroad-integration"


def _git(*args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {' '.join(args)} failed: {p.stderr}"
    return p.stdout.strip()


def _commit(tree: Path, msg: str):
    _git("add", "-f", ".", cwd=tree)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", msg, cwd=tree)


def _fleet(tmp_path: Path, our_file: str, our_body: str,
           upstream_file: str, upstream_body: str, extra: dict = None):
    """A two-remote fleet whose two sides touch DIFFERENT files.

    That is the whole point: the merge must be textually clean, so that anything
    the test catches is caught by the post-merge check and not by git.
    """
    up_bare = tmp_path / "upstream.git"
    fork_bare = tmp_path / "fork.git"
    for b in (up_bare, fork_bare):
        _git("init", "--bare", "-b", "master", str(b), cwd=tmp_path)

    # --- the common ancestor, pushed to both sides
    base = tmp_path / "base"
    base.mkdir()
    _git("init", "-b", "master", cwd=base)
    for rel, body in (extra or {}).items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(body)
    (base / "README").write_text("base\n")
    _commit(base, "base")
    _git("remote", "add", "up", str(up_bare), cwd=base)
    _git("remote", "add", "fk", str(fork_bare), cwd=base)
    _git("push", "up", "master", cwd=base)
    _git("push", "fk", f"master:{BRANCH}", cwd=base)

    # --- upstream moves in ITS file
    _git("checkout", "-B", "upmove", cwd=base)
    (base / upstream_file).parent.mkdir(parents=True, exist_ok=True)
    (base / upstream_file).write_text(upstream_body)
    _commit(base, "upstream: take the id")
    _git("push", "up", "upmove:master", cwd=base)

    # --- our fork moves in OURS, from the same ancestor
    _git("checkout", "-B", "ourmove", "master", cwd=base)
    (base / our_file).parent.mkdir(parents=True, exist_ok=True)
    (base / our_file).write_text(our_body)
    _commit(base, "ours: the resistance-clamp warning")
    _git("push", "fk", f"ourmove:{BRANCH}", cwd=base)

    # --- the checkout daily_merge operates on
    root = tmp_path / "forks"
    root.mkdir()
    src = root / "OpenROAD"
    _git("clone", "--quiet", str(fork_bare), str(src), cwd=tmp_path)
    _git("remote", "add", "upstream", str(up_bare), cwd=src)
    _git("fetch", "--quiet", "upstream", cwd=src)
    # Set upstream/HEAD locally so merge_one never falls back to `gh api`.
    _git("remote", "set-head", "upstream", "master", cwd=src)
    return root, fork_bare


def _tip(bare: Path) -> str:
    return _git("rev-parse", f"refs/heads/{BRANCH}", cwd=bare)


def _run(monkeypatch, root: Path, checks):
    monkeypatch.setattr(D, "FORKS_ROOT", root)
    return D.merge_one("OpenROAD", BRANCH, dry=False, checks=checks)


# ---------------------------------------------------------------- mechanism

#: A checker the test owns, so the mechanism tests do not depend on any fork.
FAILING = "#!/usr/bin/env python3\nimport sys\nprint('boom: id 0524 used 2 times')\nsys.exit(1)\n"
PASSING = "#!/usr/bin/env python3\nprint('clean')\n"


def _mech(tmp_path, checker_body):
    return _fleet(
        tmp_path,
        our_file="src/ours.cpp", our_body="warn(RCX, 524)\n",
        upstream_file="src/theirs.cpp", upstream_body="warn(RCX, 524)\n",
        extra={"etc/check.py": checker_body})


CHECK = [{"name": "dup-logger-ids", "path": "etc/check.py",
          "cmd": ["python3", "etc/check.py"]}]


def test_a_failing_check_blocks_the_publish(tmp_path, monkeypatch):
    """rc!=0 on the merged tree -> nothing pushed, fork left at its previous tip."""
    root, bare = _mech(tmp_path, FAILING)
    before = _tip(bare)
    res = _run(monkeypatch, root, CHECK)
    assert res["state"] == "POST_MERGE_CHECK_FAILED", res
    assert res["conflicts"] == [], "the merge must have been textually CLEAN"
    assert "boom" in res["detail"], res["detail"]
    assert _tip(bare) == before, \
        "the fork tip MOVED — the check reported and the merge published anyway"


def test_a_clean_check_publishes(tmp_path, monkeypatch):
    """…and the other direction, or every test above is satisfied by never pushing."""
    root, bare = _mech(tmp_path, PASSING)
    before = _tip(bare)
    res = _run(monkeypatch, root, CHECK)
    assert res["state"] == "MERGED", res
    assert _tip(bare) != before, "a clean check must not stop the merge"
    assert [c["ok"] for c in res["checks"]] == [True]


def test_a_declared_check_missing_from_the_tree_is_not_a_pass(tmp_path, monkeypatch):
    """MISSING is not CLEAN. If upstream deletes or moves the checker, the merge
    is one nobody verified — and this repo's oldest rule is that a guard which
    could not run did not pass."""
    root, bare = _mech(tmp_path, PASSING)
    before = _tip(bare)
    res = _run(monkeypatch, root, [{"name": "gone", "path": "etc/not_here.py",
                                    "cmd": ["python3", "etc/not_here.py"]}])
    assert res["state"] == "POST_MERGE_CHECK_FAILED", res
    assert "MISSING" in res["detail"]
    assert _tip(bare) == before


def test_a_malformed_declaration_is_not_a_pass(tmp_path, monkeypatch):
    """A typo in FORKS.json must not read as 'this fork declares no checks',
    because that is silently identical to deleting the gate."""
    root, bare = _mech(tmp_path, PASSING)
    before = _tip(bare)
    res = _run(monkeypatch, root, [{"name": "typo", "cmd": "python3 etc/check.py"}])
    assert res["state"] == "POST_MERGE_CHECK_FAILED", res
    assert "MALFORMED" in res["detail"]
    assert _tip(bare) == before


def test_a_fork_declaring_nothing_still_merges(tmp_path, monkeypatch):
    """Most sources have no invariant a merge can silently break. Declaring
    nothing is an allowed answer, not a blocked fork."""
    root, bare = _mech(tmp_path, PASSING)
    before = _tip(bare)
    res = _run(monkeypatch, root, [])
    assert res["state"] == "MERGED", res
    assert _tip(bare) != before


def test_a_check_that_cannot_be_executed_blocks(tmp_path, monkeypatch):
    """Timeout / OSError is 'we could not tell', and we could not tell is not a
    pass — the rule capability_gate uses for its rc=2."""
    root, bare = _mech(tmp_path, PASSING)
    before = _tip(bare)
    monkeypatch.setattr(D, "POST_MERGE_CHECK_TIMEOUT", 0)
    res = _run(monkeypatch, root, CHECK)
    assert res["state"] == "POST_MERGE_CHECK_FAILED", res
    assert _tip(bare) == before


def test_the_checks_run_before_the_push_not_after(tmp_path, monkeypatch):
    """Order is the whole design. Checking after the push would mean the broken
    merge is already on the build branch and the report is an obituary."""
    src = Path(D.__file__).read_text()
    body = src[src.index("def merge_one("):]
    assert body.index("run_post_merge_checks") < body.index('"push"'), \
        "post-merge checks run AFTER the push — the fork is already published"


# ---------------------------------------------------------------- fidelity

def _find_messages() -> Path | None:
    """The real checker, from any OpenROAD checkout on this host."""
    for root in (Path(os.environ.get("VIBEIC_FORKS_ROOT", "/home/reyerchu/vibe-ic-forks")),
                 Path("/home/reyerchu/vibe-ic-forks-wt")):
        if not root.is_dir():
            continue
        for c in sorted(root.glob("OpenROAD*/etc/find_messages.py")):
            return c
    return None


#: The two call sites of the REAL third collision (724a389026): ours in
#: extmain_v2.cpp, upstream's in ext.i, both on RCX 524, in different files.
OURS_524 = '''#include "rcx.h"
void ExtMain::clamp() {
  logger_->warn(RCX, 524, "Resistance model lookup fell out of range {} times.", n);
}
'''
OURS_527 = OURS_524.replace("RCX, 524", "RCX, 527")
THEIRS_524 = '''
  logger->error(RCX, 524, "Extraction rules file {} not found.", file);
'''


@pytest.mark.skipif(_find_messages() is None,
                    reason="no OpenROAD checkout on this host to take the real "
                           "etc/find_messages.py from")
def test_the_real_rcx_524_collision_is_refused(tmp_path, monkeypatch):
    """The historical defect, reproduced, driven through the real detector.

    Reproduces 724a389026 exactly: upstream's ext.i takes RCX 524 while our
    extmain_v2.cpp already holds it. Different files, so git merges it silently
    — which is how this shipped three times.
    """
    checker = _find_messages().read_text()
    root, bare = _fleet(
        tmp_path,
        our_file="src/rcx/src/extmain_v2.cpp", our_body=OURS_524,
        upstream_file="src/rcx/src/ext.i", upstream_body=THEIRS_524,
        extra={"etc/find_messages.py": checker})
    before = _tip(bare)
    res = _run(monkeypatch, root, D.post_merge_checks("OpenROAD"))
    assert res["state"] == "POST_MERGE_CHECK_FAILED", res
    assert res["conflicts"] == [], \
        "git must have merged this cleanly, or the test is not reproducing #89"
    assert "0524 used 2 times" in res["detail"], res["detail"]
    assert _tip(bare) == before, "the collision was published anyway"


@pytest.mark.skipif(_find_messages() is None, reason="no OpenROAD checkout")
def test_the_same_merge_with_the_fix_applied_publishes(tmp_path, monkeypatch):
    """524 -> 527 is the fix 724a389026 actually made. With it, the identical
    merge must go through — otherwise the check is red for the wrong reason and
    would block every OpenROAD merge forever."""
    checker = _find_messages().read_text()
    root, bare = _fleet(
        tmp_path,
        our_file="src/rcx/src/extmain_v2.cpp", our_body=OURS_527,
        upstream_file="src/rcx/src/ext.i", upstream_body=THEIRS_524,
        extra={"etc/find_messages.py": checker})
    before = _tip(bare)
    res = _run(monkeypatch, root, D.post_merge_checks("OpenROAD"))
    assert res["state"] == "MERGED", res
    assert _tip(bare) != before


# ---------------------------------------------------------------- declaration

def test_openroad_declares_the_dup_id_check(tmp_path):
    """The mechanism with no consumer is the defect it was built to fix."""
    checks = D.post_merge_checks("OpenROAD")
    assert checks, "OpenROAD declares no post-merge check — #89 is not wired"
    assert any("find_messages" in " ".join(c.get("cmd", [])) for c in checks)


def test_every_declared_check_names_a_fork_that_is_merged():
    """A declaration keyed to a tool nothing merges is a gate that cannot fire,
    and it looks exactly like a gate that passed."""
    forks = json.loads((Path(D.__file__).parent / "FORKS.json").read_text())["forks"]
    names = {f["tool"] for f in forks}
    for f in forks:
        if f.get("post_merge_check"):
            assert f["tool"] in names


def test_the_declared_command_can_actually_run_from_a_plain_checkout():
    """etc/find_dup_ids.sh — the script vibeic-eda#89 names — resolves
    find_messages.py through BAZEL RUNFILES and exits 1 from a plain checkout,
    which a merge worktree is. Registering it would have produced a check that
    is red every day for a reason that is not a collision. This pins the reason
    the declaration names find_messages.py instead, so nobody 'corrects' it back.
    """
    fm = _find_messages()
    if fm is None:
        pytest.skip("no OpenROAD checkout")
    wrapper = fm.parent / "find_dup_ids.sh"
    if not wrapper.is_file():
        pytest.skip("this checkout predates find_dup_ids.sh")
    repo = fm.parent.parent
    p = subprocess.run(["bash", str(wrapper)], cwd=str(repo),
                       capture_output=True, text=True, timeout=900)
    assert p.returncode != 0 and "find_messages.py" in (p.stderr + p.stdout), \
        ("find_dup_ids.sh now runs from a plain checkout — the FORKS.json note "
         "explaining why it is not the declared command is out of date")
