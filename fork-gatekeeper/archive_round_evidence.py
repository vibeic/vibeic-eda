#!/usr/bin/env python3
"""Copy a failed round's stage evidence out before the next round overwrites it.

THE DEFECT THIS CLOSES. Every stage of `run_tick` writes its output to a FIXED
path in `$GK_STATE_DIR` — `daily-merge.txt`, `source-guards.txt`,
`fork-selftests.txt`, `vendored-closure.txt` — with `>`, and the next 05:30 round
overwrites all of them. `daily_release` already learned this the hard way
(vibeic-eda#96 -> the `failed-releases/` archive), but only for ITS OWN output,
and only on the branch where a build actually ran.

So the failure class that stops the round BEFORE the build kept no evidence at
all. Measured: the 2026-08-25, 2026-09-07 and 2026-09-08 rounds each exited 4
(`daily_merge` refused, `daily_release` never invoked). By the time anyone read
the alert PR, `daily-merge.txt` had either already been overwritten or was hours
from being overwritten, and the three alerts were byte-identical and said
nothing. There is no way to answer "which fork refused, and is it the same one
every round?" from outside the build host, which is the only question that
distinguishes a transient from a repeating one.

ABSENCE IS RECORDED, NOT SKIPPED. A file the round never wrote and a file this
program failed to copy look identical in a directory listing, so `MANIFEST.txt`
names every file that was ASKED for and says `copied` or `ABSENT` for each. That
distinction is the whole point: on 2026-09-08 the reader could not tell a round
that failed from a round that never ran, because the only evidence was a missing
file.

    rc 0  an archive directory was created and the manifest written
    rc 2  the state directory does not exist — nothing could be archived, which
          is not the same as nothing needing to be
    rc 1  the archive could not be written
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

RC_OK, RC_FAILED, RC_NOSTATE = 0, 1, 2

ARCHIVE_DIRNAME = "failed-rounds"

#: How many failed rounds to retain. Bounded on purpose: a round that fails the
#: same way every morning would otherwise grow this without limit, and the fifth
#: copy of an identical `daily-merge.txt` answers nothing the first four did not.
#: Five is enough to see "the same fork, four mornings running", which is the
#: transient-versus-repeating question.
DEFAULT_KEEP = 5


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def archive(state_dir: Path, names, keep: int = DEFAULT_KEEP,
            label: str = "", stamp: str = "") -> tuple:
    """Copy `names` out of `state_dir`. Returns (rc, archive_dir, rows)."""
    if not state_dir.is_dir():
        return RC_NOSTATE, None, []
    root = state_dir / ARCHIVE_DIRNAME
    name = stamp or _stamp()
    if label:
        name = f"{name}.{label}"
    dest = root / name
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[FAILED] could not create {dest}: {exc}", file=sys.stderr)
        return RC_FAILED, None, []

    rows = []
    for n in names:
        src = state_dir / n
        # A name is a basename inside the state dir, never a path out of it. An
        # evidence list is assembled by the caller, but this program is the thing
        # that writes into a retained directory; it does not get to be the reason
        # something outside the state dir is copied.
        if Path(n).name != n or not n:
            rows.append((n, "REFUSED-not-a-basename"))
            continue
        if not src.is_file():
            rows.append((n, "ABSENT"))
            continue
        try:
            shutil.copy2(src, dest / n)
        except OSError as exc:
            rows.append((n, f"COPY-FAILED {exc}"))
            continue
        rows.append((n, f"copied {src.stat().st_size} byte(s)"))

    manifest = [f"# round evidence archived {name}",
                f"# state dir: {state_dir}", ""]
    manifest += [f"{outcome}\t{n}" for n, outcome in rows]
    try:
        (dest / "MANIFEST.txt").write_text("\n".join(manifest) + "\n",
                                          encoding="utf-8")
    except OSError as exc:
        print(f"[FAILED] could not write manifest in {dest}: {exc}",
              file=sys.stderr)
        return RC_FAILED, dest, rows

    # Prune AFTER writing, so a prune that goes wrong cannot destroy the archive
    # this invocation exists to create.
    if keep > 0:
        try:
            kept = sorted((d for d in root.iterdir() if d.is_dir()),
                          key=lambda d: d.name, reverse=True)
            for stale in kept[keep:]:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError as exc:  # noqa: BLE001
            print(f"[warn] could not prune {root}: {exc}", file=sys.stderr)
    return RC_OK, dest, rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("names", nargs="+",
                    help="basenames inside the state dir to retain")
    ap.add_argument("--state-dir", default=os.environ.get(
        "GK_STATE_DIR", "/home/reyerchu/.cache/eda-fork-gatekeeper"))
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    ap.add_argument("--label", default="")
    ap.add_argument("--stamp", default="",
                    help="archive directory stamp; defaults to now, UTC")
    a = ap.parse_args(argv)

    rc, dest, rows = archive(Path(a.state_dir), a.names, keep=a.keep,
                             label=a.label, stamp=a.stamp)
    if rc == RC_NOSTATE:
        print(f"[NOT ARCHIVED] state dir {a.state_dir} does not exist — "
              "nothing could be retained")
        return rc
    if rc != RC_OK:
        return rc
    present = sum(1 for _, o in rows if o.startswith("copied"))
    print(f"[ARCHIVED] {present} of {len(rows)} evidence file(s) -> {dest}")
    for n, outcome in rows:
        print(f"  {outcome}\t{n}")
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
