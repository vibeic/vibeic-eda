#!/usr/bin/env python3
"""The cheap source gate must preserve room below Docker's hard pull limit."""
from __future__ import annotations

import importlib.util
import json
import subprocess
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


# --------------------------------------------------------------------------
# The chain that actually ships (vibe-ic#2079).
#
# The four tests above grade the ROOT composition, and they are exact for it:
# 0.3.6, 0.3.41 and 0.3.49 each measure 109 RootFS layers, which is what
# `base 57 + final-stage 52` predicts. They say nothing at all about the
# lineage the fleet pulls -- 0.3.47 = 116 and 0.3.48 = 118 measured layers --
# because that lineage is built by `Dockerfile.derived` FROM the previous
# release's digest. Nine layers of real depth, and the last three generations
# of headroom, were outside every gate's model.
# --------------------------------------------------------------------------

DERIVED_DIGEST = "sha256:" + "8d" * 32
OTHER_DIGEST = "sha256:" + "77" * 32


def _derived(tmp_path: Path, *, generation: str, base_layers: int = 116,
             pull_limit: int = 125, declared_digest: str | None = DERIVED_DIGEST,
             declare: bool = True, write_dockerfile: bool = True) -> tuple[Path, Path]:
    """Stage a budget and a Dockerfile.derived independently of each other.

    They are written separately on purpose: the interesting failures are the
    ones where the declaration and the tree disagree, and a helper that keeps
    them in step could not express those.
    """
    budget = tmp_path / "budget.json"
    data: dict = {
        "base_image": "base@sha256:measured",
        "base_layers": 57,
        "max_total_layers": 110,
    }
    if declare:
        data["derived"] = {
            "base_digest": declared_digest,
            "base_version": "0.0.1",
            "base_layers": base_layers,
            "pull_limit": pull_limit,
        }
    budget.write_text(json.dumps(data))
    derived = tmp_path / "Dockerfile.derived"
    if write_dockerfile:
        derived.write_text(generation)
    return budget, derived


ONE_COPY_ONE_RUN = f"""
ARG BASE_IMAGE=ghcr.io/vibeic/vibeic-eda@{DERIVED_DIGEST}
FROM ${{BASE_IMAGE}}
USER root
ENV PYTHONDONTWRITEBYTECODE=1
COPY bin/openroad /foss/tools/openroad/bin/openroad
RUN set -e; echo assert
LABEL org.opencontainers.image.version="0.0.2"
USER 1000
"""


def test_a_derived_generation_is_costed_against_the_pull_limit(tmp_path, capsys):
    budget, derived = _derived(tmp_path, generation=ONE_COPY_ONE_RUN)
    assert C.check_derived(budget, derived, repository=None) == 0
    out = capsys.readouterr().out
    assert "116 + this generation's RUN/COPY/ADD 2 = 118/125" in out
    assert "7 layer(s) free" in out
    assert "3 more generation(s) of 2 layer(s) fit" in out


def test_refuses_a_generation_that_cannot_be_pulled(tmp_path, capsys):
    budget, derived = _derived(tmp_path, generation=ONE_COPY_ONE_RUN,
                               base_layers=124)
    assert C.check_derived(budget, derived, repository=None) == 1
    err = capsys.readouterr().err
    assert "126/125" in err
    assert "cannot be pulled" in err
    assert "Squash the derived chain back into a root build" in err


def test_refuses_when_the_headroom_no_longer_fits_a_generation(tmp_path, capsys):
    """Below one generation of headroom the verdict must name the remedy.

    124/125 is not over the limit, so a program grading only `shipped <= limit`
    would pass it -- and the very next generation would be unpullable with no
    gate having said so, which is the failure mode the issue reports.
    """
    budget, derived = _derived(tmp_path, generation=ONE_COPY_ONE_RUN,
                               base_layers=122)
    assert C.check_derived(budget, derived, repository=None) == 1
    err = capsys.readouterr().err
    assert "124/125" in err
    assert "1 layer(s) free and the next generation costs 2" in err
    assert "SQUASH THE DERIVED CHAIN" in err


def test_a_derived_base_move_requires_a_new_measurement(tmp_path, capsys):
    budget, derived = _derived(tmp_path, generation=ONE_COPY_ONE_RUN,
                               declared_digest=OTHER_DIGEST)
    assert C.check_derived(budget, derived, repository=None) == 1
    err = capsys.readouterr().err
    assert "base moved without a new layer measurement" in err
    assert DERIVED_DIGEST in err and OTHER_DIGEST in err


def test_an_undeclared_derived_chain_is_not_checked_rather_than_green(tmp_path, capsys):
    """The exact vibe-ic#2079 state: the tree builds a chain nobody accounts for.

    A green root prediction beside an unmeasured shipped image is the thing the
    issue was filed about, so this state must not exit 0.
    """
    budget, derived = _derived(tmp_path, generation=ONE_COPY_ONE_RUN,
                               declare=False)
    assert C.check_derived(budget, derived, repository=None) == 2
    err = capsys.readouterr().err
    assert "declares no derived accounting" in err
    assert "the depth of the image that ships is unmeasured" in err


def test_a_declared_chain_with_no_dockerfile_is_not_checked_either(tmp_path, capsys):
    budget, derived = _derived(tmp_path, generation=ONE_COPY_ONE_RUN,
                               write_dockerfile=False)
    assert C.check_derived(budget, derived, repository=None) == 2
    assert "declares a derived chain but" in capsys.readouterr().err


def test_no_chain_declared_and_none_in_the_tree_is_a_pass(tmp_path, capsys):
    budget, derived = _derived(tmp_path, generation=ONE_COPY_ONE_RUN,
                               declare=False, write_dockerfile=False)
    assert C.check_derived(budget, derived, repository=None) == 0
    assert "no derived chain in this tree" in capsys.readouterr().out


def test_a_declared_base_depth_is_re_measured_where_the_bytes_are(tmp_path, capsys,
                                                                 monkeypatch):
    """A declaration nobody re-measures is the rot this issue is made of.

    The digest is resolved, never a tag: this registry serves a 109-layer root
    composition and the 118-layer derived image under the same 0.3.48 label.
    """
    seen: list[list[str]] = []

    def fake_run(args, **kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, "121\n", "")

    monkeypatch.setattr(C.subprocess, "run", fake_run)
    budget, derived = _derived(tmp_path, generation=ONE_COPY_ONE_RUN)
    assert C.check_derived(budget, derived, repository="reg.example/img") == 1
    err = capsys.readouterr().err
    assert "not what the base's own bytes report" in err
    assert "measured: 121 layers" in err
    assert seen and seen[0][:3] == ["docker", "image", "inspect"]
    assert seen[0][3] == f"reg.example/img@{DERIVED_DIGEST}"


def test_an_agreeing_re_measurement_passes_and_says_so(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        C.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, "116\n", ""),
    )
    budget, derived = _derived(tmp_path, generation=ONE_COPY_ONE_RUN)
    assert C.check_derived(budget, derived, repository="reg.example/img") == 0
    assert "base depth re-measured from reg.example/img@" in capsys.readouterr().out


def test_an_unmeasurable_base_names_what_was_missing(tmp_path, capsys, monkeypatch):
    """Not being able to re-measure must never read as a measurement that agreed.

    It also must not fail the cheap source gate: fork-only.yml runs this with no
    image and no registry. The declared count is still graded; only the
    cross-check is unavailable, and it is named.
    """
    monkeypatch.setattr(
        C.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "No such image"),
    )
    budget, derived = _derived(tmp_path, generation=ONE_COPY_ONE_RUN)
    assert C.check_derived(budget, derived, repository="reg.example/img") == 0
    out = capsys.readouterr().out
    assert "not re-measured: No such image" in out

    assert C.check_derived(budget, derived, repository=None) == 0
    assert "VIBEIC_EDA_IMAGE_REPO is unset" in capsys.readouterr().out


def test_main_grades_the_derived_chain_not_only_the_root(tmp_path, capsys):
    """A green root composition must not be able to carry an unmeasured chain."""
    df = tmp_path / "Dockerfile"
    df.write_text("""
ARG BASE_IMAGE=base@sha256:measured
FROM ${BASE_IMAGE}
RUN one
""")
    budget, _ = _derived(tmp_path, generation=ONE_COPY_ONE_RUN, base_layers=124)
    assert C.main(["--dockerfile", str(df), "--budget", str(budget)]) == 1
    captured = capsys.readouterr()
    assert "[PASS] Dockerfile layer budget" in captured.out
    assert "cannot be pulled" in captured.err


def test_a_digest_is_required_to_identify_a_derived_base(tmp_path):
    """A moving tag cannot say which image a layer count was measured from.

    The reference below is deliberately a neutral registry, not this project's
    own: `sync_image_version.py`'s repo-wide net reads any fully-qualified
    `ghcr.io/vibeic/vibeic-eda:X.Y.Z` as a live pull pointer that must track
    VERSION, and it is right to -- a floating pointer in a fixture is
    indistinguishable from one in an install doc. What this test needs is a
    reference that is a TAG rather than a digest; whose registry it names is
    beside the point.
    """
    budget, derived = _derived(tmp_path, generation="""
ARG BASE_IMAGE=registry.example/vibeic-eda:0.0.1
FROM ${BASE_IMAGE}
RUN one
""")
    try:
        C.check_derived(budget, derived, repository=None)
    except C.BudgetError as exc:
        assert "not pinned to a digest" in str(exc)
    else:
        raise AssertionError("a floating tag was accepted as a derived base")


def test_this_tree_ships_a_measured_derived_chain():
    """The reality check, on the files this repository actually holds.

    Measured on 8HD-4, 2026-09-07: the base release's own bytes report 116
    RootFS layers, Dockerfile.derived spends 2 more, and the pinned 0.3.48
    image measures 118 -- so the prediction below reproduces the shipped depth
    exactly. The last assertion is the point of the whole change: what ships is
    EIGHT layers deeper than the root budget the old gate kept, which is why
    that gate could not see it.
    """
    declared = C.load_derived_budget(ROOT / "tools" / "image_layer_budget.json")
    assert declared is not None, "the chain this fleet pulls must be accounted for"
    derived_file = ROOT / "Dockerfile.derived"
    assert derived_file.is_file()
    reference, generation_layers = C.source_shape(derived_file.read_text())
    assert C.digest_of(reference) == declared["base_digest"], (
        "Dockerfile.derived's base moved without a fresh layer measurement"
    )
    assert declared["base_layers"] == 116, "re-measure the base release's RootFS"
    assert generation_layers == 2, "review which instruction this generation added"
    shipped = declared["base_layers"] + generation_layers
    assert shipped == 118
    assert declared["pull_limit"] == 125
    assert shipped <= declared["pull_limit"] - generation_layers, (
        "under one generation of headroom: squash the chain into a root build"
    )
    _, _, root_maximum = C.load_budget(ROOT / "tools" / "image_layer_budget.json")
    assert shipped > root_maximum, (
        "the shipped chain is deeper than the root budget; that gap is what "
        "vibe-ic#2079 reports and it must stay measured, not assumed away"
    )


def test_the_derived_build_measures_the_image_it_built():
    """`build_derived.sh` is the only path that produces the shipped lineage.

    It ran neither depth gate: `check_image_depth.py` is wired into the release
    COMPOSITION path, which is exactly the path a derived build exists to avoid.
    """
    script = (ROOT / "tools" / "build_derived.sh").read_text()
    assert "tools/check_dockerfile_layer_budget.py" in script, (
        "cost the generation before spending the build"
    )
    assert "tools/check_image_depth.py" in script, (
        "measure the bytes the build produced, not only the prediction"
    )
    assert script.index("tools/check_dockerfile_layer_budget.py") < script.index(
        "docker build -f"
    ), "the cheap prediction belongs before the build it would have saved"
    assert script.index("docker build -f") < script.index(
        "tools/check_image_depth.py"
    ), "the measured backstop can only run on a built image"
    depth_call = script[script.index('tools/check_image_depth.py"'):]
    assert "--compact" not in depth_call.splitlines()[0], (
        "flattening a derived generation answers the depth question by "
        "discarding the lineage; the remedy is a deliberate squash"
    )
