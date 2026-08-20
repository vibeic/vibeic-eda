#!/usr/bin/env python3
"""Nothing in this repo opens a PR on vibe-ic.

Three paths did. All three are gone, and the reason is the same each time: the
PR was a document, and an open PR made `fork_github_convergence` non-zero, which
skipped the NEXT round's certified publish. The round blocked itself by
finishing its own work.

  * the image anchor      — asserted a verification that never ran (see
                            `test_anchor_pr_follows_the_publish`)
  * the release assessment— written to tools/vibeic-eda/upstream-assessments/
  * the daily sync report — written to tools/vibeic-eda/EDA_FORK_SYNC_LOG.md
  * the publish-silence alarm — briefly filed here, now files on THIS repo

HOW TWO OF THEM CAME BACK is the part worth remembering. The assessment and
sync paths had been failing silently for weeks (`skip: branch push failed`):
vibe-ic's push-gate context was prepared for one PR path and not the other two.
Repairing that hole turned them back on, and three rounds of working
notifications proved only that nothing consumes them. A mechanism worth
repairing is one something reads.

The alarm still exists and still opens a PR — on `vibeic/vibeic-eda`. That is
both the right owner (a stalled fork page is this pipeline's failure) and the
only workable one: `fork_github_convergence`'s denominator is the registered
forks plus `vibeic/vibe-ic`, so an alarm filed there would make the next round's
publish non-zero. The alarm about a stalled page would stall the page.
`test_publish_silence_is_audible` asserts its target.
"""
from __future__ import annotations

import ast
import pathlib
import re

_HERE = pathlib.Path(__file__).resolve().parent
#: Nothing may reach a PR-opening function in `pr_notify`. Not a shrunken
#: list — an empty one, so adding a caller is a deliberate edit to this rule
#: with a reason attached, not a quiet append.
_ALLOWED: set[str] = set()
_OPENERS = ("open_pr", "open_assessment_pr", "open_alert_pr")


def _callers_of_pr_openers() -> dict[str, list[str]]:
    """Every module that CALLS one of the openers, by AST — not by grep, which
    counts the dozen comments that merely name them."""
    found: dict[str, list[str]] = {}
    for path in sorted(_HERE.glob("*.py")):
        if path.name in ("pr_notify.py",) or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else None)
            if name in _OPENERS:
                hits.append(f"{name}:{node.lineno}")
        if hits:
            found[path.name] = hits
    return found


def test_only_the_publish_silence_alarm_opens_a_pr_on_vibe_ic():
    callers = _callers_of_pr_openers()
    unexpected = {k: v for k, v in callers.items() if k not in _ALLOWED}
    assert not unexpected, (
        "these open a PR on vibe-ic and should not: "
        f"{unexpected}. A round's own output belongs in a file it writes, not "
        "in a PR that then blocks the next round's publish. If this is a new "
        "ALARM rather than a document, add it to _ALLOWED with the reason.")


def test_the_pr_openers_have_no_production_caller_left():
    """`open_pr` and `open_assessment_pr` still exist in `pr_notify` — deleting
    them is a separate change — but nothing calls them. This is the assertion
    that catches a future edit re-wiring one 'just for this case'."""
    callers = _callers_of_pr_openers()
    assert callers == {}, f"something opens a PR on vibe-ic again: {callers}"


def test_the_two_retired_paths_say_what_replaced_them():
    """A caller that stopped notifying must still record; a step that goes
    quiet without saying where its output went is indistinguishable from one
    that stopped working."""
    src = (_HERE / "gatekeeper.py").read_text(encoding="utf-8")
    for marker in ("upstream-assessments/", "EDA_FORK_SYNC_LOG.md"):
        assert marker in src, (
            f"gatekeeper no longer opens the PR but does not point at {marker}")
