"""A release record that never leaves the cron host is not a record.

MEASURED 2026-08-12. The 05:30 round published `vibeic-eda:0.2.86` to the
registry at 06:19, committed the record and the eight pin files it had moved,
and stopped:

    $ grep -n "git push" run_0530.sh daily_0530.py daily_release.py
    (no output)

    $ git log --oneline origin/main..HEAD
    8a1f10c release: record 0.2.86 as published

The image was public. The record of what was in it was on one disk.

WHAT THAT COST, and it is the reason this was noticed at all: `discover_forks`
reads every pin from GITHUB, not from the checkout. So the page rendered the
PRE-release ngspice pin (b2e9c4067) while the repository already carried
94d5ff8e1, and against the stale pin it reported

    our commits PAST THE PIN (not shipped): 1 substantive
        ngspice/28d4211f6924

Against the real pin, `rev-list 94d5ff8e..origin/master --not upstream/master`
is 0. The public page was advertising a gap the repository had already closed,
and the owner asked why it did not say zero.

The second cost is the one `commit_release_record` exists to prevent: a fresh
clone rebuilds something other than what RELEASED.json says shipped. That fix
made the pins get COMMITTED. Committing without pushing is half of it.

WHAT THE FIX MUST NOT DO. It must not push over work that arrived while the
image was building -- a real window, since the build takes tens of minutes.
So the push is refused unless `origin/<branch>` is an ancestor of HEAD, and a
refusal is reported rather than swallowed.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location("daily_release", HERE / "daily_release.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daily_release"] = mod
    spec.loader.exec_module(mod)
    return mod


D = _load()


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)


def _repo_pair(tmp_path):
    """A bare 'origin' and a clone of it, both with one commit."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    # The bare repo's HEAD defaults to `master`. Without this, a later
    # `git clone` produces a checkout with no branch and `push origin main`
    # dies with "src refspec main does not match any" -- which git reports on
    # stderr and a `-q` push swallows, so the "someone else landed a commit"
    # setup silently does not happen and the refusal test passes vacuously.
    # Measured while writing these tests.
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD",
                    "refs/heads/main"], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "VERSION").write_text("0.0.1\n")
    _git("add", "VERSION", cwd=work)
    _git("commit", "-qm", "base", cwd=work)
    _git("branch", "-M", "main", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    return origin, work


def test_a_committed_record_reaches_the_remote(tmp_path):
    """The regression: the record must not stay on one disk."""
    origin, work = _repo_pair(tmp_path)
    (work / "RELEASED.json").write_text('{"version":"0.0.2"}\n')
    _git("add", "RELEASED.json", cwd=work)
    _git("commit", "-qm", "release: record 0.0.2", cwd=work)

    before = _git("rev-parse", "origin/main", cwd=work).stdout.strip()
    ok, why = D.push_release_record(work)
    after = _git("rev-parse", "origin/main", cwd=work).stdout.strip()

    assert ok, why
    assert after != before, "origin/main did not move — the record was not published"
    assert _git("rev-parse", "HEAD", cwd=work).stdout.strip() == after


def test_release_head_is_pushed_even_when_checkout_branch_is_not_main(
        tmp_path):
    """Validate and publish the same ref, not HEAD versus local ``main``.

    The old implementation proved origin/main was an ancestor of HEAD, then
    ran ``git push origin main``.  On a release work branch Git reported
    success while pushing the unchanged local main and leaving the record on
    one disk.
    """
    origin, work = _repo_pair(tmp_path)
    _git("switch", "-qc", "release-work", cwd=work)
    (work / "RELEASED.json").write_text('{"version":"0.0.2"}\n')
    (work / "VERSION").write_text("0.0.2\n")
    _git("add", "RELEASED.json", "VERSION", cwd=work)
    _git("commit", "-qm", "release: record 0.0.2", cwd=work)
    head = _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    ok, why = D.push_release_record(work)

    assert ok, why
    assert _git("rev-parse", "refs/heads/main", cwd=origin).stdout.strip() == head


def test_it_refuses_rather_than_discard_work_that_arrived_meanwhile(tmp_path):
    """The build takes tens of minutes; the remote can move inside that window."""
    origin, work = _repo_pair(tmp_path)

    # someone else lands a commit on origin while our image is building
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git("config", "user.email", "o@o", cwd=other)
    _git("config", "user.name", "o", cwd=other)
    (other / "THEIRS").write_text("x\n")
    _git("add", "THEIRS", cwd=other)
    _git("commit", "-qm", "theirs", cwd=other)
    _git("push", "-q", "origin", "main", cwd=other)
    theirs = _git("rev-parse", "HEAD", cwd=other).stdout.strip()

    (work / "RELEASED.json").write_text('{"version":"0.0.2"}\n')
    _git("add", "RELEASED.json", cwd=work)
    _git("commit", "-qm", "release: record 0.0.2", cwd=work)

    ok, why = D.push_release_record(work)

    assert ok is False, "it pushed over a commit it had never seen"
    assert "NOT PUSHED" in why and "ancestor" in why, why
    # and their commit is still the tip
    assert _git("rev-parse", "origin/main", cwd=other).stdout.strip() == theirs or True
    _git("fetch", "-q", "origin", cwd=other)
    assert _git("rev-parse", "origin/main", cwd=other).stdout.strip() == theirs, (
        "the other session's commit was discarded")


def test_the_refusal_is_reported_not_swallowed(tmp_path):
    """A silent failure here is how the record went missing in the first place."""
    origin, work = _repo_pair(tmp_path)
    _git("remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"), cwd=work)
    (work / "RELEASED.json").write_text('{"version":"0.0.2"}\n')
    _git("add", "RELEASED.json", cwd=work)
    _git("commit", "-qm", "release: record 0.0.2", cwd=work)

    ok, why = D.push_release_record(work)
    assert ok is False
    assert "NOT PUSHED" in why, why
    assert why.strip() != "", "a failure must carry a reason"


def test_commit_release_record_reports_the_push_outcome(tmp_path):
    """The caller prints this string; the push result has to be in it.

    Without this, `push_release_record` could be perfect and never called.
    """
    origin, work = _repo_pair(tmp_path)
    (work / "RELEASED.json").write_text('{"version":"0.0.2"}\n')
    (work / "VERSION").write_text("0.0.2\n")

    ok, note = D.commit_release_record(work, "0.0.2")
    assert ok, note
    assert "pushed" in note, (
        f"commit_release_record must say what happened to the push; got: {note!r}")
    assert _git("rev-parse", "origin/main", cwd=work).stdout.strip() == \
           _git("rev-parse", "HEAD", cwd=work).stdout.strip()
