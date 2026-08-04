"""A comparison is only as good as the ref it is made against.

`step1_upstream` and `_fork_branches` both discarded their fetch's
`CompletedProcess`, so a fetch that did not do its job left the comparison to
run against whatever the clone last managed to fetch. `behind == 0` off a stale
ref is "we could not reach upstream" wearing the words "we are up to date".

EVERY TEST HERE DRIVES REAL GIT over real repositories on disk. The defect is a
property of what git does — which exit codes it returns, which refs it updates,
what a degraded clone actually behaves like — and a mocked `sh` can only assert
that the code reads a number some other code put there. The one thing that
cannot be staged locally, an unreachable remote, is staged as a remote URL that
does not exist, which is the same rc=128 path.

BOTH DIRECTIONS ARE ASSERTED THROUGHOUT. A guard that reports UNKNOWN for every
fetch would satisfy every "must not say current" test in this file and break the
round completely; the paired case pins the other side each time.
"""
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_0530 as D                                     # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────
def _run(*a, cwd=None):
    return subprocess.run(a, capture_output=True, text=True, cwd=cwd)


def _commit(repo, msg):
    _run("git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", msg, cwd=str(repo))


@pytest.fixture
def fleet(tmp_path):
    """An upstream, a fork of it, and a clone that tracks both."""
    up = tmp_path / "up"
    up.mkdir()
    _run("git", "init", "-q", "-b", "master", str(up))
    _commit(up, "a")
    clone = tmp_path / "clone"
    _run("git", "clone", "-q", str(up), str(clone))
    _run("git", "remote", "add", "upstream", str(up), cwd=str(clone))
    _run("git", "fetch", "-q", "upstream", cwd=str(clone))
    return {"up": up, "clone": clone, "g": ("git", "-C", str(clone)),
            "tmp": tmp_path}


def _break_remote(clone, remote, tmp_path):
    _run("git", "remote", "set-url", remote, str(tmp_path / "gone"),
         cwd=str(clone))


# ── 1. a failed fetch is UNKNOWN, and a working one is still `already current` ─
def test_a_failed_upstream_fetch_is_unknown_not_current(fleet):
    """The reported defect. Upstream really is ahead; the fetch cannot run."""
    for n in ("b", "c", "d"):
        _commit(fleet["up"], n)
    _break_remote(fleet["clone"], "upstream", fleet["tmp"])
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    assert "UNKNOWN" in rep["upstream"], rep
    assert "already current" not in rep["upstream"], rep


def test_a_working_fetch_that_finds_nothing_is_still_already_current(fleet):
    """The other side. Break this and the round stops working entirely."""
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    assert rep["upstream"] == "already current", rep


def test_a_working_fetch_that_finds_commits_still_merges_them(fleet):
    """And the third state stays distinct from both of the above."""
    for n in ("b", "c", "d"):
        _commit(fleet["up"], n)
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    assert rep["upstream"] == "merged 3 upstream commit(s)", rep


# ── 2. rc 0 is not enough: the ref must be CONFIRMED, not assumed ─────────────
def test_rc_zero_over_a_stale_ref_is_unknown_not_current(fleet, monkeypatch):
    """THE CASE AN rc-ONLY GUARD DOES NOT COVER.

    A fetch that exits 0 and refreshes nothing is locally indistinguishable from
    a fetch that found nothing new. Here upstream has genuinely moved and the
    fetch is a no-op that reports success — the exact shape the gc theory
    proposed, staged directly instead of via a mechanism that does not produce
    it. The ref is stale, `master..upstream/master` is 0, and rc is 0.
    """
    for n in ("b", "c", "d"):
        _commit(fleet["up"], n)
    real = D.sh

    def _fetch_is_a_silent_noop(*a, **k):
        if "fetch" in a:
            return subprocess.CompletedProcess(a, 0, "", "")
        return real(*a, **k)
    monkeypatch.setattr(D, "sh", _fetch_is_a_silent_noop)

    # precondition: rc 0, ref stale, and the naive comparison says "current"
    assert D.out(*fleet["g"], "rev-list", "--count",
                 "master..upstream/master") == "0"
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    assert "UNKNOWN" in rep["upstream"], rep
    assert "already current" not in rep["upstream"], rep
    assert "NOT CONFIRMED CURRENT" in rep["upstream"], rep


def test_rc_zero_with_a_genuinely_current_ref_is_current(fleet, monkeypatch):
    """The paired direction: the same no-op fetch, with upstream NOT moved, must
    still be `already current`. Without this the guard above could simply refuse
    every unchanged ref."""
    real = D.sh

    def _fetch_is_a_silent_noop(*a, **k):
        if "fetch" in a:
            return subprocess.CompletedProcess(a, 0, "", "")
        return real(*a, **k)
    monkeypatch.setattr(D, "sh", _fetch_is_a_silent_noop)
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    assert rep["upstream"] == "already current", rep


def test_when_the_remote_cannot_be_asked_the_answer_is_unknown(fleet, monkeypatch):
    """`ls-remote` is the only thing that can separate the two no-op cases. If it
    cannot be reached either, the honest answer is UNKNOWN — not the reassuring
    one."""
    real = D.sh

    def _ls_remote_is_dead(*a, **k):
        if "ls-remote" in a:
            return subprocess.CompletedProcess(a, 128, "", "fatal: unreachable")
        return real(*a, **k)
    monkeypatch.setattr(D, "sh", _ls_remote_is_dead)
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    assert "UNKNOWN" in rep["upstream"], rep
    assert "UNVERIFIED" in rep["upstream"], rep


# ── 3. the sibling: a branch list that could not be read is not an empty one ──
def test_fork_branches_returns_none_when_the_fetch_fails(fleet):
    _break_remote(fleet["clone"], "origin", fleet["tmp"])
    assert D._fork_branches(fleet["g"], "master") is None


def test_fork_branches_returns_a_list_when_the_fetch_works(fleet):
    """Empty LIST and None are different answers and both must be reachable."""
    got = D._fork_branches(fleet["g"], "master")
    assert got == [], got            # a fork with only a mainline
    _run("git", "-C", str(fleet["clone"]), "push", "-q", "origin",
         "master:refs/heads/vibeic/work")
    _run("git", "-C", str(fleet["clone"]), "fetch", "-q", "origin")
    assert D._fork_branches(fleet["g"], "master") == ["origin/vibeic/work"]


def test_step2_ours_says_unknown_rather_than_nothing_to_merge(fleet):
    _break_remote(fleet["clone"], "origin", fleet["tmp"])
    rep = {}
    D.step2_ours(fleet["g"], "master", rep)
    assert "UNKNOWN" in rep["ours_skipped"], rep
    assert rep["needs_human"] is True, rep
    # The number it could not take must not be published as if it had been.
    assert "carried_nothing_new" not in rep, rep


def test_step2_ours_still_reports_normally_when_the_fetch_works(fleet):
    rep = {}
    D.step2_ours(fleet["g"], "master", rep)
    assert "ours_skipped" not in rep, rep
    assert not rep.get("needs_human"), rep
    assert rep["carried_nothing_new"] == 0, rep


def test_step4_prune_deletes_nothing_off_an_unreadable_listing(fleet):
    _break_remote(fleet["clone"], "origin", fleet["tmp"])
    rep = {}
    D.step4_prune(fleet["g"], "master", rep, apply=True)
    assert rep["pruned"] == [], rep
    assert "could not list" in rep["prune_skipped"], rep
    assert rep["needs_human"] is True, rep


def test_step4_prune_still_runs_when_the_listing_is_readable(fleet):
    rep = {}
    D.step4_prune(fleet["g"], "master", rep, apply=False)
    assert "prune_skipped" not in rep or "could not list" not in rep["prune_skipped"], rep


# ── 4. the diagnosis git wrote, not the advice it appended ───────────────────
def test_the_reported_line_is_the_one_that_names_the_cause(fleet):
    """git puts the diagnosis first and the boilerplate last; `detail[-1]` is
    `and the repository exists.`, which identifies nothing."""
    _break_remote(fleet["clone"], "upstream", fleet["tmp"])
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    assert "does not appear to be a git repository" in rep["upstream"], rep
    assert not rep["upstream"].rstrip().endswith("and the repository exists."), rep


def test_first_error_line_prefers_stderr_and_skips_blanks():
    cp = subprocess.CompletedProcess(
        ["x"], 128, "",
        "\n\nfatal: the cause\nfatal: Could not read from remote repository.\n"
        "\nPlease make sure you have the correct access rights\n"
        "and the repository exists.\n")
    assert D._first_error_line(cp) == "fatal: the cause"


def test_first_error_line_has_something_to_say_when_the_tool_said_nothing():
    cp = subprocess.CompletedProcess(["x"], 1, "", "")
    assert D._first_error_line(cp) == "no error text"


# ── 5. a hung command is answered, not waited on forever ─────────────────────
def test_a_hung_command_returns_no_answer_rather_than_blocking():
    cp = D.sh("sleep", "30", timeout=1)
    assert cp.returncode == D.RC_NO_ANSWER
    assert "timed out" in cp.stderr


def test_a_normal_command_is_untouched_by_the_bound():
    cp = D.sh("true")
    assert cp.returncode == 0
    assert cp.returncode != D.RC_NO_ANSWER


def test_a_hung_fetch_is_unknown_not_current(fleet, monkeypatch):
    """End to end: the round must not read a fetch it never got an answer from
    as `already current`.

    The rc is written as the literal 124 rather than `D.RC_NO_ANSWER` on
    purpose. Naming the constant would make this test fail on a tree that
    predates it with an AttributeError — "the symbol is missing", which is not
    the defect. A literal reaches the old code's own logic and shows what it
    does with a timed-out fetch, which is the thing under test.
    """
    for n in ("b", "c"):
        _commit(fleet["up"], n)
    real = D.sh

    def _fetch_hangs(*a, **k):
        if "fetch" in a:
            return subprocess.CompletedProcess(a, 124, "",
                                               "no answer: timed out after 1800s")
        return real(*a, **k)
    monkeypatch.setattr(D, "sh", _fetch_hangs)
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    assert "UNKNOWN" in rep["upstream"], rep
    assert "no answer" in rep["upstream"], rep


# ── 6. the verdict is kept, and so is the OBSERVATION it was made from ───────
#
# vibeic-eda#85. Everything above pins what the round CONCLUDES. None of it is
# retained past the next morning, so "did this fire in the field?" was answered
# for #82 from GitHub's Events API and a reflog rather than from any record of
# ours. `rep["upstream_evidence"]` is the round's own observation, written
# beside its verdict so a retained row can be checked instead of taken.
#
# `confirmed_by` is the load-bearing field. A row saying `already current` with
# `confirmed_by: null` is "the round could not tell", published as a round that
# could — which is exactly the rendering-of-unknowns this file exists to remove.
# Recording only the verdict string would rebuild the defect one layer up.
def _evidence(rep):
    """The block, or a message that says the round published a verdict without
    recording what it saw — an assertion about BEHAVIOUR rather than a KeyError
    about a name."""
    ev = rep.get("upstream_evidence")
    assert isinstance(ev, dict), (
        "the round published a verdict with no record of the observation it "
        f"was made from: {rep}")
    return ev


def test_the_evidence_block_has_exactly_the_fields_a_reader_needs(fleet):
    """Pinned as literals. The set is the contract a retained row is read by;
    deriving it from the module under test would assert only that the module
    equals itself."""
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    assert set(_evidence(rep)) == {
        "ref", "tip_before_fetch", "tip_seen", "fetch", "confirmed_by",
        "remote_tip", "behind"}


def test_a_ref_that_moved_is_confirmed_by_the_fetch_itself(fleet):
    """The ordinary merge morning. The ref MOVING is proof the fetch reached the
    remote — as strong as the ls-remote below — and a record that left this
    blank would make every normal morning read as unverified."""
    for n in ("b", "c", "d"):
        _commit(fleet["up"], n)
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    ev = _evidence(rep)
    assert rep["upstream"] == "merged 3 upstream commit(s)", rep
    assert ev["fetch"] == "ok"
    assert ev["confirmed_by"] == "fetch_moved_ref", ev
    assert ev["behind"] == 3, ev
    assert ev["tip_seen"] != ev["tip_before_fetch"], ev


def test_a_ref_that_did_not_move_is_confirmed_by_the_remote_it_asked(fleet):
    """The quiet morning, and the one #82 was about. The round DID make an
    independent observation of upstream's tip; `remote_tip` is that number, and
    without it in the record a later reader can only re-read the conclusion."""
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    ev = _evidence(rep)
    assert rep["upstream"] == "already current", rep
    assert ev["confirmed_by"] == "ls_remote", ev
    assert ev["remote_tip"] == ev["tip_seen"], ev
    assert len(ev["remote_tip"] or "") == 40, ev


def test_no_upstream_remote_is_recorded_as_unconfirmed(fleet):
    _run("git", "-C", str(fleet["clone"]), "remote", "remove", "upstream")
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    ev = _evidence(rep)
    assert rep["upstream"] == "no upstream remote", rep
    assert ev["fetch"] == "no_upstream_remote", ev
    assert ev["confirmed_by"] is None, ev


def test_a_failed_fetch_is_recorded_as_unconfirmed(fleet):
    for n in ("b", "c"):
        _commit(fleet["up"], n)
    _break_remote(fleet["clone"], "upstream", fleet["tmp"])
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    ev = _evidence(rep)
    assert ev["fetch"] == "failed", ev
    assert ev["confirmed_by"] is None, ev
    assert ev["behind"] is None, ev            # never 0: it was not measured


def test_a_hung_fetch_is_recorded_as_unconfirmed(fleet, monkeypatch):
    """rc written as the literal 124 for the reason given at
    `test_a_hung_fetch_is_unknown_not_current`."""
    real = D.sh

    def _fetch_hangs(*a, **k):
        if "fetch" in a:
            return subprocess.CompletedProcess(a, 124, "",
                                               "no answer: timed out after 1800s")
        return real(*a, **k)
    monkeypatch.setattr(D, "sh", _fetch_hangs)
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    ev = _evidence(rep)
    assert ev["fetch"] == "no_answer", ev
    assert ev["confirmed_by"] is None, ev


def test_an_rc_zero_fetch_over_a_stale_ref_is_recorded_as_unconfirmed(fleet,
                                                                      monkeypatch):
    """THE #82 SHAPE. rc 0, ref unmoved, and upstream really has moved. The
    verdict is already UNKNOWN; what is added here is that the RECORD of it
    cannot be mistaken for the quiet morning above."""
    for n in ("b", "c", "d"):
        _commit(fleet["up"], n)
    real = D.sh

    def _fetch_is_a_silent_noop(*a, **k):
        if "fetch" in a:
            return subprocess.CompletedProcess(a, 0, "", "")
        return real(*a, **k)
    monkeypatch.setattr(D, "sh", _fetch_is_a_silent_noop)
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    ev = _evidence(rep)
    assert ev["fetch"] == "ok", ev             # the fetch itself said nothing wrong
    assert ev["confirmed_by"] is None, ev      # and it still did not confirm
    assert ev["remote_tip"] != ev["tip_seen"], ev
    assert ev["behind"] is None, ev


def test_a_remote_that_cannot_be_asked_is_recorded_as_unconfirmed(fleet, monkeypatch):
    real = D.sh

    def _ls_remote_is_dead(*a, **k):
        if "ls-remote" in a:
            return subprocess.CompletedProcess(a, 128, "", "fatal: unreachable")
        return real(*a, **k)
    monkeypatch.setattr(D, "sh", _ls_remote_is_dead)
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    ev = _evidence(rep)
    assert ev["confirmed_by"] is None, ev
    assert ev["remote_tip"] is None, ev        # it could not be asked, so no number


def test_a_working_fetch_with_no_upstream_branch_is_recorded_as_unconfirmed(fleet):
    """`fetch: ok` is NOT the confirmation. Here the fetch works perfectly and
    there is still nothing to compare against, because the remote's mainline is
    called neither `main` nor `master`."""
    other = fleet["tmp"] / "trunk-only"
    other.mkdir()
    _run("git", "init", "-q", "-b", "trunk", str(other))
    _commit(other, "a")
    _run("git", "-C", str(fleet["clone"]), "remote", "set-url", "upstream", str(other))
    _run("git", "-C", str(fleet["clone"]), "update-ref", "-d",
         "refs/remotes/upstream/master")
    rep = {}
    D.step1_upstream(fleet["g"], "master", rep)
    ev = _evidence(rep)
    assert rep["upstream"] == "no upstream branch", rep
    assert ev["fetch"] == "ok", ev
    assert ev["confirmed_by"] is None, ev
