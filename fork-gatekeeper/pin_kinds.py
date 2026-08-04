#!/usr/bin/env python3
"""What a hex `ARG` in a Dockerfile MEANS — a build input, or a claim about one.

WHY THIS EXISTS (vibeic-eda#79)
===============================
`OPEN_PDKS_REF` was proposed for advancement twice (#74, #78) and refused twice.
Both proposals cited `fork_gap_report`'s "18 commits image-behind-upstream" and
both were wrong for the same reason: **that ARG is not a build input.** Nothing
clones open_pdks at it. The PDK arrives as a PREBUILT ciel volume — measured on
the published 0.2.63 image:

    /foss/pdks/sky130A -> ciel/sky130/versions/b344c97e…/sky130A
    /foss/pdks/sky130A/SOURCES: `open_pdks b344c97e…`

so the ARG RECORDS which upstream commit the volume already carries. Advancing it
rebuilds nothing, changes no installed byte, and turns a true statement into a
false one — which is exactly what the build guard caught, twice.

The refusals were correct and they were also unrepeatable: every sweep reads the
ARG list and asks "is this ref behind upstream?". For 28 of 29 ARGs that is the
right question. For this one it is a category error, and NOTHING IN THE NAME
DISTINGUISHED IT. A comment does not help — a sweep is mechanical and does not
read comments. So the distinction is moved into the one thing every sweep already
parses: the ARG NAME.

THE CONVENTION
==============
    ARG <TOOL>_REF=<40 hex>                    PIN — a build input. The build
                                               clones/checks out at it. "Is it
                                               behind upstream?" is the right
                                               question; sweeps must ask it.

    ARG <TOOL>_VOLUME_CONTENTS_SHA=<40 hex>    CONTENTS ASSERTION — a statement
                                               about a PREBUILT artefact the
                                               build does not fetch. The build
                                               ASSERTS it and refuses to ship if
                                               the artefact disagrees. "Is it
                                               behind upstream?" is a category
                                               error; sweeps must not ask it.

A NAME, NOT A LIST. An exclusion list is a rule someone has to remember to
update; the next prebuilt artefact wired in would be named `*_REF` out of habit
and swept on its first morning. A suffix is applied by the regex every sweep
already runs: `check_pins_current`, `daily_release.ref_arg_names`,
`inbound_survey` and `fork_gap_report.ARG_RE` all key on `_REF`, so a name off
that suffix is skipped by four programs with no code in them at all.

AND THE NAME IS CORROBORATED, NOT TRUSTED
=========================================
A convention that is only a convention fails in the other direction: someone
names a REAL pin `*_VOLUME_CONTENTS_SHA` and it silently stops being swept — a
pin nobody checks, which is the vibeic-eda#60 defect wearing the opposite mask.
So the file's own text decides:

  * referenced in a `git clone` / `git checkout` / `--branch` step  -> MISNAMED
    PIN. Classified `pin` anyway and flagged, so it fails loudly instead of
    disappearing.
  * never referenced after its declaration                          -> UNENFORCED.
    Not honoured at all. "A bare ARG nobody checks is a comment with a colour"
    is already this repo's rule for ARG-only pins (`discover_forks`); an
    assertion nobody asserts earns no exemption either.
  * referenced, and by nothing that fetches                          -> the
    assertion it claims to be.

ONE AUTHORITY, IMPORTED. `check_pins_current` records why: "two programs carrying
two copies of this answer is exactly how they came to say opposite things about
the same four pins (#29)". `discover_forks` and `fork_gap_report` both need this
answer and neither may re-derive it.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

#: The ARG name suffix that declares a build input.
PIN_SUFFIX = "_REF"
#: The ARG name suffix that declares a claim about a prebuilt artefact.
ASSERTION_SUFFIX = "_VOLUME_CONTENTS_SHA"

KIND_PIN = "pin"
KIND_CONTENTS_ASSERTION = "contents_assertion"
#: Declared with an assertion name and asserted by nothing. Not honoured.
KIND_UNENFORCED = "unenforced"

#: `ARG <STEM>_VOLUME_CONTENTS_SHA=<40 hex>`, optionally with a trailing comment.
#: A 40-hex is required because a 40-hex is a COMMIT: a branch name tracks
#: whatever that branch becomes, which is the opposite of a statement about a
#: fixed artefact. The trailing comment IS allowed here, unlike in the pin form
#: below — every real pin in the composing Dockerfile carries one, so accepting
#: it there would widen `discover_forks`' ARG-only fallback beyond what it
#: matched before, and this change may not move that boundary.
ASSERTION_ARG_RE = re.compile(
    r"^ARG\s+(\w+)" + ASSERTION_SUFFIX + r"\s*=\s*([0-9a-f]{40})\s*(?:#.*)?$",
    re.M)

#: A step that FETCHES at a ref. If an assertion-named ARG appears in one of
#: these, the name is wrong and the ARG is a pin.
_FETCHES = re.compile(r"git\s+(?:clone|checkout|fetch|reset)\b|--branch\b")


def is_pin_arg(name: str) -> bool:
    """`YOSYS_REF` -> True. The suffix every existing sweep keys on."""
    return name.endswith(PIN_SUFFIX)


def is_assertion_arg(name: str) -> bool:
    """`OPEN_PDKS_VOLUME_CONTENTS_SHA` -> True."""
    return name.endswith(ASSERTION_SUFFIX)


def _instructions(text: str) -> List[str]:
    """Dockerfile instructions with `\\`-continuations joined.

    The instruction is the unit that decides whether an ARG is FETCHED at: a
    `git clone` and the `git checkout ${X}` that follows it are steps of ONE
    `RUN`, and a line-at-a-time scan would see the checkout without the clone —
    or, worse, see neither because the flag wrapped.
    """
    out: List[str] = []
    buf: List[str] = []
    for line in text.splitlines(keepends=True):
        buf.append(line)
        if not line.rstrip("\n").rstrip().endswith("\\"):
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return out


def classify(text: str) -> Dict[str, Dict[str, object]]:
    """The hex `ARG`s this Dockerfile declares -> what each one MEANS.

    {name: {"stem", "sha", "kind", "why", "misnamed"}}. `stem` is the ARG name
    with its suffix removed — the key the sweeps match a tool against.

    SCOPE, stated because it is narrower than "every ARG" and deliberately so.
    An `ARG X_REF=main` is not classified: a branch is not a commit, and this
    module answers about commits. Nor is a `_REF` line with anything after the
    sha — that is the exact rule `discover_forks`' ARG-only fallback has always
    applied, and every real pin in the composing Dockerfile carries a trailing
    comment, so widening it here would silently enlarge that fallback's input.
    The consumer that needs ALL pins (`fork_gap_report.pins_from_dockerfiles`,
    `check_pins_current`) has its own looser reader and keeps it.
    """
    out: Dict[str, Dict[str, object]] = {}
    instrs = _instructions(text)

    # `\w+` rather than `[A-Z0-9_]+` deliberately: `discover_forks`' ARG-only
    # loop, whose behaviour this replaces, matched `\w+`, and narrowing the class
    # here would silently drop an ARG it used to see.
    for stem, sha in re.findall(r"^ARG\s+(\w+)" + PIN_SUFFIX
                                + r"\s*=\s*([0-9a-f]{40})\s*$", text, re.M):
        name = stem + PIN_SUFFIX
        out[name] = {"stem": stem, "sha": sha, "kind": KIND_PIN,
                     "why": "named `%s`, so it is a build input" % PIN_SUFFIX,
                     "misnamed": False}

    for stem, sha in ASSERTION_ARG_RE.findall(text):
        name = stem + ASSERTION_SUFFIX
        ref = "${%s}" % name
        # The declaration itself must not count as a use — otherwise every ARG
        # asserts itself. Look only after the line that declares it, exactly as
        # `discover_forks` does for ARG-only pins.
        tail = text.split("ARG " + name, 1)[-1]
        used = ref in tail
        fetched = any(ref in s and _FETCHES.search(s) for s in instrs)
        if fetched:
            out[name] = {
                "stem": stem, "sha": sha, "kind": KIND_PIN, "misnamed": True,
                "why": ("named `%s` but a fetch step reads it — this is a PIN "
                        "with an assertion's name. Classified as a pin so it "
                        "keeps being swept; rename it to `%s%s`."
                        % (ASSERTION_SUFFIX, stem, PIN_SUFFIX))}
            continue
        if not used:
            out[name] = {
                "stem": stem, "sha": sha, "kind": KIND_UNENFORCED,
                "misnamed": False,
                "why": ("declared and asserted by nothing — a bare ARG nobody "
                        "checks is a comment with a colour, so it buys no "
                        "exemption from the sweep")}
            continue
        out[name] = {
            "stem": stem, "sha": sha, "kind": KIND_CONTENTS_ASSERTION,
            "misnamed": False,
            "why": ("records what a PREBUILT artefact carries and the build "
                    "ASSERTS it — nothing fetches at it, so advancing it "
                    "rebuilds nothing and makes a true statement false")}
    return out


def contents_assertions(text: str) -> Dict[str, str]:
    """{stem: sha} for the assertions this file both declares AND enforces.

    Stem, not full ARG name, because that is what a sweep has to match a tool
    against: `OPEN_PDKS` for the fork `open_pdks`.
    """
    return {str(v["stem"]): str(v["sha"]) for v in classify(text).values()
            if v["kind"] == KIND_CONTENTS_ASSERTION}


def misnamed_pins(text: str) -> List[Tuple[str, str]]:
    """[(arg name, why)] for assertion-named ARGs a fetch step reads.

    Returned so callers can REPORT them. Silently reclassifying is right; doing
    it silently is not — the name and the build would stay in disagreement.
    """
    return [(k, str(v["why"])) for k, v in classify(text).items()
            if v.get("misnamed")]
