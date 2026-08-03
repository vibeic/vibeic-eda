"""A fingerprint mismatch has two causes and they need opposite actions. #73.

`check_release_recorded` compared the recorded fingerprint to the one the tree
recomputes and called every mismatch UNREPRODUCIBLE. But the tree's fingerprint
changes whenever a PIN MOVES, which is the normal state between one release and
the next.

Observed the day #71's durable-record fix landed: 0.2.58 was built, pushed and
recorded CORRECTLY; #72 then advanced the pyuvm pin; and the checker called the
good record unreproducible. That leaves the tick red for the whole interval
between releases, and a reader cannot tell a genuinely broken record from
"someone advanced a pin".

The split is decidable from data already on disk — the record names the pin set
it was built from:

    pins MOVED since the release   -> PINS_AHEAD. Nothing is wrong; the next
                                      release closes it.
    pins IDENTICAL, fingerprint    -> the record really cannot be re-derived.
    still differs                     The 0.2.45 shape. Stays a FAIL.

MEASURED both directions on the live repo:

    as committed (pyuvm advanced)        rc=0, "PINS_AHEAD ... (pyuvm)"
    same pins + corrupted fingerprint    rc=1, "records the SAME pin set ...
                                               yet its fingerprint ... "
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_CHECK = _HERE / "check_release_recorded.py"
_ROOT = _HERE.parent


def _run():
    return subprocess.run([sys.executable, str(_CHECK)],
                          capture_output=True, text=True, timeout=180,
                          cwd=str(_ROOT))


def _released():
    return json.loads((_ROOT / "RELEASED.json").read_text())


def test_the_repo_as_committed_is_not_a_finding():
    """Whatever state the pins are in, a correctly-recorded release must not be
    reported as a broken record."""
    r = _run()
    assert r.returncode == 0, (r.stdout + r.stderr)[-500:]


def test_a_pin_advance_is_named_not_called_unreproducible():
    """The case that started this. If the pins have moved, the line must say so
    — and must NOT claim the tree reproduces a fingerprint it does not."""
    r = _run()
    out = r.stdout + r.stderr
    if "PINS_AHEAD" not in out:
        return                    # pins currently match the record; nothing to assert
    assert "UNREPRODUCIBLE" not in out
    assert "with a fingerprint this tree reproduces" not in out, (
        "the green line states something untrue on this path")
    assert "NOT a broken record" in out


def test_the_same_pin_set_with_a_wrong_fingerprint_still_FAILS(tmp_path):
    """THE DIRECTION THAT MAKES THE SPLIT SAFE. Widening a check until it stops
    firing is not a fix; the 0.2.45 shape must still be caught."""
    p = _ROOT / "RELEASED.json"
    orig = p.read_text()
    spec = importlib.util.spec_from_file_location(
        "daily_release", _HERE / "daily_release.py")
    DR = importlib.util.module_from_spec(spec)
    sys.modules["daily_release"] = DR
    try:
        spec.loader.exec_module(DR)
    except SystemExit:
        pass
    try:
        d = json.loads(orig)
        d["pins"] = DR.pinned_refs(_ROOT)          # identical to the tree
        d["pins_fingerprint"] = "deadbeefcafe"     # but unreproducible
        p.write_text(json.dumps(d, indent=2) + "\n")
        r = _run()
        assert r.returncode == 1, (r.stdout + r.stderr)[-400:]
        assert "SAME pin set" in (r.stdout + r.stderr)
    finally:
        p.write_text(orig)


def test_recipe_hashes_are_deliberately_not_part_of_the_split():
    """`recipe:` entries hash file CONTENT, so an unrelated Dockerfile edit moves
    them with no pin having moved. The question is 'did the PINS move', and the
    recorded `pins` map is the only thing that answers it — comparing recipe
    hashes would classify every doc edit as a pin advance."""
    src = _CHECK.read_text(encoding="utf-8")
    i = src.index('stats["pins_moved"]')
    seg = src[max(0, i - 1200):i + 400]
    assert "recorded_pins != tree_pins" in seg
    assert "recipe:" not in seg.split("stats[\"pins_moved\"]")[1]


def test_the_record_still_names_the_pins_it_was_built_from():
    """The whole split rests on this field existing. If a future record drops
    it, the checker must not silently fall back to calling everything broken."""
    assert isinstance(_released().get("pins"), dict)
