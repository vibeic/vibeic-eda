#!/usr/bin/env python3
"""Refuse to publish an image Docker cannot pull because its layer graph is too deep.

Docker's graph-driver pull path accepts at most 125 parent layers. A locally
composed image can exceed that limit and still build, run, and push: 0.3.0 did
exactly that with 126 layers. The consumer then receives a pull command that
cannot succeed. This check therefore runs against the local release candidate
before any version or ``latest`` tag is pushed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Sequence


DEFAULT_MAX_LAYERS = 125


def layer_count(image: str) -> int:
    proc = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{len .RootFS.Layers}}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "docker image inspect failed"
        raise RuntimeError(detail)
    value = proc.stdout.strip()
    try:
        count = int(value)
    except ValueError as exc:
        raise RuntimeError(f"docker returned a non-integer layer count: {value!r}") from exc
    if count < 1:
        raise RuntimeError(f"docker returned an impossible layer count: {count}")
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="local image reference to inspect before publish")
    parser.add_argument(
        "--max-layers",
        type=int,
        default=DEFAULT_MAX_LAYERS,
        help=f"maximum pullable layer depth (default: {DEFAULT_MAX_LAYERS})",
    )
    args = parser.parse_args(argv)
    if args.max_layers < 1:
        parser.error("--max-layers must be positive")

    try:
        count = layer_count(args.image)
    except RuntimeError as exc:
        print(f"[REFUSED] cannot prove image depth for {args.image}: {exc}", file=sys.stderr)
        return 2

    if count > args.max_layers:
        print(
            f"[REFUSED] {args.image} has {count} layers; Docker can pull at most "
            f"{args.max_layers}. Compact the Dockerfile and rebuild before publishing.",
            file=sys.stderr,
        )
        return 1

    print(f"[OK] {args.image}: {count}/{args.max_layers} pullable layers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
