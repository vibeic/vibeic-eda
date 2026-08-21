#!/usr/bin/env python3
"""Commit a working tree left dirty with NOTHING BUT pin edits, or refuse.

A release writes the pin files BEFORE it builds, and commits them only after the
image is in a registry.  So a release that builds and then cannot publish leaves
those edits uncommitted — and that poisons every LATER round:

    daily_release writes pins -> build OK -> push fails (network) -> no commit
    next round: the page-publication tests are integration tests that check the
      REAL repository at the publication boundary, so a dirty tree turns 5 of
      them red -> the semantic pytest gate is RED -> run_tick is NOT CHECKED ->
      the release never retries -> the pins stay dirty -> forever.

MEASURED 2026-08-21: round 11 built 0.3.20 ("gates: 10/10", "smoke: all 11 tools
run"), lost the upload to `net/http: timeout awaiting response headers`, and
round 12 died on exactly those 5 tests without ever reaching the retry.

A deadlock that needs a human after every network hiccup is not a flow.  This
closes it at the START of a round, before the gate, and it is deliberately
NARROW: the only thing it will commit is a file whose entire working-tree diff
is pin-shaped, judged by the same `_diff_is_only_pins` the release commit uses.
Anything else — an untracked file, a deletion, a rename, a staged entry, a
source edit — is REFUSED AND NAMED.  A guard that quietly degrades to "nothing
to do" would report success while the deadlock stayed, so refusing is the only
other outcome this has.

The pins it commits are not a claim that anything shipped.  A tree ahead of the
released image is the PINS_AHEAD state `check_release_recorded` already models
as benign, and the next release closes it.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import daily_release as DR  # noqa: E402

RC_OK, RC_REFUSED = 0, 1

COMMIT_SUBJECT = "pins: commit the advance a build could not publish"


def _porcelain(eda_root: Path) -> Tuple[List[str], str]:
    """Every path git considers not-committed, with its two status columns."""
    proc = subprocess.run(
        ["git", "-C", str(eda_root), "status", "--porcelain", "-z",
         "--untracked-files=all"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return [], f"git status failed: {proc.stderr.strip()[:200]}"
    # -z so a path with a space or a quote cannot be mis-split. Rename records
    # carry a second NUL-separated path, which `_classify` refuses anyway.
    return [f for f in proc.stdout.split("\0") if f], ""


def _classify(entries: List[str]) -> Tuple[List[str], List[str]]:
    """Split into (pin candidates, refusals). Only ` M` — unstaged modification
    of a tracked file — can be a candidate. A STAGED change is someone mid-edit,
    a deletion or rename is not a pin advance, and untracked is not ours."""
    candidates: List[str] = []
    refusals: List[str] = []
    for entry in entries:
        status, rel = entry[:2], entry[3:]
        if status == " M":
            candidates.append(rel)
        else:
            refusals.append(f"{rel} ({status!r} is not an unstaged edit)")
    return candidates, refusals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root", default=str(_HERE.parent))
    ap.add_argument("--dry-run", action="store_true",
                    help="report the verdict without committing")
    a = ap.parse_args(argv)
    root = Path(a.eda_root).resolve()

    entries, err = _porcelain(root)
    if err:
        print(f"[FAIL] {err}")
        return RC_REFUSED
    if not entries:
        print("[PASS] working tree is clean; nothing to recover")
        return RC_OK

    candidates, refusals = _classify(entries)
    for rel in candidates:
        only_pins, why = DR._diff_is_only_pins(root, rel)
        if not only_pins:
            refusals.append(f"{rel} ({why or 'diff is not pin-shaped'})")
    if refusals:
        print("[FAIL] the working tree is dirty with work this cannot own, so "
              "the pin advance is NOT committed. Each path and why:")
        for line in refusals:
            print(f"  - {line}")
        return RC_REFUSED

    print(f"[PASS] {len(candidates)} pin file(s) carry nothing but pin edits: "
          + ", ".join(sorted(candidates)))
    if a.dry_run:
        return RC_OK

    body = (
        f"{COMMIT_SUBJECT}\n\n"
        "A release writes its pins before building and commits them only after\n"
        "the image is in a registry, so a build that cannot publish leaves them\n"
        "uncommitted — which turns the next round's page-publication tests red\n"
        "and stops the release that would retry. Committed by explicit path,\n"
        "every diff verified pin-shaped. This claims nothing about publication:\n"
        "a tree ahead of the released image is PINS_AHEAD, and the next release\n"
        "closes it.\n")
    add = subprocess.run(["git", "-C", str(root), "add", "--"] + candidates,
                         capture_output=True, text=True)
    if add.returncode != 0:
        print(f"[FAIL] could not stage the pin files: {add.stderr.strip()[:200]}")
        return RC_REFUSED
    commit = subprocess.run(["git", "-C", str(root), "commit", "-m", body,
                             "--"] + candidates, capture_output=True, text=True)
    if commit.returncode != 0:
        print(f"[FAIL] could not commit the pin files: "
              f"{commit.stderr.strip()[:200]}")
        return RC_REFUSED
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    print(f"[PASS] committed the unpublished pin advance as {head}")
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
