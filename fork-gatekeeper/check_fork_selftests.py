#!/usr/bin/env python3
"""Run each fork's DECLARED post-merge checks on the clone the image is built from.

WHY THIS EXISTS, AND WHY IT IS NOT A SECOND MECHANISM (vibe-ic#813)
------------------------------------------------------------------
vibeic-eda#89 built the right thing: a fork DECLARES its own invariants in
`FORKS.json` under `post_merge_check`, and `daily_merge.py` runs them in the
merge worktree AFTER the merge and BEFORE the push, so a failing check leaves
the fork at its previous tip and publishes nothing. That is strictly stronger
than reporting afterwards, and this program does not duplicate it, replace it,
or re-declare anything. There is ONE declaration and ONE implementation of
"run a declared check and judge it" -- `daily_merge.run_post_merge_checks`,
imported below rather than reimplemented.

What it adds is the case that mechanism CANNOT reach, which is visible in
`daily_merge.py` itself:

    if behind == 0:
        res.update(state="ALREADY_CURRENT", ...)
        return res          # <- returns BEFORE run_post_merge_checks

The checks are MERGE-TRIGGERED. On a day upstream is level there is no merge
worktree and nothing runs. That is not a hypothetical gap here: the 27 unwired
tests in vibe-ic#813 arrived through OUR OWN patches, not through an upstream
merge, so `post_merge_check` would not have caught a single one of them. Same
for a fix that lands on the fork by any route other than the daily merge.

So the two call sites answer two different questions:

    daily_merge.py   "may this MERGE be published?"      blocks the push
    this program     "is the tree we BUILD FROM still    fails the tick
                      clean today, however it got here?"

Every day, on the checked-out clone, against the same declared commands.

THREE STATES
------------
    PASS             the fork's own check ran and was clean
    FAIL             it ran and was red
    COULD-NOT-CHECK  clone absent, checker absent from the tree, malformed
                     declaration, could not execute, timed out

`daily_merge` deliberately collapses the last two into one "do not publish"
verdict, which is correct where the only decision is push-or-not. Here the
verdict is a REPORT, and "the fork deleted the checker" and "the checker found
something" need different actions, so they are named differently. Both still
fail the tick -- absence is never a pass.

EXIT
----
    0   every declared check ran and passed
    1   at least one ran and FAILED
    2   nothing failed, but at least one COULD NOT BE CHECKED

Usage:
    python3 check_fork_selftests.py [--forks-dir DIR] [--tool NAME]
                                    [--json OUT.json] [--log-dir DIR]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEFAULT_FORKS_DIR = Path(
    os.environ.get("GK_FORKS_DIR") or "/home/reyerchu/vibe-ic-forks")

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "COULD-NOT-CHECK"


def _daily_merge():
    """Import by ABSOLUTE path.

    A bare relative `spec_from_file_location` resolves against the WORKING
    DIRECTORY, and the tick runs from `/`. That exact shape already hid a whole
    suite in this repo once -- see `test_every_test_module_imports_by_ABSOLUTE_path`.
    """
    spec = importlib.util.spec_from_file_location(
        "_gk_daily_merge", str(HERE / "daily_merge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def declared(forks_json: Path | None = None) -> list[tuple[str, list[dict]]]:
    """[(tool, checks)] for every fork that declares post_merge_check.

    Read from FORKS.json, never from a list in this file. A second copy of a
    declaration is a second thing to forget: `build_branches` in daily_merge.py
    carries the same warning, learned the same way, from vibeic-eda#30.
    """
    f = forks_json or (HERE / "FORKS.json")
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: cannot read {f}: {exc}")
    out = []
    for entry in data.get("forks", []):
        if entry.get("post_merge_check"):
            out.append((entry.get("tool", ""), entry["post_merge_check"]))
    return out


def check_fork(tool: str, checks: list[dict], forks_dir: Path,
               dm, log_dir: Path | None) -> list[dict]:
    clone = forks_dir / tool
    if not clone.is_dir() or not (clone / ".git").exists():
        # One row per declared check, so a missing clone is as loud as a missing
        # checker rather than one line that hides how much went unasked.
        return [{
            "tool": tool,
            "name": str((c.get("name") or c.get("path") or "unnamed")
                        if isinstance(c, dict) else "malformed"),
            "state": UNKNOWN, "rc": None, "head": None,
            "reason": f"clone absent: {clone} — nothing was checked",
            "why": c.get("why", "") if isinstance(c, dict) else "",
        } for c in checks]

    head = None
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=str(clone), capture_output=True, text=True,
                              timeout=60).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass

    rows = []
    # THE SHARED IMPLEMENTATION. Same declaration, same runner, same judgement
    # as the pre-push gate, so the two call sites cannot drift into disagreeing
    # about what "clean" means.
    for row, decl in zip(dm.run_post_merge_checks(clone, checks), checks):
        if row["ok"]:
            state = PASS
        elif row["rc"] in (-1, 127):
            # daily_merge's own codes: -1 malformed or absent, 127 could-not-run.
            state = UNKNOWN
        else:
            state = FAIL
        rows.append({
            "tool": tool, "name": row["name"], "state": state, "rc": row["rc"],
            "head": head,
            "reason": "" if state == PASS else row["detail"],
            "why": decl.get("why", "") if isinstance(decl, dict) else "",
        })
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"fork-selftest-{tool}.json").write_text(
                json.dumps(rows, indent=2), encoding="utf-8")
        except OSError:
            pass
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--forks-dir", default=str(DEFAULT_FORKS_DIR))
    ap.add_argument("--tool", action="append", help="only these tools")
    ap.add_argument("--forks-json", help="override FORKS.json (tests)")
    ap.add_argument("--json", help="write the full verdict here")
    ap.add_argument("--log-dir", help="write per-fork detail here")
    args = ap.parse_args(argv)

    forks_dir = Path(args.forks_dir)
    log_dir = Path(args.log_dir) if args.log_dir else None
    dm = _daily_merge()

    decls = declared(Path(args.forks_json) if args.forks_json else None)
    if args.tool:
        decls = [(t, c) for t, c in decls if t in args.tool]
    if not decls:
        # A registry that declares nothing is not a clean fleet, it is an
        # unasked question -- and it is what a bad edit to FORKS.json looks like.
        print("COULD-NOT-CHECK: no fork declares post_merge_check in FORKS.json")
        print("\nfork self-checks: 0 declared — nothing was asked, which is "
              "not a clean result")
        return 2

    results = []
    for tool, checks in decls:
        results += check_fork(tool, checks, forks_dir, dm, log_dir)

    failed = [r for r in results if r["state"] == FAIL]
    unknown = [r for r in results if r["state"] == UNKNOWN]

    for r in results:
        line = f"  {r['state']:<15} {r['tool']}:{r['name']}"
        if r["head"]:
            line += f"  @{r['head']}"
        print(line)
        if r["reason"]:
            # Fixed, greppable prefix. The tick surfaces a FILTERED view of this
            # into its log, and for COULD-NOT-CHECK the reason IS the
            # information: "clone absent" and "the fork dropped the checker"
            # need different actions. A filter that showed only the verdict
            # would print the word and nothing anyone could act on.
            print(f"      -> {r['reason']}")

    print(f"\nfork self-checks: {len(results)} declared, "
          f"{len(results) - len(failed) - len(unknown)} pass, "
          f"{len(failed)} FAIL, {len(unknown)} COULD-NOT-CHECK")

    if failed:
        print("\nFAIL: a fork's own declared check is red on the tree the image "
              "is built from:")
        for r in failed:
            print(f"  {r['tool']}:{r['name']} — {r['why'][:300]}")

    if unknown:
        print("\nCOULD NOT CHECK — this is NOT a pass. The checker lives inside "
              "the tree it audits, so its absence and its silence look alike:")
        for r in unknown:
            print(f"  {r['tool']}:{r['name']} — {r['reason']}")

    if args.json:
        try:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(json.dumps({
                "forks_dir": str(forks_dir),
                "declared": len(results),
                "failed": len(failed),
                "could_not_check": len(unknown),
                "results": results,
            }, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"  (could not write --json: {exc})")

    if failed:
        return 1
    if unknown:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
