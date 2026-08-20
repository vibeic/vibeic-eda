#!/usr/bin/env python3
"""The round must NOTICE its own silence, and say so somewhere a person looks.

vibeic-eda#58 already stated the problem: three consecutive days published
nothing and raised no alert, and the only visible symptom was a public page that
did not move -- noticed by a person, on day three. A step was added to measure
it. It writes `CONVERGENCE: FAILED` and `build_page SKIPPED` into a log file.

On 2026-08-20 it happened AGAIN, the same way, and was again found by a person
looking at the page: build_page had been skipped every round since 08-18T07:48,
for four different reasons on different days, and each round said so only in
`daily_0530.log`. DETECTION THAT LANDS IN A FILE NOBODY OPENS IS NOT AN ALERT.
The round noticed; nothing was told.

This program closes the last hop. It owns the publish history end to end -- one
program appends the row and the same program counts the streak, so no two
callers can disagree about the format -- and when the page has failed to publish
for `--threshold` consecutive rounds it opens a PR on vibe-ic through
`pr_notify`, which is the channel the owner designated for this repo ("use a PR
on vibe-ic to handle all forked OSS EDA tools"). One PR per streak: a streak
that is already escalated stays escalated rather than opening a PR a day.

REFUSES RATHER THAN DEGRADES. A history it cannot read, a row it cannot append,
or an escalation that does not go out are all NORECORD (rc=2), never a quiet
"nothing to report". An alerter that fails to "no alert" is worse than none,
because it reports success while the thing it guards burns.

    rc 0  the page published this round, or the streak is below the threshold
    rc 1  the page has been silent for >= threshold rounds; escalation is out
    rc 2  NORECORD -- the state could not be read, written, or delivered
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

RC_OK = 0
RC_SILENT = 1
RC_NORECORD = 2

DEFAULT_THRESHOLD = 2


def _rows(history: Path) -> list[dict]:
    """Every well-formed row, oldest first. A malformed line is NOT skipped."""
    if not history.exists():
        return []
    rows = []
    for n, line in enumerate(history.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{history}:{n} is not JSON: {exc}") from exc
        if not isinstance(row, dict) or "published" not in row:
            raise ValueError(f"{history}:{n} is not a publish row: {line[:120]}")
        rows.append(row)
    return rows


def _streak(rows: list[dict]) -> list[dict]:
    """The trailing run of rounds that did NOT publish."""
    out = []
    for row in reversed(rows):
        if row.get("published") is True:
            break
        out.append(row)
    return list(reversed(out))


def _report(streak: list[dict], threshold: int) -> str:
    first, last = streak[0], streak[-1]
    lines = [
        "## The fork page has stopped publishing",
        "",
        f"`build_page` has not published for **{len(streak)} consecutive "
        f"rounds** (threshold {threshold}).",
        "",
        f"* first silent round: `{first.get('round_at')}`",
        f"* latest silent round: `{last.get('round_at')}`",
        "",
        "https://vibeic.ai/eda-forks.html is therefore serving the numbers from "
        "the last round that did publish. The page is not wrong by accident -- "
        "the round preserves the last good page on purpose when it cannot "
        "certify a new one -- but a reader cannot tell a quiet week from a "
        "stuck pipeline, which is the whole reason this PR exists.",
        "",
        "### Why each round declined",
        "",
    ]
    for row in streak:
        lines.append(f"* `{row.get('round_at')}` — {row.get('reason') or 'unstated'}")
    lines += [
        "",
        "Each reason is the round's own verdict, not a diagnosis. Several "
        "different reasons across a streak usually means the pipeline is "
        "failing in more than one place, not that one bug recurs.",
    ]
    return "\n".join(lines) + "\n"


def assess(history: Path, *, published: bool, reason: str, round_at: str,
           threshold: int, escalate) -> tuple[int, dict]:
    """Append this round's row, then judge the trailing streak."""
    result = {"program": "check_publish_silence", "schema_version": 1,
              "round_at": round_at, "published": bool(published),
              "reason": reason, "threshold": threshold,
              "streak": 0, "escalated": False, "errors": []}
    try:
        rows = _rows(history)
    except (ValueError, OSError) as exc:
        result["errors"].append(f"publish history unreadable: {exc}")
        return RC_NORECORD, result

    # Already escalated for THIS streak? Read it before appending, so the new
    # row cannot mark itself.
    already = any(r.get("escalated") is True for r in _streak(rows))

    row = {"round_at": round_at, "published": bool(published),
           "reason": reason if not published else ""}
    try:
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        result["errors"].append(f"publish history could not be appended: {exc}")
        return RC_NORECORD, result

    rows.append(row)
    streak = _streak(rows)
    result["streak"] = len(streak)
    if published or len(streak) < threshold:
        return RC_OK, result
    if already:
        result["escalated"] = True
        result["note"] = "this streak was already escalated; not opening another PR"
        return RC_SILENT, result

    ok, detail = escalate(
        f"fork page has not published for {len(streak)} consecutive rounds",
        _report(streak, threshold))
    result["escalation_detail"] = str(detail)[:400]
    if not ok:
        # THE WHOLE POINT IS THE ALERT. If it did not go out, this program has
        # not done its job, and saying rc=1 would claim it had.
        result["errors"].append(f"escalation did not go out: {detail}")
        return RC_NORECORD, result

    result["escalated"] = True
    try:
        _mark_escalated(history)
    except OSError as exc:
        # The PR IS out; failing to record that would only re-open it tomorrow.
        result["errors"].append(f"escalation sent but not recorded: {exc}")
        return RC_NORECORD, result
    return RC_SILENT, result


def _mark_escalated(history: Path) -> None:
    """Stamp the last row so the next round does not open a second PR."""
    lines = history.read_text().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            row = json.loads(lines[i])
            row["escalated"] = True
            lines[i] = json.dumps(row, ensure_ascii=False)
            break
    tmp = history.with_suffix(history.suffix + f".tmp.{os.getpid()}")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(history)


def _default_escalate(summary: str, report_md: str, round_at: str = "undated"):
    """Raise the alert through the PR channel.

    NOT `pr_notify.open_pr`. That one takes the fork-sync SUMMARY DICT and
    derives verdict counts from it, so a string argument dies on
    `AttributeError: 'str' object has no attribute 'get'` — measured on this
    program's first real escalation, 2026-08-20, publish streak 2. The alerter
    behaved correctly then (NORECORD, reason printed, round failed) but the
    alert did not go out, which is the one thing it exists to do.
    `open_alert_pr` is the path shaped for a dated note plus a title.
    """
    try:
        import pr_notify
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        return (False, f"pr_notify unavailable: {type(exc).__name__}: {exc}")
    date = (round_at or "undated")[:10] or "undated"
    try:
        return pr_notify.open_alert_pr(summary, report_md,
                                       slug="publish-silence", date=date)
    except Exception as exc:                       # noqa: BLE001
        return (False, f"pr_notify raised: {type(exc).__name__}: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history", required=True)
    ap.add_argument("--round-at", required=True)
    ap.add_argument("--published", required=True, choices=["0", "1"])
    ap.add_argument("--reason", default="")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--json")
    ap.add_argument("--no-escalate", action="store_true",
                    help="measure and record, but do not open a PR (for tests)")
    args = ap.parse_args(argv)

    if args.threshold < 1:
        print("--threshold must be >= 1", file=sys.stderr)
        return RC_NORECORD

    escalate = ((lambda s, r: (True, "escalation suppressed by --no-escalate"))
                if args.no_escalate
                else (lambda s, r: _default_escalate(s, r, args.round_at)))
    rc, result = assess(Path(args.history), published=args.published == "1",
                        reason=args.reason, round_at=args.round_at,
                        threshold=args.threshold, escalate=escalate)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2) + "\n")

    if rc == RC_OK:
        if result["published"]:
            print("check_publish_silence: PASS — the page published this round")
        else:
            print(f"check_publish_silence: PASS — {result['streak']} silent "
                  f"round(s), below the threshold of {result['threshold']}")
    elif rc == RC_SILENT:
        print(f"check_publish_silence: SILENT — the page has not published for "
              f"{result['streak']} consecutive rounds; "
              + (result.get("note") or "a PR was opened on vibe-ic"))
    else:
        for err in result["errors"]:
            print(f"check_publish_silence: NOT CHECKED — {err}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
