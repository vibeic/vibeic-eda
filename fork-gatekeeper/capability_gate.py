#!/usr/bin/env python3
"""Turn capability_smoke.py's measurement into a BLOCKING release decision.

WHY THIS IS SEPARATE FROM capability_smoke.py
=============================================
`capability_smoke.py` is a MEASUREMENT: it drives each capability's real entry
point and reports WORKS / BROKEN / INCONCLUSIVE.  It has no opinion about which
breakages we have decided to live with, and it should not have one -- the moment
a probe file starts carrying exceptions, the exceptions become invisible.

This file is the POLICY, and it is deliberately the only place a breakage can be
excused.  Waivers live in `capability_waivers.txt`, by NAME, with a RECORDED
DECISION -- measured reason, cost to fix, ruling, owner, date.

WHAT MAKES A GATE DIFFERENT FROM A REPORT
=========================================
`daily_release.py:SMOKE` and `build_and_regress.sh` already print things.  The
audit that produced this file (vibeic-eda#84/#87) found nine capabilities dead in
a shipped image while every existing probe passed, so "we print it" is exactly
the state being fixed.  This returns a non-zero status the caller must act on.

FOUR DECISIONS, EACH ONE A FAILURE MODE SOMEONE ELSE ALREADY HIT
================================================================
1. INCONCLUSIVE BLOCKS.  capability_smoke's own exit code counts only BROKEN, so
   a probe whose control is ALSO red exits 0.  That is "unmeasured reads as
   zero", and it is the single most common way this class of defect survives.
   Here, not knowing does not promote.

2. A STALE WAIVER IS A FAILURE.  If a waived capability now WORKS, the gate goes
   RED until the line is deleted.  A waiver list that only ever grows becomes a
   record of what we once believed; making the fix break the build is what keeps
   it a record of what is true.  (The adjacent trap -- raising a baseline COUNT
   until the gate goes green -- is why there is no count anywhere in this file
   or in the waiver file.  You waive by name or not at all.)

3. AN UNKNOWN WAIVER NAME IS A FAILURE.  A typo would otherwise waive nothing
   while reading as though it waived something, which is worse than no waiver.

4. A WAIVER WITH NO RECORDED DECISION IS A FAILURE (vibeic-eda#90).  Rules 1-3
   keep the LIST honest and say nothing about whether anyone ever DECIDED.  For
   three releases the file carried three names whose rationale was a free-text
   comment nobody owned, and one of those comments named the wrong mechanism --
   `-DBUILD_PYTHON=ON`, a CMake flag, on an OpenROAD that has been a Bazel build
   since upstream switched.  It read as a decision and was a guess, and it would
   have sent the next person to add a flag that does nothing.  So a waiver now
   carries a STRUCTURED record -- measured `why`, the `evidence` command that
   produced it, the `cost` to have the capability, the `decision`, `by`, `on` --
   and the gate refuses the entry without one.

   The decision vocabulary is deliberately small and two of its four members
   BLOCK:

     DO-NOT-ADVERTISE  terminal.  The image does not offer this; passes.
     BUILD             we intend to build it; passes, and prints as DATED debt.
     PENDING-OWNER     the reason is measured, nobody has ruled.  BLOCKS.
     UNKNOWN-WHY       we could not determine why it is absent.  BLOCKS.

   The last two exist so that "we do not know" is WRITABLE.  Without them the
   only way to make the gate green is to invent a decision, and an invented
   decision is the exact defect rule 4 is closing -- it launders a guess into
   the permanent record, where it is indistinguishable from a measurement.
   Blocking on them is the same principle as decision 1: not knowing does not
   promote.

   WHAT RULE 4 DOES NOT DO, stated so a green gate is not read as more than it
   is: a BUILD decision does not expire.  It is dated and printed as outstanding
   debt on every release; it is not counted and not aged out.  An expiry would
   be a number, and a number is what rule 2's parenthesis exists to keep out.

Exit: 0 = every non-waived capability WORKS and every waiver is still earned and
          carries a settled decision.
      1 = a capability is BROKEN/INCONCLUSIVE unwaived; or a waiver is stale,
          unknown, malformed, or has no settled decision.
      2 = capability_smoke could not run the image at all (RC_NOIMAGE), i.e. we
          measured nothing -- which is a failure, not a pass.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SMOKE = os.path.join(HERE, "capability_smoke.py")
WAIVERS = os.path.join(HERE, "capability_waivers.txt")
VERSION_FILE = os.path.join(HERE, os.pardir, "VERSION")

RC_OK, RC_FAIL, RC_NOIMAGE = 0, 1, 2

#: The CLOSED set of fields a waiver entry may carry.  Closed rather than open
#: for the same reason rule 3 exists: an unrecognised key is a typo, and a typo
#: that is silently accepted records nothing while looking like it recorded
#: something.  `whys:` must fail, not vanish.
WAIVER_FIELDS = ("why", "evidence", "cost", "decision", "recommend", "by", "on")

#: Every one of these must be present and non-empty.  `evidence` is required for
#: the reason the module docstring gives: the defect being closed is a rationale
#: that was never measured, and a rationale with no command behind it is exactly
#: that.  `cost` is required because it is the information the decision turns on
#: -- "build it or do not advertise it" is unanswerable without it.
WAIVER_REQUIRED = ("why", "evidence", "cost", "decision", "by", "on")

#: A settled decision.  BUILD passes: it is a real ruling, just not a finished
#: one, and it is printed as dated debt rather than hidden.
DECISION_SETTLED = {"DO-NOT-ADVERTISE", "BUILD"}

#: An UNSETTLED decision blocks, each under its own heading.  These are not
#: error states -- they are the states that make "we have not ruled" and "we do
#: not know why" writable at all.  Take them away and the only green path is an
#: invented decision.
DECISION_UNSETTLED = {
    "PENDING-OWNER": "measured, but nobody has ruled on it",
    "UNKNOWN-WHY": "we could not determine WHY the capability is absent",
}

DECISION_ALL = DECISION_SETTLED | set(DECISION_UNSETTLED)

#: An indented `key:` opens a field; anything else indented CONTINUES the value
#: above it.  Restricted to lowercase words so that a value which merely happens
#: to contain a colon (`RC=16: ...`) reads as continuation, while a mistyped key
#: still matches here and is rejected by name rather than silently absorbed.
_FIELD = re.compile(r"^\s+([a-z][a-z_-]*):\s?(.*)$")

#: The escape hatch.  `evidence:` values quote tool output, and tool output is
#: full of lines that open with a lowercase word and a colon -- `error:`,
#: `warning:`, `note:`.  Wrapping one onto a continuation line reads as a
#: mistyped field, which is what happened to capability_waivers.txt's own
#: yosys/vhdl-synth entry the first time it was written.
#:
#: THAT case is merely loud.  The case this exists for is SILENT: a value that
#: wraps onto a line beginning with a key we DO know -- `... the fix would\ncost:
#: a new fork` -- opens a real field.  No error, the first value truncated at the
#: wrap, and `cost` holding half a sentence from a different thought.  A leading
#: `...` says CONTINUATION unconditionally and is stripped from the value, so the
#: closed-key rule can stay strict without that trap.
_CONT = re.compile(r"^\s+\.\.\.\s?(.*)$")


def _valid_date(value: str) -> bool:
    """A real calendar date in YYYY-MM-DD, not merely something date-shaped.

    `on: soon` and `on: 2026-13-40` both have to fail.  A date field nobody
    validates is a free-text field with a misleading name, and the point of
    recording WHEN a decision was made is to be able to tell a fresh ruling from
    one that has been sitting unexamined.
    """
    try:
        datetime.date.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def _example_image() -> str:
    """An example tag for --help, READ from VERSION rather than written here.

    A literal `ghcr.io/vibeic/vibeic-eda:0.2.NN` in this file is exactly the
    defect this gate exists to catch, one level up: it is a pointer at a
    specific image that nothing keeps in step with the one being shipped, so it
    goes stale silently and then describes the wrong artefact. The repo's own
    `test_image_version_history_line.py` fails on an unregistered live pointer,
    and it was right to -- the first version of this file hardcoded a tag that
    did not even exist yet.
    """
    try:
        with open(VERSION_FILE, encoding="utf-8") as fh:
            return "ghcr.io/vibeic/vibeic-eda:" + fh.read().strip()
    except OSError:
        return "ghcr.io/vibeic/vibeic-eda:<version>"


def read_waivers(path: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Parse capability_waivers.txt -> (name -> fields, malformed-entry errors).

    An entry is a NAME at column 0 followed by indented `key: value` fields; a
    value continues on any further indented line that does not open a field of
    its own.  Comments (`#` at the start of a stripped line) and blanks are
    ignored anywhere.

    RETURNING THE ERRORS RATHER THAN RAISING is deliberate.  A malformed waiver
    file must make the gate go RED with every problem named at once, not abort on
    the first one -- the caller of this gate is a release script, and a traceback
    there reads as "the tooling broke" rather than "your waiver is wrong".  The
    parse never throws on content; it only reports.

    THE OLD ONE-LINE FORM IS REJECTED, NOT MIGRATED.  `name  # reason` used to be
    the whole format, and quietly accepting it would leave entries whose decision
    is a comment -- the exact state vibeic-eda#90 closed.  It is named as an
    error with the fix in the message.
    """
    entries: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    if not os.path.exists(path):
        # NO FILE IS NOT AN ERROR, and making it one would block the state this
        # whole gate is aiming at: an image with nothing left to waive.  A wrong
        # --waivers path cannot hide behind this -- every capability it meant to
        # waive then lands in "not working and not waived", which is louder.
        return entries, errors

    cur: str | None = None
    last_key: str | None = None
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh.readlines(), 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.strip().startswith("#"):
                continue

            if line[:1].isspace():
                esc = _CONT.match(line)
                m = None if esc else _FIELD.match(line)
                if esc:
                    if cur is None or last_key is None:
                        errors.append(f"line {lineno}: `...` continuation before "
                                      f"any field")
                        continue
                    entries[cur][last_key] = (
                        entries[cur][last_key] + " " + esc.group(1).strip()).strip()
                    continue
                if not m:
                    # Continuation of the field above.  Joined with a single
                    # space so a value wrapped for readability compares equal to
                    # the same value written on one line.
                    if cur is None or last_key is None:
                        errors.append(f"line {lineno}: indented text before any "
                                      f"capability name: {line.strip()[:60]!r}")
                        continue
                    entries[cur][last_key] = (
                        entries[cur][last_key] + " " + line.strip()).strip()
                    continue
                key, value = m.group(1), m.group(2).strip()
                if cur is None:
                    errors.append(f"line {lineno}: field {key!r} before any "
                                  f"capability name")
                    continue
                if key not in WAIVER_FIELDS:
                    errors.append(
                        f"{cur}: line {lineno}: unknown field {key!r} "
                        f"(known: {', '.join(WAIVER_FIELDS)}).  If this is not a "
                        f"field but a wrapped line of {last_key or 'the value'} "
                        f"above that happens to start `{key}:`, prefix it with "
                        f"`...` to force continuation")
                    continue
                if key in entries[cur]:
                    errors.append(f"{cur}: line {lineno}: field {key!r} given twice")
                    continue
                entries[cur][key] = value
                last_key = key
                continue

            name = line.strip()
            if "#" in name:
                errors.append(
                    f"line {lineno}: {name.split('#')[0].strip()!r} carries its "
                    f"reason as a trailing comment.  That is the pre-#90 format; "
                    f"the reason goes in a `why:` field with `evidence:`, "
                    f"`cost:`, `decision:`, `by:` and `on:` beneath the name")
                name = name.split("#")[0].strip()
            if name in entries:
                errors.append(f"line {lineno}: {name!r} is waived twice")
            entries.setdefault(name, {})
            cur, last_key = name, None

    for name, fields in entries.items():
        missing = [k for k in WAIVER_REQUIRED if not fields.get(k, "").strip()]
        if missing:
            errors.append(f"{name}: no recorded decision -- missing "
                          f"{', '.join(missing)}")
        dec = fields.get("decision", "").strip()
        if dec and dec not in DECISION_ALL:
            errors.append(f"{name}: decision {dec!r} is not one of "
                          f"{', '.join(sorted(DECISION_ALL))}")
        on = fields.get("on", "").strip()
        if on and not _valid_date(on):
            errors.append(f"{name}: on={on!r} is not a YYYY-MM-DD calendar date")

    return entries, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", help="image to probe, e.g. " + _example_image())
    ap.add_argument("--waivers", default=WAIVERS)
    ap.add_argument("--json", metavar="FILE", help="keep the raw probe table here")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument(
        "--check-waivers", action="store_true",
        help="validate the waiver file's recorded decisions and exit, without "
             "probing the image.  Same verdict as the full gate applies to the "
             "file, in a second instead of half an hour -- so a typo in a "
             "decision is caught by the test suite and by a pre-push hook, not "
             "at the end of a release run.")
    a = ap.parse_args()

    waived, waiver_errors = read_waivers(a.waivers)
    unsettled = [(c, f["decision"].strip()) for c, f in sorted(waived.items())
                 if f.get("decision", "").strip() in DECISION_UNSETTLED]
    debt = [(c, f.get("on", "").strip()) for c, f in sorted(waived.items())
            if f.get("decision", "").strip() == "BUILD"]

    if a.check_waivers:
        for err in waiver_errors:
            print(f"  BLOCKING -- {err}")
        for cap, dec in unsettled:
            print(f"  BLOCKING -- {cap}: decision is {dec} "
                  f"({DECISION_UNSETTLED[dec]})")
        for cap, on in debt:
            print(f"  debt      -- {cap}: decision BUILD, recorded {on}, not built")
        good = not waiver_errors and not unsettled
        print(f"\ncapability-gate waivers ({len(waived)} entries): "
              f"{'PASS' if good else 'FAIL'}")
        return RC_OK if good else RC_FAIL

    tmp = a.json or os.path.join(tempfile.mkdtemp(prefix="capgate-"), "r.json")

    proc = subprocess.run(
        [sys.executable, SMOKE, a.image, "--json", tmp],
        capture_output=True, text=True, timeout=a.timeout)
    sys.stdout.write(proc.stdout)
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)

    if proc.returncode == RC_NOIMAGE or not os.path.exists(tmp):
        print(f"\ncapability-gate: FAIL -- {a.image} could not be probed at all; "
              f"nothing was measured, so nothing is proven", file=sys.stderr)
        return RC_NOIMAGE

    with open(tmp, encoding="utf-8") as fh:
        rows = json.load(fh)
    seen = {r["capability"]: r["verdict"] for r in rows}

    unwaived_bad, stale, unknown = [], [], []
    for r in rows:
        cap, verdict = r["capability"], r["verdict"]
        if verdict == "WORKS":
            if cap in waived:
                stale.append(cap)
        elif cap not in waived:
            # BROKEN and INCONCLUSIVE both land here on purpose -- see decision 1.
            unwaived_bad.append(f"{cap} [{verdict}] {r.get('reason') or ''}"[:160])
    unknown = [w for w in waived if w not in seen]

    print(f"\ncapability-gate on {a.image}")
    print(f"  probes            : {len(rows)}")
    print(f"  WORKS             : {sum(1 for v in seen.values() if v == 'WORKS')}")
    print(f"  waived (still red): {sum(1 for c, v in seen.items() if c in waived and v != 'WORKS')}")
    for cap in sorted(c for c, v in seen.items() if c in waived and v != "WORKS"):
        f = waived[cap]
        print(f"      - {cap}  [{f.get('decision') or 'NO DECISION'}"
              f" / {f.get('by') or '?'} / {f.get('on') or '?'}]  "
              f"{(f.get('why') or '(no measured reason)')[:120]}")

    ok = True
    if unwaived_bad:
        ok = False
        print("\n  BLOCKING -- not working and not waived:")
        for line in unwaived_bad:
            print(f"      {line}")
    if stale:
        ok = False
        print("\n  BLOCKING -- STALE WAIVER (these WORK now; delete the line):")
        for cap in stale:
            print(f"      {cap}")
    if unknown:
        ok = False
        print("\n  BLOCKING -- waiver names no probe knows (typo?):")
        for cap in unknown:
            print(f"      {cap}")
    if waiver_errors:
        # A waiver without a recorded decision is not a formatting complaint --
        # it is a breakage nobody has agreed to ship.  Same verdict as an
        # unwaived one, which is the whole point of vibeic-eda#90.
        ok = False
        print("\n  BLOCKING -- waiver entries with no usable recorded decision:")
        for err in waiver_errors:
            print(f"      {err}")
    for dec, why in sorted(DECISION_UNSETTLED.items()):
        caps = [c for c, d in unsettled if d == dec]
        if caps:
            # Each unsettled kind gets its OWN heading.  Folding "nobody ruled"
            # into "we don't know why" would hide the difference between a
            # decision that is waiting and a measurement that was never made --
            # and that difference is the one thing this record is for.
            ok = False
            print(f"\n  BLOCKING -- decision {dec} ({why}):")
            for cap in caps:
                print(f"      {cap}  # {waived[cap].get('recommend') or ''}"[:200])
    if debt:
        # NOT a failure: BUILD is a real ruling.  It is printed on every release
        # so that "we said we would build it" cannot become invisible, and it is
        # dated so its age is readable.  It is deliberately not aged out.
        print("\n  outstanding BUILD debt (decided, not built):")
        for cap, on in debt:
            print(f"      {cap}  recorded {on}")

    print(f"\ncapability-gate: {'PASS' if ok else 'FAIL'}")
    return RC_OK if ok else RC_FAIL


if __name__ == "__main__":
    sys.exit(main())
