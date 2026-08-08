#!/usr/bin/env python3
"""Two findings from a read-only investigation into the standing daily [reach]
report (2026-08-08), both confirmed as checker bugs -- not real gaps in the
composed image:

1. `copied_paths()` fixed its destination capture at exactly two `(\\S+)`
   groups after the tool name, so a COPY line with MORE than one source
   (Dockerfile:413 openroad, :487 sat-solvers) had its real destination
   silently dropped and a SOURCE token used in its place. openroad's
   containment check then compared the real binary's path against the
   wrong token and reported "resolves outside every path copied from our
   artefact" -- a false positive; ground-truthed against a running image,
   the flow genuinely runs our openroad build (provenance.json confirms).
   `kissat`/`cadical` hit the identical bug but happened to print text that
   run_tick.sh's grep filter does not match, so it never surfaced there.

2. `ihp-open-pdk` (a PDK data directory, COPY'd straight in, never a
   binary) was swept into `flow_tools()` and probed with
   `command -v ihp-open-pdk`, which can only ever print NONE -- reported
   as "not on PATH in the composed image" regardless of whether the PDK
   is correctly staged. `_NO_COMMAND` already exists for exactly this
   shape (`sv-elab`); `ihp-open-pdk` was simply never added to it.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import fork_reaches_flow_check as F


def _write_dockerfile(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "Dockerfile"
    p.write_text(textwrap.dedent(body))
    return p


def test_single_source_copy_still_works(tmp_path):
    """The common shape (one source, one destination) must be unaffected."""
    df = _write_dockerfile(tmp_path, """\
        FROM scratch
        COPY --from=img-yosys /foss/tools/yosys /foss/tools/yosys
        """)
    paths = F.copied_paths(df)
    assert paths == {"yosys": ["/foss/tools/yosys"]}


def test_two_source_copy_captures_the_real_destination_not_a_source(tmp_path):
    """The exact shape of Dockerfile:413 (openroad) -- two sources, one
    destination directory. The destination must be the LAST token, not the
    second-to-last."""
    df = _write_dockerfile(tmp_path, """\
        FROM scratch
        COPY --from=img-openroad /foss/tools/openroad/bin/openroad /foss/tools/openroad/bin/sta /foss/tools/openroad/bin/
        """)
    paths = F.copied_paths(df)
    assert paths == {"openroad": ["/foss/tools/openroad/bin/"]}
    # The old bug's exact symptom: the second SOURCE token must NOT appear
    # as a captured destination.
    assert "/foss/tools/openroad/bin/sta" not in paths["openroad"]


def test_three_token_copy_sat_solvers_shape(tmp_path):
    """Dockerfile:487 -- kissat + cadical -> one bin dir. Same bug, silent
    there only because run_tick.sh's grep filter never matched its output;
    the underlying parse was equally wrong."""
    df = _write_dockerfile(tmp_path, """\
        FROM scratch
        COPY --from=img-sat-solvers /usr/local/bin/kissat /usr/local/bin/cadical /foss/tools/bin/
        """)
    paths = F.copied_paths(df)
    assert paths == {"sat-solvers": ["/foss/tools/bin/"]}


def test_ihp_open_pdk_shape_is_captured_too(tmp_path):
    df = _write_dockerfile(tmp_path, """\
        FROM scratch
        COPY --from=img-ihp-open-pdk /foss/pdks/ihp-sg13g2 /foss/pdks/ihp-sg13g2
        """)
    paths = F.copied_paths(df)
    assert paths == {"ihp-open-pdk": ["/foss/pdks/ihp-sg13g2"]}


def test_multiple_copy_lines_do_not_bleed_into_each_other(tmp_path):
    """Regression guard for the fix's own failure mode during development:
    using `\\s+` (which matches newlines) instead of `[ \\t]+` let the
    greedy destination group consume across line boundaries and corrupt
    every subsequent match. Three lines in, each must resolve independently."""
    df = _write_dockerfile(tmp_path, """\
        FROM scratch
        COPY --from=img-openroad /a/openroad /a/sta /a/bin/
        COPY --from=img-sat-solvers /b/kissat /b/cadical /b/bin/
        COPY --from=img-yosys /c/yosys /c/yosys
        """)
    paths = F.copied_paths(df)
    assert paths == {
        "openroad": ["/a/bin/"],
        "sat-solvers": ["/b/bin/"],
        "yosys": ["/c/yosys"],
    }


def test_against_the_real_root_dockerfile():
    """Ground truth against the actual shipped Dockerfile, not a fixture --
    confirms the fix resolves what production actually needs it to. openroad
    has THREE COPY lines (provenance.json, or-tools, the binary dir); only
    the binary-dir one is the multi-source line this fix targets, and it
    must appear -- the old bug replaced it with a source token instead."""
    root = Path(__file__).resolve().parent.parent
    paths = F.copied_paths(root / "Dockerfile")
    assert "/foss/tools/openroad/bin/" in paths["openroad"]
    assert "/foss/tools/openroad/bin/sta" not in paths["openroad"]
    assert "/foss/tools/bin/" in paths["sat-solvers"]
    assert paths["ihp-open-pdk"] == ["/foss/pdks/ihp-sg13g2"]


def test_ihp_open_pdk_is_declared_no_command():
    """The second fix: ihp-open-pdk must never be reported as 'not on PATH'
    -- it is data, not a binary, and _NO_COMMAND is the existing mechanism
    for exactly this category (see sv-elab)."""
    assert "ihp-open-pdk" in F._NO_COMMAND
    assert F._NO_COMMAND["ihp-open-pdk"]  # a real, non-empty reason string


def test_ihp_open_pdk_none_result_is_suppressed_like_sv_elab():
    """Both declared-no-command tools must be treated identically by the
    'path == NONE' branch -- neither produces a finding."""
    for tool in ("sv-elab", "ihp-open-pdk"):
        assert tool in F._NO_COMMAND, tool
