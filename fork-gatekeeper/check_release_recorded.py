#!/usr/bin/env python3
"""check_release_recorded — a published version must be RECORDED as published.

WHY (vibeic-eda#51)
===================
`RELEASED.json` states its own contract:

    What the last PUBLISHED image was built from. A pin set not matching this
    has never been released, however current the pins look.

0.2.53 was cut BY HAND in response to #45/#46: VERSION advanced, the image was
built, pushed, and verified — and `RELEASED.json` still described 0.2.52.
Measured:

    VERSION on main                     0.2.53
    RELEASED.json "version"             0.2.52
    main tools/openroad/Dockerfile      OPENROAD_REF=47636465f9…
    RELEASED.json openroad pin          09d67f08f8…
    ghcr.io/vibeic/vibeic-eda:0.2.53    EXISTS, and its own
      /vibeic/provenance/openroad.json  {"ref":"47636465f969…"}

So the shipped image carried main's pin set exactly, and by the ledger's own
definition that pin set had never shipped. `daily_release` decides what to build
by comparing the tree's fingerprint against the stored one, so the next tick
would have published 0.2.54 byte-identical to 0.2.53.

THE SHAPE, and it is the inverse of one this repo already fixed: the record is
written by ONE path and a release can happen by ANOTHER. #43's failure was the
ledger claiming shipped when nothing had; this is it claiming unshipped when
something did. `daily_release.write_released_record` is now the single writer
and `--record-release` lets a hand release use it, but no writer can cover a
path that does not call it — so this asks the registry instead.

WHAT IT ASKS
============
1. If an image is published for VERSION, `RELEASED.json` must record VERSION.
2. If it records VERSION, the stored fingerprint must be the one the CURRENT
   tree recomputes — otherwise the record is unreproducible and every later run
   reads "never released" anyway. That is the 0.2.45 failure (the shipped tree
   hashed to e4e0a5f6 while the file recorded 94d85fda) and it is free to check
   here.

NOT A FINDING: VERSION bumped and nothing published yet. That is the normal
mid-release state and the ledger is CORRECT to still name the previous release.

Exit: 0 recorded / 1 published-but-unrecorded (or an unreproducible record) /
      2 could not ask the registry — never a pass that claims it checked.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import daily_release as DR  # noqa: E402

TOOL = "check_release_recorded"
RC_OK, RC_FINDINGS, RC_CANNOT_ASK = 0, 1, 2

IMAGE = "ghcr.io/vibeic/vibeic-eda"


def published(tag: str, image: str = IMAGE, timeout: int = 180):
    """True / False / None — None is COULD NOT ASK, never folded into False.

    Local first (a `docker image inspect` hit is proof the tag resolves without
    a network round-trip), then the registry. A registry error is not "the tag
    does not exist": reading it as absent turns an outage into a clean bill of
    health for an unrecorded release.
    """
    ref = f"{image}:{tag}"
    try:
        r = subprocess.run(["docker", "manifest", "inspect", ref],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        return True
    blob = (r.stdout + r.stderr).lower()
    # `manifest unknown` / `not found` is a real answer; anything else (auth,
    # DNS, rate limit, TLS) is not.
    if "manifest unknown" in blob or "not found" in blob or "no such manifest" in blob:
        return False
    return None


def audit(eda_root: Path, image: str = IMAGE) -> tuple[str, list[str], dict]:
    version = (eda_root / "VERSION").read_text().strip() if (
        eda_root / "VERSION").is_file() else None
    rec = DR.released_record(eda_root)
    recorded = rec.get("version")
    stats = {"version": version, "recorded": recorded,
             "recorded_fingerprint": rec.get("pins_fingerprint")}
    if not version:
        return "CANNOT_ASK", ["no VERSION file — there is no version to ask "
                              "about, which is not a clean result"], stats

    is_pub = published(version, image)
    stats["published"] = is_pub
    if is_pub is None:
        return "CANNOT_ASK", [
            f"could not establish whether {image}:{version} is published; the "
            f"ledger is therefore UNCHECKED, not confirmed"], stats

    findings: list[str] = []
    if is_pub and recorded != version:
        findings.append(
            f"{image}:{version} IS published and RELEASED.json records "
            f"{recorded!r}. By the ledger's own contract the shipped pin set "
            f"has never shipped, so the next release run will cut a version "
            f"byte-identical to one already published. Record it with: "
            f"python3 fork-gatekeeper/daily_release.py --record-release {version}")

    if recorded == version:
        targets = DR.bake_targets(eda_root)
        fp_now = DR.pins_fingerprint({
            **DR.pinned_refs(eda_root),
            **{f"recipe:{k}": DR.recipe_hash(eda_root, k) for k in targets},
            "recipe:__compose__": DR.compose_recipe_hash(eda_root)})
        stats["fingerprint_now"] = fp_now
        if rec.get("pins_fingerprint") != fp_now:
            findings.append(
                f"RELEASED.json names {version} but its fingerprint "
                f"{rec.get('pins_fingerprint')!r} is not the one this tree "
                f"recomputes ({fp_now}). The record is UNREPRODUCIBLE, so every "
                f"later run reads 'this pin set has never been released' — the "
                f"0.2.45 failure, from the other direction.")

    return ("FINDINGS" if findings else "OK"), findings, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root", default=str(_HERE.parent))
    ap.add_argument("--image", default=IMAGE)
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args(argv)

    verdict, findings, stats = audit(Path(a.eda_root), a.image)
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(
            {"tool": TOOL, "verdict": verdict, "findings": findings, **stats},
            indent=2) + "\n", encoding="utf-8")

    for f in findings:
        print(f"[FAIL] {f}", file=sys.stderr)
    if verdict == "CANNOT_ASK":
        print(f"[SKIP] {TOOL}: {findings[0] if findings else 'unknown'}",
              file=sys.stderr)
        return RC_CANNOT_ASK
    if verdict == "FINDINGS":
        return RC_FINDINGS
    if stats.get("published"):
        print(f"[PASS] {a.image}:{stats['version']} is published and "
              f"RELEASED.json records it, with a fingerprint this tree "
              f"reproduces ({stats.get('fingerprint_now')})")
    else:
        print(f"[PASS] {a.image}:{stats['version']} is not published yet; "
              f"RELEASED.json correctly still names {stats['recorded']!r} — "
              f"the normal mid-release state, not a gap")
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
