#!/usr/bin/env python3
"""Same stage, same fork, four mornings running — or a different one each time?

THE BRIEF THIS ANSWERS. A pre-release stage that refuses on a fixed assertion an
upstream merge has moved past FAILS AT THE MOMENT OF SUCCESS and repeats every
round until the assertion is repaired. A stage that refuses because a shared
checkout was busy fails once and clears. Those two need OPPOSITE responses, and
until now nothing in the pipeline could tell them apart:

  * `check_release_failed` opens ONE PR PER FAILING DAY, by design, and each PR is
    independent of the one before it. The 2026-08-25, 2026-09-07 and 2026-09-08
    alerts were byte-identical and NOTHING said so.
  * `check_publish_silence` counts consecutive rounds, but it measures the PAGE,
    which is a consequence, and only after the streak crosses a threshold.

So this reads the retained refusal archive `failed-rounds/` and puts the question
directly. The vocabulary is the discriminator, not a count:

  REPEATING_SAME_SUBJECT  the same stage refused on the same fork across rounds.
        This is the moving-target signature: something fixed on our side is being
        outrun by something that moves upstream. It will not clear on its own.
  REPEATING_STAGE_ONLY    the same stage refused on DIFFERENT forks each round.
        The stage is not the subject — its environment is. A shared checkout, a
        registry, a network. Repairing an assertion here would be repairing the
        wrong thing.
  ONCE                    the stage refused on exactly one retained DAY.
  UNDETERMINED            fewer than two retained DAYS carry a refusal, so no
        comparison is possible. THIS IS NOT "no repeats". One observation cannot
        distinguish a transient from a generator, and reporting it as "transient"
        is the false clean this whole family of defects is made of.

    rc 0  a verdict was formed
    rc 2  the archive could not be read — which is not "there were no repeats"
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

RC_OK, RC_NORECORD = 0, 2

ARCHIVE_DIRNAME = "failed-rounds"
BLOCKED_RECORD = "round-blocked.json"
MERGE_RECORD = "daily-merge.json"

#: daily_merge's clean states. A row outside them is a fork that was not
#: published, which is the subject we are asking about across rounds.
MERGE_CLEAN = ("MERGED", "ALREADY_CURRENT", "WOULD_MERGE")

VERDICTS = ("REPEATING_SAME_SUBJECT", "REPEATING_STAGE_ONLY", "ONCE",
            "UNDETERMINED")


def _read_round(d: Path) -> dict | None:
    """One retained round: when it was, which stages refused, which forks."""
    try:
        blocked = json.loads((d / BLOCKED_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(blocked, dict):
        return None
    rows = blocked.get("blocked_by")
    if not isinstance(rows, list):
        return None
    stages = {r.get("stage") for r in rows if isinstance(r, dict)}
    subjects: set = set()
    try:
        merge = json.loads((d / MERGE_RECORD).read_text(encoding="utf-8"))
        for row in merge.get("forks") or []:
            if isinstance(row, dict) and row.get("state") not in MERGE_CLEAN:
                subjects.add((row.get("repo"), row.get("state")))
    except (OSError, ValueError):
        # A round whose stage record exists and whose merge record does not still
        # counts as a round the stage refused in. Dropping it would make a
        # generator look like a transient.
        pass
    round_at = blocked.get("round_at") or d.name
    return {"dir": d.name, "round_at": round_at,
            # THE UNIT IS A DAY, NOT A DIRECTORY. `check_publish_silence`'s
            # escalation keys itself `f"{kind}-{round_at[:10]}"` precisely because
            # "a round that fails four times in a day does not open four PRs" —
            # and a verdict that counted four archive directories as four rounds
            # would turn ONE bad morning into REPEATING, which is the false
            # certificate this program exists to avoid producing.
            "day": (str(round_at) or "undated")[:10] or "undated",
            "stages": stages, "subjects": subjects}


def history(state_dir: Path) -> tuple:
    """(rc, rounds) — every retained round, oldest first. Names sort in time."""
    root = state_dir / ARCHIVE_DIRNAME
    if not root.is_dir():
        return RC_NORECORD, []
    rounds = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        r = _read_round(d)
        if r is not None:
            rounds.append(r)
    return RC_OK, rounds


def by_day(rounds: list) -> list:
    """One entry per DAY, oldest first; a day's runs are one observation.

    Subjects are UNIONED within a day: two runs on one morning are two looks at
    the same morning, so a fork that refused in either of them refused that day.
    """
    days: dict = {}
    for r in rounds:
        d = days.setdefault(r["day"], {"day": r["day"], "stages": set(),
                                       "subjects": set(), "runs": 0})
        d["stages"] |= r["stages"]
        d["subjects"] |= r["subjects"]
        d["runs"] += 1
    return [days[k] for k in sorted(days)]


def verdict(rounds: list, stage: str) -> dict:
    """Is `stage`'s refusal a generator or an incident?"""
    rounds = by_day(rounds)
    carrying = [r for r in rounds if stage in r["stages"]]
    out = {"stage": stage, "retained_rounds": len(rounds),
           "rounds_with_this_stage": len(carrying),
           "round_ats": [r["day"] for r in carrying],
           "verdict": "UNDETERMINED", "shared_subjects": [], "why": ""}
    if len(carrying) < 2:
        out["verdict"] = "ONCE" if len(carrying) == 1 and len(rounds) >= 2 \
            else "UNDETERMINED"
        out["why"] = (
            f"{len(carrying)} of {len(rounds)} retained day(s) carry a "
            f"{stage} refusal; at least two are needed to compare, so this is "
            f"not evidence of a transient"
            if out["verdict"] == "UNDETERMINED" else
            f"{stage} refused on exactly one of {len(rounds)} retained days")
        return out

    with_subjects = [r for r in carrying if r["subjects"]]
    shared = set.intersection(*[r["subjects"] for r in with_subjects]) \
        if with_subjects else set()
    if shared:
        out["verdict"] = "REPEATING_SAME_SUBJECT"
        out["shared_subjects"] = sorted(f"{repo} {state}"
                                       for repo, state in shared)
        out["why"] = (
            f"{stage} refused on {len(carrying)} of {len(rounds)} retained "
            f"days, on the SAME subject every time: "
            + ", ".join(out["shared_subjects"])
            + " — the moving-target signature; it will not clear on its own")
    else:
        out["verdict"] = "REPEATING_STAGE_ONLY"
        out["why"] = (
            f"{stage} refused on {len(carrying)} of {len(rounds)} retained "
            f"days, on a DIFFERENT subject each time"
            + (" (no day retained a per-fork record)"
               if not with_subjects else "")
            + " — the stage is not the subject, its environment is; repairing an "
              "assertion here would repair the wrong thing")
    return out


def report(state_dir: Path, stage: str) -> tuple:
    rc, rounds = history(state_dir)
    if rc != RC_OK:
        return rc, (f"[NORECORD] no refusal archive at "
                    f"{state_dir / ARCHIVE_DIRNAME} — whether this refusal has "
                    f"happened before COULD NOT BE ASKED, which is not the same "
                    f"as it not having happened")
    v = verdict(rounds, stage)
    lines = [f"[{v['verdict']}] {v['why']}"]
    if v["round_ats"]:
        lines.append("  days: " + ", ".join(str(x) for x in v["round_ats"]))
    return rc, "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", required=True,
                    help="the stage whose refusal history to put the question to")
    ap.add_argument("--state-dir", default=os.environ.get(
        "GK_STATE_DIR", "/home/reyerchu/.cache/eda-fork-gatekeeper"))
    a = ap.parse_args(argv)
    rc, text = report(Path(a.state_dir), a.stage)
    print(text)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
