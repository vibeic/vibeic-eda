#!/usr/bin/env python3
"""Nothing should be sitting in the tree that is neither tracked nor ignored.

WHY (vibeic-eda 2026-07-30, same class as vibe-ic#720)
======================================================
Sixteen files sat untracked-and-unignored in this repository for eight to eleven
days: `.bak` copies of the Dockerfile and the fork ledgers, build logs from a
failed 0.2.27 attempt, an ALIGN verification dump, a ledger-backup directory.

Untracked-but-not-ignored is the state one `git add -A` turns into a commit,
which is exactly why this repo forbids `-A`. That prohibition is a rule everyone
must remember; this is the rule nobody has to.

THE RULE THAT WAS THERE DID NOT COVER IT
========================================
`.gitignore` already carried `*.log`, and `build_0.2.27.log.attempt1` was not
matched — `.log` is not at the end. A rule written for the case its author met,
which is the shape of the whole problem and the reason this checks the OUTCOME
rather than the presence of any particular pattern.

WHAT IT DELIBERATELY DOES NOT DO
================================
It does not delete anything. A file the author is still using is not litter, and
a guard that tidies up behind people gets switched off. It names what is there
and refuses; ignoring it or removing it is a decision with a name on it.

Exit: 0 the tree is clean, 1 something is neither tracked nor ignored,
2 the question could not be asked.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import List

RC_OK, RC_FINDING, RC_NOTHING = 0, 1, 2


def untracked(root: Path) -> tuple:
    """Paths git reports as untracked, i.e. NOT covered by .gitignore.

    `git status --porcelain` already excludes ignored paths, so this is the
    question asked of git rather than a second implementation of its matching —
    the lesson from vibe-ic#555, where my model of what a negation inside an
    excluded directory does was wrong and git's answer was right.
    """
    r = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        return None, f"git status failed: {r.stderr.strip()[:160]}"
    return [ln[3:] for ln in r.stdout.splitlines() if ln.startswith("?? ")], ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    root = Path(a.root)
    found, err = untracked(root)
    if found is None:
        print(f"[NOT CHECKED] {err} — nothing was examined, which is not a "
              f"clean result", file=sys.stderr)
        return RC_NOTHING

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "untracked_artefact_guard", "root": str(root),
             "untracked": sorted(found)}, indent=2) + "\n", encoding="utf-8")

    print(f"untracked_artefact_guard: {len(found)} path(s) neither tracked nor "
          f"ignored under {root}")
    for f in sorted(found):
        print(f"  {f}")

    if found:
        print(f"[FAIL] {len(found)} path(s) are one `git add -A` from being "
              f"committed. Either ignore them or remove them — this program "
              f"will not choose, because a file someone is still using is not "
              f"litter.", file=sys.stderr)
        return RC_FINDING
    print("[PASS] every path in the tree is tracked or deliberately ignored")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
