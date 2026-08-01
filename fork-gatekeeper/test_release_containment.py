#!/usr/bin/env python3
"""`behind_releases` was a DATE COMPARISON wearing a measurement's clothes.

On 2026-08-01 the published monitor page said "4 tools with a new release".
Measured against the real repositories, exactly one of the four was missing any
upstream work at all. The other three were the three ways a publication date
diverges from the property it was standing in for — "does the ref we build
already contain this?":

  * a tag pointing at EXACTLY our pinned commit. The release was published the
    day after the commit was authored, so `published_at > fork_point_date`, and
    a tag naming our own pin counted as work we lacked.
  * two tags on ONE commit, and a prerelease sharing its content with its final
    release. Three names, two of them for work already counted, all three past
    the fork-point date.
  * a tag on a RELEASE BRANCH that only ever merges the branch we track. The
    merge commit wraps content we already have, so it is dated after that
    content BY CONSTRUCTION and is an ancestor of nothing on the merged-from
    side — both the date test and the lone ancestry probe say "new" about a
    delta of zero bytes.

And underneath all three, the shape that makes them dangerous rather than merely
wrong: when the containment probe ERRORED, the code took the same branch as a
measured "not contained" and emitted a NUMBER. A fabricated measurement is worse
than a missing one, because nothing downstream can tell them apart.

WHAT THESE TESTS DO. Each shape is built as a REAL git repository with the exact
topology, and driven through the REAL `discover_one` with the upstream stubbed —
so the fixture exercises the production containment path (a local clone) rather
than a re-implementation of it. Every one of them FAILS against the code as it
was: the numbers below are what the date comparison got wrong.

The fifth shape is the control that keeps the fix honest: a release that is
genuinely ahead of us must still be counted. The count alone does not
discriminate — the old code also reaches 3 for it — so that test additionally
requires the answer to be a MEASUREMENT: a resolved target commit per release, a
`behind_releases_status`, and a release we provably contain excluded from the
count even though it is dated after our fork point.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import discover_forks as df           # noqa: E402
import build_page as bp               # noqa: E402
import gk_state                       # noqa: E402
import pr_notify as prn               # noqa: E402

ORG = "vibeic"
UP = "them/Tool"
UP_OWNER = UP.split("/")[0]
TOOL = "Widget"      # not "Tool": the report table's own header row starts "| Tool |"


# ── git fixtures ────────────────────────────────────────────────────────────
# Real repositories, because the property under test is a property of commit
# graphs and trees. A mocked "clone" would let the fix be written against the
# mock — which is how the thing being replaced (a date comparison) got shipped
# as an ancestry check in the first place.

def _git(repo, *args, **kw):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, **kw)
    assert r.returncode == 0, f"git {' '.join(args)} → {r.returncode}: {r.stderr}"
    return r.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "master", str(path)], check=True,
                   capture_output=True)
    _git(path, "config", "user.email", "fixture@example.invalid")
    _git(path, "config", "user.name", "fixture")
    return path


def _commit(repo: Path, fname: str, body: str, when: str) -> str:
    (repo / fname).write_text(body)
    _git(repo, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{when}T00:00:00Z",
           "GIT_COMMITTER_DATE": f"{when}T00:00:00Z"}
    r = subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", fname],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return _git(repo, "rev-parse", "HEAD")


def _one_patch_id(repo: Path, sha: str) -> str:
    """The patch-id of ONE commit, for the fixture self-checks that assert two
    commits carry the same patch under different shas.

    `git show <sha> | git patch-id --stable` under `shell=True` was the third
    copy of the unguarded pipeline in this file. It is a FIXTURE assertion rather
    than a prover, so its failure mode was an `IndexError` on `.split()[0]` rather
    than a false verdict — but a fixture that cannot say WHY it did not produce a
    patch-id sends the reader looking for a wrong commit graph, and the shell was
    doing nothing here that a pipe does not do. Both statuses are checked.
    """
    with tempfile.TemporaryFile(mode="w+", errors="replace") as err:
        show = subprocess.Popen(["git", "-C", str(repo), "show", sha],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=err, text=True)
        pid_ = subprocess.Popen(["git", "patch-id", "--stable"], stdin=show.stdout,
                                stdout=subprocess.PIPE, stderr=err, text=True)
        show.stdout.close()
        out, _ = pid_.communicate(timeout=120)
        show.wait(timeout=120)
        err.seek(0)
        diag = (err.read() or "").strip()
    assert show.returncode == 0 and pid_.returncode == 0, (
        f"git show|patch-id for {sha[:12]} exited "
        f"({show.returncode}, {pid_.returncode}): {diag[:200]}")
    assert out.split(), f"no patch-id for {sha[:12]}: {diag[:200]}"
    return out.split()[0]


def _merge_no_ff(repo: Path, other: str, when: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{when}T00:00:00Z",
           "GIT_COMMITTER_DATE": f"{when}T00:00:00Z"}
    r = subprocess.run(["git", "-C", str(repo), "merge", "--no-ff", "-q",
                        "-m", f"Merge branch '{other}'", other],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return _git(repo, "rev-parse", "HEAD")


# ── the driver ──────────────────────────────────────────────────────────────

def _discover(monkeypatch, clones: Path, ref: str, releases, tags=(),
              fork_point: tuple[str, str] = ("0" * 40, "2026-01-01"),
              behind_commits: int = 0, ahead: int = 0,
              release_probe=None):
    """Run the REAL `discover_one` against a REAL clone and a stubbed upstream.

    `release_probe` is what the OLD single all-or-nothing containment probe
    (`compare/<owner>:<latest tag>...<ref>`) returns. Its default is the HTTP 404
    a mirror-style repository gives for a cross-repo compare — measured, not
    imagined: several of our repos are independent repositories rather than
    GitHub forks, so that query cannot work for them at all.
    """
    calls: list[str] = []

    def fake_gh(path):
        calls.append(path)
        if path == f"repos/{ORG}/{TOOL}":
            return {"created_at": "2020-01-01T00:00:00Z", "default_branch": "master"}
        if path == f"repos/{UP}":
            return {"default_branch": "master"}
        if f"/compare/{UP_OWNER}:master..." in path:          # carried patches
            return {"merge_base_commit": {
                        "sha": fork_point[0],
                        "commit": {"message": "fork point",
                                   "author": {"date": f"{fork_point[1]}T00:00:00Z"}}},
                    "ahead_by": ahead, "behind_by": behind_commits, "commits": []}
        if f"/compare/{UP_OWNER}:" in path:                   # the OLD release probe
            return (release_probe if release_probe is not None
                    else {"_err": "gh: Not Found (HTTP 404)"})
        if "/releases?" in path:
            out = []
            for tag, date, pre in releases:
                r = {"tag_name": tag, "published_at": f"{date}T00:00:00Z"}
                if pre is not None:
                    r["prerelease"] = pre
                out.append(r)
            return out
        return {"_err": f"unexpected path {path}"}

    monkeypatch.setattr(df, "gh", fake_gh)
    monkeypatch.setattr(df, "_tags_by_date",
                        lambda up, limit=30: [{"tag": t, "date": d} for t, d in tags])
    # `raising=False` on these two so the SAME fixture runs against the code as it
    # was, where neither name exists. A negative control that dies in setup proves
    # only that the fix added a symbol; this one makes the old path produce its
    # answer from the same repository and the same release feed, and the failure
    # that comes back is the wrong NUMBER.
    monkeypatch.setattr(df, "FORK_CLONES", clones, raising=False)
    # No network, ever: an ls-remote that silently answered would make a test that
    # is supposed to prove the clone was consulted pass for the wrong reason.
    monkeypatch.setattr(df, "_ls_remote_tags", lambda url: {}, raising=False)
    # …and the same for the fork-point path's remote probe: the shapes below are
    # about the RELEASE classification, so the clone must not also become the
    # source of the fork point and change what is being measured.
    monkeypatch.setattr(df, "_ls_remote_head", lambda url, branch: None, raising=False)
    led = df.discover_one({"tool": TOOL, "upstream": UP, "role": "r"},
                          {TOOL.lower(): {"ref": ref, "arg": "TOOL_REF"}}, "0.9.9")
    led["_gh_calls"] = calls
    return led


def _tags_of(led, key="new_releases"):
    return [r["tag"] for r in (led.get(key) or [])]


# ── SHAPE 1 — a tag pointing at exactly our pinned commit ───────────────────

def test_a_tag_on_our_own_pinned_commit_is_not_a_release_we_are_missing(monkeypatch):
    """MEASURED: reported 1, truth 0.

    Our pin IS the release commit. The release was PUBLISHED the day after the
    commit was authored — which is the ordinary case, not an anomaly — so
    `published_at (day+1) > fork_point commit date (day)` and the tag naming our
    own pin was counted as work we lacked. The cross-repo containment probe in
    front of it 404s for a mirror-style repository, and that 404 took the same
    branch as a measured "not contained".
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        prev = _commit(repo, "a", "a", "2026-07-01")
        _git(repo, "tag", "rel-3.0.0", prev)
        pin = _commit(repo, "b", "b", "2026-07-19")
        _git(repo, "tag", "rel-3.0.1", pin)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("rel-3.0.1", "2026-07-20", False),
                                  ("rel-3.0.0", "2025-12-23", False)],
                        fork_point=(pin, "2026-07-19"))
    assert led["behind_releases"] == 0, \
        f"a tag on our own pinned commit counted as new: {_tags_of(led)}"
    assert led["behind_releases_status"] == "measured"
    assert led["undetermined_releases"] == []
    assert led["base_release"] == "rel-3.0.1", \
        f"base_release names a release older than the one we build: {led['base_release']}"


# ── SHAPE 2 — two tags pointing at the same commit ──────────────────────────

def test_two_tags_on_one_commit_are_one_release(monkeypatch):
    """MEASURED: two of the nine releases on one row were a bare `16.2.0` beside
    `trilinos-release-16-2-0`, and a bare `17.0.0rc0` beside
    `trilinos-release-17-0-0-rc0` — one commit each, counted twice each.

    Identity of a version is its COMMIT. Merging the release feed with the tag
    feed by NAME, which is what produced the duplicates, cannot see that.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        pin = _commit(repo, "a", "a", "2026-01-01")
        c = _commit(repo, "b", "b", "2026-02-01")
        _git(repo, "tag", "v2.0", c)
        _git(repo, "tag", "2.0", c)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False)],
                        tags=[("v2.0", "2026-02-01"), ("2.0", "2026-02-01")],
                        fork_point=(pin, "2026-01-01"))
    assert led["behind_releases"] == 1, \
        f"one commit counted once per tag name: {_tags_of(led)}"
    assert len(led["new_releases"]) == 1
    assert sorted(led["new_releases"][0].get("also_tagged", []) + [led["new_releases"][0]["tag"]]) \
        == ["2.0", "v2.0"], "the collapsed duplicate is not disclosed on the row"


# ── SHAPE 3 — a prerelease and its final release sharing content ────────────

def test_a_prerelease_contained_in_its_final_release_is_not_counted_twice(monkeypatch):
    """MEASURED: three of the nine were 17.0.0 prereleases whose work ships in
    the 17.0.0 release counted beside them.

    Collapsed by the API's OWN `prerelease` flag plus ancestry — never by
    matching "rc"/"beta" in a tag name, which would be a second proxy standing in
    for the same property.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        pin = _commit(repo, "a", "a", "2026-01-01")
        rc = _commit(repo, "b", "b", "2026-02-01")
        _git(repo, "tag", "v3.0.0-rc1", rc)
        final = _commit(repo, "c", "c", "2026-03-01")
        _git(repo, "tag", "v3.0.0", final)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v3.0.0", "2026-03-02", False),
                                  ("v3.0.0-rc1", "2026-02-02", True)],
                        fork_point=(pin, "2026-01-01"))
    assert led["behind_releases"] == 1, \
        f"a prerelease was counted beside the release that contains it: {_tags_of(led)}"
    assert _tags_of(led) == ["v3.0.0"]
    # …in the bucket that says what it is. `contained_releases` is a claim about
    # OUR TREE, and our pinned ref does not contain this prerelease — it is work
    # we lack, counted once, under the release that carries it.
    assert _tags_of(led, "folded_releases") == ["v3.0.0-rc1"]
    assert "v3.0.0-rc1" not in _tags_of(led, "contained_releases")


def test_a_prerelease_nobody_superseded_is_still_counted(monkeypatch):
    """…or the test above is met by dropping every prerelease. A prerelease with
    no final release containing it is adoptable work and must survive."""
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        pin = _commit(repo, "a", "a", "2026-01-01")
        rc = _commit(repo, "b", "b", "2026-02-01")
        _git(repo, "tag", "v3.0.0-rc1", rc)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v3.0.0-rc1", "2026-02-02", True)],
                        fork_point=(pin, "2026-01-01"))
    assert _tags_of(led) == ["v3.0.0-rc1"], \
        "an unsuperseded prerelease was dropped; that is the same fabrication, downward"


# ── SHAPE 4 — a tag on a release branch that only merges the branch we track ─

def test_a_release_branch_that_only_merges_our_branch_carries_no_new_work(monkeypatch):
    """MEASURED: reported 1, truth 0.

    Upstream bumps the version on master and only afterwards merges master onto a
    release branch in order to tag it. The tag therefore sits on a MERGE COMMIT
    whose tree equals the tree of the master commit we already build:

      * it is not an ancestor of our pin — correct, and the reason the single
        ancestry probe returned "behind by 223 commits";
      * it is dated AFTER the content it contains — guaranteed, because a merge
        commit is created after what it merges;
      * and it changes nothing.

    Ancestry is a sufficient shortcut, never the verdict. Content is the verdict.
    Note this fixture makes the OLD probe SUCCEED with a non-zero `behind_by`, so
    it cannot be passed by handling API errors alone.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        old = _commit(repo, "a", "a", "2026-06-01")
        _git(repo, "tag", "1.5.322", old)
        master_head = _commit(repo, "b", "b", "2026-07-06")
        _git(repo, "checkout", "-q", "-b", "rel-1.5", old)
        tagged = _merge_no_ff(repo, "master", "2026-07-07")
        _git(repo, "tag", "1.5.323", tagged)
        _git(repo, "checkout", "-q", "master")
        # our pin: upstream master HEAD plus a patch of our own, which is what a
        # fork that carries patches actually builds
        pin = _commit(repo, "ours", "ours", "2026-07-06")
        assert tagged != master_head
        led = _discover(monkeypatch, clones, pin,
                        releases=[],
                        tags=[("1.5.323", "2026-07-07"), ("1.5.322", "2026-06-01")],
                        fork_point=(master_head, "2026-07-06"), ahead=1,
                        release_probe={"ahead_by": 1, "behind_by": 223,
                                       "merge_base_commit": {}, "commits": []})
    assert led["behind_releases"] == 0, \
        f"a zero-byte release-branch merge counted as new work: {_tags_of(led)}"
    assert led["behind_releases_status"] == "measured"
    assert led["base_release"] == "1.5.323", \
        f"base_release names an older tag than the tree we build: {led['base_release']}"


# ── SHAPE 6 — a release cut from a line we have already moved past ──────────

def test_a_release_cut_before_our_fork_point_is_not_one_we_are_behind(monkeypatch):
    """MEASURED, and the reason the count needs a bound that is not a date.

    A project that cuts every release on its own maintenance branch keeps
    patching the OLD branches after we have moved to a newer one. Such a release
    genuinely carries commits our pin lacks — the branch-only ones — so "not
    contained" alone says yes to it, and deleting the date filter without
    replacing it counts the project's whole release history.

    The graph supplies the bound: our FORK POINT (the merge-base of our pinned ref
    with the upstream trunk) must be an ancestor of the release. A release whose
    line was cut before we branched is not something we can advance to. Measured
    on the shape that forced it: the maintenance releases' merge-base with our pin
    is a PROPER ANCESTOR of our own fork point, while every release we would
    actually adopt has our fork point as its merge-base.

    Note the maintenance release here is PUBLISHED AFTER our fork point — which is
    what a backport is — so the date comparison counts it, and this test fails
    against a fix that reinstated dates as the bound.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        cut = _commit(repo, "a", "a", "2025-03-01")           # where the old line forks
        # the old maintenance branch, still receiving work
        _git(repo, "checkout", "-q", "-b", "rel-16-1", cut)
        old_rel = _commit(repo, "backport", "backport", "2026-05-18")
        _git(repo, "tag", "v16.1.0", old_rel)
        _git(repo, "checkout", "-q", "master")
        fork = _commit(repo, "b", "b", "2025-11-24")          # OUR fork point
        pin = fork
        ahead_sha = _commit(repo, "c", "c", "2026-05-31")
        _git(repo, "tag", "v17.1.1", ahead_sha)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v17.1.1", "2026-05-31", False),
                                  ("v16.1.0", "2026-05-18", False)],
                        fork_point=(fork, "2025-11-24"), behind_commits=1)
    assert _tags_of(led) == ["v17.1.1"], \
        f"a release cut from a line we moved past counted as work we lack: {_tags_of(led)}"
    assert led["behind_releases"] == 1
    assert _tags_of(led, "superseded_releases") == ["v16.1.0"], \
        "the release was dropped without the ledger saying why it was dropped"


def test_without_a_fork_point_an_uncontained_release_is_undetermined(monkeypatch):
    """…and the bound obeys the same rule as everything else here: when it cannot
    be computed the release is UNDETERMINED, never quietly counted and never
    quietly dropped. Not knowing where we branched from is not the same as
    knowing what is ahead of us."""
    calls = []

    def no_compare(path):
        calls.append(path)
        if path == f"repos/{ORG}/{TOOL}":
            return {"created_at": "2020-01-01T00:00:00Z", "default_branch": "master"}
        if path == f"repos/{UP}":
            return {"default_branch": "master"}
        if "/compare/" in path:
            return {"_err": "gh: Not Found (HTTP 404)"}
        if "/releases?" in path:
            return [{"tag_name": "v2.0", "published_at": "2026-02-02T00:00:00Z",
                     "prerelease": False}]
        return {"_err": "unexpected"}

    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        pin = _commit(repo, "a", "a", "2026-01-01")
        c = _commit(repo, "b", "b", "2026-02-01")
        _git(repo, "tag", "v2.0", c)
        monkeypatch.setattr(df, "gh", no_compare)
        monkeypatch.setattr(df, "_tags_by_date", lambda up, limit=30: [])
        monkeypatch.setattr(df, "FORK_CLONES", clones, raising=False)
        monkeypatch.setattr(df, "_ls_remote_tags", lambda url: {}, raising=False)
        monkeypatch.setattr(df, "_ls_remote_head", lambda url, branch: None, raising=False)
        led = df.discover_one({"tool": TOOL, "upstream": UP, "role": "r"},
                              {TOOL.lower(): {"ref": pin, "arg": "TOOL_REF"}}, "0.9.9")
    assert led.get("fork_point") is None, "the fixture failed to remove the fork point"
    assert led["behind_releases"] is None and led["behind_releases_status"] == "unknown", \
        f"a release was classified with no fork point to bound it: {led['behind_releases']}"
    assert _tags_of(led, "undetermined_releases") == ["v2.0"]


# ── SHAPE 5 — a release genuinely ahead of us (the control) ─────────────────

def test_releases_genuinely_ahead_of_us_are_still_counted(monkeypatch):
    """THE CONTROL, and it needs more than a count.

    Three quarterly tags, each adding content our pin does not have, must still
    report 3 — a fix that returns 0 for everything would pass every test above.
    The count alone does not discriminate (the date comparison also reaches 3
    here), so this additionally requires the 3 to be a MEASUREMENT:

      * every counted release resolved to a target commit;
      * `behind_releases_status == "measured"`;
      * and the tag our pin IS excluded even though it is PUBLISHED after our
        fork-point commit date — the one perturbation that separates a
        containment test from a date test on this shape.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        _commit(repo, "a", "a", "2023-12-01")
        pin = _commit(repo, "b", "b", "2024-01-04")
        _git(repo, "tag", "v3.0", pin)
        for name, when, tag in (("c", "2026-01-11", "26Q1"),
                                ("d", "2026-04-07", "26Q2"),
                                ("e", "2026-06-30", "26Q3")):
            sha = _commit(repo, name, name, when)
            _git(repo, "tag", tag, sha)
        led = _discover(monkeypatch, clones, "v3.0",     # the pin is a TAG, not a sha
                        releases=[],
                        tags=[("26Q3", "2026-06-30"), ("26Q2", "2026-04-07"),
                              ("26Q1", "2026-01-11"),
                              # published AFTER the fork-point commit date, and it
                              # is exactly what we build
                              ("v3.0", "2024-01-05")],
                        fork_point=(pin, "2024-01-04"), behind_commits=3)
    assert led["behind_releases"] == 3, \
        f"the genuinely-ahead releases were lost: {_tags_of(led)}"
    assert sorted(_tags_of(led)) == ["26Q1", "26Q2", "26Q3"]
    assert led["behind_releases_status"] == "measured"
    assert all(r.get("sha") for r in led["new_releases"]), \
        "a counted release has no resolved target commit — it was not measured"
    assert "v3.0" not in _tags_of(led), \
        "the tag our pin IS was counted, because it was PUBLISHED a day later"
    assert led["base_release"] == "v3.0"


# ── SHAPE 7 — the same defect pointing the other way ────────────────────────

def test_a_release_we_lack_is_counted_even_though_it_predates_our_fork_point(monkeypatch):
    """MEASURED on a fifth tool, and it is the defect's OTHER direction.

    Upstream cuts a patch release from the release we build, on its own branch,
    while the trunk moves on separately. The patch release is therefore PUBLISHED
    BEFORE our fork point — and the date filter concluded from that that we must
    already have it: it counted zero, and named that very release as the one we
    build. Confirmed against the live API for the real row this fixture is drawn
    from: `compare/<our pin>...<the release>` reports 64 commits and 41 changed
    files we do not have, under a ledger that said "on the latest upstream
    release".

    A false CLEAN is the worse half of this defect — a false alarm gets looked at.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        _commit(repo, "a", "a", "2025-10-01")
        rel = _commit(repo, "b", "b", "2025-11-01")
        _git(repo, "tag", "v2.0.0", rel)
        # the patch release, cut from v2.0.0 on its own branch
        _git(repo, "checkout", "-q", "-b", "rel-2.0", rel)
        patch = _commit(repo, "patch", "patch", "2025-11-15")
        _git(repo, "tag", "v2.0.1", patch)
        _git(repo, "checkout", "-q", "master")
        fork = _commit(repo, "c", "c", "2026-07-30")      # trunk moved on, we track it
        led = _discover(monkeypatch, clones, fork,
                        releases=[("v2.0.1", "2025-11-15", False),
                                  ("v2.0.0", "2025-11-01", False)],
                        fork_point=(fork, "2026-07-30"))
    assert _tags_of(led) == ["v2.0.1"], \
        f"a release we provably do not have went uncounted: {_tags_of(led)}"
    assert led["behind_releases"] == 1
    assert led["base_release"] == "v2.0.0", \
        f"the page would name a release we do not build as the one we build: {led['base_release']}"


# ── SHAPE 8 — a release on a history that shares no ancestor with ours ──────

def test_a_release_on_an_abandoned_history_is_not_a_gap_when_we_are_anchored(monkeypatch):
    """MEASURED: an upstream that RE-IMPORTED its source. Its newest release is a
    115-commit tree whose root commit differs from the 3459-commit history its
    older tags sit on — the two share no ancestor at all.

    `git merge-base` reporting no common ancestor is a definite answer, not a
    failed measurement, and it says something neither "contained" nor "ahead" can:
    no rebase reaches that release, so it is not a gap. It is only safe to say so
    while we are ANCHORED — while our pin contains some release of this project.
    Without an anchor the same observation could mean upstream re-rooted and left
    us on the abandoned side, and the answer is then undecided.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        # the abandoned history
        _commit(repo, "old1", "old1", "2024-01-01")
        gone = _commit(repo, "old2", "old2", "2024-06-01")
        _git(repo, "tag", "Release-7.9.0", gone)
        # the re-imported one, with its own root commit
        _git(repo, "checkout", "-q", "--orphan", "fresh")
        _git(repo, "rm", "-rq", "--cached", ".")
        for f in ("old1", "old2"):
            (repo / f).unlink()
        fresh = _commit(repo, "new1", "new1", "2026-01-01")
        _git(repo, "tag", "Release-7.10.0", fresh)
        pin = _commit(repo, "ours", "ours", "2026-02-01")
        assert not subprocess.run(["git", "-C", str(repo), "merge-base",
                                   gone, fresh], capture_output=True).returncode == 0, \
            "the fixture failed to produce two disjoint histories"
        led = _discover(monkeypatch, clones, pin,
                        releases=[("Release-7.10.0", "2026-01-01", False),
                                  ("Release-7.9.0", "2024-06-01", False)],
                        fork_point=(fresh, "2026-01-01"))
    assert led["behind_releases"] == 0, \
        f"a release on an abandoned history counted as a gap: {_tags_of(led)}"
    assert led["behind_releases_status"] == "measured", \
        "a definite 'no common ancestor' was reported as a failure to measure"
    assert _tags_of(led, "superseded_releases") == ["Release-7.9.0"]
    assert led["base_release"] == "Release-7.10.0"


# ── NO SILENT FALLBACK ──────────────────────────────────────────────────────

def test_a_containment_probe_that_errors_yields_unknown_not_a_number(monkeypatch):
    """THE defect underneath the other four.

    When the probe 404s there is no third state: the error takes the same branch
    as a measured "not contained", the date path runs, and a NUMBER is published
    that nothing downstream can distinguish from a measurement. The field must
    say so instead — null, a status, and the literal error text per release.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)          # deliberately EMPTY: no clone can answer
        led = _discover(monkeypatch, clones, "a" * 40,
                        releases=[("v2.0", "2026-02-02", False)],
                        fork_point=("b" * 40, "2026-01-01"))
    assert led["behind_releases"] is None, \
        f"an unanswerable question produced the number {led['behind_releases']}"
    assert led["behind_releases_status"] == "unknown"
    assert _tags_of(led, "undetermined_releases") == ["v2.0"]
    assert led["undetermined_releases"][0].get("error"), \
        "the release is undetermined but the row does not say what stopped it"
    assert df.release_gap_unknown(led) is True
    assert df.release_gap(led) is None, "unknown resolved to a number"


def test_one_undetermined_release_does_not_let_the_others_publish_a_count(monkeypatch):
    """A partial answer is not a count. If ONE release could not be decided, the
    total is unknown — publishing the decided subset as `behind_releases` states
    a bound as if it were the value."""
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        pin = _commit(repo, "a", "a", "2026-01-01")
        c = _commit(repo, "b", "b", "2026-02-01")
        _git(repo, "tag", "v2.0", c)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False),
                                  ("v9.9", "2026-03-01", False)],   # no such tag anywhere
                        fork_point=(pin, "2026-01-01"))
    assert led["behind_releases"] is None
    assert led["behind_releases_status"] == "unknown"
    assert _tags_of(led) == ["v2.0"], "the decided part is still reported"
    assert _tags_of(led, "undetermined_releases") == ["v9.9"]


def test_no_date_takes_part_in_the_count(monkeypatch):
    """The date path, removed rather than reordered. Every release here is
    PUBLISHED long after our fork point and every one of them is content we
    already build, so any surviving date comparison reports 3."""
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2020-01-01")
        b = _commit(repo, "b", "b", "2020-01-02")
        pin = _commit(repo, "c", "c", "2020-01-03")
        for sha, tag in ((a, "v1"), (b, "v2"), (pin, "v3")):
            _git(repo, "tag", tag, sha)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v3", "2026-07-30", False), ("v2", "2026-07-29", False),
                                  ("v1", "2026-07-28", False)],
                        fork_point=(pin, "2020-01-03"))
    assert led["behind_releases"] == 0, \
        f"releases we already contain counted because they were published later: {_tags_of(led)}"


def test_the_ledger_index_carries_the_status_beside_the_count(monkeypatch):
    """`index.json` is what most readers load first. A null in it with no status
    beside it is exactly the value nobody can tell from a measurement."""
    src = (HERE / "discover_forks.py").read_text()
    m = re.search(r"index\.append\(\{k: led\.get\(k\) for k in \((.*?)\)\}\)", src, re.S)
    assert m, "the ledger index no longer builds its row from a key list"
    assert "behind_releases_status" in m.group(1), \
        "the index publishes the count without the status that says whether it is one"


# ── EVERY CONSUMER RENDERS UNKNOWN AS UNKNOWN ───────────────────────────────

UNKNOWN_LED = {
    "tool": TOOL, "integrated": True, "upstream": UP,
    "upstream_default_branch": "master", "pinned_ref_full": "a" * 40,
    "base_release": "v1.0", "upstream_latest_release": "v2.0",
    "behind_releases": None, "behind_releases_status": "unknown",
    "behind_commits": 0, "new_releases": [],
    "undetermined_releases": [{"tag": "v2.0", "date": "2026-02-02",
                               "error": "gh: Not Found (HTTP 404)"}],
    "role": "r",
}


def test_assess_release_does_not_call_an_unmeasured_gap_clean():
    """assess_release.py's clean gate. `(led.get("behind_releases") or 0) == 0`
    maps null onto zero, so a tool whose containment could not be decided was
    about to be published as CLEAN — "on the latest upstream release" — on
    evidence nobody has."""
    import importlib
    import assess_release as A
    with tempfile.TemporaryDirectory() as d:
        os.environ["GK_STATE_DIR"] = d
        try:
            importlib.reload(A)
            (Path(d) / "ledger").mkdir(parents=True, exist_ok=True)
            (Path(d) / "ledger" / f"{TOOL}.json").write_text(json.dumps(UNKNOWN_LED))
            A.upstream_commits = lambda up, base, new: ([], [])
            A.our_patch_files = lambda *a: set()
            A._commit_files = lambda *a: set()
            A.clean_cherrypick = lambda *a: True
            A.classify_commits = lambda tool, role, commits: {}
            A._confirm_candidates = lambda *a, **k: {}
            r = A.assess(TOOL)
        finally:
            os.environ.pop("GK_STATE_DIR", None)
            importlib.reload(A)
    assert r.get("status") != "clean", \
        f"an undetermined release gap was published as CLEAN: {r}"


def test_the_daily_report_table_prints_unknown_not_a_digit():
    """gatekeeper's report table. The column is a MEASUREMENT of how much
    upstream work we lack, and a digit there for a row nobody could measure is
    the one thing a reader cannot recover from."""
    import importlib
    gk = importlib.import_module("gatekeeper")
    summary = {"date": "2026-08-01", "generated_at": "x", "image_version": "0.9.9",
               "counts": {"MERGED": 0, "DEFERRED": 1, "CLEAN": 0, "NOT_LAYERED": 0},
               "results": [{"tool": TOOL, "verdict": "DEFERRED", "new_releases": None,
                            "new_releases_status": "unknown",
                            "latest_release": "v2.0", "note": "n"}]}
    md = gk._report_md(summary)
    row = next(ln for ln in md.splitlines() if ln.startswith(f"| {TOOL} |"))
    assert "unknown" in row, f"the report row states no unknown: {row}"
    assert "| 0 |" not in row and "| None |" not in row, \
        f"an unmeasured gap rendered as a value: {row}"


def test_the_report_row_names_the_releases_that_could_not_be_decided():
    """A count nobody could make must not be published as silence either. The
    row names each release and the literal error that stopped it, so the reader's
    next move is a command rather than a guess."""
    import importlib
    gk = importlib.import_module("gatekeeper")
    note = gk._undetermined_note(UNKNOWN_LED)
    assert "v2.0" in note and "404" in note, note
    assert "UNDETERMINED" in note


def test_a_tool_with_an_unmeasured_gap_is_actionable_for_the_pr():
    """pr_notify decides which DEFERRED rows a human is shown. `(x or 0) > 0` on
    a null drops the row where nobody knows what we are missing, on the grounds
    that we are missing nothing."""
    summary = {"results": [{"tool": TOOL, "verdict": "DEFERRED", "new_releases": None,
                            "new_releases_status": "unknown"}]}
    merged, failed = prn._actionable(summary)
    assert [r["tool"] for r in failed] == [TOOL], \
        "a tool whose release gap is unknown was filtered out as having nothing new"


def test_a_measured_zero_is_still_not_actionable():
    """…or the test above is met by treating every row as actionable, which would
    open a PR every day for every clean fork."""
    summary = {"results": [{"tool": TOOL, "verdict": "DEFERRED", "new_releases": 0,
                            "new_releases_status": "measured"}]}
    assert prn._actionable(summary)[1] == []


# ── the published page ──────────────────────────────────────────────────────

# From the pill helper through the end of `relPill`, whose closing brace is the
# first one at column zero after it. Sliced rather than re-implemented so the
# node run below executes THE PAGE'S OWN readers, not a copy of them.
_JS = re.compile(r"const pill = .*?\n\n", re.S)


def test_the_page_never_coerces_an_unmeasured_gap_to_zero():
    """`d.behind_releases||0` is the defect in JavaScript: it renders "we could
    not find out" as a confident 0 in a KPI, a table cell and a list."""
    src = bp.PAGE
    assert not re.search(r"behind_releases\s*\|\|\s*0", src), \
        "the page still coerces a null release gap to 0"


def test_the_pages_release_gap_readers_report_unknown_as_unknown():
    """Executed, not grepped. The three readers the page is allowed to use are
    run in node against a measured gap, a measured zero, and an unknown."""
    m = _JS.search(bp.PAGE)
    assert m, "the page's release-gap readers are gone or were renamed"
    prog = m.group(0) + """
const rows = [
  {behind_releases: 3, behind_releases_status: "measured", integrated: true},
  {behind_releases: 0, behind_releases_status: "measured", integrated: true},
  {behind_releases: null, behind_releases_status: "unknown", integrated: true,
   undetermined_releases: [{tag: "v2.0", error: "gh: Not Found (HTTP 404)"}]}
];
console.log(JSON.stringify(rows.map(d => [relUnknown(d), relGap(d), relPill(d)])));
"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "p.js"
        p.write_text(prog)
        r = subprocess.run(["node", str(p)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got[0][0] is False and got[0][1] == 3 and ">3<" in got[0][2]
    assert got[1][0] is False and got[1][1] == 0 and ">0<" in got[1][2]
    assert got[2][0] is True, "an unknown gap did not read as unknown"
    assert got[2][1] is None, "an unknown gap resolved to a number"
    assert ">?<" in got[2][2] and ">0<" not in got[2][2], \
        f"an unmeasured gap rendered as a count in the table: {got[2][2]}"


def test_the_page_carries_no_display_layer_override_of_a_measured_field():
    """The attempt that was rejected. A note beside a row may explain a real gap;
    it may never replace a number the same page prints, because the reader then
    sees the table say one thing and the prose under it say another. The
    measurement is made correctly where it is produced instead."""
    assert all("ours" not in n for n in bp.PIN_NOTES.values()), \
        "PIN_NOTES still overrides base_release from the display layer"
    assert all(n.get("kind") != "on-it" for n in bp.PIN_NOTES.values()), \
        "PIN_NOTES still carries a mark whose whole meaning is 'the count is wrong'"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

# ════════════════════════════════════════════════════════════════════════════
# ANCESTRY CANNOT SEE PATCH-EQUIVALENCE — and the count paid for it twice
#
# The measurement above replaced a date comparison with a containment test built
# from ancestry plus one tree comparison. Swept over the whole corpus it produced
# two rows that were WRONG IN THE OTHER DIRECTION: releases counted as work we
# lack whose content our pinned ref already carries, byte for byte, reached by a
# route ancestry cannot follow.
#
#   * a release tag on a version-stamp commit that upstream ALSO squash-merged to
#     its trunk beforehand. Identical patch-id, different sha, our own public
#     header already declaring the released version — and `git merge-base
#     --is-ancestor` says no, because the sha we have is not the sha it tagged.
#     Reported 2 behind. Truth 0, and the row named a release three minor
#     versions older than the one we build as the one we build.
#   * a release whose branch upstream REWROTE after cutting it. Every commit on
#     it has a new sha; the two version-stamp commits are not patch-identical to
#     anything (their diffs step through an intermediate our trunk never had);
#     and the tree they produce is the tree we already build. Reported 2, of
#     which one was the release itself and one was its own prerelease, counted
#     separately because a rewritten branch leaves the prerelease an ancestor of
#     nothing.
#
# Each shape below is a real git repository with that topology, driven through
# the real `discover_one`. Two of them are CONTROLS that must keep counting a
# release we genuinely lack — the failure direction that matters, because a
# false CLEAN is the half nobody looks at.


def _cherry_pick(repo: Path, sha: str, when: str) -> str:
    """Apply `sha` here as a NEW commit — same patch, different sha.

    `-x` appends a provenance line to the message, so the commit object differs
    while `git patch-id --stable` is identical. That is the whole shape: a change
    that is ours without being the object upstream tagged.
    """
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{when}T00:00:00Z",
           "GIT_COMMITTER_DATE": f"{when}T00:00:00Z"}
    r = subprocess.run(["git", "-C", str(repo), "cherry-pick", "-x", sha],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return _git(repo, "rev-parse", "HEAD")


# ── SHAPE 9 — the release's missing commit is one we carry under another sha ─

def test_a_release_whose_only_missing_commit_is_one_we_already_carry(monkeypatch):
    """MEASURED: reported 2, truth 0, and `base_release` regressed by three minor
    versions.

    `git cherry <our pin> <the release>` prints exactly one line and it is a `-`:
    the single commit the release has that we do not is patch-identical
    (`git patch-id --stable`) to a commit that IS an ancestor of our pin. The
    ancestry test cannot see it — the sha it looks for was never on our line —
    and the tree test cannot either, because our trunk moved on afterwards and
    the release's tree no longer matches the merge-base's.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        c0 = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", c0)                 # a release we plainly contain
        # upstream cuts the release on its own branch: one version-stamp commit
        _git(repo, "checkout", "-q", "-b", "rel-2", c0)
        stamp = _commit(repo, "VERSION", "2.0\n", "2026-02-01")
        _git(repo, "tag", "v2.0", stamp)
        # …and the same change reaches the trunk we track under a different sha
        _git(repo, "checkout", "-q", "master")
        _cherry_pick(repo, stamp, "2026-02-02")
        pin = _commit(repo, "b", "b", "2026-03-01")
        assert not subprocess.run(["git", "-C", str(repo), "merge-base",
                                   "--is-ancestor", stamp, pin],
                                  capture_output=True).returncode == 0, \
            "the fixture failed: the release commit is an ancestor after all"
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-01", False),
                                  ("v1.0", "2026-01-01", False)],
                        fork_point=(pin, "2026-03-01"))
    assert led["behind_releases"] == 0, \
        f"a release we carry under a different sha counted as new: {_tags_of(led)}"
    assert led["behind_releases_status"] == "measured"
    assert led["base_release"] == "v2.0", \
        f"base_release names an older release than the one we build: {led['base_release']}"
    # ROUND 3 MOVED THIS ROW, AND ONLY THIS ROW. It was `contained_releases`, which
    # is the claim that our tree already holds the release — and for this shape our
    # trunk has moved on past it, so merging it is not a no-op and the claim was
    # only ever true in the weaker patch sense. Run against the real corpus rather
    # than a fixture, that overstatement was live on yices2 `yices-2.7.0` and
    # cocotb `v1.5.0rc1`. The count and `base_release` are asserted above and are
    # unchanged; what moved is the heading.
    assert "v2.0" not in _tags_of(led, "contained_releases")
    assert "v2.0" in _tags_of(led, "patch_equivalent_releases")


def test_patch_equivalence_does_not_zero_a_release_that_also_carries_real_work(monkeypatch):
    """THE CONTROL for the shape above, and the one that keeps it honest.

    Same release, same already-carried version stamp — plus ONE commit that is
    nobody's cherry-pick. `git cherry` prints a `+` for it, so the release still
    carries work we lack and is still counted. A fix that answered "contained"
    whenever ANY commit matched would pass the test above and lose this one.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        c0 = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", c0)
        _git(repo, "checkout", "-q", "-b", "rel-2", c0)
        stamp = _commit(repo, "VERSION", "2.0\n", "2026-02-01")
        _commit(repo, "fix", "a real fix", "2026-02-02")      # nobody has this
        _git(repo, "tag", "v2.0", _git(repo, "rev-parse", "HEAD"))
        _git(repo, "checkout", "-q", "master")
        _cherry_pick(repo, stamp, "2026-02-03")
        pin = _commit(repo, "b", "b", "2026-03-01")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False),
                                  ("v1.0", "2026-01-01", False)],
                        fork_point=(pin, "2026-03-01"))
    assert _tags_of(led) == ["v2.0"], \
        f"a release carrying a commit nobody has was zeroed: {_tags_of(led)}"
    assert led["behind_releases"] == 1
    assert led["base_release"] == "v1.0"


# ── SHAPE 10 — upstream rewrote the release branch after cutting it ──────────

def test_a_release_whose_rewritten_commits_produce_the_tree_we_already_build(monkeypatch):
    """MEASURED: reported 2, truth 0.

    Upstream cuts `release/N`, stamps it `N-rc2` and then `N`, and rewrites the
    branch in between. Our trunk reached the released version in ONE step, so:

      * ancestry says no — different shas;
      * the tree test says no — our trunk carries 131 further commits;
      * patch-id says no — `rc1 → rc2` and `rc2 → N` are two diffs, and our
        trunk's single `rc1 → N` matches neither.

    What is true is the thing the gatekeeper would actually do: MERGE it. The
    three-way merge of the release into our pinned ref produces the tree we
    already build, so adopting the release moves no byte. `git merge-tree
    --write-tree` answers exactly that, and it is the only one of the four tests
    that survives a rewritten branch.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        (repo / "VERSION").write_text("1.0.0rc1\n")
        c0 = _commit(repo, "f", "x", "2026-01-01")
        _git(repo, "tag", "v0.9", c0)
        # the release branch: two stamps, stepping through an intermediate
        _git(repo, "checkout", "-q", "-b", "rel-1", c0)
        _commit(repo, "VERSION", "1.0.0rc2\n", "2026-02-01")
        final = _commit(repo, "VERSION", "1.0.0\n", "2026-02-02")
        _git(repo, "tag", "v1.0", final)
        # our trunk: the same released version in one step, plus its own work
        _git(repo, "checkout", "-q", "master")
        _commit(repo, "VERSION", "1.0.0\n", "2026-02-03")
        pin = _commit(repo, "f", "y", "2026-03-01")
        cherry = subprocess.run(["git", "-C", str(repo), "cherry", pin, final],
                                capture_output=True, text=True)
        assert cherry.stdout.count("+") == 2, \
            f"the fixture failed: the stamps are patch-equivalent after all: {cherry.stdout}"
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-02-02", False),
                                  ("v0.9", "2026-01-01", False)],
                        fork_point=(pin, "2026-03-01"))
    assert led["behind_releases"] == 0, \
        f"a release that merges into our pin as a no-op counted as new: {_tags_of(led)}"
    assert led["behind_releases_status"] == "measured"
    assert led["base_release"] == "v1.0", \
        f"base_release names an older release than the one we build: {led['base_release']}"


def test_a_merge_that_would_change_our_tree_is_still_a_gap(monkeypatch):
    """THE CONTROL for the merge test. One extra file on the release branch and
    the merge stops being a no-op, so the release is still counted. A fix that
    treated "the merge ran" as "contained" would pass the test above and lose
    this one."""
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        (repo / "VERSION").write_text("1.0.0rc1\n")
        c0 = _commit(repo, "f", "x", "2026-01-01")
        _git(repo, "tag", "v0.9", c0)
        _git(repo, "checkout", "-q", "-b", "rel-1", c0)
        _commit(repo, "VERSION", "1.0.0rc2\n", "2026-02-01")
        _commit(repo, "VERSION", "1.0.0\n", "2026-02-02")
        _git(repo, "tag", "v1.0", _commit(repo, "feature", "real work", "2026-02-03"))
        _git(repo, "checkout", "-q", "master")
        _commit(repo, "VERSION", "1.0.0\n", "2026-02-04")
        pin = _commit(repo, "f", "y", "2026-03-01")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-02-03", False),
                                  ("v0.9", "2026-01-01", False)],
                        fork_point=(pin, "2026-03-01"))
    assert _tags_of(led) == ["v1.0"], \
        f"a release whose merge adds a file was zeroed: {_tags_of(led)}"
    assert led["behind_releases"] == 1


# ── SHAPE 11 — the prerelease fold, on a branch that was rewritten ───────────

def test_a_prerelease_folds_into_its_final_when_the_branch_was_rewritten(monkeypatch):
    """MEASURED: reported 2 for ONE release we lack.

    The fold collapses a prerelease into the final release that carries its work.
    It tested ANCESTRY only — and an upstream that rewrites its release branch
    between cutting the prerelease and tagging the final leaves the prerelease an
    ancestor of nothing, while its commits exist in the final under new shas with
    identical patch-ids. Two names, one piece of missing work, counted twice.

    The fold now asks the same question the containment test asks: ancestry OR
    patch-equivalence. It still reads the API's own `prerelease` flag and never
    the tag text.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        pin = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v0.9", pin)
        # the prerelease, cut on the first cut of the branch
        _git(repo, "checkout", "-q", "-b", "rel-a", pin)
        feature = _commit(repo, "x", "a feature", "2026-02-01")
        _git(repo, "tag", "v1.0rc1", feature)
        # …and the branch rewritten: same patch, new sha, then the final stamp
        _git(repo, "checkout", "-q", "-b", "rel-b", pin)
        _cherry_pick(repo, feature, "2026-02-05")
        final = _commit(repo, "VERSION", "1.0\n", "2026-02-06")
        _git(repo, "tag", "v1.0", final)
        _git(repo, "checkout", "-q", "master")
        assert not subprocess.run(["git", "-C", str(repo), "merge-base",
                                   "--is-ancestor", feature, final],
                                  capture_output=True).returncode == 0, \
            "the fixture failed: the prerelease is an ancestor of the final after all"
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-02-06", False),
                                  ("v1.0rc1", "2026-02-01", True),
                                  ("v0.9", "2026-01-01", False)],
                        fork_point=(pin, "2026-01-01"))
    assert led["behind_releases"] == 1, \
        f"a prerelease was counted beside the release that carries it: {_tags_of(led)}"
    assert _tags_of(led) == ["v1.0"]


# ── THE BUCKET NAMES WHAT IS IN IT ──────────────────────────────────────────

def test_a_folded_prerelease_is_not_filed_as_contained_in_our_pinned_ref(monkeypatch):
    """MEASURED on the live ledger: two release candidates filed under
    `contained_releases` while an independent compare put them 225 and 15 commits
    and 300+ changed files AHEAD of our pin.

    The COUNT was right — they fold into a final release that is counted — but
    `contained` is a claim about our tree, and our tree does not contain them.
    They belong to a bucket that says what they are: work counted once, under the
    release that carries it.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        pin = _commit(repo, "a", "a", "2026-01-01")
        rc = _commit(repo, "b", "b", "2026-02-01")
        _git(repo, "tag", "v3.0.0-rc1", rc)
        final = _commit(repo, "c", "c", "2026-03-01")
        _git(repo, "tag", "v3.0.0", final)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v3.0.0", "2026-03-02", False),
                                  ("v3.0.0-rc1", "2026-02-02", True)],
                        fork_point=(pin, "2026-01-01"))
        # …and the bucket's claim, checked against the repository itself.
        for row in led.get("contained_releases") or []:
            anc = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                                  row["tag"] + "^{commit}", pin], capture_output=True)
            mt = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree",
                                 f"--merge-base={pin}", pin, row['tag'] + "^{commit}"],
                                capture_output=True, text=True)
            pin_tree = _git(repo, "rev-parse", pin + "^{tree}")
            noop = mt.returncode == 0 and mt.stdout.split("\n")[0].strip() == pin_tree
            assert anc.returncode == 0 or noop, \
                (f"{row['tag']} is filed under contained_releases, but our pinned ref "
                 f"neither contains it nor merges it as a no-op")
    assert led["behind_releases"] == 1, "the count changed when the bucket did"
    assert "v3.0.0-rc1" not in _tags_of(led, "contained_releases"), \
        "a release our pinned ref does not contain is filed as contained"
    assert _tags_of(led, "folded_releases") == ["v3.0.0-rc1"], \
        "the folded prerelease is in no bucket at all — it vanished from the ledger"
    assert (led["folded_releases"][0].get("counted_under")) == "v3.0.0", \
        "the row does not say which release its work is counted under"


# ════════════════════════════════════════════════════════════════════════════
# THE FORK POINT — the same defect, at the other site that computes one
#
# `discover_one` derives our fork point from
# `repos/vibeic/<tool>/compare/<upstream owner>:<branch>...<head>`, a CROSS-REPO
# compare. GitHub resolves that only through a shared fork network, and several
# of our repositories are MIRRORS rather than GitHub forks (`fork: false`,
# `parent: null`). For those it does not fail sometimes: it 404s every time,
# permanently — and so does the reversed query, whenever our pin is a sha that
# exists only in our mirror.
#
# `fork_point` then stayed None, which the release path reads as "we do not know
# where we branched from", so EVERY release became undetermined and the row was
# permanently `behind_releases: null` over three junk test tags. Meanwhile the
# local clone answers the same question in milliseconds, because it holds both
# sides in one object store and needs no fork network at all.
#
# Routing on the ERROR is what made this invisible: a 404 is what a broken query
# and an unreachable API look like alike. `fork`/`parent` is a fact the metadata
# call already returns, so the form that CAN answer is chosen before a request is
# spent on one that cannot.

def _discover_mirror(monkeypatch, clones: Path, ref: str, releases,
                     up_head: str | None, repo_meta=None, compare_ok=False):
    """`discover_one` against a repo that states `fork: false, parent: null`.

    Every compare 404s, exactly as the live API does for such a repository. The
    only thing that can answer is the clone.
    """
    def fake_gh(path):
        if path == f"repos/{ORG}/{TOOL}":
            return {"created_at": "2020-01-01T00:00:00Z", "default_branch": "master",
                    "fork": False, "parent": None, **(repo_meta or {})}
        if path == f"repos/{UP}":
            return {"default_branch": "master"}
        if "/compare/" in path:
            return {"_err": "gh: Not Found (HTTP 404)"}
        if "/releases?" in path:
            return [{"tag_name": t, "published_at": f"{dt}T00:00:00Z", "prerelease": pre}
                    for t, dt, pre in releases]
        return {"_err": f"unexpected path {path}"}

    monkeypatch.setattr(df, "gh", fake_gh)
    monkeypatch.setattr(df, "_tags_by_date", lambda up, limit=30: [])
    monkeypatch.setattr(df, "FORK_CLONES", clones, raising=False)
    monkeypatch.setattr(df, "_ls_remote_tags", lambda url: {}, raising=False)
    monkeypatch.setattr(df, "_ls_remote_head", lambda url, branch: up_head, raising=False)
    return df.discover_one({"tool": TOOL, "upstream": UP, "role": "r"},
                           {TOOL.lower(): {"ref": ref, "arg": "TOOL_REF"}}, "0.9.9")


def test_a_mirror_repo_gets_its_fork_point_from_the_local_clone(monkeypatch):
    """MEASURED: `fork_point: null`, `behind_releases: null`, permanently.

    Nothing about this repository can be compared through GitHub, and everything
    about it can be answered by `git merge-base` in the clone we already keep on
    disk. The fork point, our carried patches and how far we trail all come back,
    and the release gap stops being undetermined for want of them.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        _commit(repo, "u0", "u0", "2026-01-01")
        u1 = _commit(repo, "u1", "u1", "2026-02-01")
        _git(repo, "tag", "v1.0", u1)
        u2 = _commit(repo, "u2", "u2", "2026-03-01")     # upstream moved on
        _git(repo, "tag", "v2.0", u2)
        _git(repo, "checkout", "-q", "-b", "vibeic", u1)  # our line, from u1
        pin = _commit(repo, "ours", "ours", "2026-02-15")
        led = _discover_mirror(monkeypatch, clones, pin,
                               releases=[("v2.0", "2026-03-01", False),
                                         ("v1.0", "2026-02-01", False)],
                               up_head=u2)
    assert (led.get("fork_point") or {}).get("sha") == u1[:12], \
        f"the fork point was not recovered from the clone: {led.get('fork_point')}"
    assert led.get("fork_point_status") == "local-clone"
    assert led.get("ahead") == 1, f"our carried patches: {led.get('ahead')}"
    assert led.get("behind_commits") == 1, f"how far we trail: {led.get('behind_commits')}"
    assert led["behind_releases"] == 1 and led["behind_releases_status"] == "measured", \
        (f"the release gap is still not a measurement: {led['behind_releases']} / "
         f"{led['behind_releases_status']}")
    assert _tags_of(led) == ["v2.0"]
    assert led["base_release"] == "v1.0"


def test_a_stale_clone_does_not_get_to_answer_about_the_fork_point(monkeypatch):
    """…and the clone only answers when it demonstrably holds the CURRENT
    upstream head. A clone that has not been fetched reports a smaller
    `behind_commits` than the truth, and small numbers read as health. The head
    is resolved with `git ls-remote` and checked against the object store; a
    clone that does not have it is not asked."""
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        u1 = _commit(repo, "u1", "u1", "2026-02-01")
        _git(repo, "checkout", "-q", "-b", "vibeic", u1)
        pin = _commit(repo, "ours", "ours", "2026-02-15")
        led = _discover_mirror(monkeypatch, clones, pin,
                               releases=[("v2.0", "2026-03-01", False)],
                               up_head="c" * 40)      # a head this clone does not have
    assert led.get("fork_point") is None, \
        "a clone that lacks the current upstream head answered anyway"
    assert led["behind_releases"] is None and led["behind_releases_status"] == "unknown", \
        "an unanswerable fork point produced a release count"
    assert "mirror" in (led.get("compare_error") or ""), \
        f"the row does not say WHY nothing could answer: {led.get('compare_error')!r}"
    assert led.get("fork_point_status") == "undetermined"


# ════════════════════════════════════════════════════════════════════════════
# NOT PROBED IS NOT MEASURED ZERO
#
# `behind_releases` is null under TWO statuses and they are different claims:
# `unknown` (we asked and could not decide) and `not-probed` (there was nothing
# to ask about — no pin, or an upstream with no release and no tag). Eleven rows
# on the corpus carry the second. Both were rendering as a confident `0`:
# `release_gap` screened out only `unknown` and then did `else 0`, and the page
# did `(d.behind_releases)||0`.

def test_release_gap_does_not_turn_a_not_probed_null_into_a_measured_zero():
    """The unit that says it in one line. `release_gap` is documented as THE ONE
    READER every consumer goes through, and it was handing back the very number
    it exists to prevent."""
    assert df.release_gap({"behind_releases": None,
                           "behind_releases_status": "not-probed"}) is None, \
        "a not-probed row resolved to a number"
    assert df.release_gap({"behind_releases": None,
                           "behind_releases_status": "unknown"}) is None
    assert df.release_gap({"behind_releases": 0,
                           "behind_releases_status": "measured"}) == 0, \
        "a measured zero stopped being a zero"
    assert df.release_gap({"behind_releases": 4,
                           "behind_releases_status": "measured"}) == 4


def test_an_upstream_with_no_release_and_no_tag_is_not_a_measured_zero(monkeypatch):
    """End to end, on the shape four upstreams in the corpus actually have: the
    project has published nothing to compare against. The question has no
    subject, and `0` answers it as though somebody had asked."""
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        pin = _commit(repo, "a", "a", "2026-01-01")
        led = _discover(monkeypatch, clones, pin, releases=[], tags=[],
                        fork_point=(pin, "2026-01-01"))
    assert led["behind_releases"] is None
    assert led["behind_releases_status"] == "not-probed"
    assert df.release_gap(led) is None, \
        f"a row with nothing to compare against reported {df.release_gap(led)}"
    assert df.release_gap_unknown(led) is False, \
        "'nothing to probe' was escalated as 'we failed to measure'"


def test_the_page_tells_the_three_statuses_apart():
    """Executed in node, not grepped. Four rows through the page's own readers:
    a measured gap, a measured ZERO, an undetermined one, and a not-probed one.
    The last two must not render as the second."""
    m = _JS.search(bp.PAGE)
    assert m, "the page's release-gap readers are gone or were renamed"
    prog = m.group(0) + """
const rows = [
  {behind_releases: 3, behind_releases_status: "measured", integrated: true},
  {behind_releases: 0, behind_releases_status: "measured", integrated: true},
  {behind_releases: null, behind_releases_status: "unknown", integrated: true,
   undetermined_releases: [{tag: "v2.0", error: "gh: Not Found (HTTP 404)"}]},
  {behind_releases: null, behind_releases_status: "not-probed", integrated: true}
];
console.log(JSON.stringify(rows.map(d => [relGap(d), relPill(d)])));
"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "p.js"
        p.write_text(prog)
        r = subprocess.run(["node", str(p)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got[0][0] == 3 and ">3<" in got[0][1]
    assert got[1][0] == 0 and ">0<" in got[1][1], \
        "a MEASURED zero stopped rendering as a zero"
    assert got[2][0] is None and ">?<" in got[2][1]
    assert got[3][0] is None, f"a not-probed row resolved to the number {got[3][0]}"
    assert ">0<" not in got[3][1], \
        f"a row nobody probed rendered a confident zero pill: {got[3][1]}"
    assert got[3][1] != got[1][1] and got[3][1] != got[2][1], \
        f"not-probed renders identically to a measured zero or to an unknown: {got[3][1]}"


def test_the_daily_report_does_not_print_a_digit_for_a_row_nobody_probed():
    """gatekeeper's report table, on the third status. `unknown` was already
    spelled out; `not-probed` fell through to the raw field and printed the null
    itself."""
    import importlib
    gk = importlib.import_module("gatekeeper")
    summary = {"date": "2026-08-01", "generated_at": "x", "image_version": "0.9.9",
               "counts": {"MERGED": 0, "DEFERRED": 0, "CLEAN": 1, "NOT_LAYERED": 0},
               "results": [{"tool": TOOL, "verdict": "CLEAN", "new_releases": None,
                            "new_releases_status": "not-probed",
                            "latest_release": None, "note": "n"}]}
    md = gk._report_md(summary)
    row = next(ln for ln in md.splitlines() if ln.startswith(f"| {TOOL} |"))
    assert "not probed" in row, f"the report row states no such thing: {row}"
    assert "| 0 |" not in row and "| None |" not in row, \
        f"a row nobody probed rendered as a value: {row}"


# ════════════════════════════════════════════════════════════════════════════
# ROUND 3 — THE HOLE ROUND 2's OWN VERIFIER FOUND IN ROUND 2's FIX
#
# Round 2 replaced ancestry-only containment with four content tests. The third
# of them, `_patch_equivalent`, ended
#
#     return all(ln.startswith('-') for ln in lines) or not lines
#
# and `all([])` is True. `git cherry` walks with `max_parents = 1`, so a MERGE
# COMMIT in the range is never listed — and an EMPTY WALK was therefore accepted
# as PROOF of containment. `_local_containment` returns on that before
# `_merge_changes_nothing`, the one test that can see a merge, ever runs.
#
# The two shapes below are the input, built as real repositories and driven
# through the real `discover_one`:
#
#   * a release tag on a merge commit whose OWN TREE adds a file, both parents
#     ancestors of our pin. `rev-list <pin>..<rel>` = 1, `git cherry` = 0 lines.
#   * the same evil merge with ONE ordinary patch-equivalent commit beside it.
#     `git cherry` prints exactly one line and it is a `-`. The output is NOT
#     empty, so the narrower repair — "treat an EMPTY `git cherry` output as
#     inconclusive" — never fires and the release is contained-by-assertion just
#     as before. That is why the fix measures the RANGE instead of the OUTPUT.
#
# LATENT, NOT LIVE, and counted rather than assumed: every tag in every pinned
# tool's clone — 2248 across the 29 tools with both a pin and a clone — was tested
# for `rev-list --count pin..tag > 0` AND `rev-list --no-merges --count
# pin..tag == 0`, and 0 matched. But netgen tags 57 merge commits in its first 60
# tags, sby 39, magic 38, yices2 9, klayout 5, cadical 4, and netgen's own
# `1.5.323` IS a merge commit — saved today only because the merge-base
# tree-equality test happens to fire in front of the patch test.
# ════════════════════════════════════════════════════════════════════════════

def _evil_merge(repo: Path, other: str, when: str, fname: str, body: str) -> str:
    """A merge commit whose OWN TREE carries a change neither parent has.

    Ordinary git, not an exotic construction: `git merge --no-commit`, edit,
    commit is how a conflict resolution, a release-time version stamp or a
    last-minute security patch lands ON the merge. Every one of those is a change
    that exists only in the merge commit, and `git cherry` cannot see any of them.
    """
    subprocess.run(["git", "-C", str(repo), "merge", "--no-commit", "--no-ff", other],
                   capture_output=True, text=True)
    (repo / fname).write_text(body)
    _git(repo, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{when}T00:00:00Z",
           "GIT_COMMITTER_DATE": f"{when}T00:00:00Z"}
    r = subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m",
                        f"Merge (+{fname})"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return _git(repo, "rev-parse", "HEAD")


def test_a_release_on_a_merge_commit_is_not_contained_by_an_empty_cherry_walk(monkeypatch):
    """MEASURED on this exact repository, through the production functions:

        rev-list --count <pin>..<v2.0>             = 1
        rev-list --no-merges --count <pin>..<v2.0> = 0
        git cherry <pin> <v2.0>                    -> rc 0, no output
        _patch_equivalent(...)                     -> True
        _merge_changes_nothing(...)                -> False
        _local_containment(...)                    -> ('contained', 'every commit …
                                                        is patch-identical …')

    The merge test WOULD have caught it. The patch test returns first, on an
    empty walk, and `cve.txt` — a file our pinned ref does not have — is filed
    as work we already carry.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)                     # the release we do contain
        _git(repo, "checkout", "-q", "-b", "side", a)
        c = _commit(repo, "c", "c", "2026-01-05")
        _git(repo, "checkout", "-q", "master")
        b = _commit(repo, "b", "b", "2026-01-10")
        _merge_no_ff(repo, c, "2026-01-11")              # the trunk absorbs the side
        pin = _commit(repo, "d", "d", "2026-01-12")      # …and moves on: our pin
        # The release: a merge of two commits our pin ALREADY HAS, carrying a
        # change of its own that our pin does not have.
        _git(repo, "checkout", "-q", "-b", "rel", b)
        rel = _evil_merge(repo, c, "2026-02-01", "cve.txt", "the fix nobody has\n")
        _git(repo, "tag", "v2.0", rel)
        _git(repo, "checkout", "-q", "master")

        assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{pin}:cve.txt"],
                              capture_output=True).returncode != 0, \
            "the fixture is wrong: our pin already has cve.txt"
        assert df._git(repo, "rev-list", "--count", f"{pin}..{rel}")[1] == "1"
        assert df._git(repo, "rev-list", "--no-merges", "--count", f"{pin}..{rel}")[1] == "0"

        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False),
                                  ("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-12"))
    assert led["behind_releases"] == 1, \
        (f"a release carrying a file our pinned ref does not have was counted as "
         f"contained: new={_tags_of(led)} contained={_tags_of(led, 'contained_releases')}")
    assert _tags_of(led) == ["v2.0"]
    assert "v2.0" not in _tags_of(led, "contained_releases"), \
        "an empty `git cherry` walk was accepted as proof our pinned ref contains it"
    assert led["base_release"] == "v1.0", \
        f"base_release names a release we do not build: {led['base_release']}"


def test_one_patch_equivalent_commit_beside_an_evil_merge_is_still_not_contained(monkeypatch):
    """THE NEGATIVE CONTROL ON THE NARROWER REPAIR.

    "Treat an EMPTY `git cherry` output as inconclusive whenever `rev-list
    --count base..head` is non-zero" closes the shape above and nothing else.
    Here the range is one ordinary commit — patch-identical to one our pin
    carries, so `-` — plus one evil merge. MEASURED:

        rev-list --count <pin>..<v2.0>             = 2
        rev-list --no-merges --count <pin>..<v2.0> = 1
        git cherry <pin> <v2.0>  -> '- c3cca768…'   (ONE line, NOT empty)
        _patch_equivalent(...)   -> True
        _local_containment(...)  -> contained

    The empty-output guard never fires. What is wrong is not that the output was
    empty; it is that the output describes a strict SUBSET of the range. So the
    fix compares the walk against the range, and this is the input that tells the
    two repairs apart.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        c = _commit(repo, "c", "c", "2026-01-05")
        ours = _commit(repo, "fix.txt", "FIX\n", "2026-01-08")    # our sha
        pin = _commit(repo, "d", "d", "2026-01-12")
        _git(repo, "checkout", "-q", "-b", "rel", c)
        theirs = _commit(repo, "fix.txt", "FIX\n", "2026-01-09")  # their sha, same patch
        _git(repo, "checkout", "-q", "-b", "rel2", c)
        rel = _evil_merge(repo, theirs, "2026-02-01", "cve.txt", "the fix nobody has\n")
        _git(repo, "tag", "v2.0", rel)
        _git(repo, "checkout", "-q", "master")

        def _pid(sha):
            return _one_patch_id(repo, sha)

        assert _pid(ours) == _pid(theirs), "the fixture is wrong: the two commits differ"
        assert df._git(repo, "rev-list", "--count", f"{pin}..{rel}")[1] == "2"
        assert df._git(repo, "rev-list", "--no-merges", "--count", f"{pin}..{rel}")[1] == "1"
        assert df._git(repo, "cherry", pin, rel)[1].strip() != "", \
            "the fixture is wrong: this shape must produce NON-empty cherry output"

        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False),
                                  ("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-12"))
    assert led["behind_releases"] == 1, \
        (f"`git cherry` summarised 1 of the 2 commits in the range and its silence "
         f"about the other was read as containment: new={_tags_of(led)} "
         f"contained={_tags_of(led, 'contained_releases')}")
    assert _tags_of(led) == ["v2.0"]
    assert "v2.0" not in _tags_of(led, "contained_releases")
    assert led["base_release"] == "v1.0"


# ── THE BUCKET NAMES WHAT IS IN IT — AT THE SITE ROUND 2 CREATED ────────────
#
# `contained_releases` filed two LIVE rows whose claim fails round 2's own
# invariant test. MEASURED against the real ledger and the real clones, with the
# TRUE merge-base rather than the fixture's `--merge-base=<pin>`:
#
#   yices2 `yices-2.7.0` — ancestor NO; merge-tree CONFLICTS on
#                          doc/sphinx/source/conf.py
#   cocotb `v1.5.0rc1`   — ancestor NO; merge-tree CONFLICTS on
#                          documentation/source/release_notes.rst
#
# Both arrived through the patch-equivalence branch, which is gated on neither
# half. The COUNT is right — our pin is AHEAD on the conflicting file, so there
# is nothing to advance to — and `base_release yices-2.7.0` is the right answer
# and must stay. What is wrong is the HEADING. "Contained" is the claim that
# adopting the release moves no byte; for these two, adopting it does not even
# apply cleanly. Their claim is a different, true one: our pinned ref carries
# every commit they have, under different shas, and has since moved past them.
# So they get a bucket that says that — the same remedy round 2 applied to
# FOLDED, at the site round 2 created.

def test_a_patch_equivalent_release_is_not_filed_as_contained_but_still_names_the_base(monkeypatch):
    """The yices2 shape in miniature, and the CONSTRAINT that pins the fix.

    A release whose one missing commit is patch-identical to one our pin carries,
    where our pin has since changed the same file again — so merging it is not a
    no-op and ancestry says no. It must:

      * NOT be counted (it is not work we lack) — unchanged;
      * STILL be `base_release` (it is the release we build) — the constraint;
      * NOT sit under a heading that says our tree contains it.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        c = _commit(repo, "c", "c", "2026-01-05")
        ours = _commit(repo, "fix.txt", "FIX\n", "2026-01-08")
        pin = _commit(repo, "d", "d", "2026-01-12")
        _git(repo, "checkout", "-q", "-b", "rel", c)
        theirs = _commit(repo, "fix.txt", "FIX\n", "2026-01-09")
        _git(repo, "tag", "v2.0", theirs)
        _git(repo, "checkout", "-q", "master")

        def _pid(sha):
            return _one_patch_id(repo, sha)

        assert _pid(ours) == _pid(theirs)
        assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                               theirs, pin], capture_output=True).returncode == 1, \
            "the fixture is wrong: this release must NOT be an ancestor of our pin"

        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-01-10", False),
                                  ("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-12"))
    # unchanged by the fix — the control that keeps patch-equivalence working
    assert led["behind_releases"] == 0, \
        f"a patch-equivalent release started being counted: {_tags_of(led)}"
    assert led["base_release"] == "v2.0", \
        (f"a patch-equivalent release stopped being eligible for base_release: "
         f"{led['base_release']} — this is the yices2 `yices-2.7.0` constraint")
    # …and the bucket, which is what round 3 changes
    assert "v2.0" not in _tags_of(led, "contained_releases"), \
        ("a release our pinned ref neither contains nor merges as a no-op is filed "
         "under a heading that claims our tree already has it")
    assert _tags_of(led, "patch_equivalent_releases") == ["v2.0"], \
        "the row is in no bucket at all — it vanished from the ledger"
    assert "v1.0" in _tags_of(led, "contained_releases"), \
        "an ordinary ancestor release stopped being contained"


def test_the_contained_bucket_is_verified_before_it_is_written_not_only_in_a_test(monkeypatch):
    """A fixture test only ever sees fixtures. This one plants the contradiction
    INSIDE the production path and requires the sweep itself to refuse it.

    `_local_containment` is replaced by one that calls a release contained when
    it plainly is not — the same lie the empty-`git cherry` walk told, arrived at
    by a shorter route. Nothing downstream of the classification can tell the two
    apart, which is the point: the ledger must not be able to publish a
    `contained_releases` row whose claim does not survive an independent check of
    the repository.

    And "inconclusive stays inconclusive": the row does not silently become NEW —
    that would invent a measurement out of a self-contradiction. It becomes
    UNDETERMINED, the count becomes null, and the row says what happened.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        pin = _commit(repo, "b", "b", "2026-01-10")
        _git(repo, "checkout", "-q", "-b", "rel", a)
        rel = _commit(repo, "brand-new.txt", "work we plainly do not have\n", "2026-02-01")
        _git(repo, "tag", "v2.0", rel)
        _git(repo, "checkout", "-q", "master")

        real = df._local_containment

        def lying(repo_, tag_sha, pin_sha, _real=real, _rel=rel):
            if tag_sha == _rel:
                return df.CONTAINED, "a claim nothing checked", False
            return _real(repo_, tag_sha, pin_sha)

        monkeypatch.setattr(df, "_local_containment", lying)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False),
                                  ("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-10"))
    assert "v2.0" not in _tags_of(led, "contained_releases"), \
        ("the ledger published a `contained` row that an independent check of the "
         "repository refutes — nothing verified the bucket before it was written")
    assert "v2.0" in _tags_of(led, "undetermined_releases"), \
        "the refuted row was dropped or silently reclassified instead of held undecided"
    assert led["behind_releases"] is None and led["behind_releases_status"] == "unknown", \
        f"a self-contradicting classification still produced a number: {led['behind_releases']}"
    chk = (led.get("release_containment") or {}).get("bucket_check") or {}
    assert chk.get("violations"), \
        f"the run recorded no violation for a row it refused: {chk}"
    assert chk.get("checked", 0) >= 1, \
        f"the check reported clean because it checked nothing: {chk}"


def test_the_bucket_check_is_not_vacuous_on_an_honest_run(monkeypatch):
    """The other half of the same guard: a checker that returns clean because it
    examined zero rows is the failure mode, not the success mode. On an ordinary
    honest run the recorded check must say how many rows it actually verified,
    and that number must not be zero.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        pin = _commit(repo, "b", "b", "2026-01-10")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-10"))
    chk = (led.get("release_containment") or {}).get("bucket_check") or {}
    assert chk.get("checked") == 1, \
        f"the bucket check did not verify the one contained row it filed: {chk}"
    assert chk.get("violations") == [], f"an honest run reported a violation: {chk}"
    assert led["behind_releases"] == 0


# ════════════════════════════════════════════════════════════════════════════
# THE SAME INVARIANT, AGAINST THE REAL CORPUS
#
# This is how (2) survived round 2: the invariant existed, and the only thing it
# ever saw was a fixture built to satisfy it. Run against the real ledger and the
# real clones it failed twice on the day it was written.
#
# The predicate below is written out IN THIS FILE rather than imported from
# `discover_forks`. A corpus check that calls the same helper production calls
# can only ever confirm that production is self-consistent; this one has to be
# able to disagree with it.
#
# It reads whatever ledger `GK_STATE_DIR` points at — the production one by
# default — SKIPS only when there is no corpus at all, and FAILS when there is a
# corpus and nothing in it was checkable, because a checker that reports clean
# over zero rows is the defect, not the absence of one.
# ════════════════════════════════════════════════════════════════════════════

def _corpus_writers(ledgers):
    """The commits that WROTE this corpus, as the ledgers themselves record."""
    return sorted({(led.get(gk_state.PROVENANCE_KEY) or {}).get("commit")
                   for led in ledgers} - {None})


def _corpus_is_of_the_code_under_test(ledgers, key):
    """('yes' | 'no' | 'unknown', why) — was this corpus written by code that
    emits `key` at all?

    THE QUESTION THE NON-VACUITY ASSERTION IS REALLY ASKING, and it was asking it
    by proxy. `assert present` fires when no ledger carries the bucket key, and
    the reason it fires is meant to be "the sweep stopped writing what it
    verifies". On a clean checkout it fired for a different reason entirely: the
    corpus in `~/.cache` was last written on 2026-07-31 by commit fdb754c4a2b9,
    two rounds before the bucket existed. That is not a defect in the code under
    test and never was; it is the absence of a corpus OF the code under test.

    So the two are told apart by MEASUREMENT rather than by weakening the
    assertion: every ledger records the commit that wrote it, and this checkout
    can be asked what that commit's `discover_forks.py` contained. A writer whose
    own source has no `patch_equivalent_releases` in it could not have emitted
    one, and a corpus it wrote proves nothing either way — that is a SKIP, with
    the commit named. A writer whose source DOES contain the key had every
    opportunity, so a corpus without it is a real failure and stays one.

    'unknown' — no provenance at all, or a commit this checkout does not have —
    is a skip too, and for the same reason the module refuses everywhere else: a
    question that could not be put to git does not get answered in either
    direction.
    """
    writers = _corpus_writers(ledgers)
    if not writers:
        return "unknown", "no ledger records which checkout wrote it"
    r = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--show-prefix"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return "unknown", "this checkout is not a git repository, so no writer can be read"
    rel = r.stdout.strip() + Path(df.__file__).name
    emits = {}
    for c in writers:
        b = subprocess.run(["git", "-C", str(HERE), "cat-file", "-p", f"{c}:{rel}"],
                           capture_output=True, text=True)
        emits[c] = (key in b.stdout) if b.returncode == 0 else None
    if any(v is True for v in emits.values()):
        return "yes", (f"written by {', '.join(c for c, v in emits.items() if v)}, whose "
                       f"{rel} emits `{key}`")
    if emits and all(v is False for v in emits.values()):
        return "no", (f"written by {', '.join(emits)}, whose {rel} has no `{key}` in it — "
                      f"this corpus predates the bucket")
    return "unknown", (f"this checkout cannot read the source of "
                       f"{', '.join(c for c, v in emits.items() if v is None)}")


def _corpus_ledgers():
    led_dir = df.LEDGER
    if not led_dir.is_dir():
        return led_dir, []
    out = []
    for f in sorted(led_dir.glob("*.json")):
        if f.name == "index.json":
            continue
        try:
            out.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            continue
    return led_dir, out


#: `rc` from `_g` when the command DID NOT RUN — see `discover_forks.DID_NOT_RUN`,
#: which this mirrors on purpose: the prover that audits that module must not have
#: the defect it audits for.
DID_NOT_RUN = None


def _g(repo, *args, timeout=600):
    """(rc, stdout, stderr) for one git command. `rc is None` means it never ran.

    THE PROVER HAD BOTH OF THE DEFECTS IT EXISTS TO CATCH, and this is the wrapper
    underneath them. `_g` returned `r.returncode` untouched and let an exception
    escape, so its callers were written as `rc == 1` and `rc != 0` — two tests
    that each cover more events than their author meant:

      * A SIGNAL GIVES A NEGATIVE RETURNCODE. `subprocess` reports a process the
        kernel killed with signal N as `-N`, so a SIGKILLed `git merge-base` is
        `rc == -9` with EMPTY stdout — and `_our_tree_already_has_it` read that
        as "shares no ancestor with our pinned ref", i.e. a REFUTATION, from a
        command that measured nothing. Measured with a `git` on PATH that kills
        itself on `merge-base`: a release the same prover calls "merging it into
        our pinned ref changes no file" on a healthy host became a corpus
        violation.
      * `rc` is now `None` when git could not be started or timed out, so a
        `rc != 0` guard catches it and a `rc == 1` guard cannot.
    """
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return DID_NOT_RUN, "", f"{e.__class__.__name__}: {e}"
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def _said(rc, code: int) -> bool:
    """Did the command RUN and exit with exactly `code`?

    The one spelling allowed for "git answered N". `rc == 1` is the same test on a
    healthy host and a DIFFERENT test on a host where the command was killed,
    because -9 is not 1 but `not out` is true of both a clean empty answer and a
    corpse. Written out here so the choice is visible at every call site.
    """
    return rc is not None and rc == code


def _ran(rc) -> bool:
    """Did the command execute and exit at all, whatever it said?"""
    return rc is not None and rc >= 0


def _how(rc) -> str:
    """How it ended, in words that cannot be mistaken for a verdict."""
    if rc is None:
        return "it never ran"
    return f"killed by signal {-rc}" if rc < 0 else f"exit {rc}"


def _our_tree_already_has_it(repo, tag_rev: str, pin: str):
    """(True / False / None, why) — recomputed from git, believing nothing.

    The four proofs that make `contained` a true sentence, and no others:
    ancestry; the release's tree IS our tree; the release's tree is the tree of
    the merge-base, so it adds nothing to the line we share; the three-way merge
    of it into our pin produces the tree we already build.

    NONE OF THEM MAY BE ANSWERED BY A COMMAND THAT DID NOT RUN. False here is a
    REFUTATION — it is reported as a corpus violation and fails the sweep — and
    True is a re-proof. Every branch below therefore asks `_said`/`_ran` rather
    than `rc == 1` / `rc != 0`, and every one that cannot get an answer returns
    None with the exit status spelled out.
    """
    rc, _, err = _g(repo, "merge-base", "--is-ancestor", tag_rev, pin)
    if _said(rc, 0):
        return True, "ancestor of our pinned ref"
    if not _said(rc, 1):
        return None, f"merge-base --is-ancestor did not answer ({_how(rc)}): {err[:100]}"
    rc, tt, _ = _g(repo, "rev-parse", f"{tag_rev}^{{tree}}")
    rc2, pt, _ = _g(repo, "rev-parse", f"{pin}^{{tree}}")
    if not _said(rc, 0) or not _said(rc2, 0):
        return None, f"could not read the trees ({_how(rc)}, {_how(rc2)})"
    if tt == pt:
        return True, "identical tree to our pinned ref"
    rc, mb, err = _g(repo, "merge-base", tag_rev, pin)
    # `rc == 1 or not mb` USED TO BE HERE, and `not mb` is true of a `merge-base`
    # the kernel killed (rc -9, empty stdout) as well as of git's own clean "these
    # share no history". The `if rc != 0: return None` on the next line was dead
    # for that case — the killed command had already been turned into a
    # refutation. Only git's own exit 1, and an exit 0 that printed nothing, are
    # the measurement.
    if _said(rc, 1) or (_said(rc, 0) and not mb):
        return False, "shares no ancestor with our pinned ref and its tree is not ours"
    if not _said(rc, 0):
        return None, f"merge-base did not answer ({_how(rc)}): {err[:100]}"
    rc, mt, _ = _g(repo, "rev-parse", f"{mb}^{{tree}}")
    if _said(rc, 0) and mt == tt:
        return True, "changes no file relative to the merge-base with our pinned ref"
    rc, mtout, mterr = _g(repo, "merge-tree", "--write-tree", f"--merge-base={mb}",
                          pin, tag_rev)
    first = ((mtout or "").splitlines() or [""])[0].strip()
    if _said(rc, 0) and re.fullmatch(r"[0-9a-f]{40}", first):
        if first == pt:
            return True, "merging it into our pinned ref changes no file"
        return False, "merging it into our pinned ref changes files we do not have"
    if _said(rc, 1):
        conflicted = [ln for ln in (mtout or "").splitlines() if ln.startswith("CONFLICT")]
        return False, ("merging it into our pinned ref CONFLICTS: "
                       + (conflicted[0][:140] if conflicted else "unresolved"))
    # NOT "git < 2.38", which is what this used to say for every remaining case: a
    # killed merge-tree lands here too, and naming a cause the reader can check and
    # disprove is worse than naming none.
    return None, (f"git merge-tree --write-tree produced no tree ({_how(rc)}): "
                  f"{(mterr or 'no diagnostics')[:100]}")


def _patch_ids(repo, rng: str):
    """{patch-id} for the non-merge commits in `rng`, via `git patch-id --stable`
    — the same normalisation `git cherry` uses internally, computed here without
    going through `git cherry` at all. None when it could not be computed.

    THE SAME UNGUARDED PIPELINE THE MODULE UNDER TEST HAD. Two processes under
    `shell=True`, screened by `if r.returncode != 0` — which is the SHELL's
    status, which is the LAST command's, and `git patch-id` exits 0 on empty
    input. A `git log -p` that died on a missing blob came back as a clean, empty
    set, and an empty set is what the caller reads as PROOF. Measured on a real
    clone with one blob deleted, no shim and no patching: this returned `set()`
    and `_we_carry_every_commit_of_it` FLIPPED from

        (False, "1 of its 1 commit(s) match nothing we carry")     to
        (True,  "all 0 of its commits are patch-identical to ours")

    — a killed command RE-PROVING the claim, in the layer whose entire job is to
    disbelieve the claim. No shell here now: two `Popen`s, both statuses checked,
    and nothing to quote (`{repo}` and `{rng}` were interpolated unquoted, so a
    clone path with a space produced the same silent empty set).
    """
    with tempfile.TemporaryFile(mode="w+", errors="replace") as err:
        try:
            log = subprocess.Popen(
                ["git", "-C", str(repo), "log", "-p", "--no-merges",
                 "--format=commit %H", rng],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, text=True)
        except OSError:
            return None
        try:
            pid_ = subprocess.Popen(["git", "patch-id", "--stable"], stdin=log.stdout,
                                    stdout=subprocess.PIPE, stderr=err, text=True)
        except OSError:
            log.kill()
            log.wait()
            return None
        log.stdout.close()
        try:
            out, _ = pid_.communicate(timeout=900)
            log.wait(timeout=900)
        except (OSError, subprocess.SubprocessError):
            for p in (log, pid_):
                p.kill()
            return None
    if log.returncode != 0 or pid_.returncode != 0:
        return None
    return {ln.split()[0] for ln in (out or "").splitlines() if ln.strip()}


def _we_carry_every_commit_of_it(repo, tag_rev: str, pin: str):
    """(True / False / None, why) for the patch-equivalence claim, recomputed."""
    rc, n_all, _ = _g(repo, "rev-list", "--count", f"{pin}..{tag_rev}")
    rc2, n_nm, _ = _g(repo, "rev-list", "--no-merges", "--count", f"{pin}..{tag_rev}")
    if not _said(rc, 0) or not _said(rc2, 0) or not n_all.isdigit() or not n_nm.isdigit():
        return None, f"could not size the range ({_how(rc)}, {_how(rc2)})"
    if int(n_all) != int(n_nm):
        return False, (f"the range holds {int(n_all) - int(n_nm)} merge commit(s) whose "
                       f"own trees nothing compared — patch equivalence was claimed over "
                       f"a range that was never fully examined")
    theirs = _patch_ids(repo, f"{pin}..{tag_rev}")
    ours = _patch_ids(repo, f"{tag_rev}..{pin}")
    if theirs is None or ours is None:
        return None, "git patch-id would not run"
    missing = theirs - ours
    if missing:
        return False, f"{len(missing)} of its {len(theirs)} commit(s) match nothing we carry"
    return True, f"all {len(theirs)} of its commits are patch-identical to ours"


def test_every_contained_release_in_the_REAL_ledger_survives_an_independent_check():
    """NOT A FIXTURE. Every row the real corpus files under `contained_releases`,
    re-proved from the real clone.

    Measured against `fix/release-containment-measured` at 3661f8d: TWO rows fail
    — yices2 `yices-2.7.0` (conflicts on doc/sphinx/source/conf.py) and cocotb
    `v1.5.0rc1` (conflicts on documentation/source/release_notes.rst).
    """
    led_dir, ledgers = _corpus_ledgers()
    if not ledgers:
        pytest.skip(f"no fork ledger at {led_dir} — the corpus half of this "
                    f"invariant checked NOTHING on this host")
    checked, unverifiable, violations, with_bucket = 0, [], [], 0
    for led in ledgers:
        rows = led.get("contained_releases")
        if rows is None:
            continue                      # a ledger written before the bucket existed
        with_bucket += 1
        pin = led.get("pinned_ref_full") or led.get("pinned_ref")
        repo = df.FORK_CLONES / led["tool"]
        if not pin or not (repo / ".git").exists():
            unverifiable.append(f"{led['tool']}: no pin or no clone")
            continue
        for row in rows:
            ok, why = _our_tree_already_has_it(repo, row["tag"] + "^{commit}", pin)
            if ok is True:
                checked += 1
            elif ok is None:
                unverifiable.append(f"{led['tool']} {row['tag']}: {why}")
            else:
                violations.append(f"{led['tool']} {row['tag']}: {why} "
                                  f"| filed as: {row.get('why')}")
    # The refutation half runs against ANY corpus, whoever wrote it: a row that
    # is there and is wrong is wrong. Only the non-vacuity half below needs to
    # know whose code produced the file.
    assert not violations, (
        f"{len(violations)} row(s) in {led_dir} are filed under `contained_releases` "
        f"while an independent check of the repository says our pinned ref neither "
        f"contains them nor merges them as a no-op:\n  " + "\n  ".join(violations))
    state, whose = _corpus_is_of_the_code_under_test(ledgers, "contained_releases")
    if state != "yes":
        pytest.skip(f"the corpus at {led_dir} is not one the code under test produced "
                    f"({whose}), so its silence about `contained_releases` says nothing "
                    f"about this code. Run discover_forks.py to produce one — until then "
                    f"this half of the invariant checked NOTHING on this host")
    assert with_bucket, (
        f"no ledger in {led_dir} carries a `contained_releases` key — and it was written "
        f"by code that emits one ({whose}), so the sweep stopped writing what it verifies")
    assert checked, (
        f"{with_bucket} ledger(s) in {led_dir} but ZERO rows could be verified "
        f"({len(unverifiable)} unverifiable: {unverifiable[:5]}). A clean result over "
        f"zero rows is the defect this test exists to catch")


def test_every_patch_equivalent_release_in_the_REAL_ledger_survives_an_independent_check():
    """The bucket round 3 splits out, checked the same way and by an
    implementation that does not go through `git cherry` at all. Splitting a
    bucket out and then not checking it moves the unverified claim rather than
    removing it.

    It is also a second, independent detector of the merge hole: a release whose
    range holds a merge commit cannot have had every commit compared, so the
    claim fails here even when `git cherry` says nothing.
    """
    led_dir, ledgers = _corpus_ledgers()
    if not ledgers:
        pytest.skip(f"no fork ledger at {led_dir}")
    checked, unverifiable, violations, present = 0, [], [], False
    for led in ledgers:
        rows = led.get("patch_equivalent_releases")
        if rows is None:
            continue
        present = True
        pin = led.get("pinned_ref_full") or led.get("pinned_ref")
        repo = df.FORK_CLONES / led["tool"]
        if not pin or not (repo / ".git").exists():
            continue
        for row in rows:
            ok, why = _we_carry_every_commit_of_it(repo, row["tag"] + "^{commit}", pin)
            if ok is True:
                checked += 1
            elif ok is None:
                unverifiable.append(f"{led['tool']} {row['tag']}: {why}")
            else:
                violations.append(f"{led['tool']} {row['tag']}: {why}")
    assert not violations, (
        f"row(s) filed under `patch_equivalent_releases` in {led_dir} that an "
        f"independent patch-id comparison refutes:\n  " + "\n  ".join(violations))
    state, whose = _corpus_is_of_the_code_under_test(ledgers, "patch_equivalent_releases")
    if state != "yes":
        pytest.skip(f"the corpus at {led_dir} was not produced by the code under test "
                    f"({whose}), so nothing here was checked. Run discover_forks.py to "
                    f"produce one")
    assert present, (
        f"no ledger in {led_dir} carries a `patch_equivalent_releases` key — and it was "
        f"written by code that emits one ({whose}), so the bucket was dropped from what "
        f"the sweep publishes")


def test_the_sweep_itself_records_the_bucket_check_for_every_tool_it_measured():
    """The production run must leave the evidence behind, because the corpus test
    above cannot run where there is no corpus. A ledger that measured releases
    and carries no `bucket_check` was written by something that did not verify
    what it wrote — which is the state the corpus was in when the two live rows
    were found.
    """
    led_dir, ledgers = _corpus_ledgers()
    if not ledgers:
        pytest.skip(f"no fork ledger at {led_dir}")
    measured = [l for l in ledgers if l.get("behind_releases_status") == df.MEASURED]
    if not measured:
        pytest.skip(f"no MEASURED row in {led_dir}")
    missing = [l["tool"] for l in measured
               if not isinstance((l.get("release_containment") or {}).get("bucket_check"),
                                 dict)]
    assert not missing, (
        f"{len(missing)} of {len(measured)} MEASURED ledger(s) in {led_dir} carry no "
        f"bucket_check — nothing verified the buckets they published: {missing[:8]}")
    dirty = {l["tool"]: l["release_containment"]["bucket_check"]["violations"]
             for l in measured
             if l["release_containment"]["bucket_check"].get("violations")}
    assert not dirty, f"the sweep recorded bucket violations and published anyway: {dirty}"


# ════════════════════════════════════════════════════════════════════════════
# ROUND 4 — A COMMAND THAT DID NOT RUN IS NOT AN ANSWER
#
# Round 1 removed a 404 that had been folded into the same boolean as a measured
# "not contained". `_git` was doing the same thing one layer down: every
# subprocess exception became `return 1, "", …`, and 1 is git's own exit code for
# a CLEAN, DEFINITE NO — "not an ancestor", "no merge base", "this merge
# conflicts". A timeout and a measurement arrived at every call site as the same
# three values.
#
# HOW THESE ARE PROVOKED. The real thing is a `git` on PATH that hangs; that is
# how the defects below were first measured, end to end through `discover_one`
# with no production code patched at all, and it costs 60 s of real sleeping per
# affected call. The tests raise `TimeoutExpired` at the boundary where a hanging
# git really delivers it — `subprocess.run`, matched on the argv of the ONE
# command under test, everything else delegating to the real one. Same exception,
# same code path, same inputs, no production symbol replaced.
# ════════════════════════════════════════════════════════════════════════════

_REAL_RUN = subprocess.run


def _hang(monkeypatch, matches):
    """Make every `git` invocation whose argv satisfies `matches` time out."""
    def fake(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and matches(list(cmd)):
            raise subprocess.TimeoutExpired(list(cmd), kw.get("timeout") or 60)
        return _REAL_RUN(cmd, *a, **kw)
    monkeypatch.setattr(df.subprocess, "run", fake)


def _merge_base_calls(cmd):
    return "merge-base" in cmd and "--is-ancestor" not in cmd


def test_a_git_that_did_not_run_does_not_return_gits_code_for_a_clean_no(monkeypatch):
    """THE ROOT, at the one function every other measurement goes through.

    A `git` that cannot be executed at all — here a real, instant `PermissionError`
    from a non-executable file, not a simulation of one — must not come back
    wearing exit code 1. Exit code 1 is what git says when it RAN and the answer
    is no.

    PATH is REPLACED rather than prepended to, measured: `execvp` treats EACCES as
    "keep looking" and finds the real git further down, so a shim in front of a
    working git proves nothing. With this PATH there is no other git to find.
    """
    with tempfile.TemporaryDirectory() as d:
        shim = Path(d) / "bin"
        shim.mkdir()
        (shim / "git").write_text("#!/bin/sh\nexit 0\n")     # never made executable
        monkeypatch.setenv("PATH", str(shim))
        rc, out, err = df._git(Path(d), "merge-base", "--is-ancestor", "a", "b")
    assert "Error" in err, f"the fixture did not stop git from running at all: {err!r}"
    assert rc != 1, (
        f"a git that could not be executed came back as rc=1, which is git's exit code "
        f"for a clean 'no'. Every caller that reads 1 as a fact now has one: {err[:120]}")
    assert rc != 0, "…and it must not read as success either"


def test_a_merge_base_that_did_not_run_is_not_a_release_that_left_our_history(monkeypatch):
    """H1, end to end and on a PUBLISHED FIELD.

    `v1.0` carries a file we plainly do not have; `v0.9` is an ancestor of our pin
    and anchors the comparison. With `git merge-base` timing out,
    `_local_containment` used to read the exception's rc=1 as "shares no ancestor
    with our pinned ref", set `disjoint`, and step 5 turned that into SUPERSEDED —
    so the release we owe left the count while the row still said `measured`.

    Measured with a hanging `git` on PATH and nothing else changed:
        control  behind_releases=1 measured   new=['v1.0']
        shim     behind_releases=0 measured   superseded=['v1.0']
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        c0 = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v0.9", c0)
        _git(repo, "checkout", "-q", "-b", "rel", c0)
        _git(repo, "tag", "v1.0", _commit(repo, "brand-new.txt", "work we lack\n", "2026-02-01"))
        _git(repo, "checkout", "-q", "master")
        pin = _commit(repo, "b", "b", "2026-01-10")
        _hang(monkeypatch, _merge_base_calls)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-02-02", False),
                                  ("v0.9", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-10"))
    assert "v1.0" not in _tags_of(led, "superseded_releases"), (
        "a release was declared to share no ancestor with the line we track — an "
        "abandoned history — on the strength of a `git merge-base` that never ran")
    assert led["behind_releases"] is None and led["behind_releases_status"] == "unknown", (
        f"the count survived a measurement that did not happen: "
        f"behind={led['behind_releases']} status={led['behind_releases_status']}")
    assert "v1.0" in _tags_of(led, "undetermined_releases"), \
        "the release neither counted nor said why it could not be decided"


def test_a_release_we_lack_is_still_counted_when_git_answers(monkeypatch):
    """THE CONTROL for the two above. Same fixture, real git, and the release is
    counted — so the assertions there are about the timeout, not about a fixture
    that produces nulls whatever happens."""
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        c0 = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v0.9", c0)
        _git(repo, "checkout", "-q", "-b", "rel", c0)
        _git(repo, "tag", "v1.0", _commit(repo, "brand-new.txt", "work we lack\n", "2026-02-01"))
        _git(repo, "checkout", "-q", "master")
        pin = _commit(repo, "b", "b", "2026-01-10")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-02-02", False),
                                  ("v0.9", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-10"))
    assert _tags_of(led) == ["v1.0"] and led["behind_releases"] == 1
    assert led["behind_releases_status"] == "measured"


def test_a_merge_base_that_did_not_run_does_not_refute_a_release_we_contain(monkeypatch):
    """H2a, at the re-prover itself. `_verify_contained` returning False is a
    REFUTATION: it nulls the row, files it under `undetermined_releases`, nulls
    the tool's count and exits the sweep non-zero. A `git merge-base` that never
    ran must not be able to produce one."""
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        (repo / "VERSION").write_text("1.0.0rc1\n")
        c0 = _commit(repo, "f", "x", "2026-01-01")
        _git(repo, "checkout", "-q", "-b", "rel-1", c0)
        _commit(repo, "VERSION", "1.0.0rc2\n", "2026-02-01")
        final = _commit(repo, "VERSION", "1.0.0\n", "2026-02-02")
        _git(repo, "checkout", "-q", "master")
        _commit(repo, "VERSION", "1.0.0\n", "2026-02-03")
        pin = _commit(repo, "f", "y", "2026-03-01")
        ok, why = df._verify_contained(clones / TOOL, final, pin)
        assert ok is True, f"the fixture is not a contained release to begin with: {why}"
        _hang(monkeypatch, _merge_base_calls)
        timed_out, why2 = df._verify_contained(clones / TOOL, final, pin)
    assert timed_out is not False, (
        f"a re-proof whose `git merge-base` never ran REFUTED a release our pinned ref "
        f"genuinely contains, saying: {why2!r}")
    assert timed_out is None, "…and the honest disposition is 'could not be re-proved'"


def test_the_sweep_does_not_refuse_a_row_because_the_re_proof_timed_out(monkeypatch):
    """H2a end to end, on PUBLISHED FIELDS. Only the re-proof's own `merge-base`
    times out — it is the second of the run, the classification's having already
    answered — so the row is classified exactly as it is on a healthy host and
    only the verification is starved.

    Measured with a hanging `git` on PATH, `GK_SHIM_FROM=2`:
        control  behind=0 measured  base=v1.0  checked=2  violations=[]
        shim     behind=None unknown  undetermined=['v1.0']  and the sweep exits non-zero
    """
    seen = []

    def second_merge_base_onward(cmd):
        if not _merge_base_calls(cmd):
            return False
        seen.append(cmd)
        return len(seen) >= 2

    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        (repo / "VERSION").write_text("1.0.0rc1\n")
        c0 = _commit(repo, "f", "x", "2026-01-01")
        _git(repo, "tag", "v0.9", c0)
        _git(repo, "checkout", "-q", "-b", "rel-1", c0)
        _commit(repo, "VERSION", "1.0.0rc2\n", "2026-02-01")
        _git(repo, "tag", "v1.0", _commit(repo, "VERSION", "1.0.0\n", "2026-02-02"))
        _git(repo, "checkout", "-q", "master")
        _commit(repo, "VERSION", "1.0.0\n", "2026-02-03")
        pin = _commit(repo, "f", "y", "2026-03-01")
        _hang(monkeypatch, second_merge_base_onward)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-02-02", False),
                                  ("v0.9", "2026-01-01", False)],
                        fork_point=(pin, "2026-03-01"))
    chk = (led.get("release_containment") or {}).get("bucket_check") or {}
    assert not chk.get("violations"), (
        f"a timed-out re-proof was recorded as the repository REFUTING a row, which "
        f"exits the sweep non-zero on a healthy host: {chk.get('violations')}")
    assert "v1.0" in _tags_of(led, "contained_releases"), \
        "the row was withdrawn because its verification could not be run"
    assert led["behind_releases"] == 0 and led["behind_releases_status"] == "measured"
    assert chk.get("unverifiable"), \
        "a re-proof that could not run left no trace at all — that is the silent part"


# ── H2b — the verdict the check refused is still the verdict everyone reads ──

def _lying_containment(monkeypatch, sha):
    """Claim CONTAINED for one release that plainly is not — the injection the
    round-3 bucket test already uses, and the only way to get a REAL refutation
    out of a healthy fixture (classifier and re-prover agree by construction when
    both are told the truth)."""
    real = df._local_containment

    def lying(repo_, tag_sha, pin_sha, _real=real, _s=sha):
        if tag_sha == _s:
            return df.CONTAINED, "a claim nothing checked", False
        return _real(repo_, tag_sha, pin_sha)
    monkeypatch.setattr(df, "_local_containment", lying)


def test_a_refuted_row_is_not_published_as_the_release_we_build(monkeypatch):
    """H2b. `_verify_buckets` refutes a row by nulling its verdict; `in_pin` was
    set BEFORE the check ran and nothing cleared it, and `base_release` is chosen
    from `in_pin`.

    Measured: `undetermined_releases = ['v2.0']`, `bucket_check.violations =
    ['v2.0']`, `base_release = 'v2.0'`. The ledger said "we cannot verify that we
    contain v2.0" and "the release we build is v2.0" in the same file.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        pin = _commit(repo, "b", "b", "2026-01-10")
        _git(repo, "checkout", "-q", "-b", "rel", a)
        rel = _commit(repo, "brand-new.txt", "work we plainly do not have\n", "2026-02-01")
        _git(repo, "tag", "v2.0", rel)
        _git(repo, "checkout", "-q", "master")
        _lying_containment(monkeypatch, rel)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False),
                                  ("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-10"))
    assert "v2.0" in _tags_of(led, "undetermined_releases"), "the fixture stopped refuting"
    assert led["base_release"] != "v2.0", (
        "the ledger publishes as `base_release` — the release we build — a row the same "
        "sweep refused to verify and filed under `undetermined_releases`")
    assert led["base_release"] == "v1.0", (
        f"`base_release` must fall back to the newest release that SURVIVED the check, "
        f"not to nothing: {led['base_release']!r}")


def test_a_refuted_row_does_not_anchor_the_trunk_order_for_the_others(monkeypatch):
    """The second half of H2b, and the one that changes another release's BUCKET.

    `ref_t` — the trunk point every other release is ordered against — is taken
    from the same refuted row. `v1.5`'s line left the trunk after `v1.0`'s and
    before `v2.0`'s, so anchoring on the refuted `v2.0` files it SUPERSEDED, "an
    older series, not a release we could advance to", and it leaves the count.
    Anchored on the release that actually survived verification it is what it is:
    a release we do not have.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "trunk-a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        b = _commit(repo, "trunk-b", "b", "2026-01-05")
        c = _commit(repo, "trunk-c", "c", "2026-01-08")
        pin = _commit(repo, "trunk-d", "d", "2026-01-10")
        _git(repo, "checkout", "-q", "-b", "mid", b)
        _git(repo, "tag", "v1.5", _commit(repo, "mid-work.txt", "theirs\n", "2026-01-20"))
        _git(repo, "checkout", "-q", "-b", "late", c)
        rel = _commit(repo, "late-work.txt", "theirs too\n", "2026-02-01")
        _git(repo, "tag", "v2.0", rel)
        _git(repo, "checkout", "-q", "master")
        _lying_containment(monkeypatch, rel)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False),
                                  ("v1.5", "2026-01-20", False),
                                  ("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-10"))
    assert "v1.5" not in _tags_of(led, "superseded_releases"), (
        "a release was dropped from the count as 'an older series' by comparing it "
        "against the trunk point of a row the sweep had just refused to verify")
    assert "v1.5" in _tags_of(led, "new_releases"), \
        f"v1.5 is a release we do not have; it is in no counted bucket: {led['new_releases']}"


# ── THE BUCKET THAT REMOVES RELEASES FROM THE COUNT ─────────────────────────

def test_a_superseded_release_is_re_proved_before_it_leaves_the_count(monkeypatch):
    """`VERIFIED_BUCKETS` re-proved `contained` and `patch-equivalent` and nothing
    else. SUPERSEDED is the bucket that takes a release OUT of
    `behind_releases` — on the corpus, offline over the 36 real clones, 59 rows of
    it, including both of the live zeroes that rest on it — and nothing re-proved
    a single one.

    Here `_carried_by` claims the release we build already carries a release that
    adds a file nobody has. Nothing downstream of step 5 could tell that from a
    true one, which is the point: the count must not shrink on a claim no second
    implementation will stand behind.
    """
    real = df._carried_by

    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        pin = _commit(repo, "b", "b", "2026-01-10")
        _git(repo, "checkout", "-q", "-b", "rel", a)
        rel = _commit(repo, "brand-new.txt", "work nobody carries\n", "2026-02-01")
        _git(repo, "tag", "v2.0", rel)
        _git(repo, "checkout", "-q", "master")

        def lying(clone, tool, up_full, x, y, out, _real=real, _s=rel):
            return True if x == _s else _real(clone, tool, up_full, x, y, out)

        monkeypatch.setattr(df, "_carried_by", lying)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False),
                                  ("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-10"))
    chk = (led.get("release_containment") or {}).get("bucket_check") or {}
    assert "v2.0" not in _tags_of(led, "superseded_releases"), (
        "a release left `behind_releases` on a superseded claim that an independent "
        "check of the repository refutes, and nothing re-proved it")
    assert led["behind_releases"] is None and led["behind_releases_status"] == "unknown", (
        f"the count shrank on a refuted claim: behind={led['behind_releases']} "
        f"status={led['behind_releases_status']}")
    assert any(v["claim"] == df.SUPERSEDED for v in chk.get("violations") or []), \
        f"the run recorded no violation for the superseded row it refused: {chk}"


def test_an_honest_superseded_release_survives_the_re_proof(monkeypatch):
    """THE NEGATIVE CONTROL for the check above, and the anti-vacuity half.

    `v0.9-old` really is behind us: its line left the upstream trunk at the root,
    before `v1.0`'s line left it, and no rebase reaches it. The re-proof must
    leave it exactly where it is — a check that refuses everything protects
    nothing — and it must SAY it re-proved a superseded row, because a `checked`
    total that never distinguishes buckets is how 59 unexamined rows hid behind a
    reassuring number.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        root = _commit(repo, "root", "r", "2026-01-01")
        _git(repo, "checkout", "-q", "-b", "old", root)
        _git(repo, "tag", "v0.9-old", _commit(repo, "old-work.txt", "old series\n", "2026-01-03"))
        _git(repo, "checkout", "-q", "master")
        a = _commit(repo, "a", "a", "2026-01-05")
        _git(repo, "tag", "v1.0", a)
        pin = _commit(repo, "b", "b", "2026-01-10")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-01-06", False),
                                  ("v0.9-old", "2026-01-03", False)],
                        fork_point=(pin, "2026-01-10"))
    chk = (led.get("release_containment") or {}).get("bucket_check") or {}
    assert _tags_of(led, "superseded_releases") == ["v0.9-old"], (
        f"the re-proof refused a release that genuinely is behind us: "
        f"superseded={_tags_of(led, 'superseded_releases')} "
        f"undetermined={_tags_of(led, 'undetermined_releases')}")
    assert not chk.get("violations"), f"an honest run reported a violation: {chk}"
    assert led["behind_releases"] == 0 and led["behind_releases_status"] == "measured"
    assert (chk.get("by_bucket") or {}).get(df.SUPERSEDED) == 1, (
        f"the record does not say that the bucket which REMOVES a release from the "
        f"count was re-proved at all: {chk}")


def test_an_is_ancestor_that_did_not_run_does_not_order_a_release_behind_us(monkeypatch):
    """`_ancestor` is the other reader that took rc=1 as a fact, and it is the one
    the trunk ordering rests on: `_ancestor(t(base), t(R))` returning False files
    R as "an older series, not a release we could advance to" and drops it.

    `--is-ancestor` between the two TRUNK POINTS is starved here; the containment
    probes, which end at our pin, are left alone. Pre-fix that exception came back
    as a definite "not an ancestor" and never fell through to the API at all.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "trunk-a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        b = _commit(repo, "trunk-b", "b", "2026-01-05")
        pin = _commit(repo, "trunk-c", "c", "2026-01-10")
        _git(repo, "checkout", "-q", "-b", "mid", b)
        _git(repo, "tag", "v1.5", _commit(repo, "mid-work.txt", "theirs\n", "2026-01-20"))
        _git(repo, "checkout", "-q", "master")
        _hang(monkeypatch, lambda c: "--is-ancestor" in c and c[-1] != pin)
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.5", "2026-01-20", False),
                                  ("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-10"))
    assert "v1.5" not in _tags_of(led, "superseded_releases"), (
        "a release was ordered behind us — and dropped — by an `is-ancestor` probe "
        "that never ran")
    assert led["behind_releases"] is None and led["behind_releases_status"] == "unknown", \
        f"the count survived: behind={led['behind_releases']} {led['behind_releases_status']}"


def test_a_folded_prerelease_is_re_proved_too(monkeypatch):
    """FOLDED removes a release from the count exactly as SUPERSEDED does — the
    prerelease is counted under the final that carries it — so it is re-proved by
    the same machinery, from the same recorded basis."""
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        pin = _commit(repo, "b", "b", "2026-01-10")
        _git(repo, "checkout", "-q", "-b", "rel", a)
        pre = _commit(repo, "feature.txt", "the work\n", "2026-02-01")
        _git(repo, "tag", "v2.0rc1", pre)
        _git(repo, "tag", "v2.0", _commit(repo, "notes.txt", "notes\n", "2026-02-02"))
        _git(repo, "checkout", "-q", "master")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False),
                                  ("v2.0rc1", "2026-02-01", True),
                                  ("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-10"))
    chk = (led.get("release_containment") or {}).get("bucket_check") or {}
    assert _tags_of(led, "folded_releases") == ["v2.0rc1"], \
        f"the fixture stopped folding: {_tags_of(led, 'folded_releases')}"
    assert not chk.get("violations"), f"the fold was refuted: {chk['violations']}"
    assert (chk.get("by_bucket") or {}).get(df.FOLDED) == 1, (
        f"the fold — a release removed from the count — was re-proved by nothing: {chk}")


def test_the_ledger_says_which_buckets_the_re_proof_covered(monkeypatch):
    """Unverifiability has to be VISIBLE, not inferred from the absence of a
    number. `checked` counts successes and cannot say which rows nobody looked
    at, which is how a line reading "271 rows re-proved" sat on top of 59
    unexamined ones."""
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        pin = _commit(repo, "b", "b", "2026-01-10")
        _git(repo, "checkout", "-q", "-b", "rel", a)
        _git(repo, "tag", "v2.0", _commit(repo, "new.txt", "theirs\n", "2026-02-01"))
        _git(repo, "checkout", "-q", "master")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v2.0", "2026-02-02", False),
                                  ("v1.0", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-10"))
    cov = ((led.get("release_containment") or {}).get("bucket_check") or {}).get("coverage")
    assert cov, "the ledger does not say what the re-proof covered at all"
    assert set(cov["buckets_reproved"]) >= {df.CONTAINED, df.EQUIVALENT, df.SUPERSEDED,
                                            df.FOLDED, df.NEW}, (
        f"a bucket that decides the count is published as re-proved by nothing: "
        f"{cov['buckets_reproved']}")
    rows = cov["rows"]
    accounted = ((led["release_containment"]["bucket_check"]["checked"])
                 + len(led["release_containment"]["bucket_check"]["unverifiable"])
                 + cov["rows_that_assert_nothing"])
    assert accounted == sum(rows.values()), (
        f"{sum(rows.values())} rows published, {accounted} accounted for by the "
        f"re-proof record: {rows} {led['release_containment']['bucket_check']}")


# ── THE SUITE ITSELF — a corpus nobody produced is not a failing invariant ───

def _fake_corpus(monkeypatch, tmp: Path, written_by: str | None, extra=None):
    tmp.mkdir(parents=True, exist_ok=True)
    led = {"tool": TOOL, "pinned_ref_full": "0" * 40, **(extra or {})}
    if written_by:
        led[gk_state.PROVENANCE_KEY] = {"commit": written_by}
    (tmp / f"{TOOL}.json").write_text(json.dumps(led))
    monkeypatch.setattr(df, "LEDGER", tmp)


def _commit_before_the_bucket(key: str):
    """The commit whose `discover_forks.py` predates `key`, measured from this
    repository rather than written down."""
    # An ABSOLUTE pathspec: with `git -C <subdir>` a relative one is resolved
    # against that subdir, and `fork-gatekeeper/discover_forks.py` matched nothing
    # from inside `fork-gatekeeper/` — which made this test skip itself.
    path = str(Path(df.__file__).resolve())
    r = subprocess.run(["git", "-C", str(HERE), "log", "-S", key, "--format=%H", "--", path],
                       capture_output=True, text=True)
    shas = (r.stdout or "").split()
    if r.returncode != 0 or not shas:
        return None
    p = subprocess.run(["git", "-C", str(HERE), "rev-parse", f"{shas[-1]}^"],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def test_a_corpus_written_before_the_bucket_existed_is_a_skip_with_a_reason(monkeypatch):
    """On a clean checkout `pytest -q` was `2 failed, 367 passed`: the corpus in
    `~/.cache` was written on 2026-07-31 by commit fdb754c4a2b9, two rounds before
    either bucket key existed, and the non-vacuity assertions read its silence as
    the code under test having stopped writing them.

    A corpus no version of this code produced says nothing about this code. That
    is a skip — and it names the commit, so it cannot be mistaken for a pass.
    """
    old = _commit_before_the_bucket("patch_equivalent_releases")
    if not old:
        pytest.skip("this checkout cannot identify a commit predating the bucket")
    with tempfile.TemporaryDirectory() as d:
        _fake_corpus(monkeypatch, Path(d) / "ledger", written_by=old)
        with pytest.raises(pytest.skip.Exception) as e:
            test_every_patch_equivalent_release_in_the_REAL_ledger_survives_an_independent_check()
    assert old[:12] in str(e.value) or old in str(e.value), \
        f"the skip does not say WHICH writer produced the corpus: {e.value}"


def _must_fail(fn, what):
    """Run a test function and require an AssertionError out of it.

    A SKIP is not an acceptable outcome and is turned into a failure here, which
    is the whole point of this helper: `pytest.raises(AssertionError)` lets
    `Skipped` straight through, and the outer test then skips too. Measured
    against a naive gate — one that skips whenever the key is absent, with no
    measurement of who wrote the corpus — the guard below reported SKIPPED, which
    is a checker reporting nothing while looking like it ran.
    """
    try:
        fn()
    except AssertionError as e:
        return str(e)
    except BaseException as e:                       # noqa: BLE001 — incl. pytest's Skipped
        pytest.fail(f"{what}: got {e.__class__.__name__} instead of a failure — {e}")
    pytest.fail(f"{what}: it passed")


def test_a_corpus_this_code_wrote_without_the_bucket_is_still_a_failure(monkeypatch):
    """The other side of the same gate, and the reason it is not a way out. If the
    corpus WAS produced by code that emits the key — HEAD's own — and the key is
    not there, the sweep stopped publishing what it verifies, and that is a
    failure exactly as before."""
    head = subprocess.run(["git", "-C", str(HERE), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if head.returncode != 0:
        pytest.skip("not a git checkout")
    with tempfile.TemporaryDirectory() as d:
        _fake_corpus(monkeypatch, Path(d) / "ledger", written_by=head.stdout.strip())
        why = _must_fail(
            test_every_patch_equivalent_release_in_the_REAL_ledger_survives_an_independent_check,
            "a corpus written by the code under test, with the bucket key missing, "
            "must still be a failure")
    assert "patch_equivalent_releases" in why


def test_a_corpus_with_rows_is_checked_whoever_wrote_it(monkeypatch):
    """And the gate never suppresses a REFUTATION. A foreign corpus that carries
    rows still has every one of them re-proved; only the assertion about what is
    ABSENT is gated, because absence is the only part whose meaning depends on
    who wrote the file."""
    old = _commit_before_the_bucket("patch_equivalent_releases") or "0" * 40
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d) / "clones"
        repo = _repo(clones / TOOL)
        a = _commit(repo, "a", "a", "2026-01-01")
        _git(repo, "tag", "v1.0", a)
        pin = _commit(repo, "b", "b", "2026-01-10")
        _git(repo, "checkout", "-q", "-b", "rel", a)
        _git(repo, "tag", "v2.0", _commit(repo, "new.txt", "theirs\n", "2026-02-01"))
        monkeypatch.setattr(df, "FORK_CLONES", clones, raising=False)
        _fake_corpus(monkeypatch, Path(d) / "ledger", written_by=old,
                     extra={"pinned_ref_full": pin,
                            "patch_equivalent_releases": [{"tag": "v2.0", "why": "a lie"}]})
        why = _must_fail(
            test_every_patch_equivalent_release_in_the_REAL_ledger_survives_an_independent_check,
            "a foreign corpus carrying a refutable row must still be refuted")
    assert "v2.0" in why, f"the row was not re-proved at all: {why}"


# ════════════════════════════════════════════════════════════════════════════
# ROUND 5 — THE DEFAULT IS THE DEFECT, AND THE PIPELINE HID THE PRODUCER
#
# Round 4 made a command that did not run stop wearing git's exit code for a
# clean NO. Its own verifier then found the same shape three more times, twice in
# production, and all three are the SAME sentence: a thing that produced no
# result was allowed to produce a verdict.
#
#   1. `_local_containment` propagated `merge-tree`'s "did not run" correctly and
#      then FELL THROUGH TO NEW — and NEW is not silence, it is the claim "this
#      release carries commits and file changes our pinned ref does not have".
#   2. `git log -p … | git patch-id --stable` under `shell=True`, screened by
#      `if r.returncode != 0`. A pipeline's status is its LAST command's and
#      `git patch-id` exits 0 on empty input, so the screen could not see the
#      producer fail. `_verify_carried_by` — the re-proof round 4 added — was
#      therefore satisfiable by a command that failed.
#   3. This file's own prover had both folds: a SIGKILLed `merge-base` (rc -9,
#      empty stdout) REFUTED a row through `rc == 1 or not mb`, and the same
#      unguarded pipeline made `_we_carry_every_commit_of_it` RE-PROVE one.
#
# HOW THESE ARE PROVOKED. A `git` on PATH, or a real clone with one object
# deleted. No production module is patched and no production symbol is replaced;
# the shim SIGKILLs itself for the one subcommand under test and `exec`s the real
# git for everything else, which is a real `rc == -9` at the real boundary and
# costs no wall time. The 180 s / 300 s timeout path was measured too, with a
# `sleep 999` shim — see the docstrings; it produces the identical published
# fields and is not run here because it costs eight minutes per case.
# ════════════════════════════════════════════════════════════════════════════

_REAL_GIT = shutil.which("git", path="/usr/bin:/bin:/usr/local/bin") or "/usr/bin/git"


def _git_shim(monkeypatch, tmp: Path, sub: str, *, when_not: str = "") -> Path:
    """Put a `git` on PATH that DIES on `sub` and is the real git otherwise.

    `kill -9 $$` rather than `exit 1`: the point of the round is that a command
    which produced no result must not produce a verdict, and an `exit 1` IS a
    result — git's own code for a clean NO. A signal gives `returncode == -9`,
    which is what a hung command killed by a supervisor, an OOM kill and a
    `TimeoutExpired`-then-kill all look like from `subprocess`.

    `when_not` excludes a form of the same subcommand (`--is-ancestor`), so a
    fixture can starve `git merge-base` without also starving the ancestry test
    that runs before it.
    """
    d = tmp / "shimbin"
    d.mkdir(parents=True, exist_ok=True)
    guard = (f'  [ "$a" = "{when_not}" ] && hit=0\n' if when_not else "")
    (d / "git").write_text(
        "#!/bin/sh\n"
        "hit=0\n"
        'for a in "$@"; do\n'
        f'  [ "$a" = "{sub}" ] && hit=1\n'
        f"{guard}"
        "done\n"
        '[ "$hit" = 1 ] && kill -9 $$\n'
        f'exec {_REAL_GIT} "$@"\n')
    (d / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ["PATH"])
    return d


def _only_the_merge_test_can_see_it(clones: Path):
    """A release our pin genuinely holds, provable by NOTHING BUT the three-way
    merge — the shape `_merge_changes_nothing` exists for.

    Our pin reaches `x = "X"` in one commit and moves on; the release reaches the
    same `x = "X"` in two, so no patch-id of ours matches and `git cherry` prints
    `+`. It is not an ancestor, its tree is not the merge-base's, and merging it
    into our pin writes EXACTLY the tree we already build. Returns (repo, pin).
    """
    repo = _repo(clones / TOOL)
    a = _commit(repo, "a", "a\n", "2026-01-01")
    _git(repo, "tag", "v0.9", a)
    _commit(repo, "x", "X\n", "2026-01-05")
    pin = _commit(repo, "z", "z\n", "2026-01-06")
    _git(repo, "checkout", "-q", "-b", "rel", a)
    _commit(repo, "x", "A\n", "2026-01-03")
    _git(repo, "tag", "v1.0", _commit(repo, "x", "X\n", "2026-01-04"))
    _git(repo, "checkout", "-q", "master")
    return repo, pin


def test_a_merge_test_that_did_not_run_does_not_become_the_claim_that_we_lack_it(monkeypatch):
    """R5-1, THE DEFAULT, end to end and on four PUBLISHED FIELDS.

    `merge-tree` is the only test that can see this release is already ours. With
    it dead, every prover has failed to prove containment — and `_local_containment`
    ran out of questions and answered NEW.

    MEASURED through `discover_one`, no production module patched, a `git` on PATH:

        control       behind=0 measured base=v1.0 contained=[v1.0,v0.9] violations=[]
        merge-tree    behind=1 measured base=v0.9 new=[v1.0]
        SIGKILLed     unverifiable=[(v1.0,new,"…produced no tree (killed by signal 9)")]
                      violations=[]   ->  main() exits 0

    …and identically with a `sleep 999` shim, where the failure arrives as the
    real `TimeoutExpired` at the real 180 s boundary:

        hang          behind=1 measured base=v0.9 new=[v1.0]
                      unverifiable=[(v1.0,new,"merge-tree --write-tree did not run:
                                               TimeoutExpired: …")]

    `behind_releases_status` read `measured` in both. A hung subprocess changed
    the release we build, and the sweep exited 0.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d) / "clones"
        clones.mkdir()
        repo, pin = _only_the_merge_test_can_see_it(clones)
        # THE CONTROL, from the same fixture in the same process: without it a
        # "0 releases behind" could equally mean the fixture never worked.
        ctl = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-02-02", False),
                                  ("v0.9", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-06"))
        assert ctl["behind_releases"] == 0 and ctl["base_release"] == "v1.0" \
            and "v1.0" in _tags_of(ctl, "contained_releases"), \
            (f"the fixture does not hold: control behind={ctl['behind_releases']} "
             f"base={ctl['base_release']} contained={_tags_of(ctl, 'contained_releases')}")
        _git_shim(monkeypatch, Path(d), "merge-tree")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-02-02", False),
                                  ("v0.9", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-06"))
    assert "v1.0" not in _tags_of(led, "new_releases"), (
        f"a release our pinned ref demonstrably contains was published as work we lack, "
        f"because the one test that could see it never ran: {led.get('new_releases')}")
    assert led["behind_releases"] is None and led["behind_releases_status"] == "unknown", (
        f"the count survived a containment nothing measured: "
        f"behind={led['behind_releases']} status={led['behind_releases_status']}")
    assert "v1.0" in _tags_of(led, "undetermined_releases"), \
        "the release neither counted nor said that it could not be decided"
    assert led["base_release"] != "v0.9", (
        "`base_release` — the release we build — moved to an older tag because a row "
        "nothing could measure dropped silently out of the election")


def test_the_release_we_build_is_not_renamed_by_a_row_nobody_could_measure(monkeypatch):
    """R5-1, the `base_release` half stated on its own, and the line it must not
    cross.

    Round 4 decided that a REFUTED row must fall back to "the newest release that
    SURVIVED the check, not to nothing" — and that is right, because a refutation
    is a measurement: an independent check of the repository established that our
    pin does not hold that release, so an older one really is the newest we hold.

    A row NOTHING COULD MEASURE gives no such licence. Both null the verdict and
    both land in `undetermined_releases`; only one of them has been measured. So
    `base_release` is withheld here and falls back there, and the two cases are
    told apart by `refuted` on the row rather than by which bucket it is in.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d) / "clones"
        clones.mkdir()
        repo, pin = _only_the_merge_test_can_see_it(clones)
        _git_shim(monkeypatch, Path(d), "merge-tree")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-02-02", False),
                                  ("v0.9", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-06"))
    assert led["base_release"] is None, (
        f"the ledger names {led['base_release']!r} as the release we build while "
        f"recording that it could not measure whether we contain v1.0")
    why = ((led.get("release_containment") or {}).get("base_withheld") or "")
    assert "v1.0" in why, (
        f"`base_release` is null with no sentence beside it — the exact value this "
        f"module exists to stop publishing: {why!r}")
    rows = {r["tag"]: r for r in (led.get("undetermined_releases") or [])}
    assert rows.get("v1.0", {}).get("refuted") is False, (
        f"the row does not say WHICH kind of undetermined it is, and the two license "
        f"different things: {rows.get('v1.0')}")


def test_a_row_nothing_could_measure_is_not_reported_as_a_refutation(monkeypatch):
    """R5-1, the DISPOSITION, decided deliberately.

    A refutation and a non-measurement are different events and get different
    dispositions. This one may NOT go to `violations`: `violations` prints under
    "BUCKET INVARIANT VIOLATED — these rows made a claim their own repository
    refutes", which would be a false sentence about a slow disk, and it would turn
    the 05:30 cron red on a transient. It may not be silent either — `violations=[]
    -> exits 0` beside "we could not measure the release we build" is the shape
    this round exists to remove. So it is a third place with a third exit status.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d) / "clones"
        clones.mkdir()
        repo, pin = _only_the_merge_test_can_see_it(clones)
        _git_shim(monkeypatch, Path(d), "merge-tree")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v1.0", "2026-02-02", False),
                                  ("v0.9", "2026-01-02", False)],
                        fork_point=(pin, "2026-01-06"))
    chk = (led.get("release_containment") or {}).get("bucket_check") or {}
    assert not chk.get("violations"), (
        f"a release nobody could measure was filed as the repository REFUTING our own "
        f"claim, which prints as an invariant violation: {chk.get('violations')}")
    assert df.release_gap_status(led) == df.UNKNOWN, \
        "…and the tool's release gap must still read `unknown`, not `measured`"


def _sweep(monkeypatch, led: dict) -> int:
    """`main()`'s exit status over ONE stubbed fork — the real function, the real
    ledger writer, no network."""
    d = Path(tempfile.mkdtemp())
    monkeypatch.setattr(df, "LEDGER", d / "ledger", raising=False)
    monkeypatch.setattr(df, "FORKS", [{"tool": TOOL, "upstream": UP, "role": "r"}],
                        raising=False)
    monkeypatch.setattr(df.gk_state, "require_writable", lambda *a, **k: None)
    monkeypatch.setattr(df, "_gh_file", lambda *a, **k: "")
    monkeypatch.setattr(df, "gh", lambda p: {"_err": "no network in this test"})
    monkeypatch.setattr(df, "discover_one", lambda fork, pins, ver: dict(led, tool=TOOL))
    return df.main()


def test_the_sweep_exit_status_tells_a_refutation_from_a_non_measurement(monkeypatch):
    """R5-1, THE DISPOSITION, on the field the cron actually reads.

    Three outcomes, three statuses. `main()` returned `len(refuted)` and
    `__main__` collapsed it with `sys.exit(1 if main() else 0)`, so a sweep that
    could not measure a single release exited 0 — indistinguishable from one that
    measured everything and found nothing wrong. That is the shape this round
    exists to remove.

    It is NOT fixed by filing the row under `violations`: that list prints as
    "BUCKET INVARIANT VIOLATED — these rows made a claim their own repository
    refutes", which is a false sentence about a slow disk, and it would turn the
    05:30 cron red on a transient. A refutation is a defect in us and stops a
    pipeline (1); a release nobody could measure is not a contradiction and gets
    its own status (2), which a caller may gate on, ignore deliberately, or page
    on — what it can no longer do is fail to notice.

    Measured on the live corpus the day this was written: 34 tools, all
    `measured`, zero undetermined rows, so a healthy sweep still exits 0.
    """
    clean = {"behind_releases": 0, "behind_releases_status": "measured",
             "undetermined_releases": [], "release_containment": {"bucket_check": {}}}
    assert _sweep(monkeypatch, clean) == 0, "a sweep with nothing wrong must exit 0"

    unmeasured = dict(clean, behind_releases=None, behind_releases_status="unknown",
                      undetermined_releases=[{"tag": "v1.0", "refuted": False,
                                              "error": "merge-tree did not run"}])
    assert _sweep(monkeypatch, unmeasured) != 0, (
        "the sweep exited 0 while its own ledger says it could not measure whether we "
        "contain v1.0 — a check that reports success for a measurement nobody made")
    assert _sweep(monkeypatch, unmeasured) == 2, (
        "…and it must not be reported as a refutation (1): nothing contradicted "
        "itself, so the daily cron would go red on a transient")

    refuted = dict(clean, behind_releases=None, behind_releases_status="unknown",
                   undetermined_releases=[{"tag": "v1.0", "refuted": True,
                                           "error": "an independent check refutes it"}],
                   release_containment={"bucket_check": {"violations": [
                       {"tag": "v1.0", "claim": "contained", "reason": "refuted"}]}})
    assert _sweep(monkeypatch, refuted) == 1, \
        "a row the repository refutes is a defect in us and must keep exiting 1"


# ── R5-2 — a producer that failed inside a pipeline ──────────────────────────

def _a_release_we_plainly_lack(clones: Path):
    """One commit ours, one commit theirs, no patch of theirs reproduced by any
    of ours — so the honest patch-equivalence answer is a REFUTATION.
    Returns (repo, pin, tag)."""
    repo = _repo(clones / TOOL)
    a = _commit(repo, "a", "a\n", "2026-01-01")
    pin = _commit(repo, "m", "ours only\n", "2026-01-05")
    _git(repo, "checkout", "-q", "-b", "rel", a)
    tag = _commit(repo, "k", "WORK NOBODY ELSE HAS\n", "2026-01-04")
    _git(repo, "tag", "v1.0", tag)
    _git(repo, "checkout", "-q", "master")
    return repo, pin, tag


def _break_one_blob(repo: Path, rev: str, path: str) -> str:
    """Delete the loose object one commit's file lives in — how clones break."""
    blob = _git(repo, "rev-parse", f"{rev}:{path}")
    victim = repo / ".git" / "objects" / blob[:2] / blob[2:]
    assert victim.is_file(), f"the fixture is wrong: {victim} is packed or absent"
    victim.unlink()
    return blob


@pytest.mark.parametrize("prover,name", [
    (lambda repo, tag, pin: df._verify_patch_equivalent(repo, tag, pin),
     "_verify_patch_equivalent"),
    (lambda repo, tag, pin: df._verify_carried_by(repo, tag, pin, "the release we build"),
     "_verify_carried_by"),
])
def test_a_re_proof_is_not_satisfied_by_a_producer_that_failed(prover, name):
    """R5-2, on the VERDICT, with NO shim and NO monkeypatch.

    One blob is deleted from a real clone. MEASURED:

        git log -p alone         -> rc=128  fatal: unable to read <blob>
        THE PIPELINE AS WRITTEN  -> rc=0    stdout=''
        _patch_id_set            -> set()
        _verify_patch_equivalent -> (True, "all 0 of its commits are patch-identical
                                            to ones we carry")
        _verify_carried_by       -> (True, …)

    `_verify_carried_by` is precisely the re-proof round 4 added to close its own
    finding that the verification layer checked what ADDS to the count and not
    what REMOVES from it. So the re-proof of a removal was satisfiable by a
    command that failed: round 2's defect (`all([])` as proof) and round 4's
    defect (a command that did not run read as a clean result) in one expression,
    inside the code written to remove both.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo, pin, tag = _a_release_we_plainly_lack(clones)
        ok, why = prover(repo, tag, pin)
        assert ok is False, f"the fixture does not hold: {name} -> {(ok, why)}"
        _break_one_blob(repo, tag, "k")
        # The producer alone says what the pipeline was hiding.
        alone = subprocess.run(["git", "-C", str(repo), "log", "-p", "--no-merges",
                                "--format=commit %H", f"{pin}..{tag}"],
                               capture_output=True, text=True)
        assert alone.returncode != 0, "the fixture did not damage the clone"
        ok, why = prover(repo, tag, pin)
    assert ok is not True, (
        f"{name} re-PROVED a claim from a `git log -p` that exited "
        f"{alone.returncode}: {why!r}")
    assert ok is None, (
        f"{name} must answer 'could not be re-proved', not a verdict in either "
        f"direction: {(ok, why)}")


def test_the_patch_id_set_of_a_broken_clone_is_not_the_empty_set():
    """R5-2 at the wrapper, and the rule this round installs stated as an
    assertion: NO RESULT and NO DATA may not be the same value.

    An empty set is a real answer — a range with nothing in it — and every caller
    reads it as one. A `git log -p` that died must not produce it.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo, pin, tag = _a_release_we_plainly_lack(clones)
        empty = df._patch_id_set(repo, f"{tag}..{tag}", 20000)
        assert empty == set(), f"an empty range stopped being an empty set: {empty!r}"
        _break_one_blob(repo, tag, "k")
        broken = df._patch_id_set(repo, f"{pin}..{tag}", 20000)
    assert broken is None, (
        f"a pipeline whose producer failed returned {broken!r}, which is the same value "
        f"a range with nothing in it returns — and `theirs - ours` is empty for both")


def test_every_undetermined_row_says_which_kind_of_undetermined_it_is(monkeypatch):
    """R5-1, the flag itself, on the two rows that are filed BEFORE any verdict
    exists — a tag that resolves to no commit, and a pin that does not either.

    `refuted` is what licenses `base_release` to step over a row. A row that omits
    it is neither: measured on the real corpus offline, `pyuvm`'s unresolvable tag
    `kaleb_decorator_fix` carried `refuted=None`, so the ledger could not say
    whether the release we build had been ruled out or merely never examined.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo = _repo(clones / TOOL)
        pin = _commit(repo, "a", "a", "2026-01-01")
        led = _discover(monkeypatch, clones, pin,
                        releases=[("v9.9", "2026-02-02", False)],   # no such tag here
                        fork_point=(pin, "2026-01-01"))
    rows = led.get("undetermined_releases") or []
    assert rows, "the fixture stopped producing an undetermined row"
    for r in rows:
        assert r.get("refuted") is False, (
            f"an undetermined row does not say which kind it is, so nothing downstream "
            f"can tell a refutation from a measurement nobody made: {r}")


#: The calls that hand a command LINE to an interpreter instead of running an
#: argv. Every one of them collapses a pipeline to a single status.
_SHELL_FUNCS = {("os", "system"), ("os", "popen"),
                ("subprocess", "getoutput"), ("subprocess", "getstatusoutput")}


def _shell_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, what) for every call in `tree` that runs text through a shell.

    PARSED, NOT GREPPED. The first version of this guard was a regex over lines
    with `#` comments stripped, and it reported six offenders in files that had
    none — every hit was the word `shell=True` inside a DOCSTRING explaining the
    defect. A checker whose own explanation trips it is a checker that will be
    switched off; and one that can be fooled by a string can be fooled into
    silence by one too.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                    and kw.value.value is True:
                out.append((node.lineno, "shell=True"))
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and (f.value.id, f.attr) in _SHELL_FUNCS:
            out.append((node.lineno, f"{f.value.id}.{f.attr}()"))
    return out


def test_no_source_file_hands_a_command_line_to_a_shell():
    """R5-2, THE GENERAL CASE. The `_patch_id_set` pipeline was found by accident,
    and the next one will not be.

    The defect is not `git patch-id`; it is that a shell reports ONE status for a
    whole pipeline and that status is the last command's. Every `shell=True` in
    this repository is therefore a place where a producer can fail invisibly,
    whether or not it holds a `|` today — `regression.json`'s `image_build.cmd`
    is shell TEXT FROM A CONFIG FILE, so the site cannot even be read to find out.

    There are now none: the four in `discover_forks.py` and this file are pipes
    between `Popen`s, and `gatekeeper._run_harness` runs `bash -o pipefail -c`
    (explicitly bash, because `/bin/sh` here is dash and `pipefail` is not POSIX).
    A new one is a deliberate act that has to edit this list, not an accident.
    """
    root = HERE.parent
    assert (root / HERE.name).is_dir(), \
        f"the scan is not rooted at the repository, so it did not scan it: {root}"
    offenders, scanned, unparsed = [], 0, []
    for py in sorted(root.rglob("*.py")):
        if ".git" in py.parts or ".bak" in py.name:
            continue
        try:
            tree = ast.parse(py.read_text(errors="replace"))
        except SyntaxError as e:
            unparsed.append(f"{py.relative_to(root)}: {e}")
            continue
        scanned += 1
        for hit in _shell_calls(tree):
            offenders.append(f"{py.relative_to(root)}:{hit[0]}: {hit[1]}")
    # A CHECKER THAT EXAMINED NOTHING RETURNS CLEAN. Both halves are asserted:
    # the walk found files, and the detector still fires on the shape it is for.
    assert scanned >= 10, f"the scan examined {scanned} file(s) — it found nothing to check"
    assert not unparsed, f"a source file could not be parsed, so it was not checked: {unparsed}"
    probe = ast.parse("import os, subprocess\n"
                      "subprocess.run('a | b', shell=True)\n"
                      "os.popen('a | b')\n")
    assert len(_shell_calls(probe)) == 2, \
        "the detector no longer recognises the shape it exists to find"
    assert offenders == [], (
        "a command line is being handed to a shell, which reports one exit status for "
        "a whole pipeline — the producer's failure is invisible there:\n  "
        + "\n  ".join(offenders))


# ── R5-3 — the independent re-prover had both folds itself ──────────────────

def test_a_killed_merge_base_does_not_refute_a_row_in_this_files_own_prover(monkeypatch):
    """R5-3a. `_our_tree_already_has_it` is the prover that caught rounds 2 and 3,
    and round 4 did not touch it.

        rc, mb, err = _g(repo, "merge-base", tag, pin)
        if rc == 1 or not mb:
            return False, "shares no ancestor with our pinned ref…"
        if rc != 0:
            return None, …                 # dead for that case

    A SIGKILLed `merge-base` has returncode **-9** and empty stdout, so `not mb`
    fires first and a killed command REFUTES a row — and False here is reported as
    a corpus violation, i.e. this file failing the real ledger over a command that
    measured nothing. Measured with a `git` on PATH:

        control  (True,  "merging it into our pinned ref changes no file")
        shim     (False, "shares no ancestor with our pinned ref and its tree is not ours")
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d) / "clones"
        clones.mkdir()
        repo, pin = _only_the_merge_test_can_see_it(clones)
        ctl = _our_tree_already_has_it(repo, "v1.0^{commit}", pin)
        assert ctl[0] is True, f"the fixture does not hold: {ctl}"
        _git_shim(monkeypatch, Path(d), "merge-base", when_not="--is-ancestor")
        raw = subprocess.run(["git", "-C", str(repo), "merge-base", "v1.0^{commit}", pin],
                             capture_output=True, text=True)
        assert raw.returncode < 0, f"the shim did not kill git: rc={raw.returncode}"
        ok, why = _our_tree_already_has_it(repo, "v1.0^{commit}", pin)
    assert ok is not False, (
        f"a `git merge-base` killed by signal {-raw.returncode} REFUTED a release our "
        f"pinned ref genuinely contains, saying: {why!r}")
    assert ok is None, f"…and the honest disposition is 'could not be re-proved': {(ok, why)}"


def test_a_broken_pipeline_does_not_re_prove_a_row_in_this_files_own_prover():
    """R5-3b. `_patch_ids` was the same unguarded pipeline, so the flip is in the
    OTHER direction — a killed command RE-PROVING the claim:

        healthy  (False, "1 of its 1 commit(s) match nothing we carry")
        damaged  (True,  "all 0 of its commits are patch-identical to ours")

    Measured on a real clone with one blob deleted, no shim.
    """
    with tempfile.TemporaryDirectory() as d:
        clones = Path(d)
        repo, pin, tag = _a_release_we_plainly_lack(clones)
        ctl = _we_carry_every_commit_of_it(repo, "v1.0^{commit}", pin)
        assert ctl[0] is False, f"the fixture does not hold: {ctl}"
        _break_one_blob(repo, tag, "k")
        ok, why = _we_carry_every_commit_of_it(repo, "v1.0^{commit}", pin)
    assert ok is not True, (
        f"the independent prover RE-PROVED a claim it refutes on a healthy clone, from "
        f"a `git log -p` that could not read a blob: {why!r}")
    assert ok is None, f"…and the honest disposition is 'could not be re-proved': {(ok, why)}"


def test_a_git_that_could_not_be_started_is_not_a_verdict_in_this_files_own_prover(monkeypatch):
    """R5-3, the wrapper. `_g` let an exception escape and returned `r.returncode`
    untouched, so every `rc == 1` and `rc != 0` written against it covered more
    events than its author meant. A `git` that cannot be executed at all must
    reach the callers as "no result", not as an exception and not as a code.
    """
    with tempfile.TemporaryDirectory() as d:
        shim = Path(d) / "bin"
        shim.mkdir()
        (shim / "git").write_text("#!/bin/sh\nexit 0\n")     # never made executable
        # PATH is REPLACED, not prepended to: `execvp` treats EACCES as "keep
        # looking" and would find the real git further down.
        monkeypatch.setenv("PATH", str(shim))
        try:
            rc, out, err = _g(Path(d), "merge-base", "--is-ancestor", "a", "b")
        except Exception as e:                               # noqa: BLE001
            raise AssertionError(
                f"`_g` let {e.__class__.__name__} escape, so a git that could not be "
                f"started reaches its callers as a crash rather than as 'no result'"
            ) from None
    assert rc is None, (
        f"a git that could not be executed came back as rc={rc!r}, and every caller here "
        f"is written as `rc == 1` / `rc != 0`: {err[:120]}")
    assert not _said(rc, 1) and not _said(rc, 0) and not _ran(rc), \
        "…and none of the three questions a caller may ask says yes to it"
