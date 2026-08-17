#!/usr/bin/env python3
"""Run one long host command under a renewable progress lease.

This is the small CLI face used by shell-only legacy paths.  It has no total
runtime ceiling: output-byte or process-tree CPU advancement renews the lease;
an entirely flat command exits with progress_watchdog.RC_STALLED.
"""
from __future__ import annotations

import argparse
import sys

import progress_watchdog as watchdog


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stall-grace", type=float, default=900.0)
    parser.add_argument("--poll", type=float, default=watchdog.DEFAULT_POLL_S)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    if args.stall_grace <= 0 or args.poll <= 0:
        parser.error("stall-grace and poll must be positive")
    result = watchdog.run_supervised(
        command,
        stall_grace_s=args.stall_grace,
        poll_s=args.poll,
        output_progress=True,
        process_tree_cpu_progress=True,
    )
    sys.stdout.write(result.out)
    sys.stderr.write(result.err)
    return result.rc


if __name__ == "__main__":
    raise SystemExit(main())
