#!/usr/bin/env python3
"""vibeic-eda#90 — a waiver with no RECORDED DECISION must fail the gate.

Rules 1-3 of capability_gate keep the waiver LIST honest: a name that now works
is stale, a name no probe knows is a typo. None of them asks whether anyone ever
DECIDED. Three names sat in that file across releases with their rationale in a
trailing comment, and one of those comments named the wrong mechanism entirely
(`-DBUILD_PYTHON=ON`, a CMake flag, on a Bazel build) — it read as a decision and
was a guess.

These tests are the reason rule 4 is a mechanism and not a paragraph. The first
one is the red proof: a waiver with a name and nothing else must be refused. A
guard that has never been shown to fail is worth nothing, so every blocking
state below is asserted by CONSTRUCTING it, not by trusting the code path.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

# Loaded by ABSOLUTE path: a bare relative spec resolves against the working
# directory, so the module only collects when pytest runs from this folder, and
# the 05:30 round runs from `/`. See test_the_round_runs_its_own_tests.py.
_spec = importlib.util.spec_from_file_location("capability_gate",
                                               HERE / "capability_gate.py")
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)                                   # noqa: E402

GATE = HERE / "capability_gate.py"
LIVE = HERE / "capability_waivers.txt"

#: A complete, well-formed entry. Tests below mutate ONE field of it, so that a
#: failure names the field that caused it rather than "the fixture is wrong".
GOOD = """\
some/capability
    why:        measured: the binary is not in the image
    evidence:   docker run --rm IMAGE -lc 'command -v thing' -> nothing
    cost:       apt `thing`, 3 MB, no new dependency
    decision:   DO-NOT-ADVERTISE
    by:         team-lead
    on:         2026-08-05
"""


def _write(tmp_path, text, name="w.txt"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _check(path):
    """Run the gate's waiver validation the way a caller does — as a process.

    Importing and calling would test the parser; this tests the GATE, including
    its exit status, which is the thing build_and_regress.sh acts on.
    """
    r = subprocess.run([sys.executable, str(GATE), "unused-image",
                        "--waivers", path, "--check-waivers"],
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


#: capability_smoke's verdict table for the FULL-gate tests below.
_STUB_SMOKE = '''\
import json, sys
json.dump([{"capability": "some/capability", "verdict": "BROKEN",
            "reason": "stubbed"}], open(sys.argv[sys.argv.index("--json") + 1], "w"))
'''


def _full_gate(tmp_path, waiver_text, smoke=_STUB_SMOKE):
    """Drive the WHOLE gate — probe table, waiver policy, verdict — not just the
    `--check-waivers` shortcut.

    The gate resolves its probe as `HERE/capability_smoke.py`, so copying the
    real gate next to a stub of that name substitutes the measurement without
    adding a test-only flag to the production file. `--check-waivers` alone left
    the full path's blocking untested, which a mutation of exactly that line
    survived while every test stayed green.
    """
    d = tmp_path / "gate"
    d.mkdir(exist_ok=True)
    (d / "capability_gate.py").write_text(GATE.read_text(encoding="utf-8"),
                                          encoding="utf-8")
    (d / "capability_smoke.py").write_text(smoke, encoding="utf-8")
    w = d / "w.txt"
    w.write_text(waiver_text, encoding="utf-8")
    r = subprocess.run([sys.executable, str(d / "capability_gate.py"),
                        "unused-image", "--waivers", str(w),
                        "--json", str(d / "r.json")],
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


# ── the red proof ─────────────────────────────────────────────────────────────
def test_a_waiver_with_no_decision_is_REFUSED(tmp_path):
    """THE POINT OF #90. A bare name used to be the entire format."""
    rc, out = _check(_write(tmp_path, "some/capability\n"))
    assert rc == 1, out
    assert "no recorded decision" in out, out
    for field in ("why", "evidence", "cost", "decision", "by", "on"):
        assert field in out, f"the refusal does not say {field} is missing:\n{out}"


def test_the_pre_90_one_line_form_is_refused_not_migrated(tmp_path):
    """`name  # reason` is exactly the state being closed.

    Accepting it quietly would leave the file readable and the decisions
    unowned — which is what it looked like for three releases.
    """
    rc, out = _check(_write(tmp_path, "some/capability  # it errors rc=16. Owner: team-lead.\n"))
    assert rc == 1, out
    assert "trailing comment" in out, out


def test_a_complete_entry_passes(tmp_path):
    """The negative control. Without it, every test above passes on a gate that
    refuses everything, which proves nothing at all."""
    rc, out = _check(_write(tmp_path, GOOD))
    assert rc == 0, out


# ── the three states: recorded / missing / could-not-determine ────────────────
def test_UNKNOWN_WHY_is_its_own_state_and_never_a_pass(tmp_path):
    """"We could not work out why" must be WRITABLE and must not promote.

    If it were not writable, the only way to green the gate would be to invent a
    reason — laundering a guess into the permanent record, which is the defect.
    If it passed, "unmeasured reads as zero" comes straight back.
    """
    rc, out = _check(_write(tmp_path, GOOD.replace("DO-NOT-ADVERTISE", "UNKNOWN-WHY")))
    assert rc == 1, out
    assert "UNKNOWN-WHY" in out and "could not determine" in out.lower(), out


def test_PENDING_OWNER_blocks_and_is_reported_separately_from_UNKNOWN_WHY(tmp_path):
    """Two different sentences: nobody ruled, vs nobody measured.

    Folding them together would hide which one a red gate is complaining about,
    and they need opposite work to clear.
    """
    both = (GOOD.replace("DO-NOT-ADVERTISE", "PENDING-OWNER")
            + GOOD.replace("some/capability", "other/capability")
                  .replace("DO-NOT-ADVERTISE", "UNKNOWN-WHY"))
    rc, out = _check(_write(tmp_path, both))
    assert rc == 1, out
    assert "some/capability" in out and "other/capability" in out, out
    assert "nobody has ruled" in out, out
    assert "could not determine" in out.lower(), out


def test_BUILD_passes_but_is_printed_as_dated_debt(tmp_path):
    """BUILD is a real ruling, so it must not block — and must not go quiet."""
    rc, out = _check(_write(tmp_path, GOOD.replace("DO-NOT-ADVERTISE", "BUILD")))
    assert rc == 0, out
    assert "debt" in out and "2026-08-05" in out, out


# ── the ways a record can look present and be useless ────────────────────────
def test_an_invented_decision_verb_is_refused(tmp_path):
    """`decision: deferred` reads like a decision and is not one of ours.

    Same family as rule 3: a value nothing validates records nothing while
    looking like it recorded something.
    """
    rc, out = _check(_write(tmp_path, GOOD.replace("DO-NOT-ADVERTISE", "deferred")))
    assert rc == 1, out
    assert "is not one of" in out, out


def test_a_date_shaped_non_date_is_refused(tmp_path):
    rc, out = _check(_write(tmp_path, GOOD.replace("2026-08-05", "soon")))
    assert rc == 1 and "calendar date" in out, out
    rc, out = _check(_write(tmp_path, GOOD.replace("2026-08-05", "2026-13-40")))
    assert rc == 1 and "calendar date" in out, out


def test_an_empty_required_field_is_not_a_recorded_decision(tmp_path):
    """`cost:` with nothing after it is the shape of a record and none of the
    content, and it is what a half-finished edit leaves behind."""
    rc, out = _check(_write(tmp_path, GOOD.replace(
        "cost:       apt `thing`, 3 MB, no new dependency", "cost:")))
    assert rc == 1, out
    assert "no recorded decision" in out and "cost" in out, out


def test_a_mistyped_field_is_an_error_not_a_silent_continuation(tmp_path):
    """`whys:` must not absorb itself into the value above it.

    That is the rule-3 failure wearing a different hat: the entry parses, reads
    as complete, and is missing the field the typo was meant to be.
    """
    rc, out = _check(_write(tmp_path, GOOD.replace("why:  ", "whys: ")))
    assert rc == 1, out
    assert "unknown field" in out and "whys" in out, out


def test_a_duplicated_field_is_refused(tmp_path):
    """Two `decision:` lines means the file says two things and the parser picks
    one. Which one it picks is not a decision anybody made."""
    rc, out = _check(_write(tmp_path, GOOD + "    decision:   BUILD\n"))
    assert rc == 1, out
    assert "twice" in out, out


def test_the_same_capability_waived_twice_is_refused(tmp_path):
    rc, out = _check(_write(tmp_path, GOOD + GOOD))
    assert rc == 1, out
    assert "waived twice" in out, out


# ── the format's own sharp edge, which bit this file on its first draft ──────
def test_quoted_tool_output_starting_error_can_be_continued(tmp_path):
    """`evidence:` quotes tool output, and tool output says `error:` constantly.

    The closed-key rule refuses it as an unknown field, which is loud and
    correct; `...` is the documented answer, and the marker must not survive
    into the recorded value — otherwise the escape trades a parse error for a
    record with punctuation noise in the middle of the evidence.
    The live capability_waivers.txt hit this on its first draft.
    """
    txt = GOOD.replace(
        "    evidence:   docker run --rm IMAGE -lc 'command -v thing' -> nothing",
        "    evidence:   yosys -p 'plugin -i ghdl.so' -> `yosys: symbol lookup\n"
        "                ... error: undefined symbol: _ZN5Yosys4Pass`")
    rc, out = _check(_write(tmp_path, txt))
    assert rc == 0, out
    e, errs = G.read_waivers(_write(tmp_path, txt, "e.txt"))
    assert not errs, errs
    ev = e["some/capability"]["evidence"]
    assert "symbol lookup error: undefined symbol" in ev, ev
    assert "..." not in ev, f"the continuation marker leaked into the record: {ev}"


def test_a_wrapped_line_starting_with_a_KNOWN_key_does_not_silently_steal_it(tmp_path):
    """The escape's real job, and the only silent failure in this format.

    A `why:` value that wraps onto a line beginning `cost:` opens a REAL field:
    no error, `why` truncated at the wrap, and `cost` holding half a sentence
    from a different thought. Nothing looks wrong. `...` is what makes it stay
    put, and this is the case that justifies the marker existing at all.
    """
    stolen = GOOD.replace(
        "    why:        measured: the binary is not in the image",
        "    why:        measured: the binary is absent and the fix would\n"
        "                cost: a new fork")
    e, _ = G.read_waivers(_write(tmp_path, stolen, "s.txt"))
    assert e["some/capability"]["cost"] == "a new fork", (
        "this documents the trap, not the desired behaviour")

    kept = stolen.replace("                cost: a new fork",
                          "                ... cost: a new fork")
    e2, errs = G.read_waivers(_write(tmp_path, kept, "k.txt"))
    assert not errs, errs
    assert e2["some/capability"]["why"].endswith("cost: a new fork"), e2
    assert e2["some/capability"]["cost"] == "apt `thing`, 3 MB, no new dependency", e2


def test_a_wrapped_value_joins_to_the_same_string_as_one_line(tmp_path):
    """Wrapping is presentation. If it changed the value, the record would
    depend on where someone happened to break the line."""
    wrapped = _write(tmp_path, GOOD.replace(
        "    cost:       apt `thing`, 3 MB, no new dependency",
        "    cost:       apt `thing`, 3 MB,\n                no new dependency"), "a.txt")
    flat = _write(tmp_path, GOOD, "b.txt")
    assert (G.read_waivers(wrapped)[0]["some/capability"]["cost"]
            == G.read_waivers(flat)[0]["some/capability"]["cost"])


# ── the FULL gate, not the --check-waivers shortcut ──────────────────────────
# Everything above validates the file. These drive the release gate itself: the
# probe table says BROKEN, the waiver says something about it, and the exit
# status is what build_and_regress.sh promotes or refuses on.
def test_full_gate_REFUSES_a_waived_capability_with_no_decision(tmp_path):
    """The red proof at gate level. The name alone waives the breakage out of
    'not working and not waived' — and must not thereby waive it at all."""
    rc, out = _full_gate(tmp_path, "some/capability\n")
    assert rc == 1, out
    assert "no usable recorded decision" in out, out
    assert "capability-gate: FAIL" in out, out


def test_full_gate_REFUSES_an_unsettled_decision(tmp_path):
    rc, out = _full_gate(tmp_path, GOOD.replace("DO-NOT-ADVERTISE", "PENDING-OWNER"))
    assert rc == 1, out
    assert "BLOCKING -- decision PENDING-OWNER" in out, out
    assert "capability-gate: FAIL" in out, out


def test_full_gate_REFUSES_could_not_determine_why(tmp_path):
    rc, out = _full_gate(tmp_path, GOOD.replace("DO-NOT-ADVERTISE", "UNKNOWN-WHY"))
    assert rc == 1, out
    assert "BLOCKING -- decision UNKNOWN-WHY" in out, out


def test_full_gate_PASSES_a_settled_waiver(tmp_path):
    """The negative control for the three above."""
    rc, out = _full_gate(tmp_path, GOOD)
    assert rc == 0, out
    assert "capability-gate: PASS" in out, out


def test_full_gate_passes_BUILD_and_prints_it_as_debt(tmp_path):
    rc, out = _full_gate(tmp_path, GOOD.replace("DO-NOT-ADVERTISE", "BUILD"))
    assert rc == 0, out
    assert "outstanding BUILD debt" in out and "2026-08-05" in out, out


def test_full_gate_still_reports_the_decision_beside_each_waived_line(tmp_path):
    """The per-waiver line in the summary used to print a free-text reason. If
    it does not print the decision and its owner, the release log shows what we
    believed and not who agreed to it."""
    rc, out = _full_gate(tmp_path, GOOD)
    assert "DO-NOT-ADVERTISE" in out and "team-lead" in out and "2026-08-05" in out, out


def test_full_gate_still_refuses_a_stale_waiver(tmp_path):
    """Rule 2 must survive rule 4. A complete, well-argued, signed decision to
    waive something that now WORKS is still a stale waiver."""
    works = _STUB_SMOKE.replace('"BROKEN"', '"WORKS"')
    rc, out = _full_gate(tmp_path, GOOD, smoke=works)
    assert rc == 1, out
    assert "STALE WAIVER" in out, out


def test_full_gate_still_refuses_an_unwaived_breakage(tmp_path):
    """Rule 1 must survive rule 4 too — an empty waiver file is not a licence."""
    rc, out = _full_gate(tmp_path, "")
    assert rc == 1, out
    assert "not working and not waived" in out, out


def test_no_waiver_file_at_all_is_not_itself_a_failure(tmp_path):
    """The goal state of this gate is an image with nothing left to waive.

    A stricter parse that treats a missing file as an error would make reaching
    that state impossible — the gate would go red for having nothing to excuse.
    Everything green plus no waiver file must PASS.
    """
    works = _STUB_SMOKE.replace('"BROKEN"', '"WORKS"')
    entries, errors = G.read_waivers(str(tmp_path / "definitely-not-here.txt"))
    assert (entries, errors) == ({}, [])
    d = tmp_path / "nofile"
    d.mkdir()
    (d / "capability_gate.py").write_text(GATE.read_text(encoding="utf-8"), encoding="utf-8")
    (d / "capability_smoke.py").write_text(works, encoding="utf-8")
    r = subprocess.run([sys.executable, str(d / "capability_gate.py"), "unused-image",
                        "--waivers", str(d / "absent.txt"), "--json", str(d / "r.json")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "capability-gate: PASS" in r.stdout, r.stdout


# ── the live file, so the real record cannot rot ─────────────────────────────
def test_the_shipped_waiver_file_parses_with_no_errors():
    """Every test above uses a fixture. This one is the only test that can catch
    the live file going malformed, which is the file that actually gates a
    release."""
    entries, errors = G.read_waivers(str(LIVE))
    assert not errors, errors
    assert entries, "the waiver file parsed to nothing at all"


def test_every_live_waiver_carries_a_full_record():
    """Not 'has a decision' — has all six fields, non-empty.

    A `decision:` with no `cost:` beside it cannot be reviewed: 'build it or do
    not advertise it' is unanswerable without knowing what building it costs.
    """
    entries, _ = G.read_waivers(str(LIVE))
    for name, f in entries.items():
        for field in G.WAIVER_REQUIRED:
            assert f.get(field, "").strip(), f"{name}: {field} is empty"
        assert f["decision"] in G.DECISION_ALL, f"{name}: {f['decision']}"


def test_a_live_waiver_that_is_unsettled_says_who_has_to_rule():
    """A PENDING-OWNER with no owner is a decision nobody is waiting on."""
    entries, _ = G.read_waivers(str(LIVE))
    for name, f in entries.items():
        if f["decision"] in G.DECISION_UNSETTLED:
            assert f["by"].strip(), f"{name}: unsettled with no `by:`"
