#!/usr/bin/env python3
"""Every tag the composing Dockerfile pulls must exist in the registry.

WHY THIS EXISTS (vibeic-eda#41, and #40 which is open as this lands)
====================================================================
`main` named `ghcr.io/vibeic/eda-tool-klayout:a5a7a2d-7cb6ee` and that tag was
never published, so `bake eda` died there. `check_pins_agree` catches THAT
instance — it re-derives the recipe digest and the tag disagreed — and it is the
right check for the class where a pin contradicts its own source.

It cannot catch the other half of the class, because the failure has nothing to
do with the source files agreeing:

  * #41 — a pin that disagrees with its own Dockerfile digest.  CAUGHT there.
  * #40 — three sites that agree PERFECTLY with each other and with the
    re-derived digest (`0e8664` in all of them), naming an image that was
    simply never pushed.  Invisible to every source-only check, by
    construction: nothing is wrong with the source.

A tag exists or it does not, and only the registry knows. That is one question
per pin and it is the last one before a build finds out the expensive way.

WHAT "CANNOT LOOK" MEANS HERE
=============================
If no registry client is available, or the registry cannot be reached, this
returns 2 and says so. It never returns 0. "I could not check whether these
exist" and "these exist" are different claims, and collapsing them is the same
defect this file is about — an absence rendering as a pass.

Exit: 0 = every pinned tag resolves / 1 = at least one does not / 2 = could not
look (missing client, unreachable registry, or nothing to compare).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: `ARG IMG_YOSYS=ghcr.io/vibeic/eda-tool-yosys:<tag>` — the composing
#: Dockerfile's pull list, which is exactly the set a build depends on.
IMG_ARG = re.compile(r"^\s*ARG\s+(IMG_[A-Z0-9_]+)\s*=\s*(\S+)", re.M)

RC_OK, RC_MISSING, RC_CANNOT_LOOK = 0, 1, 2


def pinned_images(dockerfile: Path):
    """`[(arg, tag)]` in file order."""
    if not dockerfile.is_file():
        return []
    return [(m.group(1), m.group(2).strip("\"'"))
            for m in IMG_ARG.finditer(dockerfile.read_text(errors="replace"))]


def _client():
    return shutil.which("docker") or shutil.which("podman")


def resolves(client: str, tag: str, timeout: int):
    """(ok, detail). `ok is None` means the question could not be asked."""
    try:
        r = subprocess.run([client, "manifest", "inspect", tag],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if r.returncode == 0:
        return True, ""
    err = (r.stderr or r.stdout or "").strip().splitlines()
    detail = err[-1][:200] if err else "no output"
    # A network/auth failure is NOT evidence the tag is absent. Only a
    # definitive not-found is, and conflating them would turn an outage into a
    # wall of false findings — the shape that gets a check switched off.
    low = detail.lower()
    if any(s in low for s in ("unauthorized", "denied", "timeout", "timed out",
                              "no such host", "connection refused",
                              "temporary failure", "i/o timeout", "eof")):
        return None, detail
    return False, detail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dockerfile", default=str(ROOT / "Dockerfile"))
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args(argv)

    pins = pinned_images(Path(a.dockerfile))
    if not pins:
        print(f"[SKIP] check_pinned_images_exist: no `ARG IMG_*` in "
              f"{a.dockerfile} — nothing was compared, which is a gap in the "
              f"check, not a pass", file=sys.stderr)
        return RC_CANNOT_LOOK

    client = _client()
    if not client:
        print(f"[SKIP] check_pinned_images_exist: no docker or podman on this "
              f"host, so none of the {len(pins)} pinned tag(s) were checked. "
              f"This is NOT a clean result.", file=sys.stderr)
        return RC_CANNOT_LOOK

    missing, unknown, ok = [], [], []
    for arg, tag in pins:
        res, detail = resolves(client, tag, a.timeout)
        if res is True:
            ok.append(tag)
        elif res is False:
            missing.append({"arg": arg, "tag": tag, "detail": detail})
        else:
            unknown.append({"arg": arg, "tag": tag, "detail": detail})

    report = {"dockerfile": a.dockerfile, "client": client,
              "checked": len(pins), "resolved": len(ok),
              "missing": missing, "unanswerable": unknown}
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(report, indent=2) + "\n")

    for m in missing:
        print(f"[FAIL] {m['arg']} pulls a tag the registry does not have:\n"
              f"    {m['tag']}\n    {m['detail']}", file=sys.stderr)
    for u in unknown:
        print(f"[SKIP] {u['arg']}: could not ask about {u['tag']} — "
              f"{u['detail']}", file=sys.stderr)

    if missing:
        print(f"\n  {len(missing)} of {len(pins)} pinned tag(s) do not exist. "
              f"`bake eda` dies on\n  the first one. A source-only check cannot "
              f"see this: the three pin sites\n  can agree perfectly and name an "
              f"image nobody pushed (vibeic-eda#40).",
              file=sys.stderr)
        return RC_MISSING
    if unknown:
        print(f"\n  {len(unknown)} of {len(pins)} could not be asked about, so "
              f"this is not a clean\n  result — it is a partial one.",
              file=sys.stderr)
        return RC_CANNOT_LOOK

    print(f"check_pinned_images_exist: all {len(pins)} pinned tag(s) resolve "
          f"in the registry")
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
