#!/usr/bin/env python3
"""Give the buildx builder a concurrency ceiling, without losing what it holds.

Tool artefacts now build two at a time (`GK_BUILD_PARALLEL`). That bounds how
many TOOLS overlap; it does not bound how many steps buildkit runs inside them.
`--oci-max-parallelism` is the daemon's own ceiling and it can only be set when
the builder is created.

Which makes this the exact shape that bit us today: a recreate starts from
nothing but its own flags. Two things would be silently lost.

  1. THE BUILDER'S CONFIG. The live builder carries
     `env.BUILDKIT_STEP_LOG_MAX_SIZE=-1`, `env.BUILDKIT_STEP_LOG_MAX_SPEED=-1`
     and `--allow-insecure-entitlement=network.host`. They are READ OFF the
     running builder here and carried across, never retyped from memory.
  2. THE CACHE. Builder state is a named volume — 98.89 GB of it on this host,
     measured 2026-08-22. `docker buildx rm` drops it unless `--keep-state`
     says otherwise, and the next round would rebuild every artefact from
     scratch. This never removes a builder without `--keep-state`, and it
     verifies the volume is still there afterwards.

    rc 0  the ceiling is already set, or was set and the state survived
    rc 1  REFUSED — a build is in flight, or the config could not be carried
    rc 2  NORECORD — the builder could not be read
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

RC_OK, RC_REFUSED, RC_NORECORD = 0, 1, 2

BUILDER = os.environ.get("GK_BUILDX_BUILDER", "vibeic-builder")
CEILING_FLAG = "--oci-max-parallelism"


def _sh(*argv, timeout: int = 180):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{argv[0]} failed: {exc}"


def _inspect(builder: str):
    rc, out = _sh("docker", "buildx", "inspect", builder)
    if rc != 0:
        return None, f"cannot inspect builder {builder}: {out.strip()[:200]}"
    driver_opts, flags = [], ""
    for line in out.splitlines():
        if line.startswith("Driver Options:"):
            driver_opts = re.findall(r'(\S+?)="([^"]*)"',
                                     line.split(":", 1)[1].strip())
        elif line.startswith("BuildKit daemon flags:"):
            flags = line.split(":", 1)[1].strip()
    return {"driver_opts": driver_opts, "flags": flags, "raw": out}, ""


def _state_volume(builder: str) -> str:
    return f"buildx_buildkit_{builder}0_state"


def _volume_exists(name: str) -> bool:
    rc, _ = _sh("docker", "volume", "inspect", name, timeout=60)
    return rc == 0


def _build_in_flight() -> bool:
    rc, out = _sh("pgrep", "-f", "docker buildx bake", timeout=30)
    return rc == 0 and out.strip() != ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--builder", default=BUILDER)
    ap.add_argument("--max-parallelism", type=int,
                    default=int(os.environ.get("GK_BUILDKIT_MAX_PARALLELISM",
                                               max(1, (os.cpu_count() or 8) // 2))))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    cfg, why = _inspect(a.builder)
    if cfg is None:
        print(f"[NORECORD] {why}")
        return RC_NORECORD

    wanted = f"{CEILING_FLAG}={a.max_parallelism}"
    if wanted in cfg["flags"]:
        print(f"[PASS] {a.builder} already carries {wanted}")
        return RC_OK
    if CEILING_FLAG in cfg["flags"]:
        print(f"[PASS] {a.builder} carries a ceiling already: "
              f"{cfg['flags']} — leaving the operator's value alone")
        return RC_OK

    vol = _state_volume(a.builder)
    if not _volume_exists(vol):
        print(f"[REFUSED] {a.builder}'s state volume {vol} is not there; "
              f"recreating would not be preserving anything and this will not "
              f"guess what that means")
        return RC_REFUSED

    # Carry EVERYTHING forward, read off the live builder.
    create = ["docker", "buildx", "create", "--name", a.builder,
              "--driver", "docker-container"]
    for k, v in cfg["driver_opts"]:
        create += ["--driver-opt", f"{k}={v}"]
    flags = " ".join(x for x in (cfg["flags"], wanted) if x).strip()
    create += ["--buildkitd-flags", flags]

    plan = (f"carry forward {len(cfg['driver_opts'])} driver-opt(s) and the "
            f"daemon flags {flags!r}, keeping state volume {vol}")
    if a.dry_run:
        print(f"[DRY-RUN] would recreate {a.builder}: {plan}")
        print(f"          rm:     docker buildx rm --keep-state {a.builder}")
        print(f"          create: {' '.join(create)}")
        return RC_OK

    if _build_in_flight():
        print("[REFUSED] a `docker buildx bake` is running. Recreating the "
              "builder now would kill it; this is not a decision to make on "
              "someone else's behalf mid-round.")
        return RC_REFUSED

    print(f"  {plan}")
    rc, out = _sh("docker", "buildx", "rm", "--keep-state", a.builder, timeout=300)
    if rc != 0:
        print(f"[REFUSED] could not remove {a.builder}: {out.strip()[:200]}")
        return RC_REFUSED
    if not _volume_exists(vol):
        print(f"[REFUSED] {vol} did NOT survive --keep-state. The cache is gone "
              f"and recreating now would hide that; stopping so it is visible.")
        return RC_REFUSED
    rc, out = _sh(*create, timeout=300)
    if rc != 0:
        print(f"[REFUSED] could not recreate {a.builder}: {out.strip()[:250]}")
        return RC_REFUSED

    after, why = _inspect(a.builder)
    if after is None or wanted not in after["flags"]:
        print(f"[REFUSED] {a.builder} was recreated but does not carry {wanted}")
        return RC_REFUSED
    print(f"[PASS] {a.builder} now carries {wanted}; state volume {vol} intact")
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
