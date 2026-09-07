#!/usr/bin/env python3
"""Say so on the day a release fails, instead of days later when the page goes stale.

2026-08-22: the 05:30 round merged 46 upstream klayout commits, the image build
died on a version assertion that had been pinned to the previous klayout, and
`build_page` was skipped. The round then finished NORMALLY. Nothing was raised:
`check_publish_silence` was rc 0 because the page had published the day before
and the streak was still under its threshold. The only alarm in the pipeline
measures the PAGE going stale, which is a consequence, and it measures it in
DAYS. A build that would fail identically every round could sit there for most
of a week before anything said a word.

So this reports the CAUSE, on the round it happens.

    rc 0  the release did not fail, or the failure is already raised
    rc 1  a release failure was found and escalated (or escalation refused)
    rc 2  NORECORD — the tick evidence could not be read, which is not "fine"

ONE PR PER FAILING DAY. The branch name carries the date, and an existing branch
on origin means the day is already raised — a round that fails four times in a
day does not open four PRs.

A ROUND THAT PUBLISHED NOTHING DID NOT NECESSARILY FAIL TO BUILD (2026-09-08).
`run_tick` assigns a DIFFERENT exit code per stage, and only exit 5 is a release
failure. Exits 3/4/6/7/8/9 mean a pre-release stage refused, so `daily_release`
was never invoked at all — `run_tick.sh` says so itself at the refusal ("This is
not a daily_release failure: no release was attempted") and deletes
`daily-release.json` so yesterday's success cannot be read as today's evidence.

This alarm read only `daily-release.json` and called every non-zero exit a
release failure. The 2026-08-25, 2026-09-07 and 2026-09-08 rounds all exited 4
(`daily_merge` refused) and all three produced the SAME BYTE-IDENTICAL alert:
"(the release record could not be read — … does not exist)", plus an instruction
to read `failed-releases/`, which this failure class never writes because no
build was ever attempted. Three rounds, zero information, and the record that
DID hold the diagnosis — `daily-merge.json`/`daily-merge.txt` — was never opened
and is overwritten by the next round.

So the exit code now selects the stage, and the stage selects the record.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from check_publish_silence import _default_escalate  # noqa: E402

RC_OK, RC_ESCALATED, RC_NORECORD = 0, 1, 2

ALARM_KIND = "release-failed"


_DETAIL_KEYS = ("summary", "reason", "category", "detail", "note", "subject",
                "stage")

#: run_tick's own exit-status map, stage by stage, and the evidence each stage
#: leaves in `GK_STATE_DIR`.  DERIVED FROM `run_tick.sh`, NOT INVENTED HERE:
#: `test_a_blocked_round_names_the_stage_that_refused` reads that script's own
#: `rc=<n>` assignments and its `<NAME>_OUT="${LOG_DIR}/<file>"` declarations and
#: fails if this table drifts from either. A hand-written list of "the ways a
#: round can stop" is exactly how the 4-vs-5 confusion survived three rounds.
#:
#: `kind` is the register the body must be written in, and there are THREE, not
#: two. `pre` — the round refused before `daily_release` was invoked, so there is
#: no build to describe and no build output to read. `release` — the build itself
#: failed (exit 5, the only one). `post` — an image WAS cut and published and a
#: check after the release boundary refused, so "no image was published" is false
#: in the other direction. Derived from the order of run_tick's own `rc=` chain:
#: a later code is reached only when every earlier one was zero, so exits 6 and 7
#: prove `release_rc` was 0, while 8 and 9 force `blocking_rc` and suppress the
#: release entirely.
_STAGES = {
    3: {"stage": "source guards", "txt": "source-guards.txt", "json": None,
        "kind": "pre", "rc_var": "guard_rc"},
    4: {"stage": "daily_merge", "txt": "daily-merge.txt",
        "json": "daily-merge.json", "kind": "pre", "rc_var": "merge_rc"},
    5: {"stage": "daily_release", "txt": "daily-release.txt",
        "json": "daily-release.json", "kind": "release",
        "rc_var": "release_rc"},
    6: {"stage": "check_our_commits_ship", "txt": "our-commits-ship.txt",
        "json": "our-commits-ship.json", "kind": "post", "rc_var": "ship_rc"},
    7: {"stage": "capability gate", "txt": "capability-lost.txt",
        "json": "capability-lost.json", "kind": "post", "rc_var": "cap_rc"},
    8: {"stage": "fork self-checks", "txt": "fork-selftests.txt",
        "json": "fork-selftests.json", "kind": "pre",
        "rc_var": "selftest_rc"},
    9: {"stage": "vendored closure", "txt": "vendored-closure.txt",
        "json": "vendored-closure.json", "kind": "pre",
        "rc_var": "vendored_rc"},
}

#: How much of a stage's plain-text output to quote when its JSON sidecar says
#: nothing.  The whole file is the wrong answer: `daily-merge.txt` carries one
#: line per fork and `daily-release.txt` has run to hundreds of lines.
_TXT_LINES = 40

#: The states `daily_merge` counts as a clean outcome.  Anything else is a fork
#: it could not publish, and those rows ARE the diagnosis.
_MERGE_CLEAN = ("MERGED", "ALREADY_CURRENT", "WOULD_MERGE")


def _state_dir() -> Path:
    """Where the round's evidence lives, resolved the way run_tick exports it."""
    return Path(os.environ.get(
        "GK_STATE_DIR", "/home/reyerchu/.cache/eda-fork-gatekeeper"))


def _detail_from_merge(record: dict) -> str:
    """The forks `daily_merge` could not publish, with what stopped each one.

    `daily_merge`'s record is a list of per-fork rows, not the release record's
    `failure`/`unmeasured` shape, so `_detail_from` reads nothing out of it. The
    rows whose state is not MERGED/ALREADY_CURRENT/WOULD_MERGE are the forks that
    "need a human" — the exact population whose count made the program exit 1 and
    the round exit 4.
    """
    forks = record.get("forks")
    if not isinstance(forks, list):
        return ""
    lines: list[str] = []
    for row in forks:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "")
        if state in _MERGE_CLEAN:
            continue
        repo = row.get("repo") or "(unnamed fork)"
        lines.append(f"{repo}: {state} — {row.get('detail') or '(no detail)'}")
        for conflict in (row.get("conflicts") or [])[:10]:
            lines.append(f"    conflict: {conflict}")
        for check in (row.get("checks") or []):
            if isinstance(check, dict) and not check.get("ok"):
                lines.append(f"    check FAIL {check.get('name')} "
                             f"rc={check.get('rc')}: {check.get('detail')}")
    if not lines:
        # Do not manufacture a diagnosis. A merge record with no unclean row and
        # a non-zero exit is itself a finding, and saying so is the honest report.
        return ("daily_merge exited non-zero but its record names no fork "
                "outside MERGED/ALREADY_CURRENT/WOULD_MERGE — the record and the "
                "exit code disagree\n")
    return "\n".join(lines) + "\n"


def _quote_txt(path: Path) -> str:
    """The head of a stage's plain-text output, or why it could not be read.

    This is the half that was missing. `run_tick` writes a human-readable line
    for every stage including the one that refuses — the 2026-09-08 round's
    `daily-release.txt` held "BLOCKED: source guards, daily merge, vendored
    closure, or fork self-checks are not green; daily_release was not invoked" —
    and the alarm read none of them.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"({path} does not exist)"
    except OSError as exc:
        return f"({path} is unreadable: {exc})"
    lines = text.splitlines()
    head = lines[:_TXT_LINES]
    if len(lines) > _TXT_LINES:
        head.append(f"… {len(lines) - _TXT_LINES} more line(s) in {path.name}")
    return "\n".join(head).strip() or f"({path} is empty)"


#: The round's own positive statement that it refused before the build. Written
#: by `run_tick.sh`'s refusal branch, and the reason an absence stopped being the
#: only evidence a blocked round left behind.
BLOCKED_RECORD = "round-blocked.json"


def _blocked_stages() -> str:
    """Every stage that refused, not just the one the exit code happens to name.

    `run_tick`'s status chain is first-wins, so a round where `daily_merge` AND
    the fork self-checks both refused exits 4 and says nothing about 8. The
    refusal branch records all four blocking rcs; read them.
    """
    record, why = _load(_state_dir() / BLOCKED_RECORD)
    if not isinstance(record, dict):
        return ""
    rows = record.get("blocked_by")
    if not isinstance(rows, list) or not rows:
        return ""
    lines = [f"the round refused before the build; {len(rows)} stage(s) "
             f"were not green:"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        lines.append(f"  {row.get('stage')} ({row.get('rc_var')}) "
                     f"rc={row.get('rc')} — evidence: {row.get('evidence')}")
    return "\n".join(lines) + "\n"


def _stage_detail(spec: dict) -> str:
    """Everything the stage that refused said, from its own two records."""
    root = _state_dir()
    parts: list[str] = []
    if spec.get("kind") == "pre":
        blocked = _blocked_stages().rstrip()
        if blocked:
            parts.append(blocked)
    if spec.get("json"):
        record, why = _load(root / spec["json"])
        if isinstance(record, dict):
            if record.get("program") == "daily_merge" or "forks" in record:
                parts.append(_detail_from_merge(record).rstrip())
            typed = _detail_from(record).rstrip()
            if typed:
                parts.append(typed)
        elif why:
            parts.append(f"(the {spec['stage']} record could not be read — {why})")
    parts = [p for p in parts if p]
    parts.append(f"--- {spec['txt']} ---\n{_quote_txt(root / spec['txt'])}")
    return "\n".join(parts) + "\n"


def _detail_from(release: dict) -> str:
    """Everything the release said about why it stopped.

    THE EVIDENCE IS NESTED AND THIS USED TO READ ONLY THE TOP LEVEL. A release
    that stops records its reason under `failure` (a dict carrying `stage`,
    `category`, `subject`, `summary`) and, for an unmeasurable outcome, one
    entry per obstacle under `unmeasured` — none of which are top-level keys.
    So the alarm printed "(no detail captured)" while the record held the whole
    story.

    Observed on the 2026-08-30 round: `run_tick` exited 5 because the release
    could not measure a registry artefact —

        failure.category = registry_artefact_state_unmeasurable
        failure.subject  = ghcr.io/vibeic/eda-tool-xyce:1fab458-71dfca
        failure.summary  = registry presence unmeasured: Get "https://…" …
        failure.retryable = true

    a transient GHCR read, which is the difference between "retry it" and
    "someone has to go and look". The alarm reported none of it, so the PR it
    opened asked a human to go and find what the record already knew.

    `retryable` is surfaced by name because it is the triage decision itself.
    """
    lines: list[str] = []

    def _emit(prefix: str, obj) -> None:
        if not isinstance(obj, dict):
            if obj:
                lines.append(f"{prefix}: {obj}")
            return
        for key in _DETAIL_KEYS:
            if obj.get(key):
                lines.append(f"{prefix}{key}: {obj[key]}")
        if "retryable" in obj:
            lines.append(f"{prefix}retryable: {obj['retryable']}")

    _emit("", release)
    _emit("failure.", release.get("failure"))
    for i, item in enumerate(release.get("unmeasured") or []):
        _emit(f"unmeasured[{i}].", item)
    return "\n".join(lines) + ("\n" if lines else "")


def _report(tick_exit, tick_state: str, round_at: str, detail: str,
            spec: Optional[dict] = None) -> tuple:
    """The alert's title and body.

    Exit 5 keeps its wording verbatim, because for exit 5 it is TRUE: the release
    was attempted and it failed. Every other code gets a body about the stage that
    actually refused, because "release failed … the archived build output under
    failed-releases/" sent three readers to a directory this failure class never
    writes.
    """
    kind = (spec or {}).get("kind", "release")
    if kind == "pre":
        summary = (f"the {round_at[:10]} round published nothing — "
                   f"{spec['stage']} refused (run_tick exit {tick_exit})")
        what = (
            f"`run_tick` exited **{tick_exit}** (state `{tick_state}`) on the "
            f"{round_at} round. That code is `run_tick`'s status for "
            f"**{spec['stage']}** (`{spec['rc_var']}`), which is a PRE-RELEASE "
            f"stage: `daily_release` was never invoked, no image was built, "
            f"nothing was published and `build_page` was skipped.\n\n"
            f"There is therefore **no build output to read**. "
            f"`daily-release.json` is deliberately deleted at the refusal "
            f"(`run_tick.sh`: \"Do not leave yesterday's successful sidecar "
            f"looking like evidence from a release that was never authorized "
            f"today\"), and `failed-releases/` is written only when a build "
            f"actually fails.\n\n"
            f"## What {spec['stage']} said\n\n"
            f"```\n{detail.strip() or '(no detail captured)'}\n```\n\n"
        )
        where = (
            "## What to check\n\n"
            f"- `$GK_STATE_DIR/{spec['txt']}`"
            + (f" and `$GK_STATE_DIR/{spec['json']}`" if spec.get("json") else "")
            + " — **the next round overwrites both**; this round's copies are "
              "retained under `$GK_STATE_DIR/failed-rounds/`.\n"
            "- Not `failed-releases/`, and not the release record: neither exists "
            "for this failure class.\n"
            "- Whether the same stage refused on the preceding rounds. A "
            "pre-release stage that refuses on a fixed assertion an upstream "
            "merge moved past fails at the moment of success and repeats every "
            "round until the assertion is repaired — which is a different thing "
            "from a transient, and the two need opposite responses.\n\n"
        )
    elif kind == "post":
        summary = (f"the {round_at[:10]} round published an image and then "
                   f"{spec['stage']} refused (run_tick exit {tick_exit})")
        what = (
            f"`run_tick` exited **{tick_exit}** (state `{tick_state}`) on the "
            f"{round_at} round. That code is `run_tick`'s status for "
            f"**{spec['stage']}** (`{spec['rc_var']}`), which runs AFTER the "
            f"release boundary — so an image was cut and pushed, and this "
            f"refusal is about that published image, not about a build that "
            f"failed. Reaching this code also proves `release_rc` was 0.\n\n"
            f"## What {spec['stage']} said\n\n"
            f"```\n{detail.strip() or '(no detail captured)'}\n```\n\n"
        )
        where = (
            "## What to check\n\n"
            f"- `$GK_STATE_DIR/{spec['txt']}`"
            + (f" and `$GK_STATE_DIR/{spec['json']}`" if spec.get("json") else "")
            + " — **the next round overwrites both**.\n"
            "- The published image itself: the refusal names something about it, "
            "so it is already in front of every consumer of the pin.\n\n"
        )
    else:
        summary = (f"release failed on the {round_at[:10]} round "
                   f"(run_tick exit {tick_exit})")
        what = (
            f"`run_tick` exited **{tick_exit}** (state `{tick_state}`) on the "
            f"{round_at} round, so no image was published and `build_page` was "
            f"skipped.\n\n"
            f"## What the release said\n\n```\n{detail.strip() or '(no detail captured)'}\n```\n\n"
        )
        where = (
            "## What to check\n\n"
            "- The archived build output under `.cache/eda-fork-gatekeeper/failed-releases/`.\n"
            "- Whether the failure is a moving target (an upstream merge advanced "
            "something a fixed assertion pinned) — that shape fails at the moment of "
            "success and repeats every round until the assertion is fixed.\n"
            "- Whether the working tree was left with uncommitted pin files; the "
            "round's own `recover_pin_only_dirty_tree` handles that at the next start.\n\n"
        )
    body = (
        what
        + "## Why this is a PR and not a page banner\n\n"
        "The page cannot report that the page did not update. Until this alarm "
        "existed the only signal was `check_publish_silence`, which measures the "
        "page going stale — a consequence, counted in DAYS. A build that fails "
        "the same way every round could sit for most of a week first.\n\n"
        + where
        + "---\n_Opened by the round itself. Operational alert, not an auto-merge._\n"
    )
    return summary, body


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, f"{path} does not exist"
    except (OSError, ValueError) as exc:
        return None, f"{path} is unreadable: {exc}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tick-exit", required=True,
                    help="run_tick's exit code, as the round saw it")
    ap.add_argument("--tick-state", default="ran")
    ap.add_argument("--round-at", required=True)
    ap.add_argument("--release-json",
                    default=str(Path(os.environ.get(
                        "GK_STATE_DIR", "/home/reyerchu/.cache/eda-fork-gatekeeper"))
                        / "daily-release.json"),
                    help="the release record, read only for its failure detail")
    a = ap.parse_args(argv)

    try:
        tick_exit = int(a.tick_exit)
    except (TypeError, ValueError):
        print(f"[NORECORD] run_tick's exit code is not an integer: {a.tick_exit!r}")
        return RC_NORECORD

    # NOT CHECKED IS NOT A FAILURE. A round that never authorised a release has
    # nothing to report here; whatever stopped it earlier is its own gate's
    # business and already said so.
    if a.tick_state != "ran":
        print(f"[PASS] run_tick was not checked ({a.tick_state}); "
              "no release was attempted, so there is no release failure to raise")
        return RC_OK
    if tick_exit == 0:
        print("[PASS] the release succeeded")
        return RC_OK

    # THE EXIT CODE SELECTS THE STAGE, AND THE STAGE SELECTS THE RECORD. Reading
    # `daily-release.json` for an exit that never reached `daily_release` is how
    # three consecutive rounds produced a byte-identical alert whose only
    # instruction pointed at a directory their failure class never writes.
    spec = _STAGES.get(tick_exit)
    if spec is not None and spec.get("kind") == "pre":
        detail = _stage_detail(spec)
    else:
        release, why = _load(Path(a.release_json))
        detail = ""
        if isinstance(release, dict):
            detail = _detail_from(release)
        elif why:
            detail = f"(the release record could not be read — {why})"
        if spec is not None and spec.get("kind") == "post":
            # A stage after the release boundary refused. Its own record is the
            # diagnosis; the release record is context, not the answer.
            detail = ((detail.rstrip() + "\n\n") if detail.strip() else "") \
                + _stage_detail(spec)

    summary, body = _report(tick_exit, a.tick_state, a.round_at, detail, spec)
    ok, note = _default_escalate(summary, body, round_at=a.round_at,
                                 kind=ALARM_KIND)
    print(f"[{'RAISED' if ok else 'COULD NOT RAISE'}] {summary} — {note}")
    return RC_ESCALATED


if __name__ == "__main__":
    raise SystemExit(main())
