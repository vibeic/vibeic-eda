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
    assert "v3.0.0-rc1" in _tags_of(led, "contained_releases")


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

_JS = re.compile(r"const pill = .*?const relPill = d => .*?;\n", re.S)


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
