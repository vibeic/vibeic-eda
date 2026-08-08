#!/usr/bin/env python3
"""vibeic-eda#96: a FAILING release round's cron-log line must name the cause.

`run_tick.sh`'s [release] block used `head -1` for both outcomes. That is
right for a PASSING round (the summary line is the whole story) and wrong for
a FAILING one -- reproduced twice for real (0.2.47 on 2026-07-31, 0.2.64 on
2026-08-05), both times rendering only:

    [release]   [FAIL] the image did not compose after a retry; no version
    was cut, so nothing claims to be a release:

a line ending in a colon, with the actual `ERROR: failed to solve: ...` from
docker buildx sitting ~130 lines further into the same file, unread, because
none of the existing grep's keywords (`VERSION|building|composing|
UNRESOLVED|FAIL`) match lowercase "failed".

These tests fabricate that exact shape -- a synthetic daily-release.txt with
noise BEFORE and AFTER a buried ERROR block -- and prove two things: (1) the
OLD single-line behaviour really does lose the cause (the red half, run
against the unmodified fixture with the pre-fix command), and (2) the NEW
`release_failure_summary.sh` recovers it regardless of where in the file it
sits.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

DIR = Path(__file__).resolve().parent
SCRIPT = DIR / "release_failure_summary.sh"

# The exact buildx failure text from the #96 report, embedded ~130 lines into
# a realistic amount of surrounding compose chatter -- position matters here,
# since the whole point is that `head -1` cannot reach it.
_NOISE_LINE = "#12 [img-iverilog builder 3/9] RUN make -j check\n"
_ERROR_BLOCK = (
    "ERROR: failed to solve: ghcr.io/vibeic/eda-tool-iverilog:26933e0-5b8871:\n"
    "  failed to resolve source metadata: ... not found\n"
    "Dockerfile:107  FROM ${IMG_IVERILOG} AS img-iverilog\n"
)
_SUMMARY_LINE = (
    "[FAIL] the image did not compose after a retry; no version was cut, so "
    "nothing claims to be a release:\n"
)


def _fixture(tmp_path: Path, error_at_line: int = 130) -> Path:
    lines = [_NOISE_LINE] * error_at_line
    lines.append(_ERROR_BLOCK)
    lines.append(_SUMMARY_LINE)
    p = tmp_path / "daily-release.txt"
    p.write_text("".join(lines))
    return p


def test_the_old_head_minus_one_loses_the_cause(tmp_path):
    """RED: reproduce the reported defect on the unmodified fixture."""
    fixture = _fixture(tmp_path)
    old = subprocess.run(["head", "-1", str(fixture)], capture_output=True, text=True)
    assert "ERROR: failed to solve" not in old.stdout
    assert old.stdout == _NOISE_LINE


def test_the_new_summary_finds_the_cause_regardless_of_position(tmp_path):
    """GREEN: the fix surfaces the real cause."""
    fixture = _fixture(tmp_path, error_at_line=130)
    new = subprocess.run([str(SCRIPT), str(fixture)], capture_output=True, text=True)
    assert new.returncode == 0
    assert "ERROR: failed to solve: ghcr.io/vibeic/eda-tool-iverilog" in new.stdout
    assert "failed to resolve source metadata" in new.stdout
    assert "Dockerfile:107" in new.stdout


def test_position_independence_at_the_very_start_and_the_very_end(tmp_path):
    for error_at_line in (0, 400):
        fixture = _fixture(tmp_path, error_at_line=error_at_line)
        new = subprocess.run([str(SCRIPT), str(fixture)], capture_output=True, text=True)
        assert "ERROR: failed to solve" in new.stdout, f"lost at line {error_at_line}"


def test_a_passing_round_has_no_error_block_to_find():
    """A file with no ERROR: line (the ordinary passing case) prints nothing
    and does not fail -- this script only reports what it finds; the CALLER
    (run_tick.sh) decides whether to invoke it at all, gated on release_rc."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("composing ghcr.io/vibeic/vibeic-eda:0.2.99 …\nVERSION: 0.2.99\n")
        path = f.name
    try:
        new = subprocess.run([SCRIPT, path], capture_output=True, text=True)
        assert new.returncode == 0
        assert new.stdout == ""
    finally:
        Path(path).unlink()


def test_missing_argument_is_a_usage_error_not_a_silent_pass():
    new = subprocess.run([str(SCRIPT)], capture_output=True, text=True)
    assert new.returncode != 0
    assert "usage" in new.stderr.lower()
