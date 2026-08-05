#!/usr/bin/env python3
"""report_counts.py — the ONE derivation of a tick's verdict tallies.

WHY THIS EXISTS (vibe-ic#875, and #838 the day before it, closed with the defect
written up in prose and left in the code).

The sync PR's TITLE said `MERGED 0 · DEFERRED 3`. The body of that same PR said
`MERGED 0 · DEFERRED 10 · CLEAN 26 · NOT_LAYERED 0` over a table with ten
DEFERRED rows. One artefact, one word, two numbers.

Neither number was wrong about its own population, which is what made it
survive two ticks:

  * the title counted `len(failed)` from `pr_notify._actionable` — DEFERRED
    tools that have a MEASURED new upstream release, plus those whose gap could
    not be measured at all;
  * the body counted `summary["counts"]["DEFERRED"]` — every DEFERRED verdict.

MEASURED, not assumed. Replaying the published reports for 2026-08-05 and
2026-08-06 through both call sites gives title 3 / body 10 on both days, and
the seven rows in the gap are the same seven on both days — ALIGN-pdk-sky130,
ASAP7_for_KLayout, FasterCap, Geometry, LinAlgebra, asap7_pdk_r1p7,
asap7sc7p5t_28 — every one of them `new_releases_status == "not-probed"`, note
"no upstream release to compare against". They are DEFERRED, and there is no
release for anyone to rebase.

THE REPAIR IS NOT "MAKE THE TWO CALL SITES AGREE". Two call sites that agree
today are two call sites, and the next edit to either one re-opens the same
hole. So:

  * the number after the word DEFERRED is rendered by ONE function, from ONE
    dict, wherever it appears — the title and the body cannot state different
    values because there is only one value;
  * a count that means a DIFFERENT POPULATION gets a DIFFERENT NAME. The
    actionable subset is still worth putting in the title — it is the reason
    the PR exists — but it is labelled `actionable`, never `DEFERRED`;
  * and before it publishes, `open_pr` re-reads the headline out of the exact
    body bytes it is about to send and REFUSES if they disagree with the
    numbers it just put in the title. A body it cannot read the counts out of
    is its own outcome (refuse), never a silently-accepted publish.

Pure and stdlib-only, so the numbers a reviewer reads FIRST can be tested
without a git worktree, a network call, or a tick.
"""
from __future__ import annotations

import re

#: The verdicts a daily report tallies, in the order the headline states them.
#: Everything below is derived from this tuple — the renderer, the parser and
#: the cross-check — so the six never drift into six different lists.
#:
#: THIS IS THE ONLY COPY. `gatekeeper.VERDICTS` is bound to this same tuple, and
#: that is load-bearing rather than tidy: while it was two tuples in two modules
#: (a four-verdict list here, a six-verdict list there) the body headline
#: rendered `MERGED · DEFERRED · RESOLVED · UNMEASURABLE · CLEAN · NOT_LAYERED`
#: and `parse_phrase` — whose pattern wants DEFERRED and CLEAN adjacent — read it
#: back as None, so `open_pr` refused every tick and no sync PR was published at
#: all. Two lists that agree today are two lists.
#:
#: RESOLVED and UNMEASURABLE are in it for the reason the count exists: a row
#: that renders in the table and is tallied in none of the headline's verdicts
#: makes the headline under-account its own table (on today's corpus, 29 rows of
#: 36), which is the same class of defect as the title/body split.
VERDICTS = ("MERGED", "DEFERRED", "RESOLVED", "UNMEASURABLE", "CLEAN", "NOT_LAYERED")

_SEP = " · "

#: Reads back a headline this module rendered. Built from `VERDICTS` for the
#: same reason: a hand-written pattern is a second spelling of the format.
_PHRASE_RE = re.compile(re.escape(_SEP).join(rf"{v}\s+(\d+)" for v in VERDICTS))


class CountsUnavailable(Exception):
    """The tallies could not be established for this summary.

    Its own outcome. The caller must refuse to state a count it does not have —
    the one thing it must never do is render a zero, because nothing downstream
    can tell a measured zero from an absent measurement.
    """


def verdict_counts(summary: dict) -> dict[str, int]:
    """`{verdict: n}` for the four verdicts, or raise `CountsUnavailable`.

    Reads the tally the tick computed (`summary["counts"]`) rather than
    computing a second one — a second computation is a second answer waiting to
    happen, which is the defect this module exists for.

    It does CROSS-CHECK that tally against the rows the same report will render,
    because "the table has ten DEFERRED rows" is half of what a reader compares.
    A headline that disagrees with its own table is not a headline that can be
    fixed by agreeing with the title as well.
    """
    counts = summary.get("counts")
    if not isinstance(counts, dict):
        raise CountsUnavailable(
            f"summary has no counts dict (got {type(counts).__name__})")
    results = summary.get("results")
    if not isinstance(results, list):
        raise CountsUnavailable(
            f"summary has no results list (got {type(results).__name__}) — the "
            f"counts cannot be checked against the rows they summarise")
    out: dict[str, int] = {}
    for v in VERDICTS:
        n = counts.get(v)
        rows = sum(1 for r in results
                   if isinstance(r, dict) and r.get("verdict") == v)
        if v not in counts and rows == 0:
            # A report written before this verdict existed — every archived day
            # under reports/ predates RESOLVED and UNMEASURABLE. This is NOT the
            # "render a zero for an absent measurement" hazard the class docstring
            # forbids: the zero is READ OFF THE ROWS, which are right here and
            # carry none. A missing key with rows that DO carry the verdict falls
            # through to the disagreement below, where it belongs.
            out[v] = 0
            continue
        if not isinstance(n, int) or isinstance(n, bool):
            raise CountsUnavailable(f"counts[{v!r}] is {n!r}, not a count")
        if rows != n:
            raise CountsUnavailable(
                f"counts[{v!r}] = {n} but {rows} row(s) carry that verdict — "
                f"the headline and the table of one report disagree")
        out[v] = n
    return out


def phrase(counts: dict[str, int], verdicts: tuple[str, ...] = VERDICTS) -> str:
    """`'MERGED 0 · DEFERRED 10 · CLEAN 26 · NOT_LAYERED 0'`.

    `verdicts` narrows WHICH tallies are stated (a PR title has no room for all
    four); it can never change WHAT a stated tally is, because every number
    still comes out of the one `counts` dict through this one formatter.
    """
    missing = [v for v in verdicts if v not in counts]
    if missing:
        raise CountsUnavailable(f"no count for {', '.join(missing)}")
    return _SEP.join(f"{v} {counts[v]}" for v in verdicts)


def parse_phrase(text: str) -> dict[str, int] | None:
    """The `{verdict: n}` a rendered document states, or None if it states none.

    None means COULD NOT DETERMINE — a document whose headline this cannot read
    has not been shown to agree with anything, and the caller must treat that as
    a refusal rather than as agreement.
    """
    m = _PHRASE_RE.search(text or "")
    if not m:
        return None
    return {v: int(m.group(i + 1)) for i, v in enumerate(VERDICTS)}
