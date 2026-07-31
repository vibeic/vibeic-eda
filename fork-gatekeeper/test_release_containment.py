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

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import discover_forks as df           # noqa: E402
import build_page as bp               # noqa: E402
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
            return subprocess.run(f"git -C {repo} show {sha} | git patch-id --stable",
                                  shell=True, capture_output=True,
                                  text=True).stdout.split()[0]

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
            return subprocess.run(f"git -C {repo} show {sha} | git patch-id --stable",
                                  shell=True, capture_output=True,
                                  text=True).stdout.split()[0]

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


def _g(repo, *args, timeout=600):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, timeout=timeout)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def _our_tree_already_has_it(repo, tag_rev: str, pin: str):
    """(True / False / None, why) — recomputed from git, believing nothing.

    The four proofs that make `contained` a true sentence, and no others:
    ancestry; the release's tree IS our tree; the release's tree is the tree of
    the merge-base, so it adds nothing to the line we share; the three-way merge
    of it into our pin produces the tree we already build.
    """
    rc, _, err = _g(repo, "merge-base", "--is-ancestor", tag_rev, pin)
    if rc == 0:
        return True, "ancestor of our pinned ref"
    if rc != 1:
        return None, f"merge-base --is-ancestor failed: {err[:100]}"
    rc, tt, _ = _g(repo, "rev-parse", f"{tag_rev}^{{tree}}")
    rc2, pt, _ = _g(repo, "rev-parse", f"{pin}^{{tree}}")
    if rc != 0 or rc2 != 0:
        return None, "could not read the trees"
    if tt == pt:
        return True, "identical tree to our pinned ref"
    rc, mb, err = _g(repo, "merge-base", tag_rev, pin)
    if rc == 1 or not mb:
        return False, "shares no ancestor with our pinned ref and its tree is not ours"
    if rc != 0:
        return None, f"merge-base failed: {err[:100]}"
    rc, mt, _ = _g(repo, "rev-parse", f"{mb}^{{tree}}")
    if rc == 0 and mt == tt:
        return True, "changes no file relative to the merge-base with our pinned ref"
    r = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree",
                        f"--merge-base={mb}", pin, tag_rev],
                       capture_output=True, text=True, timeout=600)
    first = ((r.stdout or "").splitlines() or [""])[0].strip()
    if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", first):
        if first == pt:
            return True, "merging it into our pinned ref changes no file"
        return False, "merging it into our pinned ref changes files we do not have"
    if r.returncode == 1:
        conflicted = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("CONFLICT")]
        return False, ("merging it into our pinned ref CONFLICTS: "
                       + (conflicted[0][:140] if conflicted else "unresolved"))
    return None, "git merge-tree --write-tree is unavailable (git < 2.38)"


def _patch_ids(repo, rng: str):
    """{patch-id} for the non-merge commits in `rng`, via `git patch-id --stable`
    — the same normalisation `git cherry` uses internally, computed here without
    going through `git cherry` at all."""
    r = subprocess.run(
        f"git -C {repo} log -p --no-merges --format='commit %H' {rng} "
        f"| git patch-id --stable", shell=True, capture_output=True, text=True,
        timeout=900)
    if r.returncode != 0:
        return None
    return {ln.split()[0] for ln in (r.stdout or "").splitlines() if ln.strip()}


def _we_carry_every_commit_of_it(repo, tag_rev: str, pin: str):
    """(True / False / None, why) for the patch-equivalence claim, recomputed."""
    rc, n_all, _ = _g(repo, "rev-list", "--count", f"{pin}..{tag_rev}")
    rc2, n_nm, _ = _g(repo, "rev-list", "--no-merges", "--count", f"{pin}..{tag_rev}")
    if rc != 0 or rc2 != 0 or not n_all.isdigit() or not n_nm.isdigit():
        return None, "could not size the range"
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
    assert not violations, (
        f"{len(violations)} row(s) in {led_dir} are filed under `contained_releases` "
        f"while an independent check of the repository says our pinned ref neither "
        f"contains them nor merges them as a no-op:\n  " + "\n  ".join(violations))
    assert with_bucket, (
        f"no ledger in {led_dir} carries a `contained_releases` key — this corpus "
        f"predates the bucket, so nothing was checked")
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
    assert present, (
        f"no ledger in {led_dir} carries a `patch_equivalent_releases` key — the "
        f"corpus was not produced by the code under test, so nothing was checked")


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
