#!/usr/bin/env python3
"""pr_precheck.py — reviewer-side redundancy gate for a fork PR, BEFORE landing.

Motivation (2026-07-21): in one gating batch, 2 of 5 fork PRs were REDUNDANT —
they were authored against an OLDER base while the fork's own working line had
ALREADY landed the same fix under a different commit. `mergeable=CLEAN` says
nothing about this: a duplicate fix in a different file/function merges cleanly
and still adds dead-weight (and, in the iverilog case, a second parse-tree-
mutating mechanism for a bug the base already fixed). The tell was always the
same and is cheap to compute:

  1. the PR is BEHIND its base (base advanced since the PR branched), and
  2. a base-only commit already Closes/Fixes the very issue the PR claims.

So before you land ANY fork PR, run this. It does NOT touch git; it only reads
GitHub via `gh api`, so it works without local clones and never mutates state.

    python3 pr_precheck.py vibeic/iverilog 1
    python3 pr_precheck.py --repo vibeic/yosys --pr 1

Verdict (also the exit code):
  OK             (0)  base has not advanced past the PR and closes no shared issue
  REVIEW         (1)  base is AHEAD of the PR (behind_by>0) — inspect for a dup fix
  REDUNDANT_RISK (2)  a base-only commit already Closes/Fixes an issue this PR claims
                      — the code change is very likely superseded; land test-only or reject

This is a SIGNAL, not a verdict machine: REDUNDANT_RISK means "prove the base
already fixes it (build the base and run the PR's own test) before landing",
which is exactly what turned both 2026-07-21 duplicates from CLEAN into REJECT.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from urllib.parse import quote

# A closing reference: "Fixes vibeic/vibe-ic#125", "Closes vibe-ic#124",
# "Resolves #77", "fixes #12" — a close/fix/resolve keyword then an issue token.
_ISSUE_RE = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[^\n#]*?"
    r"((?:[\w.-]+/)?(?:[\w.-]+)?#\d+)",
    re.IGNORECASE,
)


def _gh_json(path: str):
    """GET a gh api path, return parsed JSON (or None on any failure)."""
    try:
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def _issue_number(token: str) -> str:
    """Normalise an issue token to its bare number so refs like
    'vibeic/vibe-ic#125' and 'vibe-ic#125' and '#125' compare equal."""
    m = re.search(r"#(\d+)", token)
    return m.group(1) if m else token


def precheck(repo: str, pr: int) -> dict:
    pr_data = _gh_json(f"repos/{repo}/pulls/{pr}")
    if not pr_data:
        return {"verdict": "ERROR", "detail": f"cannot read {repo}#{pr} via gh api"}

    base = pr_data["base"]["ref"]
    head = pr_data["head"]["ref"]
    body = pr_data.get("body") or ""
    state = pr_data.get("state")

    # issues this PR claims to close (dedup by bare number)
    pr_issues = {_issue_number(t) for t in _ISSUE_RE.findall(body)}

    # base-only commits = commits in base that head does NOT have (head...base).
    # These are the superseding-fix candidates the PR was authored without.
    cmp_data = _gh_json(f"repos/{repo}/compare/{quote(head, safe='')}...{quote(base, safe='')}")
    behind_by = (cmp_data or {}).get("ahead_by", 0)     # base ahead of head == head behind base
    base_only = (cmp_data or {}).get("commits", []) or []

    # does a base-only commit already Close/Fix an issue this PR claims? Parse the
    # base messages with the SAME closing-keyword-anchored regex used for the PR body,
    # so a bare "see #124" mention never trips it — only an actual close/fix/resolve.
    redundant = []
    for c in base_only:
        msg = c.get("commit", {}).get("message", "")
        closed = {_issue_number(t) for t in _ISSUE_RE.findall(msg)}
        shared = pr_issues & closed
        if shared:
            redundant.append({"sha": c["sha"][:10],
                              "issues": sorted(shared),
                              "headline": msg.splitlines()[0][:80]})

    if redundant:
        verdict, code = "REDUNDANT_RISK", 2
    elif behind_by > 0:
        verdict, code = "REVIEW", 1
    else:
        verdict, code = "OK", 0

    return {"verdict": verdict, "code": code, "repo": repo, "pr": pr, "state": state,
            "base": base, "head": head, "behind_by": behind_by,
            "pr_closes_issues": sorted(pr_issues), "base_already_closes": redundant}


def _print(rep: dict) -> None:
    print(f"{rep['repo']}#{rep['pr']}  [{rep.get('state','?')}]  base={rep.get('base')} "
          f"head={rep.get('head')}")
    if rep["verdict"] == "ERROR":
        print(f"  ERROR: {rep['detail']}")
        return
    print(f"  behind_by={rep['behind_by']}  PR-closes={rep['pr_closes_issues'] or '—'}")
    for r in rep["base_already_closes"]:
        print(f"  ⚠ base commit {r['sha']} already closes {r['issues']}: {r['headline']}")
    print(f"  VERDICT: {rep['verdict']}")
    if rep["verdict"] == "REDUNDANT_RISK":
        print("  → the base already fixes this. PROVE it (build base + run the PR's own "
              "test) before landing; prefer test-only graft or reject the code change.")
    elif rep["verdict"] == "REVIEW":
        print("  → base advanced since the PR branched; re-test on the REBASED tree and "
              "check for a duplicate fix before landing (mergeable=CLEAN is not enough).")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reviewer-side redundancy precheck for a fork PR.")
    ap.add_argument("repo", nargs="?", help="owner/repo, e.g. vibeic/iverilog")
    ap.add_argument("pr", nargs="?", type=int, help="PR number")
    ap.add_argument("--repo", dest="repo_opt", help="owner/repo (alternative to positional)")
    ap.add_argument("--pr", dest="pr_opt", type=int, help="PR number (alternative to positional)")
    ap.add_argument("--json", action="store_true", help="emit the raw report as JSON")
    a = ap.parse_args(argv)
    repo, pr = a.repo or a.repo_opt, a.pr or a.pr_opt
    if not repo or not pr:
        ap.error("need a repo and a PR number (positional or --repo/--pr)")
    rep = precheck(repo, pr)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print(rep)
    return rep.get("code", 1)


if __name__ == "__main__":
    sys.exit(main())
