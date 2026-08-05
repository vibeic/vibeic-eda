#!/usr/bin/env python3
"""A question that was not answered must not be published as an answer.

vibeic-eda#101. The daily report had four verdicts — MERGED / DEFERRED / CLEAN /
NOT_LAYERED — and a row whose measurement did not happen had to be rendered with
one of them. Both plausible choices are wrong IN THE SAME DIRECTION: CLEAN and
DEFERRED each read as "measured, here is the answer", and the operator's next move
differs from the one the row deserves.

MEASURED ON THE 2026-08-06 TICK, which is the corpus every fixture below is drawn
from:

  * SEVEN rows printed `DEFERRED … harness returned no result for this tool` whose
    upstream publishes no release at all (FasterCap, Geometry, LinAlgebra,
    ALIGN-pdk-sky130, ASAP7_for_KLayout, asap7_pdk_r1p7, asap7sc7p5t_28). Nothing
    was deferred; nothing was askable. "We could not look" was rendered as "we
    looked and chose not to act", and the harness was named for it.
  * `open_pdks` printed the same harness sentence beside a concrete target, when
    the round had EXCLUDED it on purpose — it is a contents assertion, and
    advancing it rebuilds nothing (vibeic-eda#79). A deliberate exclusion reported
    as a tool failure.
  * `unassessed_drift` still carried `led.get("behind_commits") or 0`, so a CLEAN
    row whose COMMIT-level compare never answered said nothing at all — the exact
    "CLEAN reads as nothing-to-do for a fork whose commit-level state is unknown"
    the issue was filed about, one function below the verdict it blamed.
  * `inbound_survey` detected "compare failed in both cross-repo and
    upstream-internal scope" for three mirrors and then counted them into its own
    headline population: "24 fork(s), 12 upstream commit(s) our pins lack" over 21
    surveyed forks.

WHAT IS DELIBERATELY NOT ASSERTED HERE: that UNMEASURABLE fails the round. It does
not, and `test_an_unmeasurable_row_does_not_fail_the_round` pins that it does not.
Seven of today's rows are permanently unmeasurable because their upstreams cut no
releases; a round that goes red on those is a round people route around, and the
verdict would then be worth less than the collapse it replaced. Counted, named,
and escalated ONLY on the `unknown` sub-status — which is the one a human can act
on — is the whole contract.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import discover_forks as disc          # noqa: E402
import inbound_survey as INB           # noqa: E402
import pr_notify as PRN                # noqa: E402


def _load(name):
    """Import a sibling module fresh, without a package — the suite's house style."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"_u_{name}", Path(__file__).resolve().parent / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_u_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def _gk():
    return _load("gatekeeper")


# ── the tick harness ────────────────────────────────────────────────────────
# A REAL tick against a prepared state dir: no network, no PR, no page. Only the
# ledger seeder and the judge are stubbed; the verdict branch, the counts, the
# cross-document gate and the rendered markdown are all the production path.
# Written here rather than imported from test_assess so that a change to that
# file's private fixture cannot silently alter what this file is asserting.

def _pin_fleet(gk, where: Path, tools):
    fleet = {"org": "vibeic", "forks": [{"tool": t, "role": "test",
                                         "upstream": f"them/{t}"} for t in tools]}
    where.mkdir(parents=True, exist_ok=True)
    (where / "FORKS.json").write_text(json.dumps(fleet, indent=2) + "\n")
    (where / "ENHANCEMENTS.json").write_text("{}\n")

    def run(*a):
        subprocess.run(("git", "-C", str(where)) + a, capture_output=True, check=True)

    if not (where / ".git").exists():
        run("init", "-q")
    run("add", "FORKS.json", "ENHANCEMENTS.json")
    if subprocess.run(("git", "-C", str(where), "diff", "--cached", "--quiet"),
                      capture_output=True).returncode:
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fleet")
    gk.fleet_config.HERE = where


def _tick(state: Path, ledgers: dict):
    (state / "ledger").mkdir(parents=True, exist_ok=True)
    for name, led in ledgers.items():
        (state / "ledger" / f"{name}.json").write_text(json.dumps(led))
    os.environ["GK_STATE_DIR"] = str(state)
    merge_pr = os.environ.pop("GK_MERGE_PR", None)
    gk = _gk()
    was = gk.fleet_config.HERE
    try:
        _pin_fleet(gk, state / "_src", sorted(ledgers))
        gk.disc = type("D", (), {"main": staticmethod(lambda: None)})()
        gk.pr_notify = None
        gk.build_page = type("B", (), {"DEFAULT_OUT": None,
                                       "build": staticmethod(lambda *a: None)})()
        gk.assess_release = type("A", (), {"assess": staticmethod(lambda t: None)})()
        return gk, gk.tick()
    finally:
        gk.fleet_config.HERE = was
        os.environ.pop("GK_STATE_DIR", None)
        if merge_pr is not None:
            os.environ["GK_MERGE_PR"] = merge_pr


def _row(summary, tool):
    return next(r for r in summary["results"] if r["tool"] == tool)


def _table_row(md, tool):
    return next(ln for ln in md.splitlines() if ln.startswith(f"| {tool} |"))


#: Column positions in the report table, by NAME. Asserting on a substring of the
#: whole row is how a test about the COMMIT-gap cell passes or fails on the
#: RELEASE-gap cell beside it — `"| 0 |" not in row` is true of a row whose
#: release gap is a legitimate measured zero, and the assertion would then be
#: about the wrong column while reading as if it were about the right one.
_COLS = ("Tool", "Verdict", "New releases", "Commit gap", "Target", "Note")


def _cell(md, tool, column):
    """One CELL of one row, located by the table's OWN header.

    The header is re-read rather than trusted from `_COLS`, so a renderer that
    reorders or renames the columns fails here loudly instead of silently moving
    what every assertion below is looking at.
    """
    head = [c.strip() for c in _table_row(md, "Tool").split("|")[1:-1]]
    assert head == list(_COLS), f"the report table's columns changed: {head}"
    parts = [c.strip() for c in _table_row(md, tool).split("|")[1:-1]]
    return parts[head.index(column)]


# The 2026-08-06 FasterCap ledger, trimmed to the fields the verdict branch reads.
# Its upstream cuts no releases at all, so the release question has no subject —
# while its commit gap is a real, clone-measured 0.
FASTERCAP = {
    "tool": "FasterCap", "integrated": True,
    "upstream": "ediloren/FasterCap", "upstream_default_branch": "master",
    "image_version": "0.2.67", "pinned_ref": "afca8f5e55bb",
    "pinned_ref_full": "afca8f5e55bb" + "0" * 28,
    "behind_releases": None, "behind_releases_status": "not-probed",
    "behind_commits": 0, "base_release": None, "upstream_latest_release": None,
    "undetermined_releases": [],
}

# The issue's named red proof: a repo with no GitHub parent and no upstream
# containment. Both compares 404, so containment could not be decided for the one
# upstream release that exists, and the commit compare never answered either.
NO_PARENT_NO_CONTAINMENT = {
    "tool": "Orphan", "integrated": True,
    "upstream": "them/Orphan", "upstream_default_branch": "main",
    "image_version": "0.2.67", "pinned_ref": "abcdef012345",
    "pinned_ref_full": "abcdef012345" + "0" * 28,
    "behind_releases": None, "behind_releases_status": "unknown",
    "behind_commits": None,
    "undetermined_releases": [{"tag": "v2.0", "error": "compare 404 in both scopes"}],
    "base_release": "v1.0", "upstream_latest_release": "v2.0",
}


# ── 1. THE RED PROOF the issue names ────────────────────────────────────────

def test_a_fork_with_no_parent_and_no_containment_is_unmeasurable_not_clean():
    """Point a tool at a repo with no parent and no upstream containment; the
    report must render UNMEASURABLE.

    On origin/main this row lands on DEFERRED with the note "harness returned no
    result for this tool" — a deferral, complete with a target, over a question
    that was never answered. CLEAN was the other reachable wrong answer and is
    asserted against too, because a fix that merely moved the row from one wrong
    verdict to the other would satisfy a laxer test.
    """
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        _, summary = _tick(state, {"Orphan": NO_PARENT_NO_CONTAINMENT})
        row = _row(summary, "Orphan")
        assert row["verdict"] == "UNMEASURABLE", (
            f"a row whose containment could not be decided was published as "
            f"{row['verdict']!r} — a verdict that reads as a measurement: {row['note']}")
        assert row["verdict"] not in ("CLEAN", "DEFERRED")
        # …and the row says WHICH of the two questions had no answer, so the
        # verdict is actionable rather than merely honest.
        assert "UNKNOWN" in row["note"], row["note"]
        assert "v2.0" in row["note"] and "404" in row["note"], (
            "the row names neither the release nor the error that stopped it")


def test_an_upstream_that_publishes_no_release_is_unmeasurable_not_deferred():
    """Seven of today's rows. `not-probed` is not a deferral: nothing is staged,
    nothing failed, and the harness — which the old note blamed — was never asked.
    """
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        _, summary = _tick(state, {"FasterCap": FASTERCAP})
        row = _row(summary, "FasterCap")
        assert row["verdict"] == "UNMEASURABLE", (
            f"an upstream with no releases at all was published as {row['verdict']!r}")
        assert "harness returned no result" not in row["note"], (
            "the row still blames the harness for a question nobody could ask: "
            + row["note"])
        assert "NOT PROBED" in row["note"], row["note"]


# ── 2. THE TWO QUESTIONS ARE SEPARATE ANSWERS ───────────────────────────────

def test_the_row_carries_the_commit_level_answer_beside_the_release_one():
    """Release-unmeasurable and commit-level-MEASURED, on one row.

    "We know nothing about FasterCap" would be as false as "FasterCap is CLEAN":
    its commit gap is a real 0. A verdict column that can only say one thing has to
    say which thing it said.
    """
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        _, summary = _tick(state, {"FasterCap": FASTERCAP})
        row = _row(summary, "FasterCap")
        # The NOTE first, because that is the sentence a human reads and it fails
        # behaviourally on origin/main rather than on a missing key.
        assert "COMMIT-level gap IS measured" in row["note"], (
            "the row states no commit-level answer at all, so UNMEASURABLE reads "
            "as 'nothing is known' — an overstatement in the other direction: "
            + row["note"])
        assert row.get("behind_commits") == 0, row
        assert row.get("behind_commits_status") == "measured", row


def test_a_clean_row_says_so_when_its_commit_gap_was_never_measured():
    """The `or 0` that was still live. CLEAN is a RELEASE-level claim; a fork that
    is on the newest tag and whose commit compare never answered used to render as
    an unqualified clean bill, because `led.get("behind_commits") or 0` sent the
    null down the same branch as a measured zero and returned "".
    """
    gk = _gk()
    led = {"tool": "X", "behind_commits": None, "pinned_ref_full": "a" * 40,
           "upstream_default_branch": "master"}
    note = gk.unassessed_drift(led)
    assert note, ("a CLEAN row whose commit gap was never measured discloses "
                  "nothing — it is indistinguishable from a measured zero")
    assert "NOT MEASURED" in note, note
    # …and a genuinely measured zero stays silent, or the test above is met by
    # printing a warning on every clean fork every day.
    assert gk.unassessed_drift({**led, "behind_commits": 0}) == ""
    # …and a measured non-zero still reports the number it always did.
    assert "7 upstream commit(s)" in gk.unassessed_drift({**led, "behind_commits": 7})


def test_the_report_table_prints_unmeasured_not_a_digit_for_the_commit_gap():
    """The second column under the first column's rule: the two states that are
    not a number are spelled out, and neither of them is 0."""
    gk = _gk()
    summary = {"date": "2026-08-06", "generated_at": "x", "image_version": "0.9.9",
               "counts": {"MERGED": 0, "DEFERRED": 0, "CLEAN": 1, "NOT_LAYERED": 0},
               "results": [{"tool": "X", "verdict": "CLEAN", "new_releases": 0,
                            "new_releases_status": "measured",
                            "behind_commits": None,
                            "behind_commits_status": "unknown",
                            "latest_release": "v1", "note": "n"}]}
    md = gk._report_md(summary)
    got = _cell(md, "X", "Commit gap")
    assert got == "unmeasured", \
        f"the commit-gap cell reads {got!r} for a compare that never answered"
    # …and the RELEASE cell beside it still carries its own, genuinely measured,
    # zero: the point of the split is that one row answers both questions.
    assert _cell(md, "X", "New releases") == "0"


def test_the_report_survives_a_row_written_before_the_commit_column_existed():
    """COULD-NOT-DETERMINE IS ITS OWN STATE, including for the archive. Every
    report already under reports/ carries neither field; the column must render
    "no answer here", not borrow the release column's."""
    gk = _gk()
    summary = {"date": "2026-07-01", "generated_at": "x", "image_version": "0.9.9",
               "counts": {"MERGED": 0, "DEFERRED": 0, "CLEAN": 1, "NOT_LAYERED": 0},
               "results": [{"tool": "X", "verdict": "CLEAN", "new_releases": 0,
                            "new_releases_status": "measured",
                            "latest_release": "v1", "note": "n"}]}
    md = gk._report_md(summary)
    assert _cell(md, "X", "Commit gap") == "—", \
        "a row written before the column existed invented an answer for it"
    assert _cell(md, "X", "New releases") == "0"


# ── 3. IT IS COUNTED AND NAMED, AND IT DOES NOT FAIL THE ROUND ──────────────

def test_the_headline_counts_the_unmeasurable_rows():
    """Counted, in the summary an operator reads first. The counts dict was a
    literal four-tuple, so a verdict absent from it was absent from the headline —
    which is how RESOLVED has been uncounted since #369."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        _, summary = _tick(state, {"FasterCap": FASTERCAP,
                                   "Orphan": NO_PARENT_NO_CONTAINMENT})
        assert summary["counts"].get("UNMEASURABLE") == 2, summary["counts"]
        assert summary["counts"]["CLEAN"] == 0
        assert summary["counts"]["DEFERRED"] == 0, (
            "an unmeasurable row is still being counted as a deferral: "
            + str(summary["counts"]))
        md = (state / "reports" / f"{summary['date']}.md").read_text()
        assert "UNMEASURABLE 2" in md, md.splitlines()[:8]


def test_an_unmeasurable_row_does_not_fail_the_round():
    """THE COUNTERWEIGHT, and it is NOT a red proof — it passes on origin/main too,
    where the same row is a DEFERRED that also does not fail the round. It is here
    so the fix cannot be "made honest by making it fatal": a permanently-red round
    is one people route around, and seven of today's rows are unmeasurable in a way
    that will never clear. The tick must publish its documents and return normally.
    """
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        gk, summary = _tick(state, {"FasterCap": FASTERCAP})
        assert (state / "reports" / f"{summary['date']}.json").is_file(), \
            "the round published nothing because a row was unmeasurable"
        assert (state / "reports" / f"{summary['date']}.md").is_file()
        assert gk.verify_documents(summary["date"]) == []


def test_an_unknown_gap_still_reaches_a_human_under_the_new_verdict():
    """THE REGRESSION THIS FIX COULD HAVE INTRODUCED. pr_notify selected the rows a
    human is shown by matching `verdict == "DEFERRED"`. Renaming the verdict
    without updating the selector would have un-escalated precisely the row that
    most needs escalating — a fix that removes an escalation, wearing the fix's
    name."""
    summary = {"results": [{"tool": "Orphan", "verdict": "UNMEASURABLE",
                            "new_releases": None,
                            "new_releases_status": "unknown"}]}
    assert [r["tool"] for r in PRN._actionable(summary)[1]] == ["Orphan"], \
        "a row whose gap is unknown stopped reaching a human when it was renamed"


def test_a_not_probed_row_is_named_but_opens_no_pr():
    """…and the counterweight to that: an upstream that publishes no release is
    unmeasurable AND unactionable. A PR every morning about a state no human can
    clear is how a notification channel gets muted."""
    summary = {"results": [{"tool": "FasterCap", "verdict": "UNMEASURABLE",
                            "new_releases": None,
                            "new_releases_status": "not-probed"}]}
    assert PRN._actionable(summary)[1] == [], \
        "a permanently-unmeasurable row would open a PR every day"


def test_the_sync_log_entry_does_not_relabel_an_unmeasurable_row_as_deferred():
    """The one document a human opens. `_log_entry` hard-coded the word DEFERRED
    for every row in its `failed` list, which now carries UNMEASURABLE rows too —
    republishing the collapse in the artefact the verdict exists to fix."""
    summary = {"date": "2026-08-06", "image_version": "0.2.67",
               "results": [{"tool": "Orphan", "verdict": "UNMEASURABLE",
                            "new_releases": None, "latest_release": "v2.0",
                            "new_releases_status": "unknown", "note": "n"}]}
    merged, failed = PRN._actionable(summary)
    entry = PRN._log_entry(summary, merged, failed)
    assert "**UNMEASURABLE** Orphan" in entry, entry
    assert "**DEFERRED** Orphan" not in entry, entry


# ── 4. A DELIBERATE EXCLUSION IS NOT A TOOL FAILURE ─────────────────────────

def test_a_contents_assertion_row_names_the_real_reason_nothing_ran():
    """open_pdks. The round excludes a CONTENTS ASSERTION from the candidate loop
    on purpose (vibeic-eda#79) and then reported that decision as "harness returned
    no result for this tool" — "we could not look" printed where "we chose not to
    look" is the truth."""
    led = {"tool": "open_pdks", "integrated": True, "image_version": "0.2.67",
           "upstream": "them/open_pdks", "upstream_default_branch": "main",
           "pinned_ref": "b344c97eacc2", "pinned_ref_full": "b344c97eacc2" + "0" * 28,
           "behind_releases": 6, "behind_releases_status": "measured",
           "behind_commits": 19, "undetermined_releases": [],
           "pin_kind": "contents_assertion",
           "dockerfile_arg": "OPEN_PDKS_VOLUME_CONTENTS_SHA",
           "base_release": "1.0.599", "upstream_latest_release": "1.0.606"}
    with tempfile.TemporaryDirectory() as d:
        _, summary = _tick(Path(d), {"open_pdks": led})
        row = _row(summary, "open_pdks")
        assert "harness returned no result" not in row["note"], (
            "a deliberate exclusion is still reported as a harness failure: "
            + row["note"])
        assert "CONTENTS ASSERTION" in row["note"], row["note"]


# ── 5. THE SURVEY'S OWN DENOMINATOR ─────────────────────────────────────────

def test_the_survey_headline_excludes_the_forks_it_could_not_ask_about(capsys):
    """`inbound_survey` detected the double-404 for three mirrors and then counted
    them into the population its headline describes: "24 fork(s), 12 upstream
    commit(s) our pins lack" on a tick that surveyed 21. Reporting a sample as a
    population is the failure this file's own docstring says it prevents.

    Two forks. `Good` is an ordinary fork-button fork and surveys cleanly.
    `Mirror` reproduces the exact state at `inbound_survey.py:199` — no GitHub
    parent (so the cross-repo compare 404s), an upstream known only from
    FORKS.json, and an upstream that does not contain our pin (so the
    upstream-internal fallback 404s too).
    """
    def fake_gh(path):
        if path == "repos/vibeic/Good":
            return {"parent": {"full_name": "them/Good", "default_branch": "main"}}
        if path == "repos/vibeic/Mirror":
            return {}                       # a pushed mirror: no parent at all
        if path == "repos/them/Mirror":
            return {"default_branch": "main"}
        if "/compare/" in path and "Mirror" in path:
            return None                     # BOTH scopes 404
        if "/compare/" in path:
            return {"total_commits": 4, "commits": [
                {"sha": "a" * 40, "commit": {"message": "fix a crash",
                                             "author": {"date": "2026-08-05"}}}]}
        return {"default_branch": "main"}

    real_gh, real_pins = INB._gh_json, INB.pinned_refs
    try:
        INB._gh_json = fake_gh
        INB.pinned_refs = lambda root: {"Good": "c" * 40, "Mirror": "b" * 40}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "fork-gatekeeper").mkdir()
            (root / "fork-gatekeeper" / "FORKS.json").write_text(json.dumps(
                {"forks": [{"tool": "Mirror", "upstream": "them/Mirror"}]}))
            out = root / "survey.json"
            rc = INB.main(["--eda-root", str(root), "--json", str(out)])
            doc = json.loads(out.read_text())
    finally:
        INB._gh_json, INB.pinned_refs = real_gh, real_pins

    out = capsys.readouterr().out.splitlines()
    assert rc == INB.RC_PARTIAL, "an unsurveyable fork must not read as a clean run"
    # THE HEADLINE, located the way run_tick.sh locates it — by the `^inbound_survey:`
    # anchor, not by position. The survey's output file is `> out 2>&1`, stderr is
    # unbuffered and stdout is block-buffered, so the FIRST line of that file is
    # routinely a per-fork error row; `head -1` was therefore printing an error
    # where the log says it is printing the summary. This assertion pins the anchor
    # so the two files cannot drift apart.
    head = next(ln for ln in out if ln.startswith("inbound_survey:"))
    assert "1 of 2 fork(s) surveyed" in head, \
        f"the headline states a population it did not survey: {head}"
    # …and the count of unaskable forks is on STDOUT beside it, not only on stderr,
    # under the `UNMEASURABLE:` token run_tick.sh greps the summary out with.
    assert any(ln.strip().startswith("UNMEASURABLE:") and "Mirror" in ln
               for ln in out), out
    assert doc["surveyed"] == 1 and doc["unmeasurable"] == 1, doc
    assert doc["unmeasurable_repos"] == ["Mirror"], doc
    states = {f["repo"]: f.get("state") for f in doc["forks"]}
    assert states == {"Mirror": "UNMEASURABLE", "Good": "SURVEYED"}, states
    # …and the totals are over the surveyed rows only, never over all of them.
    assert doc["total_behind"] == 4, doc


# ── 6. the ledger reader the report is built on ─────────────────────────────

def test_the_commit_gap_reader_has_three_states():
    """A null `behind_commits` is not zero, and "nothing pins this tool" is not the
    same claim as "the compare failed". `record_behind` already writes None rather
    than 0 for a failed compare; this is the reader that keeps it that way."""
    pinned = {"pinned_ref_full": "a" * 40}
    assert disc.commit_gap_status({**pinned, "behind_commits": 7}) == "measured"
    assert disc.commit_gap({**pinned, "behind_commits": 7}) == 7
    assert disc.commit_gap_status({**pinned, "behind_commits": 0}) == "measured"
    assert disc.commit_gap({**pinned, "behind_commits": 0}) == 0
    assert disc.commit_gap_status({**pinned, "behind_commits": None}) == "unknown"
    assert disc.commit_gap({**pinned, "behind_commits": None}) is None
    assert disc.commit_gap_status({"behind_commits": None}) == "not-probed"
    # a bool is not a count — `True` must not be read as "1 commit behind"
    assert disc.commit_gap_status({**pinned, "behind_commits": True}) == "unknown"


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
