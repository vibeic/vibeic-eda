"""A red-suite refusal must say WHICH TREE the suite ran on.

MEASURED 2026-08-29. The 05:30 round logged, in this order:

    check_round_code_is_current exit 1
    fork-gatekeeper tests exit 1
    run_tick NOT CHECKED — semantic pytest RED

and `cron.log` — the channel a human actually reads — carried only the third
line. The checkout was parked on an unmerged feature branch, and the single red
test guarded a `COPY` of a file that exists ONLY on that branch. A reader
following cron.log went hunting for a test defect and began editing
`.dockerignore` on main, where the file that test names does not exist.

The information that would have stopped that was already computed and one
variable away. These tests pin it into the message.

They are STRUCTURAL, on purpose: driving the real `run_0530.sh` would clone
forks and build images. What is asserted is the property that was missing --
the refusal that mentions the tests also mentions the checkout, on both
channels -- and that it is conditional, so a current checkout is not accused.
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "run_0530.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _pytest_refusal_block() -> str:
    """The branch taken when the semantic suite is not green."""
    text = _text()
    start = text.index('if [ "${TESTS}" -ne 0 ]; then')
    end = text.index('elif [ "${CODE}" -ne 0 ]; then', start)
    return text[start:end]


def test_the_refusal_consults_the_checkout_verdict():
    """`${CODE}` is computed before this branch; it must be read inside it."""
    block = _pytest_refusal_block()
    assert '"${CODE}"' in block, (
        "the pytest refusal never reads check_round_code_is_current's verdict, "
        "so it cannot tell a reader that the suite measured another tree")


def test_both_channels_carry_the_checkout_note():
    """cron.log is the one a human reads; it must not be the poorer of the two."""
    block = _pytest_refusal_block()
    # COMMENTS ARE NOT EMITS. The comment above this branch quotes the very log
    # line under test, and counting it found three "refusal lines" where the
    # script emits two -- prose read as code, one level down from the defect
    # this whole change is about.
    lines = [ln for ln in block.splitlines()
             if "run_tick NOT CHECKED" in ln and ln.lstrip().startswith("echo ")]
    assert len(lines) == 2, lines
    for ln in lines:
        assert "${CHECKOUT_NOTE}" in ln, (
            f"this refusal line omits the checkout note: {ln.strip()}")
    assert any('cron.log' in ln for ln in lines), lines


def test_the_note_is_conditional_and_names_the_exit_code():
    """A current checkout must not be accused, and the reader gets the number."""
    block = _pytest_refusal_block()
    assert re.search(r'CHECKOUT_NOTE=""', block), (
        "the note is not cleared, so it could leak from a previous round")
    guard = re.search(r'if \[ "\$\{CODE\}" -ne 0 \]; then\s*\n\s*CHECKOUT_NOTE=', block)
    assert guard, "the note is not guarded by a non-zero CODE"
    assert "check_round_code_is_current exit ${CODE}" in block, (
        "the note does not name the verdict a reader would grep for")


def test_the_note_says_what_to_do_before_reading_the_test_as_a_defect():
    """The measured harm was a reader fixing the wrong repository."""
    block = _pytest_refusal_block()
    note = block[block.index('CHECKOUT_NOTE="'):]
    note = note[:note.index('"\n', 20) if '"\n' in note[20:] else 400]
    assert "would not have published" in note or "not the current round code" in note
    assert "before reading the test as a defect" in note


def test_the_pytest_result_still_decides():
    """This changes what the refusal SAYS, never what it gates."""
    block = _pytest_refusal_block()
    assert "TICK=2" in block
    assert "no release was authorised" in block
