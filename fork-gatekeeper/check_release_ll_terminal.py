#!/usr/bin/env python3
"""Authorize a zero 05:30 exit from this round's complete Release LL.

``capture_release_ll`` returning zero means it formed a valid audit record.  It
does not mean the delivery converged: a valid record can deliberately say
``finished_with_gap``.  The outer cron used to confuse those two facts and exit
zero with release lag still open.  This checker keeps them separate and grants
terminal success only when the exact current round says ``round_converged``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional


RC_CONVERGED, RC_OPEN, RC_NORECORD = 0, 1, 2


def assess(path: Path, round_at: str, run_id: str) -> tuple[int, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return RC_NORECORD, f"Release LL is missing or unreadable: {exc}"
    if not isinstance(value, dict) or value.get("program") != "release_ll":
        return RC_NORECORD, "record is not a Release LL object"
    if value.get("round_at") != round_at or value.get("run_id") != run_id:
        return RC_NORECORD, "Release LL belongs to a different 05:30 round"
    errors = value.get("source_errors")
    convergence = value.get("convergence")
    if (not isinstance(errors, list) or errors
            or not isinstance(convergence, dict)
            or convergence.get("round_measured") is not True
            or type(convergence.get("round_converged")) is not bool):
        return RC_NORECORD, "Release LL has incomplete convergence authority"
    zero_fields = (
        "sync_lag_at_merge", "release_lag", "ours_past_pin",
        "ours_past_pin_substantive", "our_commits_stranded",
    )
    exact = {name: convergence.get(name) for name in zero_fields}
    if not all(type(value) is int and value >= 0 for value in exact.values()):
        return RC_NORECORD, "Release LL convergence counters are not exact integers"
    if convergence["round_converged"] is True and any(exact.values()):
        return RC_NORECORD, (
            "Release LL claims convergence while a release-boundary counter "
            "is nonzero"
        )
    if convergence["round_converged"] is not True:
        return RC_OPEN, (
            "current round finished with a delivery gap "
            f"(sync={convergence.get('sync_lag_at_merge')}, "
            f"release={convergence.get('release_lag')}, "
            f"image={convergence.get('image_behind_upstream')}, "
            f"past-pin={convergence.get('ours_past_pin')})"
        )
    return RC_CONVERGED, "current round Release LL is converged"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release-ll", type=Path, required=True)
    parser.add_argument("--round-at", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    rc, detail = assess(args.release_ll, args.round_at, args.run_id)
    label = {RC_CONVERGED: "PASS", RC_OPEN: "OPEN", RC_NORECORD: "NORECORD"}[rc]
    print(f"check_release_ll_terminal: {label} — {detail}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
