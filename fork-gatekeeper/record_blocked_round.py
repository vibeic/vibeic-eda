#!/usr/bin/env python3
"""Say, positively, that this round refused before the build.

A RECORD THAT IS ABSENT AND A RECORD THAT SAYS "FAILED" ARE DIFFERENT STATES,
and a reader who cannot tell them apart cannot tell a failed round from a round
that never ran.

`run_tick.sh` deletes `daily-release.json` when a pre-release stage refuses, and
that deletion is right: leaving yesterday's successful sidecar in place would let
it be read as evidence from a release that was never authorized today. But what
it leaves behind is an ABSENCE, and on the 2026-08-25, 2026-09-07 and 2026-09-08
rounds that absence was the ENTIRE content of the alert PR the round opened:
"(the release record could not be read — …/daily-release.json does not exist)",
three times, byte-identical.

So the refusal is recorded here instead, under a name of its own. NOT
`daily-release.json`: every consumer of that path reads it as a release record,
and a release record describing a release that never happened is exactly the
false certificate this repo keeps producing one layer down.

IT RECORDS EVERY BLOCKING STAGE, not just one. `run_tick`'s status chain is
first-wins (`[ "${rc}" = "0" ] && rc=<n>`), so a round where `daily_merge` AND
the fork self-checks both refused exits 4 and says nothing at all about 8.

    rc 0  the record was written
    rc 1  it could not be written
    rc 2  no stage was named as blocking, so there is nothing to record — a
          caller that reaches this program with four zeroes has a bug, and a
          record saying "BLOCKED by nothing" would hide it
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

RC_OK, RC_FAILED, RC_NOTHING_BLOCKING = 0, 1, 2

RECORD_NAME = "round-blocked.json"

#: The four stages whose non-zero result sets `blocking_rc` in `run_tick.sh`, in
#: the order that script evaluates them, each with the evidence file it leaves in
#: the state dir. `test_a_blocked_round_names_the_stage_that_refused` proves this
#: list is the same set the script actually gates on.
BLOCKING_STAGES = (
    ("source guards", "guard_rc", "source-guards.txt"),
    ("daily_merge", "merge_rc", "daily-merge.txt"),
    ("vendored closure", "vendored_rc", "vendored-closure.txt"),
    ("fork self-checks", "selftest_rc", "fork-selftests.txt"),
)


def record(rcs: dict, round_at: str = "") -> dict:
    """The record's content. `rcs` maps rc_var -> the stage's exit code."""
    blocked = [{"stage": stage, "rc_var": var, "rc": int(rcs.get(var, 0) or 0),
                "evidence": evidence}
               for stage, var, evidence in BLOCKING_STAGES
               if int(rcs.get(var, 0) or 0) != 0]
    return {
        "schema_version": 1,
        "program": "run_tick",
        "state": "BLOCKED",
        "release_attempted": False,
        "round_at": round_at or None,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "blocked_by": blocked,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", default=os.environ.get(
        "GK_STATE_DIR", "/home/reyerchu/.cache/eda-fork-gatekeeper"))
    ap.add_argument("--round-at", default="")
    for _, var, _ in BLOCKING_STAGES:
        ap.add_argument(f"--{var.replace('_', '-')}", default="0")
    a = ap.parse_args(argv)

    rcs = {var: getattr(a, var) for _, var, _ in BLOCKING_STAGES}
    body = record(rcs, a.round_at)
    if not body["blocked_by"]:
        print("[NOT RECORDED] no blocking stage was named — nothing refused, so "
              "there is no refusal to record")
        return RC_NOTHING_BLOCKING
    path = Path(a.state_dir) / RECORD_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        print(f"[FAILED] could not write {path}: {exc}")
        return RC_FAILED
    print(f"[RECORDED] {path} — "
          + "; ".join(f"{r['stage']} rc={r['rc']}" for r in body["blocked_by"]))
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
