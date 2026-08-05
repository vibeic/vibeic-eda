"""A commit OUR OWN MAINLINE already merged is not a human adopt/skip decision.

MEASURED, 2026-08-06, on the tick's own PR (#874) — per commit, via the GitHub compare
the assessment states and `git merge-base --is-ancestor` in the shared clones:

    tool                    range   in the PIN   on OUR MAINLINE   genuinely new
    OpenROAD                   22            0                14               8
    OpenROAD-flow-scripts      14            0                12               2
    klayout                     6            0                 6               0
    slang                       2            0                 2               0
    verilator                   5            0                 5               0
    yosys                       5            0                 3               2

"0 already carried" was ARITHMETICALLY CORRECT on every row: nothing in any of those
ranges is an ancestor of the shipped pin, and it cannot be — on the commit-range code
path the range's base IS the pin (`base_ref, new_ref = our_ref, up_branch`), so
`already_carried`'s ancestry half is structurally incapable of firing there. Yet 42 of
the 54 commits the PR asked a human to adopt-or-skip had already been merged into our
fork mainline, most of them by the SAME 05:30 tick minutes before the PR was written
(klayout's mainline tip that morning reads "Merge upstream into master (daily 05:30)").
klayout, slang and verilator had nothing left to decide at all.

The right-hand column is `sync_lag`, which `discover_forks.lag_split` already computes;
it matched this per-commit walk on 6 rows out of 6.

The fixture below is a real git repository, not a mock: X is a genuine ancestor of our
mainline tip and Y is genuinely not, so the assertions test `git`'s answer rather than a
stub's.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import assess_release as A  # noqa: E402

TOOL = "magic"


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} → {r.returncode}\n{r.stderr}"
    return r.stdout.strip()


def _commit(repo: Path, name: str, body: str) -> str:
    """Distinct content per commit, so no two share a patch-id and `already_carried`'s
    cherry-pick half cannot answer this test's question by accident."""
    (repo / f"{name}.c").write_text(body)
    _git(repo, "add", f"{name}.c")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _build_fork_clone(forks: Path) -> dict[str, str]:
    """P (the shipped pin) → X (upstream, MERGED into our mainline) → N (ours, the tip),
    with Y a second upstream commit our mainline has NOT taken.

        P ── X ── N          refs/remotes/origin/master = N
              └── Y          upstream has it; we do not
    """
    repo = forks / TOOL
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "master")
    p = _commit(repo, "p", "pin\n")
    x = _commit(repo, "x", "upstream commit our mainline merged\n")
    n = _commit(repo, "n", "our own follow-up\n")
    _git(repo, "update-ref", "refs/remotes/origin/master", n)
    _git(repo, "checkout", "-q", "-b", "side", x)
    y = _commit(repo, "y", "upstream commit our mainline has NOT taken\n")
    _git(repo, "checkout", "-q", "master")
    return {"P": p, "X": x, "N": n, "Y": y}


def _rows(shas: dict[str, str], *names: str) -> list[dict]:
    return [{"sha": shas[k][:12], "sha_full": shas[k], "title": k, "body": "",
             "url": "", "author": "up"} for k in names]


def _assess(tmp: Path, shas: dict[str, str], commits: list[dict],
            ledger_extra: dict | None = None, stub: Path | None = None):
    """`assess()` with the network stubbed and the AI judge switched OFF (or replaced by
    a fixed stub verdict), so every commit that reaches the judge lands on
    `decision == "human"`. What decides the outcome here is the real git repository built
    above — nothing else."""
    os.environ["GK_STATE_DIR"] = str(tmp / "state")
    os.environ["GK_FORKS_DIR"] = str(tmp / "forks")
    if stub is None:
        os.environ["GK_ASSESS_AI"] = "0"      # kill-switch: no LLM, no HTTP
        os.environ.pop("GK_ASSESS_STUB", None)
    else:
        # a COMPLETE judgment, so the report is cacheable and the replay path is reachable
        os.environ.pop("GK_ASSESS_AI", None)
        os.environ["GK_ASSESS_STUB"] = str(stub)
    importlib.reload(A)
    led = {"tool": TOOL, "integrated": True, "behind_releases": 0, "behind_commits": 2,
           "upstream": "up/magic", "upstream_default_branch": "master",
           "pinned_ref_full": shas["P"], "base_release": "8.3.674",
           "upstream_latest_release": "8.3.674", "role": "DRC",
           "ours_unshipped_measured_against": "origin/master"}
    led.update(ledger_extra or {})
    (tmp / "state" / "ledger").mkdir(parents=True, exist_ok=True)
    (tmp / "state" / "ledger" / f"{TOOL}.json").write_text(json.dumps(led))
    A.upstream_commits = lambda *a: (commits, ["f.c"])
    A.our_patch_files = lambda *a: set()
    A._commit_files = lambda *a: {"f.c"}
    A.clean_cherrypick = lambda *a: None
    A._reachability = lambda *a: None
    A.recorded_decisions = lambda *a: {}
    try:
        return A.assess(TOOL)
    finally:
        for k in ("GK_STATE_DIR", "GK_FORKS_DIR", "GK_ASSESS_AI", "GK_ASSESS_STUB"):
            os.environ.pop(k, None)
        importlib.reload(A)


def test_a_commit_our_mainline_merged_is_not_re_proposed_as_a_human_decision():
    """THE DEFECT. X is already on `origin/master`; Y is not. Both were being handed to a
    human as "adopt or skip?", and the summary said "0 already carried" — true of the pin,
    and a complete answer to the wrong question."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        shas = _build_fork_clone(tmp / "forks")
        rep = _assess(tmp, shas, _rows(shas, "X", "Y"))
    sx, sy = shas["X"][:12], shas["Y"][:12]

    assert rep["carried"] == [], \
        "nothing is in the shipped pin here — `already carried: 0` is CORRECT"
    assert sx not in rep["outstanding"], (
        f"{sx} is already merged into our fork mainline (origin/master), yet the "
        f"assessment still asks a human to decide adopt/skip on it: {rep['outstanding']}")
    assert rep["on_mainline"] == [sx], rep["on_mainline"]
    assert rep["outstanding"] == [sy], \
        f"the genuinely new commit must survive as the human decision: {rep['outstanding']}"
    assert rep["mainline_undetermined"] == [], rep["mainline_undetermined"]
    assert rep["mainline_ref"] == "origin/master", rep["mainline_ref"]

    row = next(c for c in rep["commits"] if c["sha"] == sx)
    assert row["decision"] == "mainline", row["decision"]
    assert row["decision"] != "carried", \
        "it is NOT in the shipped pin — calling it carried trades one false number for another"


def test_the_documents_say_it_and_still_parse():
    """All three renderings must state it, and `parse_headline` must still recover the
    same four counts from each — the clause is appended AFTER the sentence those regexes
    own, exactly like the not-assessed warning."""
    import gatekeeper
    import pr_notify
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        shas = _build_fork_clone(tmp / "forks")
        rep = _assess(tmp, shas, _rows(shas, "X", "Y"))

    md = A.render_md(rep)
    assert "ALREADY MERGED INTO OUR FORK MAINLINE" in md, md
    assert "PIN BUMP" in md, "the reader is not told what the open action actually is"
    assert A.parse_headline("assessment", md) == {
        "carried": 0, "decided": 0, "clearly_safe": 0, "outstanding": 1}, md.splitlines()[2]

    line = pr_notify.tally_line(TOOL, rep)     # the PR body's one line per tool
    assert line is not None, "the PR body line could not be rendered at all"
    assert "ALREADY MERGED INTO OUR FORK MAINLINE" in line, line
    assert A.parse_headline("pr", line) == {
        "carried": 0, "decided": 0, "clearly_safe": 0, "outstanding": 1}, line

    entry = gatekeeper.assessment_entry(rep, 1, "8.3.674")   # the daily report's row
    assert "ALREADY MERGED INTO OUR FORK MAINLINE" in entry["note"], entry["note"]
    assert A.parse_headline("report", entry["note"]) == {
        "carried": 0, "decided": 0, "clearly_safe": 0, "outstanding": 1}, entry["note"]
    assert entry["assessed"]["on_mainline"] == 1, entry["assessed"]

    # and the three documents must not disagree — the cross-check the repo already owns
    assert A.cross_check(rep, {"assessment": md, "pr": line,
                               "report": entry["note"]}) == [], "the documents drifted"


def test_could_not_read_our_mainline_is_its_own_state_not_a_silent_no():
    """THREE STATES. With no resolvable mainline ref the probe has NOT found that these
    commits are new — it has found nothing. They stay in human review (the over-asking
    direction) and both the report and the rendered document say the probe did not run."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        shas = _build_fork_clone(tmp / "forks")
        # a fork whose mainline lives nowhere this clone can resolve
        rep = _assess(tmp, shas, _rows(shas, "X", "Y"),
                      ledger_extra={"ours_unshipped_measured_against": None,
                                    "vibeic_branch": None})
        subprocess.run(["git", "-C", str(tmp / "forks" / TOOL), "update-ref", "-d",
                        "refs/remotes/origin/master"], capture_output=True)
        rep_gone = _assess(tmp, shas, _rows(shas, "X", "Y"),
                           ledger_extra={"ours_unshipped_measured_against": None,
                                         "vibeic_branch": None})
    sx, sy = shas["X"][:12], shas["Y"][:12]
    # the ledger said nothing, but the clone still resolves origin/master — still measured
    assert rep["on_mainline"] == [sx] and rep["mainline_undetermined"] == [], rep["on_mainline"]

    assert rep_gone["mainline_ref"] is None, rep_gone["mainline_ref"]
    assert rep_gone["on_mainline"] == [], \
        "an unread mainline must not be reported as an empty one"
    assert sorted(rep_gone["mainline_undetermined"]) == sorted([sx, sy]), \
        rep_gone["mainline_undetermined"]
    assert sx in rep_gone["outstanding"] and sy in rep_gone["outstanding"], \
        "undetermined must fail SAFE — into human review, never out of it"
    md = A.render_md(rep_gone)
    assert "OUR MAINLINE COULD NOT BE READ for 2 commit(s)" in md, md
    assert "the probe did not run" in md, md


def test_a_commit_in_the_shipped_pin_stays_carried_not_mainline():
    """Precedence. Everything in the pin is also on the mainline the pin descends from;
    the STRONGER statement must win, or `already carried` would empty itself out."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        shas = _build_fork_clone(tmp / "forks")
        # assess a range that (impossibly, but the filter must still hold) contains the pin
        rep = _assess(tmp, shas, _rows(shas, "P", "X", "Y"))
    sp, sx = shas["P"][:12], shas["X"][:12]
    assert rep["carried"] == [sp], rep["carried"]
    assert sx not in rep["outstanding"], (
        f"{sx} is on our mainline and is still being handed to a human: {rep['outstanding']}")
    assert rep["on_mainline"] == [sx], rep["on_mainline"]
    assert sp not in rep["on_mainline"], "a shipped commit was double-counted as merely ours"


def test_a_replayed_assessment_re_measures_our_mainline_instead_of_replaying_it():
    """The cache stops the JUDGE re-sampling an unchanged range. Our mainline is not part
    of that range and moves every day — the same 05:30 tick merges into it. Trilinos and
    cocotb replayed 2026-08-05 AND 2026-08-06 off verdicts computed on 08-02 / 08-03, so
    this path is not hypothetical: replaying the mainline answer publishes a measurement
    taken on another day, and for an entry written before the probe existed it publishes
    a 0 nobody measured."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        shas = _build_fork_clone(tmp / "forks")
        rows = _rows(shas, "X", "Y")
        stub = tmp / "judgement.json"
        stub.write_text(json.dumps({r["sha"]: {
            "category": "other", "relevant": False, "risk": "low",
            "summary": "judged", "reproduce": "", "recommend": "skip"} for r in rows}))

        first = _assess(tmp, shas, rows, stub=stub)
        assert not first.get("cached"), "the first pass must actually judge"
        sx, sy = shas["X"][:12], shas["Y"][:12]
        assert sy in first["outstanding"], first["outstanding"]

        # our mainline moves — exactly what daily_merge does at 05:30, every day
        repo = tmp / "forks" / TOOL
        _git(repo, "update-ref", "refs/remotes/origin/master", shas["Y"])

        second = _assess(tmp, shas, rows, stub=stub)

    assert second.get("cached") is True, "this test is not exercising the replay path"
    assert sy not in second["outstanding"], (
        f"{sy} is now on our mainline, but the replayed report still asks a human to "
        f"decide adopt/skip on it: {second['outstanding']}")
    assert second["outstanding"] == [], second["outstanding"]
    assert sorted(second["on_mainline"]) == sorted([sx, sy]), second["on_mainline"]
    row = next(c for c in second["commits"] if c["sha"] == sy)
    assert row["decision"] == "mainline"
    assert row["decision_before_mainline"] == "human", \
        "the verdict it replaced must survive, so a mainline that loses the commit again "\
        "restores a recorded decision rather than an invented one"
    assert "judged" in row["summary"], "the judge's reading was resolved away"


def test_on_our_mainline_never_raises_and_answers_undetermined_without_a_clone():
    """Same fail-open contract `already_carried` carries — but reported, not swallowed."""
    orig = A.FORKS_DIR
    try:
        A.FORKS_DIR = Path("/nonexistent-fork-dir")
        on, unk = A.on_our_mainline(TOOL, "origin/master",
                                    [{"sha": "aaa111", "sha_full": "a" * 40}])
        assert on == set()
        assert unk == {"aaa111"}, "a missing clone is UNDETERMINED, not 'not ours'"
        assert A.on_our_mainline(TOOL, "origin/master", []) == (set(), set())
        assert A.on_our_mainline(TOOL, None, [{"sha": "b", "sha_full": "b" * 40}]) \
            == (set(), {"b"})
    finally:
        A.FORKS_DIR = orig
