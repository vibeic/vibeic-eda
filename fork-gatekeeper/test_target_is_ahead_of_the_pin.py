#!/usr/bin/env python3
"""The proposed target has to be somewhere we can GO — measured against the LIVE pin.

THE DEFECT, measured 2026-08-06 on Trilinos and published three days running:

    Trilinos | DEFERRED | 3 | trilinos-release-17-1-1 | 250 upstream commit(s)
    trilinos-release-16-2-1 → trilinos-release-17-1-1 …

    our TRILINOS_REF   381bf0316b98 / 5edda67161cc   (the branch in the image)
    target tag         8e7286cc842c                  (trilinos-release-17-1-1)
    ours ahead of target by   407
    target ahead of ours by    12

The row proposed advancing onto a tag four hundred commits BEHIND the ref the image
builds, and named a base (`trilinos-release-16-2-1`) that is the retained FALLBACK pin
— `TRILINOS_REF` moved to the 17.2 branch on 2026-08-01. Nothing in the pipeline
compared the candidate against the pin: `behind_releases` orders releases by where
their line left the upstream TRUNK, anchored at the newest release our pin contains,
and by that rule a maintenance tag carrying 12 branch-only commits legitimately counts
as work upstream has and we do not. That is a true answer to a different question.
"Can we advance onto it" is this one, and six forks were being told yes to it
(Trilinos, cocotb, gtkwave, iverilog, slang, yices2).

WHAT IS PINNED HERE — every assertion drives `assess_release.assess()` over a REAL
git repository built in tmp_path, and reads the report and the rendered daily-report
row. Nothing asserts on a symbol this change introduced, so each one is a claim about
behaviour that a pristine tree can be run against and seen to FAIL:

  * BEHIND + upstream moved  → the tag is refused and the FORWARD range (our pin →
    upstream's default branch) is assessed instead;
  * BEHIND + nowhere forward → its own state, which says the pin is ahead of the
    newest tagged release rather than proposing a downgrade;
  * FORWARD through the fork point → still accepted. This is the shape a rule of
    "the target must be a descendant of the pin" would wrongly refuse: netgen's
    `1.5.323` is 15 of OUR commits behind the pin and 223 upstream commits ahead of
    it, and it is a real target because a merge re-applies our own patches;
  * not measurable → its own third state, disclosed in the published row, never
    rendered as a measured forward.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _git(repo: Path, *a: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,
                       check=True)
    return p.stdout.strip()


def _commit(repo: Path, msg: str) -> str:
    (repo / "f.txt").write_text(msg)
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def clone(tmp_path: Path) -> dict:
    """One repository carrying every shape the predicate has to separate.

        base ─ m1 ─ m2 ─ m3 ─ m4 ─ m5              (master; `trunk-v3.0` tags m4)
                          │         │
                          │         └─ q1          (`ours5`  — pin, fork point m5)
                          └─ p1                    (`ours3`  — pin, fork point m3)
          └─ r1 ─ r2                               (`old-line-v2.0` — off base)

    `old-line-v2.0` is the Trilinos shape: two commits neither pin has, cut from a
    point five trunk commits behind both of them. `trunk-v3.0` is netgen's: ahead of
    the fork point of `ours3`, behind the pin only by our own patch.
    """
    root = tmp_path / "clones"
    repo = root / "toolx"
    repo.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "master", str(repo))
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")

    base = _commit(repo, "base")
    trunk = [_commit(repo, f"m{i}") for i in range(1, 6)]
    _git(repo, "tag", "trunk-v3.0", trunk[3])

    _git(repo, "checkout", "-q", "-b", "rel", base)
    _commit(repo, "r1")
    _commit(repo, "r2")
    _git(repo, "tag", "old-line-v2.0")

    _git(repo, "checkout", "-q", "-b", "ours3", trunk[2])
    pin3 = _commit(repo, "p1 — our carried patch")
    _git(repo, "checkout", "-q", "-b", "ours5", trunk[4])
    pin5 = _commit(repo, "q1 — our carried patch")
    _git(repo, "checkout", "-q", "master")

    return {"root": root, "repo": repo, "base": base, "trunk": trunk,
            "pin3": pin3, "pin5": pin5}


def _wire(state: Path, forks: Path, led: dict):
    """assess() with its network layers stubbed and its git layer REAL.

    Only the four layers that would spend a GitHub call or an LLM call are replaced.
    The ancestry this test is about is computed by git against the fixture clone —
    stubbing that would make the whole file a test of its own mock.
    """
    os.environ["GK_STATE_DIR"] = str(state)
    os.environ["GK_FORKS_DIR"] = str(forks)
    os.environ["GK_ASSESS_NOCACHE"] = "1"
    import assess_release as A
    importlib.reload(A)
    (state / "ledger").mkdir(parents=True, exist_ok=True)
    (state / "ledger" / f"{led['tool']}.json").write_text(json.dumps(led))
    A.upstream_commits = lambda *a: ([{"sha": "cc4da9a05fde", "sha_full": "c" * 40,
                                       "title": "fix a thing", "body": "", "url": "",
                                       "author": "x"}], ["ext.c"])
    A.our_patch_files = lambda *a: set()
    A._commit_files = lambda *a: {"ext.c"}
    A.clean_cherrypick = lambda *a: True
    A.classify_commits = lambda tool, role, commits: {
        c["sha"]: {"category": "bugfix", "relevant": True, "risk": "low",
                   "summary": "s", "reproduce": "", "recommend": "adopt"}
        for c in commits}
    A._confirm_candidates = lambda tool, role, cands, cls_map: {
        c["sha"]: {"agree": True, "complete": True, "detail": "stubbed: agreed"}
        for c in cands}
    return A


def _ledger(pin: str, fork_point: str, latest: str, *, behind_commits: int,
            base_release: str = "old-base") -> dict:
    return {"tool": "toolx", "integrated": True, "behind_releases": 1,
            "behind_releases_status": "measured", "behind_commits": behind_commits,
            "upstream": "up/toolx", "upstream_default_branch": "master",
            "pinned_ref_full": pin, "fork_point": {"sha": fork_point},
            "base_release": base_release, "upstream_latest_release": latest,
            "role": "a build dependency"}


def _teardown():
    for k in ("GK_STATE_DIR", "GK_FORKS_DIR", "GK_ASSESS_NOCACHE"):
        os.environ.pop(k, None)


def _row(A, rep: dict, latest: str = "old-line-v2.0") -> str:
    """The daily report's sentence for this assessment — what actually got published.

    `latest` is what `tick()` hands in: the LEDGER's newest tag, which is exactly the
    value the defective row printed as the right-hand end of a range it did not assess.
    """
    import gatekeeper as gk
    importlib.reload(gk)
    return gk.assessment_entry(rep, 1, latest)["note"]


# ── the reproduction ─────────────────────────────────────────────────────────
def test_a_target_behind_the_pin_is_not_proposed(clone, tmp_path):
    """THE RED PROOF. `old-line-v2.0` is 2 commits ahead of our pin and 6 behind it;
    advancing to it deletes five trunk commits the image already builds. Upstream's
    default branch HAS moved past the pin, so there is a forward range and it is the
    one that must be assessed."""
    A = _wire(tmp_path / "state", clone["root"],
              _ledger(clone["pin5"], clone["trunk"][4], "old-line-v2.0",
                      behind_commits=3))
    try:
        rep = A.assess("toolx")
        assert not rep.get("error"), rep
        assert rep["latest"] != "old-line-v2.0", (
            "the assessment proposed a target the ref we ship is 6 commits AHEAD of "
            f"— adopting it is a downgrade: {rep['latest']}")
        assert rep["latest"] == "master", rep["latest"]
        assert rep["base_release"] == clone["pin5"], (
            "the range must start at the ref we actually ship, not at a retained "
            f"fallback tag: {rep['base_release']}")
        note = _row(A, rep)
        assert "old-line-v2.0" in note and "TARGET REFUSED" in note, note
    finally:
        _teardown()


def test_pin_ahead_with_nowhere_forward_is_its_own_state(clone, tmp_path):
    """Same refusal, no forward range to fall back on: upstream's default branch is
    not ahead of our pin either. The honest row says the pin is ahead of the newest
    tagged release. It must NOT quietly assess the tag anyway."""
    A = _wire(tmp_path / "state", clone["root"],
              _ledger(clone["pin5"], clone["trunk"][4], "old-line-v2.0",
                      behind_commits=0))
    try:
        rep = A.assess("toolx")
        assert rep.get("status") == "pin_ahead_of_release", (
            "a fork with no forward target published an ordinary assessment of a "
            f"backwards one: status={rep.get('status')} latest={rep.get('latest')}")
        assert rep.get("commits") == [], rep.get("commits")
        note = _row(A, rep)
        for want in ("NOT a descendant", "DOWNGRADE", "cherry-pick"):
            assert want in note, f"{want!r} missing from the published row: {note}"
        md = A.render_md(rep)
        assert "PIN IS AHEAD OF THE NEWEST TAGGED RELEASE" in md, md
        # …and the stub states no headline counts, so the cross-document guard has
        # nothing to compare and must not invent a disagreement.
        assert A.cross_check(rep, {"assessment": md, "report": note}) == [], rep
    finally:
        _teardown()


def test_an_unrecorded_commit_gap_is_not_reported_as_a_measured_zero(clone, tmp_path):
    """The refused-target row's second clause is a claim about the DEFAULT BRANCH: "no
    forward range exists either". It is only a measurement when the ledger carries a
    `behind_commits` number. A ledger that carries none must not have `or 0` decide it
    — that substitution is the one the release-gap work spent a whole round removing,
    and it would put a confident sentence about upstream on a row nothing measured."""
    led = _ledger(clone["pin5"], clone["trunk"][4], "old-line-v2.0", behind_commits=0)
    led.pop("behind_commits")
    A = _wire(tmp_path / "state", clone["root"], led)
    try:
        rep = A.assess("toolx")
        assert rep.get("status") == "pin_ahead_of_release", rep.get("status")
        note = _row(A, rep)
        assert "NOT recorded" in note, note
        assert "has not moved past our pin" not in note, (
            "an unrecorded commit gap was published as a measured zero: " + note)
    finally:
        _teardown()


# ── the other half: a legitimate forward target still lands ──────────────────
def test_a_forward_target_is_still_accepted(clone, tmp_path):
    """`trunk-v3.0` sits at m4. Our pin is m3 + one carried patch, so the tag is NOT a
    descendant of the pin — and it IS a target: the only commit it lacks is ours, and
    a merge re-applies it. This is netgen `1.5.323` (15 of ours ahead, 223 upstream
    commits to take). A check that demanded pin-descendancy would refuse it, and this
    assertion is what stops the refusal being written that way."""
    A = _wire(tmp_path / "state", clone["root"],
              _ledger(clone["pin3"], clone["trunk"][2], "trunk-v3.0",
                      behind_commits=3))
    try:
        rep = A.assess("toolx")
        assert rep.get("status") == "assessed", rep.get("status")
        assert rep["latest"] == "trunk-v3.0", (
            "a legitimate forward release was refused as if it were behind us: "
            f"{rep['latest']}")
        assert rep["base_release"] == "old-base", rep["base_release"]
        note = _row(A, rep)
        assert "TARGET REFUSED" not in note and "UNMEASURED" not in note, note
    finally:
        _teardown()


def test_a_target_the_pin_is_an_ancestor_of_is_accepted(clone, tmp_path):
    """The plain case, for completeness: pin ON the trunk, tag further along it."""
    A = _wire(tmp_path / "state", clone["root"],
              _ledger(clone["trunk"][2], clone["trunk"][2], "trunk-v3.0",
                      behind_commits=3))
    try:
        rep = A.assess("toolx")
        assert rep["latest"] == "trunk-v3.0", rep["latest"]
        assert "TARGET REFUSED" not in _row(A, rep)
    finally:
        _teardown()


# ── the third state ──────────────────────────────────────────────────────────
def test_unmeasurable_direction_is_disclosed_not_read_as_forward(clone, tmp_path):
    """COULD-NOT-DETERMINE IS NOT A PASS. With no clone to ask, nothing establishes
    which way the tag lies. The range is still assessed — refusing a whole fork over a
    missing clone loses more than it protects — but the published row has to SAY the
    direction was not measured, or an unchecked target is indistinguishable from a
    checked one."""
    A = _wire(tmp_path / "state", tmp_path / "no-clones-here",
              _ledger(clone["pin5"], clone["trunk"][4], "old-line-v2.0",
                      behind_commits=3))
    try:
        rep = A.assess("toolx")
        note = _row(A, rep)
        assert "DIRECTION UNMEASURED" in note, (
            "an unmeasured target direction was published exactly like a measured "
            f"forward one: {note}")
        assert "old-line-v2.0" in note, note
    finally:
        _teardown()


def test_the_three_states_produce_three_different_rows(clone, tmp_path):
    """The states are only useful if a reader can tell them apart. Three runs, three
    distinct published sentences — and the unmeasured one is not the forward one."""
    rows = {}
    for name, forks, led in (
            ("forward", clone["root"],
             _ledger(clone["pin3"], clone["trunk"][2], "trunk-v3.0", behind_commits=3)),
            ("behind", clone["root"],
             _ledger(clone["pin5"], clone["trunk"][4], "old-line-v2.0", behind_commits=3)),
            ("undetermined", tmp_path / "nope",
             _ledger(clone["pin5"], clone["trunk"][4], "old-line-v2.0", behind_commits=3))):
        A = _wire(tmp_path / f"state-{name}", forks, led)
        try:
            rows[name] = _row(A, A.assess("toolx"))
        finally:
            _teardown()
    assert len({*rows.values()}) == 3, rows
    assert rows["undetermined"] != rows["forward"], rows
    assert "TARGET REFUSED" in rows["behind"], rows["behind"]
    assert "UNMEASURED" in rows["undetermined"], rows["undetermined"]
    assert "TARGET REFUSED" not in rows["forward"] and \
           "UNMEASURED" not in rows["forward"], rows["forward"]


# ── the corpus this was measured on ──────────────────────────────────────────
def test_the_real_trilinos_row_no_longer_names_the_backwards_tag(tmp_path):
    """THE INCIDENT ITSELF, replayed against the live ledger and the live clone.

    Network-free: the ledger is copied into a scratch state dir and the four layers
    that would spend a GitHub or LLM call are stubbed, exactly as above. The ancestry
    is the real repository's.

    SKIPPED — not passed — when the clone or the ledger is absent. A check that
    reports success because its subject was missing is the failure mode this repo
    keeps hitting, and this one has a fixture-built twin above that always runs.
    """
    live = Path(os.environ.get("GK_FORKS_DIR_LIVE")
                or os.path.expanduser("~/vibe-ic-forks"))
    led_p = (Path(os.environ.get("GK_STATE_DIR_LIVE")
                  or os.path.expanduser("~/.cache/eda-fork-gatekeeper"))
             / "ledger" / "Trilinos.json")
    if not (live / "Trilinos" / ".git").is_dir() or not led_p.is_file():
        pytest.skip("no live Trilinos clone/ledger on this host")
    led = json.loads(led_p.read_text())
    if not (led.get("pinned_ref_full") and led.get("upstream_latest_release")):
        pytest.skip("the live Trilinos ledger carries no pin/latest to compare")
    tag = led["upstream_latest_release"]

    A = _wire(tmp_path / "state", live, led)
    # `already_carried` is network-free but patch-ids 400 real commits; it has nothing
    # to do with which range was chosen, and this test is about the range.
    A.already_carried = lambda *a: set()
    try:
        rep = A.assess("Trilinos")
        assert not rep.get("error"), rep
        # ASSERT THE VERDICT, NOT THE FACT.
        #
        # `latest` answers "what is upstream's newest tag", and
        # `trilinos-release-17-1-1` IS the newest tag -- a correct measurement.
        # Requiring the row to FORGET it would delete a true fact in order to
        # express a decision, and the next reader would have no way to see what
        # was refused. The behaviour that matters is that the report does not
        # PROPOSE it as somewhere to advance to, and says why.
        #
        # The earlier form of this test asserted `latest != tag`, went red the
        # moment the refusal was implemented correctly, and would have stayed red
        # forever -- a test demanding that a measurement be wrong.
        # ASSERT THE PROPERTY, NOT ONE SPELLING OF IT. The refusal is worded
        # differently on the assessed path ("is NOT a descendant of the ref we
        # ship ... NOTHING is proposed") than on the fallback ("TARGET REFUSED"),
        # and pinning one string made this red while the behaviour was correct.
        # What must hold is that the row does not offer the tag as a target and
        # says why -- either wording satisfies that.
        note = _row(A, rep, tag)
        _refused = ("TARGET REFUSED" in note
                    or "NOT a descendant" in note
                    or "NOTHING is proposed" in note)
        assert _refused, (
            f"the row proposes {tag} without refusing it. Our ref is 407 commits "
            f"ahead of that tag and it is not on the branch we track, so adopting "
            f"it would drop work we build.\n{note}")
        assert tag in note, (
            f"the refusal does not NAME the tag it refused, so a reader cannot "
            f"tell what was rejected.\n{note}")
    finally:
        _teardown()
