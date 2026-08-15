#!/usr/bin/env python3
"""The cheap source gate must preserve room below Docker's hard pull limit."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / "tools" / "check_dockerfile_layer_budget.py"
SPEC = importlib.util.spec_from_file_location("layer_budget", PROGRAM)
assert SPEC and SPEC.loader
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


def _files(tmp_path: Path, dockerfile: str, *, maximum: int = 60,
           base: str = "base@sha256:measured") -> tuple[Path, Path]:
    df = tmp_path / "Dockerfile"
    budget = tmp_path / "budget.json"
    df.write_text(dockerfile)
    budget.write_text(json.dumps({
        "base_image": base,
        "base_layers": 57,
        "max_total_layers": maximum,
    }))
    return df, budget


def test_counts_only_filesystem_instructions_in_the_final_stage(tmp_path):
    df, budget = _files(tmp_path, """
ARG BASE_IMAGE=base@sha256:measured
FROM alpine AS builder
RUN one
COPY a b
FROM ${BASE_IMAGE}
ENV X=y
RUN --mount=type=bind,from=builder,target=/src echo ok \\
    && echo still-one
COPY --from=builder /x /x
USER 1000
""")
    assert C.main(["--dockerfile", str(df), "--budget", str(budget)]) == 0
    assert C.source_shape(df.read_text()) == ("base@sha256:measured", 2)


def test_rejects_spending_past_the_soft_budget(tmp_path, capsys):
    df, budget = _files(tmp_path, """
ARG BASE_IMAGE=base@sha256:measured
FROM ${BASE_IMAGE}
RUN one
COPY a b
ADD c d
RUN four
""")
    assert C.main(["--dockerfile", str(df), "--budget", str(budget)]) == 1
    assert "61/60" in capsys.readouterr().err


def test_a_base_move_requires_a_new_measurement(tmp_path, capsys):
    df, budget = _files(tmp_path, """
ARG BASE_IMAGE=base@sha256:different
FROM ${BASE_IMAGE}
RUN one
""")
    assert C.main(["--dockerfile", str(df), "--budget", str(budget)]) == 1
    assert "changed without a new layer measurement" in capsys.readouterr().err


def test_this_tree_has_real_headroom():
    expected, base_layers, maximum = C.load_budget(
        ROOT / "tools" / "image_layer_budget.json"
    )
    actual, final_layers = C.source_shape((ROOT / "Dockerfile").read_text())
    assert actual == expected
    assert base_layers == 57, "a base change needs a fresh measured layer count"
    assert final_layers == 52, "review which new filesystem instruction spent the budget"
    assert base_layers + final_layers == 109
    assert base_layers + final_layers <= maximum == 110
