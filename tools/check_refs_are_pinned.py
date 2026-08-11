#!/usr/bin/env python3
"""Every `ARG <NAME>_REF=` must name a commit, not a branch.

MEASURED 2026-08-12. The root Dockerfile carried

    ARG ASAP7SC_REF=main
    ARG ASAP7PDK_REF=main
    ARG ASAP7KL_REF=main

and cloned with `--branch ${REF}`, so the ASAP7 device-LVS source of truth --
the golden CDL, the HSPICE models and the KLayout tech -- was whatever those
branches pointed at on the morning of the build. Two builds of the SAME
vibeic-eda commit could ship different PDK data.

WHY EVERY EXISTING GATE WAS GREEN OVER IT
------------------------------------------
Not one of them was wrong. They were all asking a question this ref could not
answer. Every pin reader in the tree is spelled the same way:

    check_pins_current.py:146        ^ARG\\s+([A-Z0-9_]+)_REF=([0-9a-f]{40})
    check_release_pins_committed.py  ^ARG\\s+([A-Z0-9_]+)_REF=([0-9a-f]{40})
    inbound_survey.py:127            ^ARG\\s+([A-Z0-9_]+)_REF=([0-9a-f]{40})
    daily_release.py:216,322,422     ^ARG\\s+([A-Z0-9_]+)_REF=[0-9a-f]{40}
    daily_merge.py:316               ^ARG {arg}_REF=([0-9a-f]{40})
    pin_kinds.py:93,153              ...=([0-9a-f]{40})

The SHA is part of the MATCH. A ref reading `main` does not fail those
patterns -- it is not seen by them at all, so it appears in no inventory, no
disagreement report and no release record. `check_fork_only` did count the
three clone URLs among its 61 references, correctly, because its question is
"is every source ours" and the answer was yes. Nothing anywhere asked "should
this ref have been a commit?"

That is the gap this closes, and it is a CLASS, not an instance: any future
`_REF` written as a branch or tag is invisible to every one of the readers
above, in exactly the same way, for exactly the same reason.

WHAT IT CHECKS
--------------
For each `ARG <NAME>_REF=<value>` in the composing Dockerfile and every
`tools/*/Dockerfile`: the value must be 40 lowercase hex characters. A
`${VAR}`-valued default is followed one level so a legitimate indirection is
not called a branch.

Exit: 0 every ref is a commit · 1 at least one is not · 2 nothing to check
      (no Dockerfile found -- absence is not a pass)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
#: `[^\s#]+`, not `\S+`, and an optional trailing comment. Nearly every pinned
#: ref in this repo carries one:
#:     ARG OPENROAD_REF=cf27a31e50...  # pinned; branch master (0.2.22 ...)
#: A pattern anchored on the value ending the line matched 14 of the 35 refs
#: present and reported PASS -- a checker with a silent blind spot exactly the
#: shape of the defect it exists to find. The count is pinned by a test against
#: an independent grep so that cannot recur.
ARG_REF = re.compile(
    r"^\s*ARG\s+([A-Z0-9_]+_REF)\s*=\s*([^\s#]+)\s*(?:#.*)?$", re.M)
ARG_ANY = re.compile(
    r"^\s*ARG\s+([A-Z0-9_]+)\s*=\s*([^\s#]+)\s*(?:#.*)?$", re.M)
INDIRECT = re.compile(r"^\$\{?([A-Za-z0-9_]+)\}?$")


def dockerfiles(root: Path) -> list[Path]:
    out = []
    if (root / "Dockerfile").is_file():
        out.append(root / "Dockerfile")
    out += sorted(p for p in root.glob("tools/*/Dockerfile") if p.is_file())
    return out


def resolve(value: str, text: str) -> str:
    """Follow one level of `ARG X=${Y}` so an indirection is not called a branch."""
    m = INDIRECT.match(value)
    if not m:
        return value
    for name, val in ARG_ANY.findall(text):
        if name == m.group(1):
            return val
    return value


def scan(root: Path) -> tuple[list[tuple[str, int, str, str]], int]:
    findings, pinned = [], 0
    for f in dockerfiles(root):
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in ARG_REF.finditer(text):
            name, raw = m.group(1), m.group(2)
            value = resolve(raw, text)
            if SHA.match(value):
                pinned += 1
                continue
            line = text[: m.start()].count("\n") + 1
            findings.append((str(f.relative_to(root)), line, name, raw))
    return findings, pinned


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="vibeic-eda checkout")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    files = dockerfiles(root)
    if not files:
        print(f"check_refs_are_pinned: COULD NOT CHECK — no Dockerfile under {root}")
        return 2

    findings, pinned = scan(root)
    if findings:
        print(f"check_refs_are_pinned: {len(findings)} ref(s) name a branch or tag "
              f"instead of a commit:")
        for rel, line, name, raw in findings:
            print(f"  {rel}:{line}  {name}={raw}")
        print("\n  A ref that is not a SHA is not merely unpinned — it is INVISIBLE")
        print("  to every pin reader in this repo, all of which match")
        print("  `ARG <NAME>_REF=[0-9a-f]{40}` with the SHA as part of the pattern.")
        print("  It appears in no inventory, no disagreement report, no release")
        print("  record, and the image it builds is not reproducible.")
        return 1

    print(f"check_refs_are_pinned: PASS — {pinned} ref(s) across {len(files)} "
          f"Dockerfile(s), every one a commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
