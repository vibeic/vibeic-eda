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
from types import SimpleNamespace

import json

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


# --- progress-supervised probes: incomplete work is not origin drift

def test_docker_probe_uses_activity_lease_without_a_total_ceiling(monkeypatch):
    seen = {}

    def run(image, script, stall_after):
        seen.update(image=image, script=script, stall_after=stall_after)
        return SimpleNamespace(rc=0, output="answer", outcome="natural")

    monkeypatch.setattr(F, "run_container_probe", run)
    got = F._sh(["docker", "run", "--rm", "--entrypoint", "bash",
                 "example:image", "-lc", "real-work"])

    assert got == F.ProbeRun(0, "answer", "", "natural")
    assert seen == {"image": "example:image", "script": "real-work",
                    "stall_after": F.PROBE_STALL_GRACE_S}


def test_stalled_path_probe_is_unknown_not_not_ours(tmp_path, monkeypatch):
    df = _write_dockerfile(tmp_path, """\
        FROM scratch
        COPY --from=img-yosys /y /foss/tools/yosys
        """)
    monkeypatch.setattr(
        F, "_sh",
        lambda *args, **kwargs: F.ProbeRun(199, "", "flat lease", "stalled"))

    rows = F.check("img", df)

    assert len(rows) == 1 and rows[0]["not_checked"] is True
    assert rows[0]["probe_outcome"] == "stalled"
    assert not rows[0].get("we_build_it")


def test_failed_source_probe_rc_does_not_fabricate_source_absence(
        tmp_path, monkeypatch):
    df = _write_dockerfile(tmp_path, """\
        FROM scratch
        COPY --from=img-yosys /y /foss/tools/yosys
        """)

    def run(cmd, timeout=None):
        del timeout
        if "command -v" in cmd[-1]:
            return 0, "sby /usr/local/bin/sby /usr/local/bin/sby\n", ""
        return F.ProbeRun(73, "", "source probe failed", "natural")

    monkeypatch.setattr(F, "_sh", run)
    sby = [row for row in F.check("img", df) if row["tool"] == "sby"]

    assert sby and all(row.get("not_checked") for row in sby)
    assert not [row for row in sby if row.get("we_build_it")]


def test_provenance_probe_failure_is_norecord_not_drift(monkeypatch):
    monkeypatch.setattr(
        F, "_sh",
        lambda *args, **kwargs: F.ProbeRun(
            127, "", "image unavailable", "launch_error"))

    rows = F.provenance_drift("img", {"yosys": "a" * 40})

    assert len(rows) == 1 and rows[0]["not_checked"] is True
    assert rows[0]["probe_outcome"] == "launch_error"
    assert "image unavailable" in rows[0]["problem"]


def test_unknown_path_probe_makes_main_norecord_and_retains_json(
        tmp_path, monkeypatch):
    df = _write_dockerfile(tmp_path, """\
        FROM scratch
        COPY --from=img-yosys /y /foss/tools/yosys
        """)
    unknown = {"tool": "yosys", "not_checked": True,
               "probe_outcome": "stalled", "probe_rc": 199,
               "problem": "PATH probe made no progress"}
    monkeypatch.setattr(F, "check", lambda image, dockerfile: [unknown])
    monkeypatch.setattr(F, "provenance_drift", lambda image, pins: [])
    monkeypatch.setattr(F, "flow_tools", lambda dockerfile: ("yosys",))
    report = tmp_path / "reach.json"

    rc = F.main(["img", "--dockerfile", str(df), "--json", str(report)])

    assert rc == F.RC_NOTHING
    assert json.loads(report.read_text(encoding="utf-8"))["findings"] == [unknown]
