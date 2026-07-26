#!/usr/bin/env python3
"""_nda_tokens.py — the ONE encoded NDA token store for this repo.

WHY THIS FILE EXISTS. `build_page.py` carried these patterns as plain
LITERALS in a file that is tracked and pushed to a PUBLIC repository, and it
covered only 5 of the 8 token roles the vibe-ic canonical store knows — a
second foundry brand, an IP vendor and an IP part number were not redacted
at all. Both were fixed in place first; this module then makes the list a
SINGLE source inside this repo, so the redactor and the pre-push guard can
never drift apart the way the redactor drifted from the canonical store.

Patterns are stored base64-encoded and decoded at run time: a redactor that
spells out what it redacts publishes it.

STILL OPEN (vibe-ic#395): this is a COPY of vibe-ic's
`programs/_commercial_pdk.py` roles. A copy that must be remembered drifts —
it already did once. The real fix is one module shared across both repos.
"""
from __future__ import annotations

import base64
import re
from typing import List, Tuple

_ENCODED: List[Tuple[str, str]] = [
    ("cmVhbC1IUDE4RTgwLWRlY2s=", "real-commercial-PDK-deck"),
    ("cmVhbC1IUDE4RTgw", "real-commercial-PDK"),
    ("SFAxOEU4MA==", "a commercial 180nm NDA PDK"),
    ("S2V5ID9Gb3VuZHJ5", "a commercial foundry"),
    ("XGJtMThlODBcdyo=", "commercial-180nm-pdk"),
    ("bWFnbmFjaGlw", "a commercial foundry"),
    ("ZU1lbW9yeQ==", "a commercial IP vendor"),
    ("RU8wMTI4WDhLQTE4MEJBMTE=", "a commercial IP part number"),
]


def subs() -> List[Tuple["re.Pattern[str]", str]]:
    """(compiled pattern, generic replacement) pairs, in order. Compound
    tokens come before bare names — replacing the bare name first would leave
    the compound half-redacted."""
    return [(re.compile(base64.b64decode(a).decode("utf-8"), re.I), b)
            for a, b in _ENCODED]


def redact(s: str) -> str:
    for pat, rep in subs():
        s = pat.sub(rep, s)
    return s


# Word-boundary anchors are correct for PROSE and WRONG for the carrier this
# guard exists for. Measured: a pattern beginning `\b` matches the token in
# `result for <tok>` and MISSES it in `runs/clean_<tok>/R.md`, because `_` and
# the token's first character are both word characters, so no boundary exists
# between them. Directory names are exactly where this project's leaks have
# landed. For SCANNING, the anchors are stripped; `redact()` keeps them
# untouched so rendered output is byte-identical to before.
_BOUNDARY = re.compile(r"\\b")


def find(text: str) -> List[int]:
    """Indices of every token present in `text`. Returns INDICES, never the
    literals — a finding that prints the token defeats the point (the same
    masked-reporting rule vibe-ic's checkers follow).

    Scans a SEPARATOR-NORMALISED copy as well, so a token split by a path
    separator or embedded in `<word>_<token>` is still found."""
    hay = (text or "")
    loose = re.sub(r"[/_.\-]+", " ", hay)
    out = []
    for i, (pat, _) in enumerate(subs()):
        stripped = re.compile(_BOUNDARY.sub("", pat.pattern), re.I)
        if pat.search(hay) or stripped.search(hay) or stripped.search(loose):
            out.append(i)
    return out
