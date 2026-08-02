#!/usr/bin/env python3
"""check_ledger_is_fresh — a ledger that stopped advancing must say so.

WHY (vibeic-eda#58)
===================
The daily tick published NOTHING for three consecutive days and produced no
alert. It exited 1 into a log, and the only visible symptom was a public page
that did not move — noticed by a person, on the third day.

    2026-07-31   last daily report written
    2026-08-01   tick exit 1, nothing published
    2026-08-02   tick exit 1, nothing published
    ledger generated_at, all three days: 2026-08-01T22:51 (a manual refresh)

THE HALF THAT MAKES IT INVISIBLE, and the reason a plain "is the file old?"
check is not enough: the tick has two halves, and only one of them died. The
SYNC half kept working and merged real upstream commits on both days —

    2026-08-01  OpenROAD +28, verilator +5, yosys +2
    2026-08-02  OpenROAD +20, verilator +5, yosys +2

— while the PUBLISH half was refusing. So from the output alone, "the ledger
stopped advancing" and "upstream was quiet, so nothing changed" are the same
picture. That is the shape this repo keeps paying for: a failed question and a
clean negative wearing one representation.

WHAT THIS CHECKS
================
1. STALE: the newest `generated_at` across the ledger is older than
   `--max-age-hours` (default 30 — one daily cycle plus slack, so an ordinary
   run that lands a little late is not an alarm).

2. DIVERGED (the load-bearing one): the fork CLONES carry commits newer than
   the ledger's own timestamp. That is the sync-alive/publish-dead state
   exactly, and it is a FACT ABOUT TWO ARTEFACTS rather than a guess: if we
   merged upstream work after the ledger was written, the ledger describes a
   tree that no longer exists.

Both are derived from timestamps the pipeline already writes. Nothing is
enumerated, so a new tool or a new PDK needs no edit here.

EXIT
    0  fresh
    1  stale and/or diverged — the message names which, and by how much
    2  could not tell (no ledger, unparseable timestamps). A check that cannot
       run has NOT passed; see the rc=2 convention in tools/git-hooks/pre-push.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple


def _parse(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.strip())
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def newest_generated_at(ledger: Path) -> Tuple[Optional[datetime], int]:
    """The newest `generated_at` in the ledger, and how many rows carried one."""
    newest, n = None, 0
    for f in sorted(ledger.glob("*.json")):
        if f.name == "index.json":
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        t = _parse(d.get("generated_at") or "")
        if t is None:
            continue
        n += 1
        if newest is None or t > newest:
            newest = t
    return newest, n


def newest_clone_commit(forks_root: Path, limit: int = 60) -> Optional[datetime]:
    """The newest commit time across the fork clones we actually build from.

    Read from git, not from a file we also write — the point is to compare two
    INDEPENDENT records. A clone we cannot read is skipped rather than assumed
    quiet.
    """
    newest = None
    if not forks_root.is_dir():
        return None
    for d in sorted(forks_root.iterdir())[:limit]:
        if not (d / ".git").is_dir():
            continue
        try:
            r = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%cI"],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        t = _parse(r.stdout.strip())
        if t and (newest is None or t > newest):
            newest = t
    return newest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", type=Path,
                    default=Path.home() / ".cache/eda-fork-gatekeeper/ledger")
    ap.add_argument("--forks-root", type=Path,
                    default=Path("/home/reyerchu/vibe-ic-forks"))
    ap.add_argument("--max-age-hours", type=float, default=30.0)
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)

    if not a.ledger.is_dir():
        print(f"check_ledger_is_fresh: rc=2 NOT CHECKED — no ledger at {a.ledger}")
        return 2
    gen, rows = newest_generated_at(a.ledger)
    if gen is None:
        print(f"check_ledger_is_fresh: rc=2 NOT CHECKED — {rows} row(s) carried a "
              f"parseable generated_at in {a.ledger}")
        return 2

    now = datetime.now(timezone.utc)
    age_h = (now - gen).total_seconds() / 3600.0
    problems: List[str] = []

    if age_h > a.max_age_hours:
        problems.append(
            f"STALE: the newest generated_at is {gen.isoformat()} "
            f"({age_h:.1f}h old, limit {a.max_age_hours:.0f}h). The daily tick has "
            f"not published.")

    clone = newest_clone_commit(a.forks_root)
    if clone is not None and clone > gen:
        lag = (clone - gen).total_seconds() / 3600.0
        problems.append(
            f"DIVERGED: a fork clone carries a commit from {clone.isoformat()}, "
            f"{lag:.1f}h NEWER than the ledger. The sync half is working and the "
            f"publish half is not — from the output alone this is indistinguishable "
            f"from 'upstream was quiet' (vibeic-eda#58).")

    out = {"generated_at": gen.isoformat(), "age_hours": round(age_h, 2),
           "rows_with_timestamp": rows,
           "newest_clone_commit": clone.isoformat() if clone else None,
           "problems": problems, "verdict": "FAIL" if problems else "PASS"}
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    if problems:
        print(f"check_ledger_is_fresh: FAIL — {len(problems)} problem(s)")
        for p in problems:
            print(f"    {p}")
        return 1
    print(f"check_ledger_is_fresh: PASS — ledger written {age_h:.1f}h ago "
          f"({rows} tools), no clone newer than it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
