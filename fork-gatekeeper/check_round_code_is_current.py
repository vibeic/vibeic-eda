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
       caller blocks the publish on both 1 and 2 while retaining the distinction:
       proven stale and unmeasured are different evidence, but neither is
       permission to ship.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import progress_watchdog as watchdog


FETCH_STALL_GRACE_S = float(
    os.environ.get("GK_CODE_FETCH_STALL_GRACE", "300")
)
PROGRESS_POLL_S = float(os.environ.get("GK_GIT_PROGRESS_POLL", "1"))


def _git(repo: Path, *args: str, timeout: Optional[int] = None,
         progress: bool = False) -> Tuple[int, str]:
    argv = ["git", "-C", str(repo), *args]
    try:
        if progress:
            r = watchdog.run_supervised(
                argv, stall_grace_s=FETCH_STALL_GRACE_S,
                poll_s=PROGRESS_POLL_S, output_progress=True,
                process_tree_cpu_progress=True,
            )
            return r.rc, (r.out or r.err).strip()
        kwargs = {"capture_output": True, "text": True}
        if timeout is not None:
            kwargs["timeout"] = timeout
        r = subprocess.run(argv, **kwargs)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return r.returncode, (r.stdout or r.stderr).strip()


#: Root-level files the round reads as FACT, not as documentation. They are in
#: this venue for the same reason the programs are: the round executes AGAINST
#: them, so a stale copy makes it act on superseded truth.
#:
#: MEASURED 2026-09-22. The venue was programs-only, and the PASS line claimed
#: "none of them touching what this round executes" — a claim wider than
#: anything it had looked at. That morning the checkout sat 5 commits behind,
#: two of which moved `RELEASED.json` from 0.3.69 to 0.3.71. This check printed
#: PASS; `check_release_recorded.py` then read the stale 0.3.69 record, compared
#: it against a registry already publishing 0.3.71, and reported
#: "publication is INCOMPLETE: :latest does not identify the recorded manifest".
#: Three tests went red on it and the whole tick stopped at
#: "run_tick NOT CHECKED — semantic pytest RED", pointing at a registry problem
#: that did not exist: `:latest` and `:0.3.71` agreed exactly. The publication
#: was complete; the CHECKOUT was behind, and this is the check whose job that
#: was. `run_0530.sh` already carries the branch that says so — CHECKOUT_NOTE,
#: "fix the checkout before reading the test as a defect" — and it stayed dark
#: because this returned 0.
_FACT_FILES = ("RELEASED.json", "FORKS.json")


def changed_programs(repo: Path, ref: str, subdir: str) -> Optional[List[str]]:
    rc, out = _git(repo, "diff", "--name-only", f"HEAD..{ref}", "--",
                   f"{subdir}/*.py", f"{subdir}/*.sh", *_FACT_FILES)
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
        rc, out = _git(a.repo, "fetch", "--progress", "origin",
                       progress=True)
        if rc != 0:
            kind = ("stalled with no forward progress" if
                    rc == watchdog.RC_STALLED else f"failed (rc={rc})")
            print("check_round_code_is_current: rc=2 NOT CHECKED — "
                  f"fetch {kind}: {out[:200]}")
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
              f"and every fact file it reads ({', '.join(_FACT_FILES)}) matches "
              f"{a.ref} ({behind or '?'} commit(s) behind overall, none of them "
              f"touching what this round executes or executes against)")
        return 0

    facts = [f for f in changed if f in _FACT_FILES]
    progs = [f for f in changed if f not in _FACT_FILES]
    what = " and ".join(
        p for p in (f"{len(progs)} program(s)" if progs else "",
                    f"{len(facts)} fact file(s)" if facts else "") if p)
    print(f"check_round_code_is_current: FAIL — this round would execute superseded "
          f"code or act on superseded facts. {what} differ from {a.ref} "
          f"({behind or '?'} commit(s) behind):")
    for f in changed:
        print(f"    {f}")
    print(f"    Remedy: update this checkout ({a.repo}) to {a.ref} before the round "
          f"publishes. Note the working tree may hold pin bumps a release step "
          f"proposed; those are a separate decision, not something to discard blindly.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
