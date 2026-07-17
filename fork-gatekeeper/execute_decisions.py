#!/usr/bin/env python3
"""execute_decisions.py — Phase 3 of the capability-separated selective merge.

The DECIDER (Claude, Phase 2) runs with NO token / NO push / NO network and writes only
`decisions/<date>.json`. This deterministic executor holds the credentials and is the ONLY
thing that can act — and it RE-VALIDATES every decision against the trusted assessment before
doing anything, so a prompt-injected or buggy decider can never make it act outside the set of
commits the deterministic assessment already blessed.

Validation rule for an `adopt` (all must hold, else REJECT → treated as defer):
  * the sha appears in that tool's assessment,
  * assessment says `clean_cherrypick == true`,
  * assessment says `touches_our_patches == false`,
  * the deterministic assessment already classified it `decision == "auto-safe"`.

So the decider's judgment can only NARROW the auto-safe set (choose which safe commits we
actually want); it can never widen it. Output: a validated decision artifact + summary.

ACTING on the validated adopts (cherry-pick → build+smoke → prepare PR / gated ship) is a
further-gated step: it requires GK_SHIP and a least-privilege write token (GK_WRITE_TOKEN),
and is intentionally NOT enabled here yet — v1 validates + records for review only.

    python3 execute_decisions.py <date>            # validate decisions/<date>.json
    python3 execute_decisions.py <date> --json     # print the validated artifact
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ.get("GK_STATE_DIR") or os.path.expanduser("~/.cache/eda-fork-gatekeeper"))
ASSESS_DIR = STATE / "reports" / "assessments"
DECIS_DIR = STATE / "decisions"


def _load_assessment(date: str, tool: str) -> dict | None:
    p = ASSESS_DIR / f"{date}-{tool}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _safe_index(assessment: dict) -> dict:
    """sha -> commit row, restricted to the deterministic auto-safe set (the only shas an
    adopt may reference). Anything not here cannot be auto-adopted, full stop."""
    idx = {}
    safe = set(assessment.get("clearly_safe") or [])
    for c in assessment.get("commits") or []:
        if (c.get("sha") in safe and c.get("decision") == "auto-safe"
                and c.get("clean_cherrypick") is True and c.get("touches_our_patches") is False):
            idx[c["sha"]] = c
    return idx


def validate(date: str) -> dict:
    """Validate decisions/<date>.json against the assessments. Never raises."""
    dp = DECIS_DIR / f"{date}.json"
    if not dp.is_file():
        return {"date": date, "status": "no_decisions", "note": f"no {dp}"}
    try:
        decisions = json.loads(dp.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {"date": date, "error": f"bad decisions file: {e}"}

    out_tools = {}
    for tool, rows in (decisions.get("decisions") or {}).items():
        assessment = _load_assessment(date, tool)
        if not assessment:
            out_tools[tool] = {"error": "no matching assessment — all deferred"}
            continue
        safe = _safe_index(assessment)
        adopt_ok, rejected, skipped, deferred = [], [], [], []
        seen = set()
        for r in rows or []:
            sha, action = r.get("sha"), r.get("action")
            seen.add(sha)
            if action == "adopt":
                if sha in safe:
                    adopt_ok.append({"sha": sha, "reason": r.get("reason", ""),
                                     "title": safe[sha].get("title", "")})
                else:
                    # an adopt the deterministic gate did NOT bless → refuse, downgrade to defer
                    rejected.append({"sha": sha, "reason": r.get("reason", ""),
                                     "why_rejected": "not in the trusted clean-safe set "
                                     "(missing / not clean-pick / conflicts with our patches)"})
            elif action == "skip":
                skipped.append(sha)
            else:
                deferred.append(sha)
        # any clean-safe commit the decider did NOT explicitly adopt is implicitly deferred
        unaddressed = [s for s in safe if s not in seen]
        out_tools[tool] = {"adopt": adopt_ok, "rejected": rejected, "skip": skipped,
                           "defer": deferred, "unaddressed_safe": unaddressed,
                           "safe_available": len(safe)}
    validated = {"date": date, "status": "validated", "tools": out_tools,
                 "totals": {"adopt": sum(len(t.get("adopt", [])) for t in out_tools.values()),
                            "rejected": sum(len(t.get("rejected", [])) for t in out_tools.values())}}
    DECIS_DIR.mkdir(parents=True, exist_ok=True)
    (DECIS_DIR / f"{date}-validated.json").write_text(json.dumps(validated, indent=2, ensure_ascii=False) + "\n")
    return validated


def render_md(v: dict) -> str:
    if v.get("status") == "no_decisions":
        return f"(no decisions to execute for {v['date']})\n"
    if v.get("error"):
        return f"decision validation error: {v['error']}\n"
    L = [f"## Validated merge decisions — {v['date']}",
         f"**adopt (validated): {v['totals']['adopt']}** · rejected (not in trusted safe set): "
         f"{v['totals']['rejected']}", ""]
    for tool, t in v["tools"].items():
        if t.get("error"):
            L.append(f"- **{tool}**: {t['error']}"); continue
        L.append(f"- **{tool}**: adopt {len(t['adopt'])} · skip {len(t['skip'])} · "
                 f"defer {len(t['defer'])} · rejected {len(t['rejected'])} "
                 f"(of {t['safe_available']} clean-safe available)")
        for a in t["adopt"]:
            L.append(f"    - ADOPT `{a['sha']}` {a.get('title','')} — {a['reason']}")
        for rj in t["rejected"]:
            L.append(f"    - ⚠ REJECTED `{rj['sha']}` — {rj['why_rejected']}")
    L += ["", "> Executor re-validated the decider's choices against the deterministic "
          "assessment; an adopt outside the trusted clean-safe set is rejected. Applying the "
          "validated adopts (cherry-pick + build + ship) is a separate gated step."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    v = validate(args[0]) if args else {"error": "usage: execute_decisions.py <date>"}
    print(json.dumps(v, indent=2, ensure_ascii=False) if "--json" in sys.argv else render_md(v))
