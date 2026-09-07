#!/usr/bin/env python3
"""Reject source changes that spend the image's pull-depth safety margin.

The registry/pull limit is 125 filesystem layers.  Waiting until a release has
been composed to discover that it is at 126 is too late and expensive, so this
guard predicts the result from reviewable facts.

TWO LINEAGES SHIP FROM THIS REPOSITORY AND BOTH ARE PREDICTED HERE.

*The root composition* (``Dockerfile``, ``docker buildx bake``) is predicted from
the pinned base image's measured RootFS layer count plus the RUN/COPY/ADD
instructions in the final stage (one layer each).  Its budget is deliberately
110, not 125: the remaining 15 layers are reserve for a base-image move and
emergency release work.

*The derived chain* (``Dockerfile.derived``, ``tools/build_derived.sh``) is built
FROM the previous release's digest, so its depth is that release's measured depth
plus this generation's own filesystem instructions.  Measured 2026-09-07: the
root lineage is 109 layers (0.3.6, 0.3.41 and 0.3.49 each measure 109, exactly
what the root prediction says) while the derived lineage the fleet pulls is
0.3.47 = 116 and 0.3.48 = 118.  The root prediction was the only one this program
made, so those nine extra layers -- and the fact that three more generations of
this size exhaust the pull limit -- were invisible to every gate until a pull
would have failed (vibe-ic#2079).

For the derived chain the ceiling is the 125-layer pull limit itself and the
reserve is ONE GENERATION: when the headroom no longer fits another generation
the answer is not a smaller generation, it is a squash of the chain back into a
root build, and this program says so by name.  The next generation is costed at
what THIS generation's ``Dockerfile.derived`` spends, the only measured estimate
the repository holds; the output states that assumption rather than hiding it.

The base depth a derived generation stands on cannot be read from source, so it
is declared beside the root base's count and keyed to the digest it was measured
from -- a generation move without a fresh measurement is refused, exactly as a
root base move is.  Where the bytes are present the declaration is re-measured
against ``docker image inspect``; the DIGEST is resolved, never a tag, because
this registry serves a 109-layer root composition and the 118-layer derived
image under the same ``0.3.48`` version label.

The release-time ``check_image_depth.py --compact`` remains the hard, measured
backstop for the root lineage; ``tools/build_derived.sh`` runs the same program
without ``--compact`` so a derived generation is measured before it is pushed.

Exit: 0 within budget · 1 over budget or stale base accounting · 2 uncheckable
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator


FILESYSTEM_INSTRUCTIONS = {"ADD", "COPY", "RUN"}
OPCODE = re.compile(r"^([A-Za-z]+)(?:\s|$)")
BASE_ARG = re.compile(r"^ARG\s+BASE_IMAGE\s*=\s*([^\s#]+)", re.I)
DIGEST = re.compile(r"@(sha256:[0-9a-f]{64})$")


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


def digest_of(reference: str) -> str:
    """Return the ``sha256:`` digest an image reference is pinned to.

    The repository half of a reference is deployment configuration -- the same
    bytes live under ``ghcr.io/vibeic/vibeic-eda`` and under a fleet registry --
    so identity comparisons here are made on the digest alone.
    """
    match = DIGEST.search(reference.strip())
    if not match:
        raise BudgetError(
            f"image reference is not pinned to a digest: {reference[:120]!r}"
        )
    return match.group(1)


def load_derived_budget(path: Path) -> dict[str, object] | None:
    """Return the declared derived-chain accounting, or None if none is declared.

    Returning None is a fact about the budget file, never a fallback: the caller
    pairs it with whether ``Dockerfile.derived`` exists and refuses either
    mismatch rather than assuming the safe side of it.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BudgetError(f"cannot read budget {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BudgetError(f"budget {path} is not an object")
    if "derived" not in data:
        return None
    derived = data["derived"]
    if not isinstance(derived, dict):
        raise BudgetError("budget derived must be an object")
    try:
        base_digest = derived["base_digest"]
        base_layers = derived["base_layers"]
        pull_limit = derived["pull_limit"]
    except KeyError as exc:
        raise BudgetError(f"budget derived is missing {exc}") from exc
    if not isinstance(base_digest, str) or not DIGEST.search("@" + base_digest):
        raise BudgetError(
            "budget derived.base_digest must be a sha256:<64 hex> digest"
        )
    if not isinstance(base_layers, int) or base_layers < 1:
        raise BudgetError("budget derived.base_layers must be a positive integer")
    if not isinstance(pull_limit, int) or pull_limit < 1:
        raise BudgetError("budget derived.pull_limit must be a positive integer")
    base_version = derived.get("base_version")
    if base_version is not None and not isinstance(base_version, str):
        raise BudgetError("budget derived.base_version must be a string")
    return {
        "base_digest": base_digest,
        "base_layers": base_layers,
        "pull_limit": pull_limit,
        "base_version": base_version,
    }


def measured_base_layers(digest: str, repository: str | None) -> tuple[int | None, str]:
    """Re-measure a declared base depth from local bytes, or say why not.

    A tag is never resolved here.  This registry serves a 109-layer root
    composition AND the 118-layer derived image under the same ``0.3.48``
    version label, so only the digest identifies the image the chain stands on.
    The second element is always a sentence naming what happened, so a run that
    could not re-measure says which fact it did not have rather than reading as
    a measurement that agreed.
    """
    if not repository:
        return None, ("not re-measured: VIBEIC_EDA_IMAGE_REPO is unset, so the "
                      "digest cannot be addressed as <repository>@<digest>")
    reference = f"{repository}@{digest}"
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", reference,
             "--format", "{{len .RootFS.Layers}}"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"not re-measured: docker could not be run ({exc})"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        first = detail[0] if detail else "docker image inspect failed"
        return None, f"not re-measured: {first}"
    value = proc.stdout.strip()
    try:
        count = int(value)
    except ValueError:
        return None, f"not re-measured: docker returned a non-integer count {value!r}"
    if count < 1:
        return None, f"not re-measured: docker returned an impossible count {count}"
    return count, f"re-measured from {reference}"


def check_derived(
    budget_path: Path,
    derived_dockerfile: Path,
    *,
    repository: str | None,
    out=None,
    err=None,
) -> int:
    """Predict and grade the depth of the chain that actually ships.

    Returns 0 within the pull limit with a generation to spare, 1 over the limit
    or a generation short of it or on stale accounting, 2 when the two halves of
    the declaration contradict each other and no trustworthy answer exists.

    The streams are resolved on entry, not bound as defaults: a default captured
    at import time writes past any caller that replaced ``sys.stdout``, which
    silently hides every verdict below from the test that asserts on it.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    declared = load_derived_budget(budget_path)
    present = derived_dockerfile.is_file()

    if declared is None and not present:
        print("[PASS] derived chain layer budget: no derived chain in this tree "
              f"({derived_dockerfile.name} absent, budget declares none)", file=out)
        return 0
    if declared is None:
        print("[NOT CHECKED] derived chain layer budget: "
              f"{derived_dockerfile.name} builds a derived generation but "
              f"{budget_path.name} declares no derived accounting, so the depth "
              "of the image that ships is unmeasured", file=err)
        return 2
    if not present:
        print("[NOT CHECKED] derived chain layer budget: "
              f"{budget_path.name} declares a derived chain but "
              f"{derived_dockerfile.name} is absent, so nothing here builds the "
              "generation the declaration accounts for", file=err)
        return 2

    text = derived_dockerfile.read_text(encoding="utf-8")
    derived_base_reference, generation_layers = source_shape(text)
    actual_digest = digest_of(derived_base_reference)
    declared_digest = declared["base_digest"]
    base_layers = declared["base_layers"]
    pull_limit = declared["pull_limit"]

    if actual_digest != declared_digest:
        print("[FAIL] derived chain layer budget: the generation's base moved "
              "without a new layer measurement", file=err)
        print(f"  {derived_dockerfile.name}: {actual_digest}", file=err)
        print(f"  budget:     {declared_digest} ({base_layers} layers"
              + (f", {declared['base_version']}" if declared["base_version"] else "")
              + ")", file=err)
        print("  Measure the new base's RootFS.Layers "
              "(docker image inspect --format '{{len .RootFS.Layers}}') and "
              f"update {budget_path} in the same change.", file=err)
        return 1

    measured, how = measured_base_layers(declared_digest, repository)
    if measured is not None and measured != base_layers:
        print("[FAIL] derived chain layer budget: the declared base depth is "
              "not what the base's own bytes report", file=err)
        print(f"  budget:   {base_layers} layers for {declared_digest}", file=err)
        print(f"  measured: {measured} layers ({how})", file=err)
        return 1

    shipped = base_layers + generation_layers
    headroom = pull_limit - shipped
    detail = (f"base release {base_layers} + this generation's RUN/COPY/ADD "
              f"{generation_layers} = {shipped}/{pull_limit}")

    if shipped > pull_limit:
        print(f"[FAIL] derived chain layer budget: {detail}", file=err)
        print(f"  The image this tree builds cannot be pulled: {shipped} layers "
              f"against Docker's {pull_limit}-layer pull graph. Squash the "
              "derived chain back into a root build (Dockerfile + "
              "docker buildx bake) instead of stacking another generation.",
              file=err)
        print(f"  base depth {how}.", file=err)
        return 1

    # The reserve for this lineage is one generation, and the next generation is
    # costed at what this one spends -- the only measured estimate the repository
    # holds. Say so, so nobody reads it as a guarantee.
    if generation_layers and headroom < generation_layers:
        print(f"[FAIL] derived chain layer budget: {detail}, {headroom} layer(s) "
              f"free and the next generation costs {generation_layers}", file=err)
        print("  SQUASH THE DERIVED CHAIN. There is no longer room for another "
              "derived generation below the pull limit, and a smaller generation "
              "is not the answer: rebuild from the root Dockerfile "
              "(docker buildx bake) so the next release starts from a shallow "
              "image again.", file=err)
        print(f"  base depth {how}.", file=err)
        return 1

    if generation_layers:
        generations = headroom // generation_layers
        forecast = (f"{generations} more generation(s) of {generation_layers} "
                    "layer(s) fit; squash before the last one")
    else:
        forecast = ("this generation adds no filesystem layer, so no generation "
                    "cost can be measured from it")
    print(f"[PASS] derived chain layer budget: {detail} "
          f"({headroom} layer(s) free; {forecast})", file=out)
    print(f"  base depth {how}.", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dockerfile", type=Path, default=root / "Dockerfile")
    parser.add_argument(
        "--budget", type=Path, default=root / "tools" / "image_layer_budget.json"
    )
    parser.add_argument(
        "--derived-dockerfile", type=Path, default=None,
        help="the derived generation's Dockerfile (default: Dockerfile.derived "
             "beside --dockerfile); its absence is a checked fact, not a reason "
             "to skip the chain that ships",
    )
    args = parser.parse_args(argv)
    derived_dockerfile = (
        args.derived_dockerfile
        if args.derived_dockerfile is not None
        else args.dockerfile.parent / "Dockerfile.derived"
    )

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

    # The root prediction above grades an image this fleet does not pull. Grade
    # the chain that ships too, and let its verdict decide the exit code: a
    # green root composition next to an unpullable derived generation is the
    # blindness vibe-ic#2079 is about.
    try:
        return check_derived(
            args.budget, derived_dockerfile,
            repository=os.environ.get("VIBEIC_EDA_IMAGE_REPO") or None,
        )
    except (OSError, UnicodeError, BudgetError) as exc:
        print(f"[NOT CHECKED] derived chain layer budget: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
