#!/usr/bin/env python3
"""check_release_pins_committed — the review a release leaves outstanding.

WHY (vibeic-eda#99)
====================
`daily_release.write_released_record` computes `RELEASED.json`'s `pins` from
the WORKING TREE, measured AFTER `rewrite_pin` has already edited it — so the
record correctly names what the tree was BUILT FROM. `commit_release_record`
then commits exactly three files — `VERSION`, `RELEASED.json`, `README.md` —
and never the pin edits themselves. That is deliberate: a release run's tree
also holds the pin edits it made, and those are a separate decision that goes
through review (vibeic-eda#71's revert dda4b8c;
`test_only_the_two_record_files_are_committed` is LOAD-BEARING and this file
does not touch it — see "WHAT THIS DOES NOT DO" below).

Between the publish and that review, the repository is coherent about
everything except the one fact that matters:

    the published image was built from pin B
    HEAD (the commit) still states pin A
    RELEASED.json — committed — names pin B
    every existing pin check reads the WORKING TREE, which still holds pin B

`check_pins_agree.py`, `check_pin_descendants.py`, and even
`check_release_recorded.py`'s own `pins_moved` branch (which compares
`RELEASED.json` against `daily_release.pinned_refs(eda_root)` — the
filesystem, not a git revision) all read clean, because the working tree
agrees with itself. Observed directly (#99's own report): after 0.2.65
published, HEAD kept stating `OPENROAD_REF=f396ce8ee` while the image had
been built from `b64a496b9`, for the whole gap between publish and review.
Nothing anywhere said so — the one thing that was wrong was the one thing
nothing looked at.

WHAT THIS CHECKS
================
`RELEASED.json`'s recorded `pins` dict against the pins actually COMMITTED at
a git revision (`--rev`, default `HEAD`) — read with `git show <rev>:<path>`,
never the working tree. An uncommitted edit that makes the working tree agree
with `RELEASED.json` cannot make this check agree too, because this check
never reads the working tree at all. A fresh clone of `main` sees exactly what
this check sees.

WHAT THIS DOES NOT DO
======================
It does not commit the pins, and it must not be changed to. That is the
reverted change (vibeic-eda#71 / dda4b8c) — sweeping an unreviewed pin move
into a release commit is exactly what banning `git add -A` in this org exists
to prevent, and `test_only_the_two_record_files_are_committed` in
`test_release_record_is_committed.py` stops it happening again. The point of
this file is to make the outstanding review VISIBLE, not to remove the review.

THREE STATES
============
    RELEASED.json's pins == the pins committed at `--rev`
        PASS (rc 0)
    they disagree
        FAIL (rc 1), every disagreeing tool named with BOTH shas
    RELEASED.json is absent, unparseable, or has no `pins` object; or `--rev`
    cannot be read (not a git repo, bad revision, a pin file unreadable there)
        COULD-NOT-TELL (rc 2), never rendered as a pass — "we could not tell"
        is exactly how the gap this issue is about went unseen

Exit: 0 committed / 1 outstanding review (named) / 2 could not tell.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

TOOL = "check_release_pins_committed"
RC_OK, RC_OUTSTANDING, RC_COULD_NOT_TELL = 0, 1, 2

#: Mirrors `check_pins_current.pinned_refs`'s name-pairing exactly — matched by
#: the `github.com/vibeic/<repo>` URL in the same file, not by position, so a
#: file that pins two tools (`tools/lvs/Dockerfile` pins magic AND netgen)
#: cannot silently drop one of them. Not imported from there because that
#: module reads `Path.read_text()` off the FILESYSTEM; this one reads
#: `git show <rev>:<path>`, which is the entire point of this check, and a
#: shared helper that took either source would be one accidental filesystem
#: read away from the exact bug #99 is about.
REF_RE = re.compile(r"^ARG\s+([A-Z0-9_]+)_REF=([0-9a-f]{40})", re.M)
REPO_RE = re.compile(r"github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?[\s\"'\\]")
DOCKERFILE_RE = re.compile(r"^tools/[^/]+/Dockerfile$")


class CouldNotTell(Exception):
    """The answer is unknown. Never rendered as a pass, and never as a FAIL —
    "could not tell" is its own state, per the issue: a check that reports
    clean when it could not look is how this gap went unseen in the first
    place."""


def _git(root: Path, *args: str, timeout: int = 60) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                                        # noqa: BLE001
        return 1, "", str(exc)


def load_released(root: Path) -> dict:
    """`RELEASED.json`'s parsed contents. Raises `CouldNotTell`, never returns
    `{}` — an empty dict reads the same as "nothing recorded yet", which is a
    different fact from "the file could not be read", and conflating them is
    the same shape of gap #99 itself is about, one file over.
    """
    f = root / "RELEASED.json"
    if not f.is_file():
        raise CouldNotTell("RELEASED.json is absent")
    try:
        rec = json.loads(f.read_text())
    except ValueError as exc:
        raise CouldNotTell(f"RELEASED.json is not valid JSON: {exc}")
    if not isinstance(rec, dict) or not isinstance(rec.get("pins"), dict):
        raise CouldNotTell("RELEASED.json has no 'pins' object")
    return rec


def files_at_rev(root: Path, rev: str) -> List[str]:
    """Every `tools/<tool>/Dockerfile`, plus the root `Dockerfile`, AS OF
    `rev` — from the git object store, never the working tree.
    """
    rc, out, err = _git(root, "ls-tree", "-r", "--name-only", rev)
    if rc != 0:
        raise CouldNotTell(f"cannot list files at {rev}: "
                           f"{(err or out).strip()[:160]}")
    names = out.split("\n")
    files = [n for n in names if DOCKERFILE_RE.match(n)]
    if "Dockerfile" in names:
        files.append("Dockerfile")
    return files


def pins_at_rev(root: Path, rev: str) -> Dict[str, str]:
    """fork repo -> pinned SHA, read from the COMMIT `rev`, never the working
    tree — this function is the entire reason this file exists.
    """
    pins: Dict[str, str] = {}
    for path in files_at_rev(root, rev):
        rc, text, err = _git(root, "show", f"{rev}:{path}")
        if rc != 0:
            raise CouldNotTell(f"cannot read {path} at {rev}: "
                               f"{(err or text).strip()[:160]}")
        refs = dict(REF_RE.findall(text))
        repos = REPO_RE.findall(text)
        for repo in dict.fromkeys(repos):
            key = repo.upper().replace("-", "_").replace(".", "_")
            sha = refs.get(key)
            if sha is None:                        # repo `OpenROAD` vs ARG `OPENROAD`
                flat = key.replace("_", "")
                sha = next((v for k, v in refs.items()
                           if k.replace("_", "") == flat), None)
            if sha:
                pins[repo] = sha
    return pins


def audit(root: Path, rev: str = "HEAD") -> Tuple[str, List[str], dict]:
    """(verdict, findings, stats). verdict in {"PASS", "OUTSTANDING"}.

    Raises `CouldNotTell` — callers must catch it and treat it as its own
    state, never as PASS or OUTSTANDING.
    """
    rec = load_released(root)
    recorded: Dict[str, str] = rec["pins"]
    committed = pins_at_rev(root, rev)

    names = sorted(set(recorded) | set(committed))
    findings: List[str] = []
    for name in names:
        r, c = recorded.get(name), committed.get(name)
        if r != c:
            findings.append(
                f"{name}: RELEASED.json={r[:9] if r else 'absent'}  "
                f"{rev}={c[:9] if c else 'absent'}")

    stats = {
        "version": rec.get("version"),
        "rev": rev,
        "recorded_count": len(recorded),
        "committed_count": len(committed),
        "outstanding": [f.split(":", 1)[0] for f in findings],
    }
    return ("OUTSTANDING" if findings else "PASS"), findings, stats


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root", default=str(ROOT))
    ap.add_argument("--rev", default="HEAD",
                    help="the git revision pins are checked against "
                         "(default: HEAD — what a fresh clone of the current "
                         "branch would see)")
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args(argv)
    root = Path(a.eda_root)

    try:
        verdict, findings, stats = audit(root, a.rev)
    except CouldNotTell as exc:
        if a.json_out:
            Path(a.json_out).write_text(json.dumps(
                {"tool": TOOL, "verdict": "COULD_NOT_TELL",
                 "findings": [str(exc)]}, indent=2) + "\n", encoding="utf-8")
        print(f"{TOOL}: could not tell whether the last released pins are "
              f"committed", file=sys.stderr)
        print(f"[COULD-NOT-TELL] {exc}", file=sys.stderr)
        return RC_COULD_NOT_TELL

    if a.json_out:
        Path(a.json_out).write_text(json.dumps(
            {"tool": TOOL, "verdict": verdict, "findings": findings,
             **stats}, indent=2) + "\n", encoding="utf-8")

    if verdict == "OUTSTANDING":
        print(f"{TOOL}: {len(findings)} pin(s) recorded in RELEASED.json as "
              f"{stats.get('version')!r} are NOT committed at {a.rev} — a "
              f"release review is outstanding:")
        for f in findings:
            print(f"  FAIL  {f}")
        print(f"[FAIL] {len(findings)} outstanding pin review(s). "
              f"RELEASED.json states what the PUBLISHED image was built from; "
              f"{a.rev} is what anyone cloning the repo gets. Until they "
              f"agree, a fresh clone's pins do not describe the image this "
              f"commit says shipped. Review and commit the pin file(s) above "
              f"by explicit path — never `git add -A` (this org bans it for "
              f"exactly this reason).")
        return RC_OUTSTANDING

    print(f"[PASS] {stats['recorded_count']} pin(s) recorded for "
          f"{stats.get('version')!r} all committed at {a.rev} — no release "
          f"review outstanding")
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
