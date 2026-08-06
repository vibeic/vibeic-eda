#!/usr/bin/env python3
"""`published_tip` took the ledger's branch on EXISTENCE, and a stale ref answers cleanly.

    cands = []
    if led.get("vibeic_branch"):
        cands.append("origin/%s" % led["vibeic_branch"])
    cands += ["origin/master", "origin/main"]
    for c in cands:
        if _git(clone, "rev-parse", "--verify", "-q", c):
            return c              # the first one that RESOLVES. Not the current one.

The ledger's `vibeic_branch` outranks `origin/master`, so a recorded branch that
stopped tracking the default resolves perfectly well and `sync_lag`,
`release_lag` and `ours_past_the_pin` are all counted from it. A stale tip counts
FEWER of our commits past the pin than there are — so the report reads GREEN
exactly when work is stranded, which is the one direction an error must never go.

The irony worth keeping: `published_tip`'s own docstring exists to REFUSE the
clone's HEAD, because these clones are shared. It closed the HEAD route and left
the ledger route open.  (vibeic-eda#92)

WHY THESE FAIL BEHAVIOURALLY AGAINST THE OLD TREE
=================================================
`test_a_stale_ledger_branch_cannot_hide_our_unshipped_commits` and
`test_a_publishing_branch_ahead_of_the_default_is_still_measured_from` drive
`analyse()` — which exists identically on both trees — over REAL git repositories
and assert on the REPORTED COUNT of our unshipped commits. Neither names
`published_tip`, `TipVerdict`, `default_ref` or any other new symbol. Dropped on
the pristine tree the first reads 0 where 2 commits of ours sit past the pin;
that is the defect, and it is an inverted NUMBER, not an absent attribute.

BIDIRECTIONAL, and the control is the one to read first. A guard that rejected
every ledger branch would satisfy the red test completely — falling through to
`origin/master` is what the red test asserts. `..._ahead_of_the_default_is_still_
measured_from` is what stops that: a publishing branch legitimately AHEAD of the
default must still be measured FROM, and a reject-everything guard undercounts it
by exactly the commit that only that branch carries.

THE DIRECTION OF THE PREDICATE IS ITSELF A CLAIM, so `test_the_predicate_is_not_
inverted` pins it. #92 asks to reject a branch that is "not ancestor-or-equal of
origin/master", and measured against the branch that prompted the issue that rule
is backwards — 18 commits behind master IS a strict ancestor of master. That test
builds the exact shape and would pass the inverted rule only by accepting a
2-behind branch, which is the bug.

HONESTLY LABELLED: `test_a_resolving_branch_with_no_default_to_check_it_against_
is_not_measured` and `test_the_three_states_are_distinct_names` call
`published_tip` DIRECTLY and assert on the new verdict type. They are unit tests
of the third state's contract — NOT evidence that the defect exists, and they are
not offered as red proof. The old tree fails them on the return type, which
proves only that a symbol changed.
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
        "fgr_currency_under_test", HERE / "fork_gap_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fgr_currency_under_test"] = mod
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
    """A REAL upstream, a REAL published fork remote, and a REAL clone of it.

    THREE repositories, not two, and the third is the point: our fork has its own
    publishing remote, distinct from upstream, and `origin/*` in the clone are
    refs on THAT remote. A fixture that made `origin` an alias of upstream could
    not hold a branch that is behind our own default, which is the whole subject.

    Topology after setup — `base` is upstream's only commit, `o1`/`o2` are ours:

        upstream/master              base
        origin/master                base - o1 - o2      (our published default)
        origin/vibeic/stale          base                (2 behind: the #92 shape)
        origin/vibeic/level          base - o1 - o2      (EQUAL: the Trilinos shape)
        origin/vibeic/pub            base - o1 - o2 - o3 (AHEAD: a real pub branch)
        ARG TOOLX_REF                base                (the image's pin)

    Nothing is stubbed: `analyse()` runs its own ls-tree, show, rev-parse,
    rev-list, merge-base and fetch against these.
    """
    up = tmp_path / "upstream"; up.mkdir()
    _git(up, "init", "-q", "-b", "master")
    base = _commit(up, "a.txt", "base")

    ours = tmp_path / "ourfork.git"
    _git(tmp_path, "clone", "-q", "--bare", str(up), str(ours))

    fork = tmp_path / "forks" / "toolx"; fork.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(ours), str(fork))
    _git(fork, "remote", "add", "upstream", str(up))
    _git(fork, "fetch", "-q", "upstream")

    o1 = _commit(fork, "b.txt", "ours 1")
    o2 = _commit(fork, "c.txt", "ours 2")
    _git(fork, "push", "-q", "origin", "master")
    _git(fork, "push", "-q", "origin", f"{base}:refs/heads/vibeic/stale")
    _git(fork, "push", "-q", "origin", f"{o2}:refs/heads/vibeic/level")
    o3 = _commit(fork, "d.txt", "ours 3")
    _git(fork, "push", "-q", "origin", f"{o3}:refs/heads/vibeic/pub")
    _git(fork, "reset", "-q", "--hard", o2)
    _git(fork, "fetch", "-q", "origin")

    repo = tmp_path / "eda"; repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "Dockerfile").write_text(f"FROM x\nARG TOOLX_REF={base}\n", encoding="utf-8")
    _git(repo, "add", "Dockerfile")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pin")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    return {"up": up, "ours": ours, "fork": fork, "repo": repo,
            "forks_root": fork.parent, "ledger": tmp_path / "ledger",
            "base": base, "o1": o1, "o2": o2, "o3": o3}


def _set_pin(fleet, sha):
    """Rewrite the DOCKERFILE ARG (vibeic-eda#102), not the ledger field.

    `analyse()`'s `row["pin"]` comes from `pins_from_dockerfiles(repo)` FIRST —
    the ledger's `pinned_ref_full` is only a fallback used when no ARG resolves.
    `pins_from_dockerfiles` reads the COMMITTED tree at `origin/main`, not the
    working copy, so the pin has to be committed and `origin/main` re-pointed,
    exactly like `_ledger`'s own setup does once at fixture build time.
    """
    repo = fleet["repo"]
    (repo / "Dockerfile").write_text(f"FROM x\nARG TOOLX_REF={sha}\n", encoding="utf-8")
    _git(repo, "add", "Dockerfile")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "repin")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def _ledger(fleet, branch, pin=None):
    """Write the ledger this fork would have, recording `branch` (or none).

    `pin` DEFAULTS TO `base` — unchanged, so every test that does not name a
    pin keeps its original numbers. Three tests now pass `pin=fleet["o1"]`
    (vibeic-eda#102): `published_tip` accepts a candidate branch when it
    contains the PIN, not the default branch, so a pin fixed at `base` sits at
    the exact same commit as `vibeic/stale` itself — "contains the pin" is
    trivially true for a branch that has not regressed from anything we have
    ACTUALLY shipped, which is a different (also correct) question from the one
    those three tests model: `master` carries `o1`/`o2` and `vibeic/stale`
    lacks them. `o1` is a real point on the line we ship from, which `stale`
    genuinely lacks and `master`/`level`/`pub` genuinely have.
    """
    led = fleet["ledger"]
    led.mkdir(exist_ok=True)
    body = {"tool": "toolx", "integrated": True, "upstream_default_branch": "master",
            "pinned_ref_full": pin or fleet["base"], "pin_kind": "pin", "ahead": 2}
    if branch is not None:
        body["vibeic_branch"] = branch
    (led / "toolx.json").write_text(json.dumps(body), encoding="utf-8")
    return led


def _run(fleet, branch, fetch=False, pin=None):
    m = _load()
    rep = m.analyse(fleet["repo"], fleet["forks_root"], _ledger(fleet, branch, pin), fetch)
    return rep, next(r for r in rep["rows"] if r["tool"] == "toolx")


# ── RED PROOF: the defect, as an inverted number ─────────────────────────────
def test_a_stale_ledger_branch_cannot_hide_our_unshipped_commits(fleet):
    """The ledger names a branch 2 commits behind our default. Two of our commits
    sit past the image's pin.

    Old tree: `origin/vibeic/stale` resolves, wins over `origin/master`, and
    `pin..stale` is EMPTY — the report publishes `0 substantive` and rc=0. That
    zero is the bug, and it is the direction that reads as health.
    """
    # pin=o1 (vibeic-eda#102): `published_tip` now accepts a candidate branch
    # that contains the PIN, not the default. `vibeic/stale` sits at `base`; if
    # the pin ALSO sat at `base` (as this fixture used to fix it, for every
    # scenario in this file) "contains the pin" is trivially true and the
    # rejection this test is about would never fire. `o1` is a real point on
    # the line we ship from that `stale` genuinely lacks.
    _set_pin(fleet, fleet["o1"])
    rep, row = _run(fleet, "vibeic/stale", pin=fleet["o1"])
    assert row["note"] is None, row["note"]
    assert rep["q2_ours_past_the_pin_substantive"] == 1, (
        "a stale ledger branch was measured from: our 1 unshipped commit "
        f"(o2, past the o1 pin) reported as "
        f"{rep['q2_ours_past_the_pin_substantive']}")
    assert row["release_lag"] == 1, row["release_lag"]
    assert row["tip"].endswith("master"), row["tip"]


def test_the_rejection_is_stated_and_holds_the_run(fleet, capsys):
    """Rejecting silently is how this became invisible. It must SAY so, and the
    round must not come back green while what we publish is an open question."""
    m = _load()
    _set_pin(fleet, fleet["o1"])
    rc = m.main(["--repo", str(fleet["repo"]),
                 "--forks-root", str(fleet["forks_root"]),
                 "--ledger", str(_ledger(fleet, "vibeic/stale", pin=fleet["o1"])),
                 "--no-fetch"])
    out = capsys.readouterr().out
    assert "LEDGER BRANCH REJECTED" in out and "toolx" in out, out
    assert "vibeic/stale" in out, out
    assert rc == 2, f"rc={rc}\n{out}"


# ── the control that must stay green — a reject-everything guard fails here ───
def test_a_publishing_branch_ahead_of_the_default_is_still_measured_from(fleet):
    """`vibeic/pub` carries one commit `origin/master` does not — the ONLY reason
    to keep a separate publishing branch.

    A guard that rejected every ledger branch would pass the red test above (it
    asserts the fallthrough to master) and quietly undercount here by exactly the
    commit only this branch has. 3, not 2.
    """
    rep, row = _run(fleet, "vibeic/pub")
    assert row["note"] is None, row["note"]
    assert row["tip"] == "origin/vibeic/pub", row["tip"]
    assert rep["q2_ours_past_the_pin_substantive"] == 3, (
        "a current publishing branch was rejected and its commit went uncounted: "
        f"{rep['q2_ours_past_the_pin_substantive']}")
    # `.get` DELIBERATELY: a control has to be satisfiable by the OLD tree too, or
    # it is just another red test wearing a control's name. On the pristine tree
    # this key does not exist and everything above still holds — which is the
    # point, because the old tree accepted this branch and was RIGHT to.
    assert rep.get("tip_rejected", []) == [], rep.get("tip_rejected")


def test_a_level_branch_is_accepted_the_trilinos_shape(fleet):
    """`Trilinos`'s `vibeic/xyce-trilinos-17.2-epetra-restored` is EQUAL to its
    default (measured 2026-08-05, both at 5edda67161cc). Equal is current, and a
    predicate written with a strict `..` would break the one live fork that
    actually uses this field."""
    rep, row = _run(fleet, "vibeic/level")
    assert row["tip"] == "origin/vibeic/level", row["tip"]
    assert rep["q2_ours_past_the_pin_substantive"] == 2
    assert rep.get("tip_rejected", []) == [] and rep.get("tip_unverified", []) == []


def test_no_ledger_branch_at_all_still_measures_from_the_default(fleet):
    """Six of the live ledgers record no `vibeic_branch`. They must be unaffected."""
    rep, row = _run(fleet, None)
    assert row["tip"] == "origin/master", row["tip"]
    assert rep["q2_ours_past_the_pin_substantive"] == 2
    assert rep.get("tip_rejected", []) == [] and rep.get("tip_unverified", []) == []


# ── the direction of the predicate, which is itself a claim ──────────────────
def test_the_predicate_is_not_inverted(fleet):
    """#92 asks to reject a branch that is "not ancestor-or-equal of origin/master".
    Measured on `vibeic/rcx-515-collision-with-upstream`, the branch that prompted
    the issue: 18 behind master, and `merge-base --is-ancestor rcx master` -> rc 0.
    A branch that has fallen behind IS an ancestor, so the literal rule accepts
    exactly the case it was written to catch.

    `vibeic/stale` here is that shape — a strict ancestor of the default. It must
    be REJECTED, and `vibeic/pub` (a strict descendant, which the literal rule
    would reject) must be ACCEPTED.
    """
    assert _git(fleet["fork"], "merge-base", "--is-ancestor",
                "origin/vibeic/stale", "origin/master", check=False) == ""
    r = subprocess.run(["git", "-C", str(fleet["fork"]), "merge-base",
                        "--is-ancestor", "origin/vibeic/stale", "origin/master"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "fixture is not the ancestor shape #92 describes"

    # Asserted on `tip`, which BOTH trees publish, so this is a claim about which
    # ref the numbers came from and not about a new field. The inverted rule
    # leaves `origin/vibeic/stale` here (as the pristine tree does) and would
    # replace `origin/vibeic/pub` with `origin/master`.
    # pin=o1 for `stale` (vibeic-eda#102) — see the comment in
    # `test_a_stale_ledger_branch_cannot_hide_our_unshipped_commits`.
    _set_pin(fleet, fleet["o1"])
    _, stale_row = _run(fleet, "vibeic/stale", pin=fleet["o1"])
    _, pub_row = _run(fleet, "vibeic/pub")
    assert stale_row["tip"] == "origin/master", stale_row["tip"]
    assert pub_row["tip"] == "origin/vibeic/pub", pub_row["tip"]
    assert stale_row.get("tip_state") == "behind", stale_row.get("tip_state")
    assert pub_row.get("tip_state") == "current", pub_row.get("tip_state")


# ── the third state: could-not-measure, which never reads as current ─────────
def test_an_unresolvable_ledger_branch_is_named_not_silently_dropped(fleet, capsys):
    """`klayout` records `vibeic/klayout-signoff-int`, derived from a Dockerfile
    comment; it resolves neither in the clone nor on the remote (verified
    2026-08-05). The old code dropped it without a word — the numbers were right
    and the ledger's claim was never examined.

    It gets its OWN state. It does not read as `current`, and it does not turn the
    round red either: a branch that cannot resolve could never have been counted
    from, so nothing was ever mismeasured by it.
    """
    m = _load()
    rc = m.main(["--repo", str(fleet["repo"]),
                 "--forks-root", str(fleet["forks_root"]),
                 "--ledger", str(_ledger(fleet, "vibeic/never-pushed")),
                 "--no-fetch"])
    out = capsys.readouterr().out
    assert "LEDGER BRANCH UNVERIFIED" in out, out
    assert "vibeic/never-pushed" in out and "does not resolve" in out, out
    assert "LEDGER BRANCH REJECTED" not in out, out

    rep, row = _run(fleet, "vibeic/never-pushed")
    assert row.get("tip_state") == "undetermined" != "current"
    assert row["tip"] == "origin/master", row["tip"]
    assert rep["q2_ours_past_the_pin_substantive"] == 2   # still measured
    assert rc == 1, f"rc={rc} — a release gap, not a currency failure\n{out}"


# ── unit tests of the new contract. NOT offered as red proof (see the header) ─
def test_a_resolving_branch_with_no_default_to_check_it_against_is_not_measured(
        fleet, tmp_path):
    """The arm #92's `klayout` row cannot exercise: the branch resolves and there
    is no default to compare it with. Unmeasurable currency is NOT MEASURED —
    never assumed current, which is the whole subject of this issue."""
    m = _load()
    bare = tmp_path / "odd.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(bare))
    _git(fleet["fork"], "remote", "add", "odd", str(bare))
    _git(fleet["fork"], "push", "-q", "odd", f"{fleet['o2']}:refs/heads/vibeic/x")

    clone = tmp_path / "oddclone"
    _git(tmp_path, "clone", "-q", str(bare), str(clone))
    assert m.default_ref(clone) is None, (
        "fixture has a default after all: " + str(m.default_ref(clone)))

    v = m.published_tip(clone, {"vibeic_branch": "vibeic/x"})
    assert v.ref is None and v.state == "undetermined", v
    assert "UNMEASURABLE" in v.why, v.why


def test_the_three_states_are_distinct_names(fleet):
    """Two outcomes is what #92 is: `resolves` stood in for `is current`. The
    names must not collapse back into each other."""
    m = _load()
    assert len({m.TIP_CURRENT, m.TIP_BEHIND, m.TIP_UNDETERMINED, m.TIP_NO_CLAIM}) == 4
    v = m.published_tip(fleet["fork"], {"vibeic_branch": "vibeic/stale"})
    assert (v.state, v.ref) == (m.TIP_BEHIND, "origin/master"), v
    assert v.why, "a rejection with no reason is a silent fallthrough"


def test_origin_head_cannot_displace_a_real_default(fleet):
    """`origin/HEAD` is a LOCAL symbolic-ref anyone can aim anywhere. If it
    outranked `origin/master` it would be a defeat switch for this whole check:
    point it at the stale branch and the branch becomes its own yardstick."""
    m = _load()
    _git(fleet["fork"], "symbolic-ref", "refs/remotes/origin/HEAD",
         "refs/remotes/origin/vibeic/stale")
    assert m.default_ref(fleet["fork"]) == "origin/master"
    assert m.published_tip(fleet["fork"],
                           {"vibeic_branch": "vibeic/stale"}).state == m.TIP_BEHIND


def test_a_dangling_origin_head_is_not_a_default(fleet):
    """A name that does not resolve is not a default branch, it is a string."""
    m = _load()
    _git(fleet["fork"], "update-ref", "-d", "refs/remotes/origin/master")
    _git(fleet["fork"], "symbolic-ref", "refs/remotes/origin/HEAD",
         "refs/remotes/origin/deleted-long-ago")
    assert m.default_ref(fleet["fork"]) is None
