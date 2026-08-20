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
for `--threshold` consecutive rounds it opens a PR on THIS repo. Not on
vibe-ic: a stalled fork page is this pipeline's own failure, and an open PR
there is counted by `fork_github_convergence`, so the alarm about a stalled
page would itself stall the page. One PR per streak: a streak
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
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

RC_OK = 0
RC_SILENT = 1
RC_NORECORD = 2

DEFAULT_THRESHOLD = 2

#: THIS repo — the page is this pipeline's own output.
REPO = Path(os.environ.get("GK_EDA_REPO", "/home/reyerchu/vibeic-eda"))
GH_REPO = "vibeic/vibeic-eda"
ALERT_DIR = "alerts"


def _sh(*argv, timeout: int = 120):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")



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
    """Raise the alarm as a PR on THIS repo.

    NOT on vibe-ic, and the reason is both ownership and mechanics. A stalled
    fork page is this pipeline's own failure, so the record belongs where the
    pipeline lives. And an open PR on vibe-ic is counted by
    `fork_github_convergence`, whose denominator is the registered forks plus
    `vibeic/vibe-ic` — so filing there would make the next round's certified
    publish non-zero, i.e. the alarm about a stalled page would stall the page.
    `vibeic/vibeic-eda` is not in that denominator; verified against the round's
    own convergence sidecar before this was written.

    Self-contained on purpose: `pr_notify` is the vibe-ic notifier and now has
    no production caller at all. Routing through it would put this back on the
    repo it must not touch.
    """
    date = (round_at or "undated")[:10] or "undated"
    branch = f"publish-silence-{date}"
    if not REPO.is_dir():
        return (False, f"eda repo not found at {REPO}")
    if not (summary.strip() and report_md.strip()):
        return (False, "refusing to open an alert PR with an empty title or body")

    rc, out = _sh("git", "-C", str(REPO), "fetch", "origin", "main", "-q")
    if rc != 0:
        return (False, f"git fetch failed: {out.strip()[:200]}")
    rc, out = _sh("git", "-C", str(REPO), "ls-remote", "--heads", "origin", branch)
    if rc == 0 and out.strip():
        return (True, f"alert branch {branch} already on origin — already raised")

    wt = Path(tempfile.gettempdir()) / f"gk-eda-alert-{date}"
    _sh("git", "-C", str(REPO), "worktree", "remove", "--force", str(wt))
    shutil.rmtree(wt, ignore_errors=True)
    rc, out = _sh("git", "-C", str(REPO), "worktree", "add", "-q", "--detach",
                  str(wt), "origin/main")
    if rc != 0:
        return (False, f"worktree add failed: {out.strip()[:200]}")
    try:
        adir = wt / ALERT_DIR
        adir.mkdir(parents=True, exist_ok=True)
        (adir / f"{date}-publish-silence.md").write_text(
            f"# {summary}\n\n{report_md}", encoding="utf-8")
        _sh("git", "-C", str(wt), "add", ALERT_DIR)
        rc, out = _sh("git", "-C", str(wt), "commit", "-q", "-m", summary)
        if rc != 0:
            return (False, f"commit failed: {out.strip()[:200]}")
        if os.environ.get("GK_PR_DRYRUN") in ("1", "true", "yes"):
            rc, diff = _sh("git", "-C", str(wt), "show", "--stat", "HEAD")
            return (True, f"DRY-RUN — would open alert PR '{summary}'\n{diff.strip()[:600]}")
        rc, out = _sh("git", "-C", str(wt), "push", "-q", "origin",
                      f"HEAD:refs/heads/{branch}")
        if rc != 0:
            return (False, f"branch push failed: {out.strip()[:250]}")
        body = (report_md.rstrip() +
                "\n\n---\n_Opened by the round itself. Operational alert, not an "
                "auto-merge._\n")
        bf = Path(tempfile.gettempdir()) / f"gk-eda-alert-body-{date}.md"
        bf.write_text(body, encoding="utf-8")
        rc, out = _sh("gh", "pr", "create", "-R", GH_REPO, "--base", "main",
                      "--head", branch, "--title", summary, "--body-file", str(bf))
        if rc != 0:
            return (False, f"gh pr create failed: {out.strip()[:250]}")
        url = out.strip().splitlines()[-1] if out.strip() else "(created)"
        return (True, f"opened alert PR: {url}")
    finally:
        _sh("git", "-C", str(REPO), "worktree", "remove", "--force", str(wt))
        shutil.rmtree(wt, ignore_errors=True)


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
              + (result.get("note") or "a PR was opened on this repo"))
    else:
        for err in result["errors"]:
            print(f"check_publish_silence: NOT CHECKED — {err}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
