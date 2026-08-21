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
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from check_publish_silence import _default_escalate  # noqa: E402

RC_OK, RC_ESCALATED, RC_NORECORD = 0, 1, 2

ALARM_KIND = "release-failed"


def _report(tick_exit, tick_state: str, round_at: str, detail: str) -> tuple:
    summary = (f"release failed on the {round_at[:10]} round "
               f"(run_tick exit {tick_exit})")
    body = (
        f"`run_tick` exited **{tick_exit}** (state `{tick_state}`) on the "
        f"{round_at} round, so no image was published and `build_page` was "
        f"skipped.\n\n"
        f"## What the release said\n\n```\n{detail.strip() or '(no detail captured)'}\n```\n\n"
        "## Why this is a PR and not a page banner\n\n"
        "The page cannot report that the page did not update. Until this alarm "
        "existed the only signal was `check_publish_silence`, which measures the "
        "page going stale — a consequence, counted in DAYS. A build that fails "
        "the same way every round could sit for most of a week first.\n\n"
        "## What to check\n\n"
        "- The archived build output under `.cache/eda-fork-gatekeeper/failed-releases/`.\n"
        "- Whether the failure is a moving target (an upstream merge advanced "
        "something a fixed assertion pinned) — that shape fails at the moment of "
        "success and repeats every round until the assertion is fixed.\n"
        "- Whether the working tree was left with uncommitted pin files; the "
        "round's own `recover_pin_only_dirty_tree` handles that at the next start.\n\n"
        "---\n_Opened by the round itself. Operational alert, not an auto-merge._\n"
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

    release, why = _load(Path(a.release_json))
    detail = ""
    if isinstance(release, dict):
        for key in ("summary", "reason", "category", "note"):
            if release.get(key):
                detail += f"{key}: {release[key]}\n"
    elif why:
        detail = f"(the release record could not be read — {why})"

    summary, body = _report(tick_exit, a.tick_state, a.round_at, detail)
    ok, note = _default_escalate(summary, body, round_at=a.round_at,
                                 kind=ALARM_KIND)
    print(f"[{'RAISED' if ok else 'COULD NOT RAISE'}] {summary} — {note}")
    return RC_ESCALATED


if __name__ == "__main__":
    raise SystemExit(main())
