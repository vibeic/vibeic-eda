#!/usr/bin/env python3
"""check_round_code_is_current — the round must not publish from superseded code.

WHY
===
The 05:30 round runs out of a long-lived checkout, and NOTHING in `run_0530.sh`
or `run_tick.sh` updates it. Measured 2026-08-03: that checkout was 5 commits
behind `origin/main`, and among those commits was the fix that changed HOW the
published numbers are computed — `pin..HEAD` to `pin..origin/<branch>`, after
HEAD was shown to overcount (another session's in-progress branch) and undercount
(a clone that had not fetched) in the same sweep.

So every fix landed in `fork-gatekeeper/` was invisible to the thing that runs it,
and the round would have published today's date over a number produced by a method
already known to be wrong. That is worse than not publishing: a stale page is
obviously stale, while a fresh page computed by superseded logic is not.

This is the same shape as the plugin-cache lag the fleet has already paid for
(a fix on `origin/main` is not live until the cache re-syncs). Landing is not
deploying, and a pipeline that cannot tell the difference reports success either way.

WHAT IT CHECKS, AND WHAT IT DELIBERATELY DOES NOT
=================================================
Not "is the checkout behind" — that fires on a README edit and would train
everyone to ignore it. The question is narrower and exactly answerable:

    did any PROGRAM THIS ROUND EXECUTES change between HEAD and origin/main?

`git diff --name-only HEAD..origin/main -- fork-gatekeeper/*.py fork-gatekeeper/*.sh`
answers it. A docs-only commit leaves the round alone; a one-line change to
`discover_forks.py` stops it publishing.

EXIT
    0  the programs this round runs are identical to origin/main
    1  at least one changed — the round is executing superseded logic
    2  could not tell (no remote, fetch failed). NOT the same as 0, and the
       caller decides: this repo blocks the PUBLISH on 1 but not on 2, because a
       proven-stale program and a transient network failure are different risks
       and only the first is a reason to leave yesterday's page up.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def _git(repo: Path, *args: str, timeout: int = 120) -> Tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return r.returncode, (r.stdout or r.stderr).strip()


def changed_programs(repo: Path, ref: str, subdir: str) -> Optional[List[str]]:
    rc, out = _git(repo, "diff", "--name-only", f"HEAD..{ref}", "--",
                   f"{subdir}/*.py", f"{subdir}/*.sh")
    if rc != 0:
        return None
    return [x for x in out.splitlines() if x.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--subdir", default="fork-gatekeeper")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args(argv)

    if not a.no_fetch:
        rc, out = _git(a.repo, "fetch", "-q", "origin", timeout=180)
        if rc != 0:
            print(f"check_round_code_is_current: rc=2 NOT CHECKED — fetch failed: {out[:200]}")
            return 2
    rc, _ = _git(a.repo, "rev-parse", "--verify", "-q", a.ref)
    if rc != 0:
        print(f"check_round_code_is_current: rc=2 NOT CHECKED — no {a.ref}")
        return 2

    changed = changed_programs(a.repo, a.ref, a.subdir)
    if changed is None:
        print("check_round_code_is_current: rc=2 NOT CHECKED — diff failed")
        return 2

    _, behind = _git(a.repo, "rev-list", "--count", f"HEAD..{a.ref}")
    if not changed:
        print(f"check_round_code_is_current: PASS — every program under {a.subdir}/ "
              f"matches {a.ref} ({behind or '?'} commit(s) behind overall, none of "
              f"them touching what this round executes)")
        return 0

    print(f"check_round_code_is_current: FAIL — this round would execute superseded "
          f"code. {len(changed)} program(s) differ from {a.ref} "
          f"({behind or '?'} commit(s) behind):")
    for f in changed:
        print(f"    {f}")
    print(f"    Remedy: update this checkout ({a.repo}) to {a.ref} before the round "
          f"publishes. Note the working tree may hold pin bumps a release step "
          f"proposed; those are a separate decision, not something to discard blindly.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
