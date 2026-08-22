"""Tool artefacts were built one at a time, so a round paid for the sum of them.

Measured 2026-08-22: OpenROAD alone ran 40+ minutes while every other artefact
sat idle. buildkit already parallelises WITHIN one target's DAG — what was
missing is that two targets never overlapped, because `daily_release` looped
`for t in build:` and each `_sh` blocks.

Each tool keeps its OWN bake invocation rather than one bake over many targets.
That is what preserves per-tool attribution — an exit code, a captured tail, a
stall verdict and a retry category that all belong to ONE tool. A single bake
returns one number for everything, and this morning's klayout failure was
diagnosed in minutes precisely because the failure named its target.

So most of this file is about what parallelism must NOT change.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_release as DR  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env():
    saved = os.environ.pop("GK_BUILD_PARALLEL", None)
    yield
    if saved is not None:
        os.environ["GK_BUILD_PARALLEL"] = saved
    else:
        os.environ.pop("GK_BUILD_PARALLEL", None)


def test_two_tools_overlap_by_default():
    """The whole point: yosys and OpenROAD must not cost the sum of both."""
    assert DR._build_parallelism(5) == 2


def test_never_more_workers_than_tools():
    """A pool wider than the work is a pool that misreports what it is doing."""
    assert DR._build_parallelism(1) == 1


def test_the_ceiling_is_raisable_for_a_bigger_host():
    os.environ["GK_BUILD_PARALLEL"] = "6"
    assert DR._build_parallelism(9) == 6


@pytest.mark.parametrize("bad", ["oops", "2.5", "-1", "0"])
def test_a_bad_ceiling_is_refused_not_defaulted(bad):
    """A typo that quietly serialises every build is invisible — it just looks
    slow, which is the state this change exists to fix. Refusing is the only
    way the operator learns their value did nothing."""
    os.environ["GK_BUILD_PARALLEL"] = bad
    with pytest.raises(SystemExit):
        DR._build_parallelism(4)


# --- what parallelism must not change ---------------------------------------

def _build_block() -> str:
    src = (HERE / "daily_release.py").read_text(encoding="utf-8")
    i = src.index("            parallel = _build_parallelism(len(build))")
    return src[i:src.index('result["built"].append(t)', i) + 40]


def test_each_tool_still_gets_its_own_bake():
    """One bake over many targets returns ONE exit code for everything. The
    failure would stop naming which tool broke, which is how today's klayout
    failure was found."""
    block = _build_block()
    assert '"--set", f"{t}.tags={want}", t]' in block
    assert block.count("buildx") == 1, "one invocation, parameterised by tool"


def test_the_verdict_is_ordered_by_the_round_not_by_who_finished_first():
    """Two tools failing must produce the same verdict every time, or a rerun
    disagrees with its own log."""
    block = _build_block()
    assert "for t in build:" in block.split("outcomes[t] = (rc, err)")[1], (
        "failures must be examined in the round's order, not completion order")


def test_a_failure_still_carries_its_tool_and_category():
    block = _build_block()
    for needle in ('"tool": t', '"category": build_category',
                   "progress_stall", "transient_failure_category(err)",
                   "RC_NEEDS_HUMAN"):
        assert needle in block, needle


def test_concurrent_output_is_line_prefixed_not_chunk_prefixed():
    """Chunks are not line aligned. Prefixing a chunk cuts lines in half and two
    tools produce something neither of them said."""
    src = (HERE / "daily_release.py").read_text(encoding="utf-8")
    i = src.index("if line_prefix:")
    seg = src[i:i + 700]
    assert 'while "\\n" in nonlocal_buf[0]' in seg, "must hold partial lines"


def test_a_single_build_is_not_prefixed():
    """A prefix on a lone build is noise in a log that has no ambiguity."""
    assert 'line_prefix=(f"[{t}] " if parallel > 1 else "")' in _build_block()


def test_line_prefixing_actually_prefixes_every_line(capfd):
    """The helper is EXECUTED, not grepped: the guarantee is about output, and
    the tests above can only see shape."""
    rc, _, _ = DR._sh(["printf", "one\\ntwo\\nthree\\n"], stream=True,
                      line_prefix="[yosys] ")
    assert rc == 0
    out = capfd.readouterr().out
    assert out.count("[yosys] ") == 3
    assert "[yosys] two" in out
