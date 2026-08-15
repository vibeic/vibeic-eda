#!/usr/bin/env python3
"""Reject source changes that spend the image's pull-depth safety margin.

The registry/pull limit is 125 filesystem layers.  Waiting until a release has
been composed to discover that it is at 126 is too late and expensive, so this
guard predicts the result from two reviewable facts:

* the pinned base image's measured RootFS layer count; and
* RUN/COPY/ADD instructions in the final Dockerfile stage (one layer each).

The budget is deliberately 110, not 125.  The remaining 15 layers are reserve
for a base-image move and emergency release work.  The release-time
``check_image_depth.py --compact`` remains the hard, measured backstop.

Exit: 0 within budget · 1 over budget or stale base accounting · 2 uncheckable
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterator


FILESYSTEM_INSTRUCTIONS = {"ADD", "COPY", "RUN"}
OPCODE = re.compile(r"^([A-Za-z]+)(?:\s|$)")
BASE_ARG = re.compile(r"^ARG\s+BASE_IMAGE\s*=\s*([^\s#]+)", re.I)


class BudgetError(ValueError):
    """The source or budget file cannot produce a trustworthy answer."""


def logical_instructions(text: str) -> Iterator[str]:
    """Yield Dockerfile instructions, joining backslash continuations.

    Full Dockerfile parsing is unnecessary for this invariant: only the leading
    opcode and the global BASE_IMAGE default are consumed.  Comment and blank
    lines inside a continued instruction are ignored as Docker does.
    """
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        yield pending
        pending = ""
    if pending:
        raise BudgetError("Dockerfile ends inside a backslash continuation")


def source_shape(text: str) -> tuple[str, int]:
    instructions = list(logical_instructions(text))
    if not instructions:
        raise BudgetError("Dockerfile contains no instructions")

    opcodes: list[str] = []
    base_image: str | None = None
    for instruction in instructions:
        match = OPCODE.match(instruction)
        if not match:
            raise BudgetError(f"cannot parse instruction: {instruction[:80]!r}")
        opcode = match.group(1).upper()
        opcodes.append(opcode)
        if opcode == "ARG" and base_image is None:
            base = BASE_ARG.match(instruction)
            if base:
                base_image = base.group(1)

    from_indexes = [i for i, opcode in enumerate(opcodes) if opcode == "FROM"]
    if not from_indexes:
        raise BudgetError("Dockerfile contains no FROM instruction")
    if base_image is None:
        raise BudgetError("Dockerfile has no default for ARG BASE_IMAGE")

    final_start = from_indexes[-1] + 1
    final_layers = sum(
        opcode in FILESYSTEM_INSTRUCTIONS for opcode in opcodes[final_start:]
    )
    return base_image, final_layers


def load_budget(path: Path) -> tuple[str, int, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        base_image = data["base_image"]
        base_layers = data["base_layers"]
        maximum = data["max_total_layers"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BudgetError(f"cannot read budget {path}: {exc}") from exc
    if not isinstance(base_image, str) or not base_image:
        raise BudgetError("budget base_image must be a non-empty string")
    if not isinstance(base_layers, int) or base_layers < 1:
        raise BudgetError("budget base_layers must be a positive integer")
    if not isinstance(maximum, int) or maximum < base_layers:
        raise BudgetError("budget max_total_layers must be >= base_layers")
    return base_image, base_layers, maximum


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dockerfile", type=Path, default=root / "Dockerfile")
    parser.add_argument(
        "--budget", type=Path, default=root / "tools" / "image_layer_budget.json"
    )
    args = parser.parse_args(argv)

    try:
        expected_base, base_layers, maximum = load_budget(args.budget)
        text = args.dockerfile.read_text(encoding="utf-8")
        actual_base, final_layers = source_shape(text)
    except (OSError, UnicodeError, BudgetError) as exc:
        print(f"[NOT CHECKED] Dockerfile layer budget: {exc}", file=sys.stderr)
        return 2

    if actual_base != expected_base:
        print("[FAIL] Dockerfile layer budget: BASE_IMAGE changed without a new "
              "layer measurement", file=sys.stderr)
        print(f"  Dockerfile: {actual_base}", file=sys.stderr)
        print(f"  budget:     {expected_base} ({base_layers} layers)", file=sys.stderr)
        print("  Measure the new digest's RootFS.Layers and update "
              f"{args.budget} in the same change.", file=sys.stderr)
        return 1

    predicted = base_layers + final_layers
    detail = (f"base {base_layers} + final-stage RUN/COPY/ADD {final_layers} "
              f"= {predicted}/{maximum}")
    if predicted > maximum:
        print(f"[FAIL] Dockerfile layer budget: {detail}", file=sys.stderr)
        print(f"  Reduce at least {predicted - maximum} committed filesystem "
              "instruction(s); the 125-layer pull limit is not the working budget.",
              file=sys.stderr)
        return 1
    print(f"[PASS] Dockerfile layer budget: {detail} "
          f"({maximum - predicted} layer(s) free)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
