"""Nothing ran this suite (vibeic-eda#80).

Until 2026-08-04 the only `pytest` invocations anywhere in the repo were inside
the test files themselves. Five tests had been failing for at least seven
releases and 0.2.63 published with all five red — one of them a NEGATIVE CONTROL
whose failure meant the release-record checker had genuinely stopped detecting an
unreproducible record. The disclosure existed the whole time, in a suite nobody
ran.
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND = HERE / "run_0530.sh"


def test_the_0530_round_invokes_the_suite():
    src = ROUND.read_text(encoding="utf-8")
    assert re.search(r"python3 -m pytest .*\$\{DIR\}", src), (
        "the daily round does not run the fork-gatekeeper tests; a suite "
        "nothing invokes is a directory of files")


def test_a_red_suite_blocks_the_publish_and_fails_the_round():
    """Running it is not enough — the result has to be load-bearing.

    It gates the PUBLISH half only, deliberately: the merge half is useful even
    from code whose tests are red, and blocking it would trade a
    correctness-reporting problem for a synchronisation one."""
    src = ROUND.read_text(encoding="utf-8")
    gate = re.search(r'if \[ "\$\{DISC\}" -eq 0 \][^\n]*\n', src)
    assert gate and "${TESTS}" in gate.group(0), (
        "the page publish does not consult the test result")
    verdict = src[src.index("# 0 only when BOTH are clean"):]
    assert '"${TESTS}" -ne 0' in verdict, (
        "a red suite does not fail the round, so cron reports success")


def test_every_test_module_imports_by_ABSOLUTE_path():
    """A bare `spec_from_file_location("x.py")` resolves against the WORKING
    DIRECTORY, so it collects only when pytest is invoked from this folder. From
    anywhere else it is a collection error and the whole run is rc=2 — "could not
    even collect", which is not a test result.

    That is precisely the shape that hid here: it was never noticed because
    nothing ran the suite from anywhere else, and wiring it into cron — which
    runs from `/` — is what surfaced it."""
    bad = []
    for p in sorted(HERE.glob("test_*.py")):
        for m in re.finditer(r'spec_from_file_location\(\s*[^,]+,\s*(["\'])([^"\']+)\1',
                             p.read_text(encoding="utf-8", errors="replace")):
            bad.append(f"{p.name}: {m.group(2)!r}")
    assert not bad, (
        "these load a module by a bare relative path, so the suite only "
        f"collects from this directory: {bad}")
