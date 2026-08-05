#!/usr/bin/env python3
"""A PR's TITLE and its own BODY stated two different numbers under one word.

vibe-ic#875 (2026-08-06) was titled `[eda-fork] 2026-08-06: MERGED 0 · DEFERRED 3`.
Its body said `MERGED 0 · DEFERRED 10 · CLEAN 26 · NOT_LAYERED 0` and carried a
table with ten DEFERRED rows. vibe-ic#838 the day before said 3 and 10 too. The
defect was written up in #838's close comment and the artefact was closed; the
next tick regenerated it verbatim, because prose is not a fix.

MEASURED, not guessed. Replaying the two published reports through both call
sites gives the same split on both days, and the seven rows in the gap are the
same seven — every one `new_releases_status == "not-probed"`, note "no upstream
release to compare against":

    title  pr_notify.open_pr        len(_actionable(summary)[1])   → 3
    body   gatekeeper._report_md    summary["counts"]["DEFERRED"]  → 10

Two populations. The title counted DEFERRED tools with a MEASURED new upstream
release (plus those whose gap could not be measured); the body counted every
DEFERRED verdict. Both correct about themselves, and the page they share
contradicts itself.

WHAT THESE TESTS DO — and why they can go red.

They drive the REAL publishing path: `gatekeeper._report_md` renders the body,
and `pr_notify.open_pr` builds the title, in `GK_PR_DRYRUN` against a REAL pair
of git repositories built in a tmpdir (a bare "origin" plus a clone), so the
worktree/commit machinery the title is produced inside runs for real. Nothing is
monkeypatched except the location of the vibe-ic clone.

Deliberately, the two primary tests name NO symbol this change adds. Against
pristine `origin/main` they fail on the ASSERTION — 3 != 10, and "the PR was
opened" where a refusal belongs — not on an ImportError for something new, which
would prove only that the file is new.

`test_a_day_where_every_deferred_is_actionable_still_agrees` is the control that
keeps the pair honest: on a day where the two populations coincide the old code
also agrees, so a test that went red on every input would not be measuring this.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gatekeeper as gk               # noqa: E402
import pr_notify as prn               # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def _git(*args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"git {' '.join(args)} failed: {p.stdout}{p.stderr}"
    return p.stdout


def _row(tool, verdict, *, new_releases=None, status="not-probed", latest=None):
    return {"tool": tool, "verdict": verdict, "note": f"{tool} note",
            "new_releases": new_releases, "new_releases_status": status,
            "latest_release": latest}


def _summary(results, date="2026-08-06"):
    """A tick summary shaped exactly like the ones on disk, counts included."""
    return {
        "date": date,
        "generated_at": f"{date}T05:30:00+00:00",
        "image_version": "0.2.58",
        "results": results,
        "counts": {v: sum(1 for r in results if r["verdict"] == v)
                   for v in ("MERGED", "DEFERRED", "CLEAN", "NOT_LAYERED")},
    }


#: The shape vibe-ic#875 published, scaled down: DEFERRED rows that have a real
#: new release (actionable) alongside DEFERRED rows nobody could probe.
def _mixed_deferred_summary():
    return _summary([
        _row("cocotb", "DEFERRED", new_releases=1, status="measured", latest="v2.0.1"),
        _row("open_pdks", "DEFERRED", new_releases=6, status="measured", latest="1.0.606"),
        _row("FasterCap", "DEFERRED"),
        _row("Geometry", "DEFERRED"),
        _row("LinAlgebra", "DEFERRED"),
        _row("yosys", "CLEAN", new_releases=0, status="measured", latest="0.57"),
    ])


@pytest.fixture()
def vibeic_clone(tmp_path, monkeypatch):
    """A real origin+clone pair standing in for the vibe-ic checkout."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "-q", "--bare", "-b", "main", ".", cwd=origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=seed)
    _git("config", "user.email", "gk@example.invalid", cwd=seed)
    _git("config", "user.name", "gk", cwd=seed)
    (seed / "README.md").write_text("vibeic-eda:0.2.57\n")
    (seed / "docs").mkdir()
    (seed / "docs" / "INSTALL.md").write_text("vibeic-eda:0.2.57\n")
    _git("add", "README.md", "docs/INSTALL.md", cwd=seed)
    _git("commit", "-q", "-m", "seed", cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)

    clone = tmp_path / "vibe-ic"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    _git("config", "user.email", "gk@example.invalid", cwd=clone)
    _git("config", "user.name", "gk", cwd=clone)

    monkeypatch.setattr(prn, "REPO", clone)
    monkeypatch.setenv("GK_PR_DRYRUN", "1")
    return clone


_TITLE_RE = re.compile(r"would open PR '(.*?)' on ")


def _title_of(detail: str) -> str:
    m = _TITLE_RE.search(detail)
    assert m, f"could not read the title out of the dry-run detail:\n{detail}"
    return m.group(1)


def _deferred_in(text: str) -> int:
    """The number this document states after the word DEFERRED, in its headline."""
    m = re.search(r"DEFERRED\s+(\d+)", text)
    assert m, f"no DEFERRED count stated in:\n{text[:400]}"
    return int(m.group(1))


# --------------------------------------------------------------------------- #
# the defect
# --------------------------------------------------------------------------- #

def test_pr_title_states_the_same_deferred_count_as_the_body_it_publishes(vibeic_clone):
    """The number after DEFERRED is one number, whichever half of the PR you read.

    RED on pristine origin/main: title 2, body 5 — the exact 3-vs-10 shape of
    vibe-ic#875, and an AssertionError, not an import error.
    """
    summary = _mixed_deferred_summary()
    body = gk._report_md(summary)

    ok, detail = prn.open_pr(summary, body)
    assert ok, f"the dry-run PR did not build: {detail}"
    title = _title_of(detail)

    body_n = _deferred_in(body)
    title_n = _deferred_in(title)
    assert body_n == 5, f"fixture drifted — body should carry 5 DEFERRED rows, got {body_n}"
    assert title_n == body_n, (
        f"the PR title says DEFERRED {title_n} and its own body says DEFERRED "
        f"{body_n}\n  title: {title}\n  body headline: "
        f"{[l for l in body.splitlines() if 'DEFERRED' in l][0]}")


def test_the_body_headline_states_as_many_deferred_as_its_table_has_rows(vibeic_clone):
    """The other half a reader compares: the headline against the table under it."""
    summary = _mixed_deferred_summary()
    body = gk._report_md(summary)
    rows = [l for l in body.splitlines()
            if l.startswith("| ") and "| DEFERRED |" in l]
    assert _deferred_in(body) == len(rows), (
        f"headline says DEFERRED {_deferred_in(body)} over {len(rows)} DEFERRED row(s)")


def test_the_actionable_subset_is_still_stated_under_its_own_name(vibeic_clone):
    """Agreement must not be bought by deleting the number that made the PR fire.

    Two of the five DEFERRED rows have a new upstream release; that is why this
    PR exists at all. It stays in the title — labelled, never as "DEFERRED".
    """
    summary = _mixed_deferred_summary()
    ok, detail = prn.open_pr(summary, gk._report_md(summary))
    assert ok, detail
    title = _title_of(detail)
    assert "2 actionable" in title, (
        f"the actionable subset (2 of 5) is not stated in the title: {title}")


def test_open_pr_refuses_a_body_that_contradicts_the_title_it_would_carry(vibeic_clone):
    """COULD-NOT-SHOW-AGREEMENT is its own outcome, never a publish.

    RED on pristine origin/main: it opens the PR (ok=True), because nothing there
    ever reads the body it is about to send.
    """
    summary = _mixed_deferred_summary()
    doctored = gk._report_md(summary).replace(
        "DEFERRED 5", "DEFERRED 99", 1)
    ok, detail = prn.open_pr(summary, doctored)
    assert not ok, (
        f"a PR was opened whose body states DEFERRED 99 while its counts say 5: {detail}")
    assert "refus" in detail.lower(), detail


def test_open_pr_refuses_a_body_whose_counts_it_cannot_read(vibeic_clone):
    """An unreadable headline is not agreement — it is an unanswered question."""
    summary = _mixed_deferred_summary()
    ok, detail = prn.open_pr(summary, "# a report with no headline counts at all\n")
    assert not ok, f"a PR was opened over a body with no stated counts: {detail}"
    assert "refus" in detail.lower(), detail


# --------------------------------------------------------------------------- #
# control — this must be green BOTH before and after, or the pair above is
# measuring "any input" rather than "this defect".
# --------------------------------------------------------------------------- #

def test_a_day_where_every_deferred_is_actionable_still_agrees(vibeic_clone):
    summary = _summary([
        _row("cocotb", "DEFERRED", new_releases=1, status="measured", latest="v2.0.1"),
        _row("open_pdks", "DEFERRED", new_releases=6, status="measured", latest="1.0.606"),
        _row("yosys", "CLEAN", new_releases=0, status="measured", latest="0.57"),
    ])
    body = gk._report_md(summary)
    ok, detail = prn.open_pr(summary, body)
    assert ok, detail
    title = _title_of(detail)
    assert _deferred_in(title) == _deferred_in(body) == 2, f"{title!r} vs headline"
    assert "actionable" not in title, (
        f"the subset equals the whole here — stating it twice is noise: {title}")


def test_an_unmeasurable_release_gap_still_counts_as_actionable(vibeic_clone):
    """A DEFERRED row whose gap is UNKNOWN is the row most in need of a human.

    It must not fall out of the actionable subset the way `(x or 0) > 0` would
    drop it — and the totals must still agree.
    """
    summary = _summary([
        _row("magic", "DEFERRED", new_releases=None, status="unknown", latest="8.3.676"),
        _row("FasterCap", "DEFERRED"),
        _row("yosys", "CLEAN", new_releases=0, status="measured", latest="0.57"),
    ])
    body = gk._report_md(summary)
    ok, detail = prn.open_pr(summary, body)
    assert ok, detail
    title = _title_of(detail)
    assert _deferred_in(title) == _deferred_in(body) == 2, title
    assert "1 actionable" in title, title


# --------------------------------------------------------------------------- #
# the derivation itself. These name the new module, so on pristine origin/main
# they error on the import rather than on an assertion — which is why they are
# supplementary and not the red proof above.
# --------------------------------------------------------------------------- #

def test_counts_that_disagree_with_their_own_rows_are_refused_not_rendered():
    import report_counts                                     # noqa: PLC0415

    summary = _summary([_row("cocotb", "DEFERRED", new_releases=1, status="measured")])
    summary["counts"]["DEFERRED"] = 7                        # headline vs table
    with pytest.raises(report_counts.CountsUnavailable):
        report_counts.verdict_counts(summary)


def test_a_missing_count_is_not_a_zero():
    import report_counts                                     # noqa: PLC0415

    summary = _summary([_row("yosys", "CLEAN", new_releases=0, status="measured")])
    del summary["counts"]["DEFERRED"]
    with pytest.raises(report_counts.CountsUnavailable):
        report_counts.verdict_counts(summary)


def test_parse_phrase_reads_back_exactly_what_phrase_wrote():
    import report_counts                                     # noqa: PLC0415

    # Built FROM the canonical list rather than spelled out, so the round trip is
    # asserted over whatever the headline currently states. A literal four-verdict
    # dict here passed only while `VERDICTS` had four entries, and went red on the
    # tick that added UNMEASURABLE — testing the fixture's age, not the property.
    counts = {v: i for i, v in enumerate(report_counts.VERDICTS)}
    assert report_counts.parse_phrase(
        f"**{report_counts.phrase(counts)}**") == counts
    # "DEFERRED 1" must never be read out of "DEFERRED 10"
    assert report_counts.parse_phrase("MERGED 0 · DEFERRED 1") is None
    assert report_counts.parse_phrase("no counts here") is None


def test_the_published_reports_on_disk_would_now_title_and_body_alike():
    """Replay the real artefacts, if this machine still has them.

    Not a fixture — the actual JSON the 05:30 tick wrote. If it is not present
    the question CANNOT BE ANSWERED here, and that is a skip with a reason, not
    a pass smuggled in as one.
    """
    import json                                              # noqa: PLC0415

    import gk_state                                          # noqa: PLC0415
    import report_counts                                     # noqa: PLC0415

    reports = gk_state.state_dir() / "reports"
    seen = 0
    for day in ("2026-08-05", "2026-08-06"):
        f = reports / f"{day}.json"
        if not f.is_file():
            continue
        seen += 1
        s = json.loads(f.read_text())
        counts = report_counts.verdict_counts(s)
        title_head = report_counts.phrase(counts, ("MERGED", "DEFERRED"))
        body_head = report_counts.phrase(counts)
        assert report_counts.parse_phrase(body_head) == counts
        assert _deferred_in(title_head) == _deferred_in(body_head) == counts["DEFERRED"], (
            f"{day}: title {title_head!r} vs body {body_head!r}")
    if not seen:
        pytest.skip("no published 2026-08-05/06 report on this machine to replay")


def test_the_worktree_is_cleaned_up_even_on_a_refusal(vibeic_clone):
    """A refusal must not leave a half-built branch behind for tomorrow's tick."""
    prn.open_pr(_mixed_deferred_summary(),
                "# a report with no headline counts at all\n")
    out = subprocess.run(["git", "-C", str(vibeic_clone), "worktree", "list"],
                         capture_output=True, text=True).stdout
    assert "gk-vibeic-pr-" not in out, out
    assert not os.path.exists(Path(os.environ.get("TMPDIR", "/tmp"))
                              / "gk-vibeic-pr-2026-08-06"), "stale worktree dir"
