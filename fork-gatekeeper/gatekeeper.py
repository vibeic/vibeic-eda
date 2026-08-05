#!/usr/bin/env python3
"""gatekeeper.py — daily upstream-sync tick for the forked EDA tools.

Owner directives:
  · daily check ALL forks
  · track RELEASES, not every commit  → a new upstream *release* is the merge trigger
  · if we merge, BUILD A NEW vibeic-eda Docker image (option B: auto-merge on green)

Flow each day (only for the forks in FORKS.json):
  1. re-seed the ledgers from live state — the vibeic-eda Dockerfile is the source of
     truth for what we ship (ARG <TOOL>_REF pins each fork's vibeic branch, and a fork
     VENDORED as a submodule of a pinned fork ships inside that fork's ref); we compare
     the release our pin is based on against the upstream's newer releases.
  2. per fork verdict:
       NOT_LAYERED — forked but NOT REACHED BY THE IMAGE BUILD at all: no ARG pin of its
                     own and not vendored inside one, so the image uses upstream directly
                     and there is nothing to sync; informational
       CLEAN       — in the image + already on the latest upstream release; filtered out.
                     A RELEASE-level claim only: the row states the commit-level gap
                     separately, and says so when that one was not measured.
       UNMEASURABLE — the release question WAS NOT ANSWERED: containment could not be
                     decided (`unknown`), or the upstream publishes no release to
                     compare against (`not-probed`). Deliberately neither CLEAN nor
                     DEFERRED — both of those read as "measured, here is the answer".
                     Counted and named; fatal to nothing (vibeic-eda#101).
       candidate   — a newer upstream release exists → try to integrate
  3. GATE (option B): integrating a candidate = rebase our vibeic branch onto the new
     release, bump the Dockerfile ARG, and **rebuild the vibeic-eda image**. That image
     build (+ the benchmark-IC regression it runs) IS the green signal. It is wired via
     `image_build.cmd` in regression.json. Until that is configured the candidate is
     DEFERRED with the reason — never a merge/image-bump without a verified green build.
       MERGED   — image rebuilt green with the new release(s); fork branch + image pushed
       DEFERRED — new release(s) available but the build gate isn't green (reason recorded)
  4. append a sync_log entry per fork + write reports/<date>.{md,json}
  5. regenerate the vibeic.ai monitor page

    python3 gatekeeper.py            # one tick
    python3 gatekeeper.py --verify [YYYY-MM-DD]
        Re-check an ALREADY-PUBLISHED day: does the daily report state the same counts
        as the assessment filed for the same date and tool, and do both name the same
        judgement? Reads only — no upstream fetch, no assessment, no PR. Exits 1 on a
        disagreement. Run it after any manual re-assessment: re-rendering an assessment
        over its date-stamped filename leaves the report that summarised the previous
        one in place, and that pair is what vibeic/vibeic-eda#7 was filed about.

regression.json (optional): {"image_build": {"cmd": "bash build_and_regress.sh", "cwd": "…"}}
The cmd should: rebase each candidate's vibeic branch onto its new release, bump the
Dockerfile ARGs, `docker build` the image, run the benchmark-IC regression, and exit 0
ONLY if the new image is green.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent          # the (version-controlled) source location
sys.path.insert(0, str(HERE))
# Runtime state lives OUTSIDE the source tree so the checked-in copy can run in-place
# without dirtying the repo. Override with GK_STATE_DIR; defaults to the user cache.
# WHO may WRITE that shared default is gk_state's question (vibeic/vibeic-eda#12).
import gk_state  # noqa: E402

STATE = gk_state.state_dir()
LEDGER = STATE / "ledger"
REPORTS = STATE / "reports"
REG_CFG = HERE / "regression.json"    # config ships WITH the source
import discover_forks as disc  # noqa: E402
# BOUND BY NAME, not looked up on the module. The reader of `behind_releases` has
# to survive the test fixtures that replace `gk.disc` wholesale to keep
# `disc.main()` off the network — a stub that neutralises the SEEDER must not
# also silently remove the one function that keeps an unmeasured gap from
# reading as zero.
from discover_forks import release_gap, release_gap_status, release_gap_unknown  # noqa: E402
from discover_forks import commit_gap, commit_gap_status  # noqa: E402
import build_page  # noqa: E402
import fleet_config  # noqa: E402  — is the configuration we ran on the committed one?
# HARD import, deliberately: this is the one derivation of the verdict tallies the
# report and the PR title both state (vibe-ic#875). A soft `try/except → None`
# fallback would be a second, unpoliced way to render those numbers, which is the
# defect itself.
import report_counts  # noqa: E402
try:
    import pr_notify  # opens a vibe-ic PR on actionable ticks (replaced email)
except Exception:  # noqa: BLE001
    pr_notify = None
try:
    import assess_release  # selective-merge assessment engine (per-commit triage)
except Exception:  # noqa: BLE001
    assess_release = None


#: Every verdict this report can publish, in the order a reader wants them.
#:
#: UNMEASURABLE is the one added by vibeic-eda#101, and it is NOT a shade of
#: DEFERRED. DEFERRED says "we looked, there is outstanding work, it is not done
#: yet"; UNMEASURABLE says "the question was not answered", which is a different
#: instruction to whoever reads the row. Folding the second into the first (or,
#: worse, into CLEAN) publishes a measurement nobody made — and both plausible
#: foldings are wrong in the same direction, because both read as "measured, here
#: is the answer".
#:
#: RESOLVED has been produced by `assessment_entry` since #369 and was in no
#: count and in no sort order; a row that verdict landed on was invisible in the
#: headline and sorted last by accident. Listing the verdicts in ONE place is
#: what stops the next one being added the same way.
#:
#: UNMEASURABLE does NOT fail the round, and that is a decision rather than an
#: oversight: seven of today's rows are unmeasurable because their upstream
#: publishes no release at all, a state nobody can act on and which will never
#: clear, and a permanently red round is one people route around. The contract is
#: COUNTED in the headline, NAMED in its own row with the reason on it, and
#: escalated to a human ONLY when the sub-status is `unknown` — "we asked and
#: could not decide". `_maybe_notify` and `pr_notify._actionable` are where that
#: last clause lives; there is no flag for it, because a constant nothing reads is
#: a comment wearing code's clothes.
#:
#: BOUND, not re-declared. `report_counts` renders the headline, parses it back
#: and cross-checks it against the rows; if this were a second tuple that happened
#: to list the same verdicts, the two would drift and the drift is silent. It was
#: literally two tuples for one integration round: a six-verdict list here and a
#: four-verdict list there rendered a headline `parse_phrase` could not read, and
#: `open_pr` then refused every tick — the sync PR stopped being published, from
#: two lists that each looked right in its own file.
VERDICTS = report_counts.VERDICTS


class CountsDisagree(RuntimeError):
    """Two documents of one tick state different counts for the same assessment.

    vibeic/vibeic-eda#7. Raised INSTEAD of publishing either of them. The tick exits
    non-zero, cron.log records it, and the day's report is absent rather than wrong —
    an operator who finds no report goes looking; one who finds two contradicting
    numbers stops reading the report at all.
    """


def _now_date() -> str:
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _image_build_cfg() -> dict | None:
    if not REG_CFG.is_file():
        return None
    try:
        return json.loads(REG_CFG.read_text()).get("image_build")
    except (OSError, json.JSONDecodeError):
        return None


def _run_harness(cfg: dict, candidates: list[dict]) -> dict:
    """Run the integration harness (rebase → build → smoke → gated promote). Returns a
    per-candidate {tool: {status, detail, sha}} map read from GK_RESULT."""
    import os as _os
    cmd, cwd = cfg.get("cmd"), cfg.get("cwd") or str(HERE)   # run the harness from the source dir
    result_path = cfg.get("result", str(STATE / "last_build_result.json"))
    if not cmd:
        return {}
    env = {**_os.environ,
           "GK_RESULT": result_path,
           "GK_STATE_DIR": str(STATE),
           "GK_MODE": _os.environ.get("GK_MODE", cfg.get("mode", "verify")),
           "VIBEIC_CANDIDATES": json.dumps(
               [{"tool": c["tool"], "arg": c.get("dockerfile_arg"), "branch": c.get("vibeic_branch"),
                 "release": c.get("upstream_latest_release"), "upstream": c.get("upstream")}
                for c in candidates])}
    # THE OTHER `shell=True` IN THIS TREE, and the one whose command is not even
    # written here — `cmd` comes from `regression.json` and today is
    # `bash build_and_regress.sh`, but the field takes any shell text, pipelines
    # included. A pipeline's exit status is its LAST command's, so under the
    # default `/bin/sh` a producer that failed inside it would be invisible; that
    # is the defect round 5 measured in `discover_forks._patch_id_set`, and the
    # fact that this site does not read the status at all makes it worse rather
    # than safer. `bash -o pipefail -c` is used instead of `shell=True` because
    # `/bin/sh` here is `dash` and `pipefail` is not POSIX, and the status is now
    # recorded on every candidate so a harness that fails without writing
    # GK_RESULT stops being indistinguishable from one that never ran.
    argv = ["/bin/bash", "-o", "pipefail", "-c", cmd]
    try:
        r = subprocess.run(argv, cwd=cwd, timeout=cfg.get("timeout", 21600), env=env)
    except subprocess.TimeoutExpired:
        return {c["tool"]: {"status": "timeout", "detail": "harness timed out"} for c in candidates}
    except OSError as e:
        return {c["tool"]: {"status": "harness_error",
                            "detail": f"the harness could not be started: {e}"}
                for c in candidates}
    try:
        arr = json.loads(Path(result_path).read_text())
        return {r_["tool"]: r_ for r_ in arr}
    except (OSError, json.JSONDecodeError):
        if r.returncode != 0:
            return {c["tool"]: {"status": "harness_error",
                                "detail": f"the harness exited {r.returncode} and wrote no "
                                          f"{result_path}"} for c in candidates}
        return {}


def assessment_entry(rep: dict, nr: int | str, latest) -> dict:
    # `nr` may be the STRING the caller renders for an unmeasured release gap
    # ("an undetermined number of"). It is only ever interpolated into a
    # human-facing note here, never counted, so both shapes are correct — and a
    # note that says "0 new release(s)" beside a DEFERRED verdict is not.
    """The daily report's row for a fork that was ASSESSED — verdict, note, counts.

    A pure function of the assessment report so the sync-log summary can be exercised
    without a tick, a ledger, or the network. It used to be an inline branch of `tick()`,
    which is why the tests that were supposed to pin its arithmetic asserted on the
    SOURCE TEXT of this file instead of on what it computes — and a test that greps for
    `cc - safe` stays green while the number it produces is wrong, and goes red on a
    refactor that changes nothing.
    """
    if rep.get("error"):
        return {"verdict": "DEFERRED",
                "note": f"{nr} new release(s) → {latest}; assessment error: {rep['error']}"}
    # THE PIN IS AHEAD OF THE TAG. Its own state, and the reason it is one: this row used
    # to read "N upstream commit(s) <base> → <latest>" for a `latest` that is BEHIND the
    # ref we ship — Trilinos published exactly that on three consecutive days, proposing
    # a move onto a tag 407 commits behind `TRILINOS_REF`. The number of releases and the
    # tag name are still stated, because upstream really does hold work we lack; what is
    # withdrawn is the claim that the tag is somewhere we can go.
    if rep.get("status") == "pin_ahead_of_release":
        d = rep.get("target_direction") or {}
        return {"verdict": "DEFERRED",
                "target_refused": d.get("target"),
                "note": (f"{nr} new release(s), but the newest ({d.get('target')}) is NOT a "
                         f"descendant of the ref we ship (`{d.get('pin')}`) — our pin is "
                         f"{d.get('pin_ahead')} commit(s) ahead of it and it carries "
                         f"{d.get('target_ahead')} we lack, on a line we cannot move onto "
                         f"without dropping the rest, so advancing to it would be a "
                         f"DOWNGRADE — "
                         f"{assess_release.no_forward_range_phrase(rep)}. NOTHING is "
                         f"proposed: adopting that work is a cherry-pick decision, not a "
                         f"release this fork can be advanced to")}
    # CONSUME the assessment's own classification — via the ONE derivation in
    # assess_release, never a private re-derivation. This block used to compute "needs
    # human" for itself as `commit_count - clearly_safe`, which silently discards the two
    # categories the assessment already resolved: commits our ref CARRIES (by ancestry or
    # cherry-pick patch-id) and commits with a RECORDED decision. Measured on magic
    # 8.3.674 → 8.3.676: 2 carried + 1 recorded skip = nothing outstanding, yet this note
    # reported "3 need human review" and the range was re-proposed on 07-23, 07-24, 07-26.
    #
    # Reading `outstanding` when present repaired the common case and left the split in
    # place (vibeic/vibeic-eda#7): the fallback here subtracted only `safe`, the one in
    # render_md subtracted `safe + carried + decided`, and pr_notify's never looked at
    # `outstanding` at all — so one tick could publish three different answers.
    # `summary_counts` is now the only place the question is answered, and it reaches
    # arithmetic only when neither the summary list nor the per-commit rows exist.
    n = assess_release.summary_counts(rep)
    cc, safe = n["commits"], n["clearly_safe"]
    carried, decided, n_open = n["carried"], n["decided"], n["outstanding"]
    # Commits the AI judge never reached a conclusion about. The operator must be told
    # the analysis did not run, rather than reading its absence as a triage result — see
    # the 2026-07-28 magic assessment, where a truncated judge reply published 105 rows
    # of fabricated "high risk".
    n_na = n["not_assessed"]
    # vibeic/vibeic-eda#5: adopt-candidates the model called relevant that nothing we run
    # can reach. DISCLOSED, never resolved away — the verdict may still be right, but its
    # stated evidence is not true of our fork.
    n_unreach = n["unreachable"]
    # vibeic/vibeic-eda#6: adopt-candidates whose judgement did not reproduce across
    # independent samples. DISCLOSED, never averaged into a majority.
    n_unconf = n["unconfirmed"]
    entry = {"verdict": "DEFERRED",
             "assessed": {"commits": cc, "clearly_safe": safe,
                          "carried": carried, "decided": decided,
                          "outstanding": n_open, "not_assessed": n_na,
                          "unreachable": n_unreach, "unconfirmed": n_unconf,
                          # WHICH assessment this report summarises. Without it the daily
                          # report and the assessment filed under the same date are
                          # indistinguishable when they are two different vintages — the
                          # 2026-07-28 pair was exactly that, and nothing on either
                          # document said so. `verify_documents` compares these stamps.
                          "assessor": rep.get("assessor"),
                          "assessed_at": rep.get("assessed_at"),
                          "replayed": bool(rep.get("cached")),
                          "derived": n["derived"],
                          # The self-check, carried in the STRUCTURED row as well as the
                          # note. 0 means the four buckets account for the range; any
                          # other value means they do not, and a downstream reader of
                          # the JSON must be able to tell without re-adding the note.
                          "unaccounted": n["unaccounted"],
                          # …and which stored summary list its own rows contradicted.
                          "stale_lists": n["stale_lists"]}}
    resolved = f"{carried} already carried, {decided} previously decided"
    # NAME THE RANGE THAT WAS ASSESSED, not the one the ledger's tag suggests. This read
    # `rep['base_release'] → latest`, pairing the report's own base with the CALLER's
    # newest-tag argument — two ends of two different ranges whenever `assess()` falls
    # back off the release range, which it does for every fork whose upstream ships from
    # rolling master and now for every refused-target fork as well. `rep['latest']` is
    # the end `rep['base_release']` was measured to; `latest` stays as the fallback for
    # a stub report that states no range of its own.
    span = f"{rep.get('base_release')} → {rep.get('latest') or latest}"
    entry["note"] = (f"{cc} upstream commit(s) {span}: "
                     f"{safe} clearly-safe, {resolved}, {n_open} need human review — "
                     f"selective-merge assessment filed (not auto-merged)"
                     f"{assess_release.direction_note(rep)}")
    if n_na:
        entry["note"] += (f" — WARNING: the AI judge did not complete, {n_na} "
                          f"commit(s) NOT ASSESSED (no classification made)")
    if n_unreach:
        entry["note"] += (f" — {n_unreach} commit(s) the judge called relevant are "
                          f"UNREACHABLE from any command our emitters issue "
                          f"(model/surface disagreement → human decision, not "
                          f"auto-proposed)")
    if n_unconf:
        entry["note"] += (
            f" — {n_unconf} commit(s) cleared every other auto-adopt condition but "
            f"their JUDGEMENT DID NOT REPRODUCE across independent samples "
            f"(every reading is on the row → human decision, not auto-proposed)")
    # THE SELF-CHECK. `clearly_safe + carried + decided + outstanding` is one `decision`
    # column sliced four ways, so it IS `commit_count`; any other value means the
    # derivation lost commits. It is stated here rather than only in `counts_conflict`
    # because this note is the sentence that carried the disproof in the first place:
    # "64 upstream commit(s) … 3 clearly-safe, 0 already carried, 0 previously decided,
    # 17 need human review" published 20 of 64 for two days, in a line that showed both
    # halves. `assess_release.cross_check` refuses to publish it; this makes the refusal
    # legible to anyone holding the row.
    if n["unaccounted"]:
        entry["note"] += (
            f" — ⚠ THESE COUNTS DO NOT ADD UP: {safe} + {carried} + {decided} + "
            f"{n_open} = {safe + carried + decided + n_open}, against the {cc} commit(s) "
            f"this same line states. A DERIVATION FAILURE, not a triage result — do not "
            f"read these numbers as the state of the range")
    if n_open == 0 and safe == 0 and not n["unaccounted"]:
        # Nothing is outstanding: reporting DEFERRED here is what turned settled work
        # into a recurring proposal.
        #
        # `not n["unaccounted"]` guards the same claim from the opposite side. This
        # branch says the range is SETTLED and drops the tool out of triage, and it is
        # reached on `outstanding == 0` — precisely the value an under-count produces. On
        # 2026-08-04 slang published "needs human decision: 0" over one commit marked
        # `human`: an understated count anywhere else costs a reader accuracy, here it
        # costs them the tool. A total nothing accounts for may not conclude anything.
        entry["verdict"] = "RESOLVED"
        entry["note"] = (f"{cc} upstream commit(s) {span}: {resolved} — nothing "
                         f"outstanding{assess_release.direction_note(rep)}")
    # AFTER the RESOLVED branch, which REPLACES the note: a stale stored list can coexist
    # with a correct re-derived zero, and dropping the disclosure exactly in the branch
    # that takes the tool out of triage is where it would cost the most. Nothing here is
    # wrong — the cache is — and that has to travel with the row either way.
    if n["stale_lists"]:
        entry["note"] += (
            f" — ⚠ the counts above were RE-DERIVED from this report's own rows because "
            f"a stored summary list contradicted them ({'; '.join(n['stale_lists'])}); "
            f"the cache still holds the old answer and will replay it until this range "
            f"is re-judged")
    return entry


def pin_provenance(led: dict) -> str:
    """The clause a report row adds when the pin is INDIRECT; "" for a direct ARG.

    vibeic/vibeic-eda#8. "pinned via `OPENROAD_REF` (`src/sta`)" and "pinned via
    `OPENSTA_REF`" are the same fact only to a reader who does not have to act on it: a
    change to the vendored copy is shipped by rebuilding the HOST, not this tool. An
    operator reading the row to decide what to rebuild has to be told which, and a row
    that says nothing is read as the ordinary case — its own ARG.
    """
    host, path = led.get("vendored_in"), led.get("vendored_path")
    if not (host and path):
        return ""
    return (f" — pinned via `{led.get('dockerfile_arg') or '?'}` (`{path}` in "
            f"vibeic/{host}), not an ARG of its own: changing it means rebuilding {host}")


def _undetermined_note(led: dict) -> str:
    """The clause a row adds when some release's CONTAINMENT could not be decided.

    A count nobody could measure must not be published as a count, and it must not
    be published as silence either. This names the releases and the literal error
    that stopped each one, so the reader's next move is a command, not a guess.
    """
    und = led.get("undetermined_releases") or []
    if not und:
        return ""
    shown = ", ".join(f"{u.get('tag')} ({u.get('error') or 'undetermined'})"
                      for u in und[:3] if isinstance(u, dict))
    more = f" +{len(und) - 3} more" if len(und) > 3 else ""
    return (f" — WARNING: containment is UNDETERMINED for {len(und)} upstream "
            f"release(s), so the release gap is unknown rather than {len(led.get('new_releases') or [])}: "
            f"{shown}{more}")


def commit_level_note(led: dict) -> str:
    """The clause that states the COMMIT-level answer on a row whose RELEASE-level
    answer does not exist (vibeic-eda#101).

    The two questions are independent and the report has one verdict column, so a
    row that can only answer one of them has to say WHICH — otherwise UNMEASURABLE
    reads as "nothing at all is known", which is its own overstatement. Measured on
    today's corpus: FasterCap's upstream publishes no release (release gap NOT
    PROBED, so the verdict is UNMEASURABLE) while its commit gap is a real,
    clone-measured 0. "We know nothing about FasterCap" would be as false as
    "FasterCap is CLEAN".

    Unlike `unassessed_drift` this states the answer in ALL THREE states, including
    a measured zero: on a row whose headline is "not measured", the one thing a
    reader must not have to infer is which half was.
    """
    st = commit_gap_status(led)
    branch = led.get("upstream_default_branch") or "the default branch"
    if st == "not-probed":
        return (" — the COMMIT-level gap has no subject either: nothing pins this "
                "tool, so there is no ref to compare from")
    if st == "unknown":
        return (f" — and the COMMIT-level gap was NOT MEASURED either: no compare "
                f"against {branch} answered")
    n = commit_gap(led)
    if not n:
        return (f" — the COMMIT-level gap IS measured and is 0: our pinned ref "
                f"carries all of {branch}")
    return (f" — the COMMIT-level gap IS measured: {n} upstream commit(s) on "
            f"{branch} are not in our pinned ref")


def unassessed_drift(led: dict) -> str:
    """What a CLEAN row does not say: upstream commits our pinned ref does not carry.

    CLEAN answers the RELEASE question, and the row used to stop there — so a fork whose
    upstream has cut no release since 2020 reads as "nothing to do" however far its
    default branch has moved. `behind_commits` measures that distance on every tick and
    was published nowhere (vibeic/vibeic-eda#8, where the complaint that a 48-commit gap
    "has never been triaged" turned out to be true of every CLEAN fork, not only the
    misclassified one).

    SUPERSEDED IN PART (owner ruling, 2026-07-29): the directive is now "daily merge
    all new commits from upstream for forked tools", so commit drift IS acted on and
    a fork behind by commits alone is a candidate. This disclosure stays because it
    is still the honest sentence for a row whose merge has not run yet, and because
    it is what surfaced the gap in the first place. The line above it used to read
    "the owner's directive is to track releases"; that is no longer true and saying
    so here matters more than the two words it costs.

    THREE STATES, since vibeic-eda#101. `led.get("behind_commits") or 0` was still
    here, and it is the `or 0` this campaign exists to remove: a commit gap that
    COULD NOT BE MEASURED took the same branch as one measured at zero, and the
    row said nothing at all. CLEAN then reads as "nothing to do" on a fork whose
    commit-level state nobody established — the exact sentence vibeic-eda#101 was
    filed about, one function below the verdict it blamed.

    So: a measured 0 stays silent (there is genuinely no drift to disclose), and
    an ABSENT measurement says so, in its own words, on a row that is otherwise
    entirely reassuring.
    """
    if commit_gap_status(led) == "not-probed":
        # No pinned ref, so there is nothing to compare FROM — a different claim
        # from "the compare did not answer", and not one this clause is about.
        # A row in that state normally reaches NOT_LAYERED rather than CLEAN
        # (`integrated` and `pinned_ref_full` are written together), so this is
        # defensive against a hand-written or pre-`pinned_ref_full` ledger; the
        # point is that it must not borrow the "NOT MEASURED" sentence below,
        # which would name a failed measurement that never started.
        return ""
    n = commit_gap(led)
    if n is None:
        return (" — and the COMMIT-LEVEL gap was NOT MEASURED: this row states we "
                "are on the newest upstream RELEASE, it does NOT state that our "
                "pinned ref carries "
                f"{led.get('upstream_default_branch') or 'the default branch'}. "
                "Read it as release-current and commit-level UNKNOWN, not as "
                "nothing-to-do")
    if not n:
        return ""
    return (f" — {n} upstream commit(s) on "
            f"{led.get('upstream_default_branch') or 'the default branch'} are not in our "
            f"pinned ref; no new release, so release-tracking does not assess them")


def tick() -> dict:
    # ── MAY THIS PROCESS PUBLISH AT ALL? (vibeic/vibeic-eda#12) ──────────────
    # Ordered ahead of the configuration gate deliberately. #10 asks "is the fleet list
    # this tick runs on the committed one" — a question about the tick's INPUT. This one
    # asks whether the tick is entitled to overwrite the ledgers, reports and cache the
    # cron reads at all, and it is answerable with no filesystem and no network. A tick
    # that may not publish must not spend a fleet-wide discovery, nor a judge call, to
    # discover that at the write.
    gk_state.require_writable(STATE, "the gatekeeper's ledgers and reports")

    # ── CONFIGURATION GATE (vibeic/vibeic-eda#10) ────────────────────────────
    # Checked FIRST, before a single upstream call: a tick running on a fleet list that
    # exists in no commit must not spend the API budget of a fleet-wide discovery to
    # produce a report nobody can reproduce. See fleet_config for the severity split —
    # a configuration that differs only in formatting, or that cannot be checked at all,
    # warns and stamps rather than refusing, because a check that fires on states the
    # operator considers normal is a check that gets commented out.
    fleet = fleet_config.check()
    for line in fleet.get("detail") or []:
        print(f"  [fleet-config] {line}")
    for name, st in fleet["files"].items():
        if name != fleet_config.FLEET_FILE and st["state"] != "committed":
            # Reported, not fatal: ENHANCEMENTS.json feeds the published monitor page's
            # counts, so drift there mis-states a public page — but it reaches no verdict
            # in this report, and stopping the audit over it would trade a wrong number
            # on a web page for no audit at all.
            print(f"  [fleet-config] {name} is {st['state']} — the monitor page's counts "
                  f"come from a file that is not the committed one")
    if fleet["fatal"]:
        for f in fleet["fatal"]:
            print(f"  [FATAL] {f}")
        raise fleet_config.FleetConfigUnversioned(
            "; ".join(fleet["fatal"]) +
            f" — nothing published. Commit it, or set {fleet_config.OVERRIDE_ENV}=1 to "
            f"publish anyway (the report then says so).")
    if fleet["override"]:
        print(f"  [fleet-config] publishing on an UNCOMMITTED fleet list because "
              f"{fleet_config.OVERRIDE_ENV} is set; the report records it")

    print(f"[{_now_iso()}] gatekeeper tick — re-seeding ledgers…")
    disc.main()
    date = _now_date()
    cfg = _image_build_cfg()

    leds = {}
    candidates = []
    for p in sorted(LEDGER.glob("*.json")):
        if p.name == "index.json":
            continue
        led = json.loads(p.read_text())
        leds[p] = led
        # OWNER RULING (2026-07-29): "daily merge all new commits from upstream
        # for forked tools." This supersedes the release-tracking-only doctrine
        # that `unassessed_drift` below still describes as the owner's directive
        # — that docstring predates this ruling and is corrected there.
        #
        # The old gate read `behind_releases` alone, and the projects that matter
        # most do not tag releases: OpenROAD's tags stop at v2.0, and yosys /
        # verilator / iverilog / ngspice / cocotb / pyuvm / sby just move master.
        # Their `behind_releases` is permanently 0, so they were NEVER
        # candidates — not "assessed and skipped", never entered. Measured the
        # day this changed: 2 candidates out of 21 forks, while the never-entered
        # ones were 1065 commits behind between them, 87% of the whole gap.
        #
        # That is how vibe-ic#551 happened: upstream fixed an `rsz::stitchTrees`
        # segfault on 2026-07-13 and nothing here could see it, because OpenROAD
        # could not become a candidate. `behind_commits` was already computed and
        # stored by discover_forks — the number sat in the ledger, unread by the
        # one condition that decides whether anyone looks.
        #
        # …and an UNDETERMINED release gap enters too. `behind_releases` is null
        # when containment could not be decided for some upstream release, and
        # `or 0` reads that as "level with upstream" — the same silence that kept
        # OpenROAD out, arrived at by a different route.
        #
        # `release_gap` rather than the raw field: `or 0` on a null is the
        # coercion this module exists to remove, and it was still here.
        _gap = release_gap(led)
        behind = ((_gap is not None and _gap > 0)
                  or (led.get("behind_commits") or 0) > 0
                  or release_gap_unknown(led))
        # …and "behind" has to MEAN something for this tool. A CONTENTS ASSERTION
        # is not a build input: the artefact is prebuilt, nothing fetches at the
        # sha, and the build only refuses to ship if the artefact disagrees with
        # it. `behind_commits` is still computed for such a row and is still a
        # true statement about the two git histories — it is simply not a
        # statement about anything this round can act on. Entering it as a merge
        # candidate is what produced vibeic-eda#74 and #78: two proposals to
        # advance `open_pdks`, both refused by the build guard, because advancing
        # it rebuilds nothing and turns a true statement false. #79 made the
        # distinction machine-visible; this is the reader that acts on it.
        if led.get("pin_kind") == "contents_assertion":
            if behind:
                print(f"  [not a candidate] {led.get('tool')}: pinned by "
                      f"`{led.get('dockerfile_arg')}`, a CONTENTS ASSERTION "
                      f"about a prebuilt artefact. Its {led.get('behind_commits')} "
                      f"commit(s) behind upstream are real and unactionable here "
                      f"— advancing the ARG rebuilds nothing (vibeic-eda#79). "
                      f"Adopting a newer upstream means CUTTING A NEW ARTEFACT, "
                      f"which is a decision, not a merge round.")
            continue
        if led.get("integrated") and behind:
            candidates.append(led)

    # A row the fleet list does not authorise (vibeic/vibeic-eda#10). Nothing prunes the
    # ledger directory, so a fork dropped from the list keeps publishing the last verdict
    # anyone computed for it — and a report that names its fleet list in the header while
    # carrying rows from outside it is a worse lie than one that names nothing. Not
    # repaired automatically: deleting a ledger discards its sync_log, which is the audit
    # trail, and a tick that edits its own inputs is the shape #10 forbids.
    phantom = fleet_config.phantom_rows(led.get("tool") for led in leds.values())
    if phantom:
        for t in phantom:
            print(f"  [FATAL] {t}: a ledger is about to publish a row for a fork "
                  f"{fleet_config.FLEET_FILE} does not name")
        raise fleet_config.FleetConfigUnversioned(
            f"{len(phantom)} ledger(s) not named by {fleet_config.FLEET_FILE} "
            f"({', '.join(phantom)}) — nothing published. Either restore the entries to "
            f"the fleet list, or remove the stale ledger(s) from {LEDGER}.")

    # SELECTIVE-MERGE doctrine (owner 2026-07-17): a behind fork gets ASSESSED, not blindly
    # rebased. Per candidate, enumerate the upstream commits and triage each (category /
    # relevance / risk / conflict-with-our-patches / clean-cherry-pick / reproduce plan);
    # the clearly-safe subset is flagged, everything else is a human decision. Assessments
    # are filed as a vibe-ic review PR (see _maybe_notify).
    assessments = {}   # tool -> assessment report
    rendered = {}      # tool -> the assessment markdown, rendered ONCE (vibeic/vibeic-eda#7)
    if candidates and assess_release is not None:
        for led in candidates:
            try:
                rep = assess_release.assess(led["tool"])
                assessments[led["tool"]] = rep
                # Rendered here, PUBLISHED later — the assessment file, the daily report
                # and the review PR must be one atomic publication, so nothing is written
                # until every document has been checked against the others. Rendering
                # once (rather than again per consumer) is the other half: two renders of
                # "the" assessment are two chances to render two different reports.
                rendered[led["tool"]] = assess_release.render_md(rep)
            except Exception as e:  # noqa: BLE001 — assessment must never break the tick
                print(f"  [assess] {led['tool']} error (ignored): {e}")

    # An assessment replayed from cache (identical tool + upstream range + carried-patch
    # ref) is NOT news: we already filed its review PR and already offered its clearly-safe
    # set on the tick that first produced it. Re-filing would reopen the same PR every day.
    # Only FRESH assessments drive PR-opening; cached ones still show in the report/summary.
    # A None assessment is a HOLE, not a fresh one. `assess()` can return None —
    # a stubbed assessor in a test, a harness that timed out, an assessor that
    # raised into the `except` above after the key was set. Treating it as fresh
    # crashes here; treating it as cached hides it. It is dropped from BOTH sets
    # and named, so a tool nobody assessed cannot be read as a tool with nothing
    # to assess. Surfaced when the owner ruling of 2026-07-29 widened the
    # candidate gate from 2 forks to 10 and the extra eight walked into it.
    unassessed = sorted(t for t, r in assessments.items() if not isinstance(r, dict))
    for t in unassessed:
        print(f"  [assess] {t:16} NOT ASSESSED — the assessor returned nothing; "
              f"this is a gap in the measurement, not a clean fork")
        assessments.pop(t, None)

    fresh = {t: r for t, r in assessments.items() if not r.get("cached")}
    for t in sorted(set(assessments) - set(fresh)):
        a = assessments[t]
        print(f"  [assess] {t:16} unchanged range AND unchanged assessor "
              f"({a.get('assessor') or '?'}) — replayed from cache "
              f"(computed {a.get('assessed_at') or 'earlier'}), no new PR")
    # vibeic/vibeic-eda#4: the cache key now identifies the ASSESSOR as well as the
    # input, so editing the judge re-judges every cached range on the next tick. That
    # is the correct behaviour and it costs real API calls — say WHY, or it reads as an
    # unexplained spike and gets "fixed" by reverting the invalidation.
    for t, r in sorted(assessments.items()):
        if r.get("reassessed_because"):
            print(f"  [assess] {t:16} RE-JUDGED (cache intentionally missed): "
                  f"{r['reassessed_because']}")

    # Phase 3: open a cherry-pick MERGE PR (deterministic; holds token) for the clearly-safe
    # commits — real upstream commits, human-reviewed, never auto-merged, never force-push.
    # Gated on GK_MERGE_PR, which run_tick.sh (the cron entrypoint) defaults to 1 = ARMED.
    # Never breaks the tick.
    if fresh and os.environ.get("GK_MERGE_PR") in ("1", "true", "yes"):
        try:
            import prepare_merge_pr
            for r in prepare_merge_pr.prepare(fresh, date):
                print(f"  [merge-pr] {r.get('tool'):16} {r.get('status')} "
                      f"{r.get('url') or r.get('note') or ''}"[:110])
        except Exception as e:  # noqa: BLE001
            print(f"  [merge-pr] error (ignored): {e}")

    # LEGACY blind-rebase harness (rebase our branch onto the WHOLE release + build). Retired
    # as the default by the selective-merge pivot; kept for reference, runs ONLY if explicitly
    # re-enabled (GK_RUN_HARNESS=1) — the multi-hour docker rebuild is not run just to canary.
    hres = {}          # tool -> {status, detail, sha}
    not_configured = ("new upstream release(s) available; auto-merge (option B) rebuilds the "
                      "vibeic-eda image as the green gate, but image_build.cmd is not configured.")
    if candidates and cfg and os.environ.get("GK_RUN_HARNESS"):
        # A VENDORED fork is never handed to this harness: its whole operation is "rebase
        # the branch, bump `<TOOL>_REF`, rebuild", and a vendored fork's ARG belongs to
        # its HOST — bumping it to this tool's release sha would repoint the host at the
        # wrong repository. Such a candidate falls through to the selective-merge
        # assessment path instead, which proposes a cherry-pick PR on the fork itself.
        hres = _run_harness(cfg, [c for c in candidates if not c.get("vendored_in")])

    results = []
    conflicts: list[str] = []    # cross-document count disagreements (vibeic/vibeic-eda#7)
    pending_ledgers: list[tuple] = []   # written only once the documents agree
    for p, led in leds.items():
        tool = led["tool"]
        # `nr` is a NUMBER OF RELEASES, and it may not exist. `rel_unknown` says
        # the ledger could not decide containment for at least one of them, in
        # which case `nr` is 0 only because arithmetic needs something — every
        # place that shows it to a human branches on `rel_unknown` first, and the
        # row carries `new_releases_status` so a reader of the JSON can too.
        rel_unknown = release_gap_unknown(led)
        rel_status = release_gap_status(led)
        # `nr` IS None when there is no number — a null travels as a null, and the
        # status beside it says which of the three claims the row is making.
        nr = release_gap(led)
        nr_txt = ("an undetermined number of" if rel_unknown
                  else "no upstream release to compare against, so no" if nr is None
                  else str(nr))
        latest = led.get("upstream_latest_release")
        entry = {"date": date, "verdict": None, "note": "", "new_releases": nr,
                 "new_releases_status": rel_status,
                 # THE SECOND QUESTION, on the row (vibeic-eda#101). "Are we on the
                 # newest tag" and "does our pin carry upstream's branch" are two
                 # measurements with two answers, and the report had one verdict
                 # column to say both in. Carried as its own pair of fields so a
                 # row can be release-current and commit-level-unknown at once —
                 # and so the table can render them side by side instead of
                 # leaving the reader to find one of them in prose, and only when
                 # it happened to be non-zero.
                 "behind_commits": commit_gap(led),
                 "behind_commits_status": commit_gap_status(led),
                 "latest_release": latest, "merged_release": None}
        cross_checked = None

        if not led.get("integrated"):
            entry["verdict"] = "NOT_LAYERED"
            # `integrated = bool(ref)` means "the pin resolver found an
            # `ARG <TOOL>_REF`". It does NOT mean the tool is absent from
            # the image, and this note used to assert exactly that
            # (vibeic-eda#32). Five of the six tools in this state WERE in
            # the image: ciel (whose managed store both sign-off PDKs
            # symlink into), open_pdks, IHP-Open-PDK, ASAP7_for_KLayout,
            # and OpenSTA, a submodule of the OpenROAD pin. Four arrive
            # from the base image and one we stage ourselves; the resolver
            # models neither route.
            #
            # It now states what is KNOWN and names the consequence, not a
            # fact about the image that nothing checked.
            # `tools/check_fork_presence_claims.py` tests the stronger
            # claim against the image on every tick.
            entry["note"] = ("no `ARG <TOOL>_REF` pin found, so its "
                             "delivery route is unmodelled and no upstream "
                             "range is assessed — this does NOT establish "
                             "that the tool is absent from the image")
        elif nr == 0 and not rel_unknown:
            entry["verdict"] = "CLEAN"
            entry["note"] = (f"on the latest upstream release "
                             f"({led.get('base_release') or led.get('pinned_ref')})"
                             f"{unassessed_drift(led)}")
        elif tool in hres:
            st = hres[tool]
            s, detail = st.get("status", "?"), st.get("detail", "")
            if s == "promoted":
                entry["verdict"], entry["merged_release"] = "MERGED", latest
                entry["note"] = f"integrated {latest} + image pushed: {detail}"
            elif s == "promote_failed":
                entry["verdict"] = "DEFERRED"
                entry["note"] = f"promote attempted for {latest} but FAILED (nothing shipped) — {detail}"
            elif s == "built_green":
                entry["verdict"] = "DEFERRED"
                entry["note"] = (f"rebased onto {latest} + image build VERIFIED GREEN — enable "
                                 f"GK_MODE=promote to auto-merge + push. {detail}")
            else:  # rebase_conflict / tag_missing / built_red / worktree_fail / no_clone / no_vibeic_branch
                entry["verdict"] = "DEFERRED"
                entry["note"] = f"{s} → target {latest}: {detail}"
        elif tool in assessments:
            # `nr_txt`, not `nr`: the one place `assessment_entry` shows this
            # number is a human-facing note, and "0 new release(s)" next to a
            # DEFERRED verdict is precisely the contradiction an unmeasured gap
            # produces. It still accepts a plain int — that is what its tests pass.
            entry.update(assessment_entry(assessments[tool], nr_txt, latest))
            cross_checked = tool
        elif nr is None:
            # THE RELEASE QUESTION HAS NO ANSWER, and nothing above supplied one
            # (vibeic-eda#101). Reached only after the harness and the assessment
            # have both declined the row, so a fork whose commits WERE triaged
            # keeps its DEFERRED and its triage — this branch is for the rows
            # where no measurement of any kind exists to defer ON.
            #
            # Both of the verdicts this used to fall through to are wrong in the
            # SAME direction: CLEAN and DEFERRED each read as "measured, here is
            # the answer". Seven rows landed on DEFERRED with the note "harness
            # returned no result for this tool" — blaming the harness for a
            # question that was never askable, because their upstreams publish no
            # release at all.
            entry["verdict"] = "UNMEASURABLE"
            entry["note"] = (
                "the RELEASE gap is " + ("UNKNOWN — we compared and could not "
                                         "decide containment for at least one "
                                         "upstream release"
                                         if rel_unknown else
                                         "NOT PROBED — this question has no "
                                         "subject: the upstream publishes no "
                                         "release or tag to compare against") +
                f", so no verdict about upstream releases is available for this "
                f"tool. This is NOT 'nothing to do' and it is NOT a deferral"
                f"{_undetermined_note(led)}{commit_level_note(led)}")
        elif not cfg:
            entry["verdict"] = "DEFERRED"
            rels = ", ".join(r.get("tag") for r in (led.get("new_releases") or [])[:5] if r.get("tag"))
            entry["note"] = (f"{nr_txt} new upstream release(s) [{rels}] → target {latest}."
                             f"{_undetermined_note(led)} {not_configured}")
        else:
            entry["verdict"] = "DEFERRED"
            # WHY nothing ran, when we know why. A CONTENTS ASSERTION is excluded
            # from the candidate loop above ON PURPOSE (vibeic-eda#79) — the round
            # decided not to look, and "harness returned no result" reports that
            # decision as a failure of the harness. Same collapse as the verdict
            # this branch sits beside: "we could not look" printed where "we chose
            # not to look" is the truth.
            why = ("not offered to the harness: pinned by "
                   f"`{led.get('dockerfile_arg') or '?'}`, a CONTENTS ASSERTION "
                   f"about a prebuilt artefact — adopting a newer upstream means "
                   f"CUTTING A NEW ARTEFACT, which is a decision, not a merge round"
                   if led.get("pin_kind") == "contents_assertion"
                   else "harness returned no result for this tool")
            entry["note"] = (f"{nr_txt} new release(s) → {latest}; "
                             f"{why}{_undetermined_note(led)}")

        # HOW this fork is pinned, appended once for every verdict — a fork vendored
        # inside another fork's ref is pinned for as long as it is in the image, not only
        # while it is a candidate (vibeic/vibeic-eda#8).
        entry["note"] += pin_provenance(led)

        # CROSS-DOCUMENT CHECK (vibeic/vibeic-eda#7). Both documents are now rendered;
        # parse the numbers back OUT of each and require them to agree. Checking the
        # rendered text rather than the ints they were built from is the point — it is
        # what a formatter that drops a field, or a caller that pairs this report with a
        # different assessment, cannot slip past. Disagreement is collected, never
        # reconciled: see the abort below.
        #
        # It runs AFTER the note is FINAL, deliberately. Checking the text before its last
        # clause is appended checks a document nobody publishes, and would have let any
        # later addition to the row past the one gate that reads what is written.
        if cross_checked:
            conflicts.extend(assess_release.cross_check(
                assessments[cross_checked],
                {"assessment": rendered.get(cross_checked) or "", "report": entry["note"]}))

        led.setdefault("sync_log", []).append(entry)
        led["last_sync"] = date
        pending_ledgers.append((p, led))
        results.append({"tool": tool, **entry})
        print(f"  {tool:16} {entry['verdict']:11} {entry['note'][:78]}")

    # ── THE GATE (vibeic/vibeic-eda#7) ───────────────────────────────────────
    # Nothing has been written yet. A tick whose documents contradict each other
    # publishes NOTHING and exits loudly: an operator who is shown two numbers for one
    # assessment learns to trust neither, and the fail-safe reading of a disagreement is
    # that the day's triage is unknown — not that one of the two answers may be picked.
    # Deliberately NOT wrapped in the "assessment must never break the tick" try: an
    # assessment that fails to compute degrades to a reported error, but an assessment
    # that computes two different results is a defect in this program.
    if conflicts:
        for c in conflicts:
            print(f"  [FATAL] cross-document count mismatch — {c}")
        raise CountsDisagree(
            f"{len(conflicts)} cross-document count mismatch(es) on {date}; "
            f"nothing published (no report, no assessment, no PR)")

    # The same check the tick opened with, at the moment it actually matters
    # (vibeic/vibeic-eda#12). The preflight above exists to avoid the SPEND; this one is
    # the invariant, and it holds for a caller that reached the publication path some
    # other way. Everything below this line overwrites what the cron reads.
    gk_state.require_writable(STATE, "the gatekeeper's ledgers and reports")
    prov = gk_state.provenance()
    for p, led in pending_ledgers:
        led[gk_state.PROVENANCE_KEY] = prov
        p.write_text(json.dumps(led, indent=2, ensure_ascii=False) + "\n")
    if rendered:
        adir = STATE / "reports" / "assessments"
        adir.mkdir(parents=True, exist_ok=True)
        for t, md in rendered.items():
            (adir / f"{date}-{t}.md").write_text(md)
            (adir / f"{date}-{t}.json").write_text(
                json.dumps(assessments[t], ensure_ascii=False))
    REPORTS.mkdir(parents=True, exist_ok=True)
    # the ledgers were seeded BEFORE promote; a successful promote ships a NEW image
    # version, so surface the actually-shipped version (parsed from the 'promoted' note)
    # in the report header instead of the stale pre-bump one.
    img_ver = leds and next(iter(leds.values())).get("image_version")
    for r in results:
        if r.get("verdict") == "MERGED":
            m = re.search(r"vibeic-eda:([0-9]+\.[0-9]+\.[0-9]+)", r.get("note", "") or "")
            if m:
                img_ver = m.group(1)
    summary = {"date": date, "generated_at": _now_iso(),
               "image_version": img_ver,
               # WHICH PROCESS produced this (vibeic/vibeic-eda#12). `generated_at` says
               # when; a report regenerated by hand from a worktree carried the same
               # shape as one the cron wrote, and the difference was inferable only from
               # an mtime.
               gk_state.PROVENANCE_KEY: prov,
               # WHICH fleet list produced this (vibeic/vibeic-eda#10). The `files`
               # sub-dict is dropped: the report states the configuration it ran on, not
               # a second copy of the checker's working notes.
               "fleet_config": {k: v for k, v in fleet.items() if k != "files"},
               # COMPLETE BY CONSTRUCTION (vibeic-eda#101). This was a literal
               # four-tuple, so a verdict absent from it was absent from the
               # headline — and `assessment_entry` has produced RESOLVED since
               # #369, uncounted the whole time. The canonical list seeds the
               # zeros so the shape never depends on the day's data; the second
               # pass then counts EVERY verdict actually published, so the next
               # verdict added cannot go missing the way this one nearly did.
               "counts": {**{v: 0 for v in VERDICTS},
                          **{v: sum(1 for r in results if r["verdict"] == v)
                             for v in {r["verdict"] for r in results}}},
               "results": results}
    (REPORTS / f"{date}.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    (REPORTS / f"{date}.md").write_text(_report_md(summary))
    # ROUND TRIP. The pre-publication gate compared what we MEANT to write; this
    # compares what is ON DISK, which is the only thing anyone reads. It costs a few
    # file reads and it is the half that survives a serialization bug — and, run later
    # as `--verify`, the half that catches a day whose assessment was regenerated
    # underneath a report that was not.
    disk = verify_documents(date)
    if disk:
        for c in disk:
            print(f"  [FATAL] published documents disagree — {c}")
        raise CountsDisagree(f"{len(disk)} mismatch(es) between the report and the "
                             f"assessments published for {date}")
    try:
        build_page.build(build_page.DEFAULT_OUT)
    except Exception as e:
        print(f"  (page rebuild failed: {e})")
    _maybe_notify(summary, fresh, rendered)   # cached (unchanged-range) assessments file no new PR
    return summary


def verify_documents(date: str) -> list[str]:
    """Do the documents PUBLISHED for `date` agree with each other?

    Re-reads `reports/<date>.json` and every `reports/assessments/<date>-<tool>.md` and
    compares, per tool, the counts the daily report states against the counts the
    assessment states — plus the PROVENANCE stamp, which is what tells two vintages of
    one date apart when their numbers happen to coincide.

    That last part is the 2026-07-28 case. The 05:32 tick wrote a report and an
    assessment that agreed with each other (0 clearly-safe, 105 needing review, from a
    judge reply that had been truncated). Four fixes landed by 06:53; the range was
    re-judged at 07:07 and the assessment for that date was re-rendered from the new
    verdict (1 clearly-safe, 2 needing review) — over the SAME date-stamped filename,
    while the daily report kept its 05:32 content. Neither file said which assessment it
    described, so the pair was indistinguishable from a tick that had contradicted
    itself. Run this after any manual re-assessment and it names the split.

    Returns [] when they agree; one line per disagreement otherwise. Never raises — a
    missing or unreadable document is reported, not thrown.
    """
    out: list[str] = []
    rp = REPORTS / f"{date}.json"
    try:
        summary = json.loads(rp.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [f"cannot read the daily report {rp}: {e}"]
    # The CONFIGURATION stamp must survive to the document people read
    # (vibeic/vibeic-eda#10). Checking the rendered markdown rather than the dict it was
    # built from is the point, exactly as for the counts above: a formatter that drops
    # the row leaves a report that names no fleet list while its JSON twin does, and the
    # markdown is the half an operator opens. Reports written before the stamp existed
    # carry no `fleet_config` and are skipped rather than failed.
    if summary.get("fleet_config"):
        want = fleet_config.stamp_line(summary["fleet_config"])
        try:
            md = (REPORTS / f"{date}.md").read_text()
        except OSError as e:
            out.append(f"the daily report {date}.json has no markdown twin ({e})")
        else:
            if want not in md:
                out.append(f"the daily report states it ran on "
                           f"{summary['fleet_config'].get('state')} "
                           f"{summary['fleet_config'].get('commit')}, but {date}.md "
                           f"does not carry that configuration stamp")
    if assess_release is None:
        return ["assess_release is not importable — the published counts cannot be parsed"]
    adir = REPORTS / "assessments"
    for r in summary.get("results") or []:
        got = r.get("assessed")
        if not isinstance(got, dict):
            continue                       # this fork filed no assessment for that date
        tool = r.get("tool", "?")
        ap = adir / f"{date}-{tool}.md"
        try:
            md = ap.read_text()
        except OSError as e:
            out.append(f"{tool}: the report summarises an assessment that is not on disk "
                       f"at {ap} ({e})")
            continue
        want = assess_release.parse_headline("assessment", md)
        if want is None:
            out.append(f"{tool}: {ap.name} states no counts — the report summarises "
                       f"numbers the assessment does not carry")
            continue
        for field in assess_release.HEADLINE:
            if got.get(field) != want[field]:
                out.append(f"{tool}: the daily report says {field}={got.get(field)}, "
                           f"{ap.name} says {want[field]}")
        # PROVENANCE: same date, same tool, different judgement.
        stamp = assess_release.parse_provenance(md)
        for field in ("assessor", "assessed_at"):
            if stamp.get(field) and got.get(field) and stamp[field] != got[field]:
                out.append(f"{tool}: the daily report summarises the assessment computed "
                           f"{field}={got[field]}, but {ap.name} on disk is "
                           f"{field}={stamp[field]} — two vintages of one date")
    return out


def _maybe_notify(summary: dict, assessments: dict | None = None,
                  rendered: dict | None = None):
    """On an actionable day — a MERGED promote, or a new upstream release — open a PR on
    vibe-ic. When there are selective-merge assessments (behind forks), the PR carries the
    per-commit triage for human review (adopt the clearly-safe subset, decide the rest);
    otherwise it records the MERGED/DEFERRED sync row. All-CLEAN days do nothing (no PR
    noise). Never raises — a PR hiccup must not break the tick."""
    if pr_notify is None:
        return
    c = summary["counts"]
    assessments = assessments or {}
    # forks the ASSESSMENT PR covers: any assessed behind fork — with commits OR an
    # enumerate error (a new release we couldn't even read still needs surfacing).
    assess_tools = {t for t, a in assessments.items()
                    if (a.get("commit_count") or 0) > 0 or a.get("error")}
    # the SYNC/BACKLOG PR covers MERGED doc-bumps + any DEFERRED-with-new-release fork the
    # assessment PR did NOT already carry (e.g. assess_release import failed entirely).
    # …or whose release gap could not be MEASURED. An unknown gap is the row most
    # in need of a human, and `(x or 0) > 0` on a null is exactly how it would
    # instead be filtered out as "nothing new".
    #
    # UNMEASURABLE joins DEFERRED here (vibeic-eda#101) because the rows that used
    # to reach this predicate as DEFERRED-with-an-unknown-gap now carry the new
    # verdict, and dropping them would have silently un-escalated the one row most
    # in need of a human — a fix that removes an escalation is a regression wearing
    # the fix's name. The `unknown` arm is what selects them: an UNMEASURABLE row
    # always has `new_releases: null`, so a NOT-PROBED row (an upstream that cuts
    # no releases, which no human can act on and which will never clear) still
    # opens no PR. That is deliberate — see the VERDICTS note at the top of this
    # module for why UNMEASURABLE is counted and named rather than made fatal.
    uncovered = any(r["verdict"] in ("DEFERRED", "UNMEASURABLE")
                    and ((isinstance(r.get("new_releases"), int)
                          and r["new_releases"] > 0)
                         or r.get("new_releases_status") == "unknown")
                    and r["tool"] not in assess_tools for r in summary["results"])
    outcomes = []
    try:
        if assess_tools and hasattr(pr_notify, "open_assessment_pr"):
            subset = {t: assessments[t] for t in assess_tools}
            # The PR carries the EXACT bytes that were checked and written, not a second
            # render of "the" assessment (vibeic/vibeic-eda#7). Two renders are two
            # chances to publish two reports; and re-rendering here would have re-run
            # the classifier's formatter after the gate that cleared it.
            md = {t: (rendered or {}).get(t) for t in subset}
            missing = [t for t, v in md.items() if not v]
            if missing and assess_release:
                for t in missing:
                    md[t] = assess_release.render_md(subset[t])
            outcomes.append(("assess",) + tuple(pr_notify.open_assessment_pr(summary, subset, md)))
        if c.get("MERGED", 0) > 0 or uncovered:
            outcomes.append(("sync",) + tuple(pr_notify.open_pr(summary, _report_md(summary))))
        if not outcomes:
            return
        for kind, ok, detail in outcomes:
            print(f"  [notify:{kind}] {'PR: ' if ok else 'skip: '}{detail}")
    except Exception as e:  # noqa: BLE001
        print(f"  [notify] error (ignored): {e}")


def _report_md(s: dict) -> str:
    # The tallies come from `report_counts`, which is also where the PR TITLE gets
    # them (vibe-ic#875): the title said "DEFERRED 3" over a body that said
    # "DEFERRED 10" for two ticks running, because the two were two renders of two
    # different populations sharing one word. One derivation, one formatter.
    c = report_counts.verdict_counts(s)
    lines = [f"# EDA Fork Gatekeeper — daily report {s['date']}", "",
             f"Generated {s['generated_at']}. Image `vibeic/vibeic-eda:{s.get('image_version')}`. "
             f"Policy: track **releases** (not commits); a new upstream release triggers an "
             f"image rebuild; **option B** — auto-merge on a green rebuild, defer on red.", ""]
    # WHICH fleet list produced this report (vibeic/vibeic-eda#10). Placed above the
    # counts, because it is the premise those counts are a summary OF: a headline that
    # agrees with itself says nothing about whether the right forks were audited.
    if s.get("fleet_config"):
        lines += [fleet_config.stamp_line(s["fleet_config"]), ""]
    # EVERY verdict, through the ONE formatter (vibe-ic#875 + vibeic-eda#101).
    # Two things had to be true at once here: the number after a verdict is
    # rendered in exactly one place so the PR title cannot state a different one,
    # AND the set of verdicts is `VERDICTS` so UNMEASURABLE has somewhere to
    # appear and RESOLVED stops being invisible. `report_counts.phrase` over the
    # shared `VERDICTS` is both; a hand-rolled join here would be the second
    # renderer again, and the four-verdict subset would under-account the table.
    lines += [f"**{report_counts.phrase(c)}**", "",
              "| Tool | Verdict | New releases | Commit gap | Target | Note |",
              "|---|---|---|---|---|---|"]
    order = {v: i for i, v in enumerate(VERDICTS)}
    for r in sorted(s["results"], key=lambda r: (order.get(r["verdict"], 99), r["tool"])):
        # `unknown`, spelled out, never a digit. The column is a MEASUREMENT of how
        # much upstream work we lack; printing 0 for a row where that could not be
        # decided is the one thing a reader cannot recover from, because nothing
        # in the table would distinguish it from a row that was checked.
        nrc = ("unknown" if r.get("new_releases_status") == "unknown"
               else "not probed" if (r.get("new_releases_status") == "not-probed"
                                     or r.get("new_releases") is None)
               else r["new_releases"])
        # THE SECOND MEASUREMENT, in its own column, under the same rule as the
        # first: three states, and the two that are not a number are spelled out.
        # A report written before this column existed carries neither field, and
        # renders `—` rather than borrowing the release column's answer.
        bcs, bc = r.get("behind_commits_status"), r.get("behind_commits")
        bcc = ("unmeasured" if bcs == "unknown"
               else "not probed" if bcs == "not-probed"
               else bc if isinstance(bc, int) else "—")
        lines.append(f"| {r['tool']} | {r['verdict']} | {nrc} | {bcc} | "
                     f"{r.get('latest_release') or '—'} | {r['note']} |")
    lines += ["", "> CLEAN = already on the latest upstream release. NOT_LAYERED = forked but "
              "the image build never fetches it — no ARG pin of its own and not vendored "
              "inside one. DEFERRED tools have a new upstream release staged; the image "
              "auto-rebuilds + merges once image_build.cmd is wired and the rebuild is green. "
              "UNMEASURABLE = THE QUESTION WAS NOT ANSWERED — the release gap is `unknown` "
              "(we compared and could not decide) or `not probed` (the upstream publishes no "
              "release to compare against). It is neither CLEAN nor DEFERRED: both of those "
              "read as \"measured, here is the answer\", and this row has no answer to give. "
              "It does not fail the round — it is counted and named, and it reaches a human "
              "only when the sub-status is `unknown`. "
              "`New releases = unknown` means CONTAINMENT COULD NOT BE DECIDED for at least "
              "one upstream release — it is not zero and it is not a count; the row's note "
              "names the releases and the error that stopped each one. "
              "`Commit gap` is the SEPARATE, commit-level measurement (does our pinned ref "
              "carry upstream's default branch); `unmeasured` there is not 0, and a row can "
              "be release-current with a commit gap nobody established."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if "--verify" in sys.argv:
        # Re-check an ALREADY-PUBLISHED day, without running a tick: no upstream fetch,
        # no assessment, no PR. `--verify [date]` (default: today).
        _rest = [a for a in sys.argv[1:] if not a.startswith("--")]
        _date = _rest[0] if _rest else _now_date()
        _bad = verify_documents(_date)
        for _c in _bad:
            print(f"[FATAL] {_date}: {_c}")
        print(f"{_date}: {'DISAGREE — ' + str(len(_bad)) + ' mismatch(es)' if _bad else 'documents agree'}")
        sys.exit(1 if _bad else 0)
    tick()
