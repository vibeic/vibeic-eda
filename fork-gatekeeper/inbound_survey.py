#!/usr/bin/env python3
"""What has upstream FIXED that our pinned fork still ships?

WHY THIS EXISTS (vibe-ic#553)
=============================
The gatekeeper's daily tick asks "should we adopt this upstream commit?" — a
selective-adoption question, answered per commit by `assess_release.py`. It has
never asked the other direction: **is upstream carrying a fix for a bug we still
ship?**

vibe-ic#551 is what surfaced the gap. Chasing an `rsz::stitchTrees` segfault to
file it upstream, we found upstream had closed it on 2026-07-13 (`5b9e0a371`,
three lines) and our pin predates that by 772 commits. We had not hit it only
because a *different* crash in our build wins the race on the same code path.
That nearly cost a day minimising a reproducer for a bug fixed in July.

WHAT THIS IS NOT
================
Not auto-adoption, and not a rebase. Being behind is deliberate — a fork that
chases master is unbuildable, and rebasing 772 commits under 51 of our own is a
large risky job whose payoff is unmeasured. This produces a LIST, so a crash we
hit can be checked against upstream's history before anyone spends a day on it.

HONEST SCOPE, because the output invites over-reading
=====================================================
* **The subject-line match is a proxy, not a classification.** It calls a
  test-only change a fix and misses one titled "handle empty input". The number
  is an order of magnitude.
* **GitHub's compare API caps a response at 250 commits.** A fork 772 behind
  reports 250 and says so. Reporting a sample as a population is the exact
  failure that produced a wrong fork count in vibeic-eda#15; every count here
  carries `truncated` when it hits the cap.
* **`paths` needs the per-commit endpoint** and is fetched only for ranked
  candidates, because 250 extra API calls per tool per tick is not free.

Exit: 0 survey completed, 1 a fork could not be surveyed, 2 nothing surveyed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

RC_OK, RC_PARTIAL, RC_NOTHING = 0, 1, 2

#: The row state for a fork this survey COULD NOT ASK ABOUT (vibeic-eda#101).
#:
#: The condition was already detected — "compare failed in both cross-repo and
#: upstream-internal scope" — and then carried only as a free-text `error`, which
#: the headline counted into the surveyed population anyway. Measured on the
#: 2026-08-06 tick: three forks (Fault, FasterCap, Trilinos) are mirrors with no
#: GitHub parent whose upstream also lacks our pin, so both compares 404 — and the
#: headline read "24 fork(s), 12 upstream commit(s) our pins lack" when 21 were
#: surveyed. Reporting a sample as a population is the exact failure this file's
#: own docstring says it was written to prevent.
UNMEASURABLE = "UNMEASURABLE"
SURVEYED = "SURVEYED"

#: GitHub returns at most this many commits from the compare endpoint. Named
#: rather than inline so the truncation disclosure below cannot drift from it.
COMPARE_CAP = 250

#: Subject-line signals for "this is a defect fix". Deliberately broad — a false
#: positive costs a reader one line, a false negative costs what #551 cost.
_FIX_RE = re.compile(
    r"\b(fix(e[sd])?|crash|segfault|sigsegv|sigill|abort|assert|null|"
    r"leak|regression|bug|hang|deadlock|overflow|uninitiali[sz]ed)\b", re.I)

#: Subtrees whose defects can reach our flow. A fix in `src/gui` matters less to
#: a headless sign-off run than one in the router or the resizer. This is a
#: RANKING input, never a filter: an unranked commit is still listed.
RELEVANT_PATHS: Dict[str, tuple] = {
    "OpenROAD": ("src/drt/", "src/rsz/", "src/grt/", "src/dpl/", "src/ant/",
                 "src/psm/", "src/cts/", "src/sta/", "src/odb/"),
    "yosys": ("frontends/", "passes/", "backends/", "kernel/"),
    "klayout": ("src/db/", "src/lay/", "src/plugins/"),
    "iverilog": ("", ),          # small enough that everything is relevant
    "verilator": ("src/", ),
    "ngspice": ("src/", ),
    "magic": ("", ),
    "netgen": ("", ),
}


def _sh(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception:
        return 1, ""


def _gh_json(path: str) -> Optional[dict]:
    rc, out = _sh(["gh", "api", path])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def pinned_refs(eda_root: Path) -> Dict[str, str]:
    """fork repo name -> 40-char SHA, read from the Dockerfiles that build it.

    Pairs each `ARG <TOOL>_REF=<sha>` with the `github.com/vibeic/<repo>` clone
    that consumes it, by NAME rather than by position: `tools/lvs/Dockerfile`
    builds two forks (magic + netgen) from one file, so a first-match parse
    silently drops the second.

    Two things my first version got wrong, both of which made the survey report
    a smaller gap than exists — the failure this program is written to prevent,
    one level up:
      * an end-of-line anchor after the SHA: every pin here carries a trailing
        `# pinned; ...` comment,
        so four of six tools matched nothing;
      * one ref per file: `lvs` has two.
    """
    pins: Dict[str, str] = {}
    for df in sorted((eda_root / "tools").glob("*/Dockerfile")):
        text = df.read_text(errors="replace")
        refs = dict(re.findall(
            r"^ARG\s+([A-Z0-9_]+)_REF=([0-9a-f]{40})", text, re.M))
        repos = re.findall(
            r"github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?[\s\"'\\]", text)
        for repo in dict.fromkeys(repos):          # order-preserving unique
            key = repo.upper().replace("-", "_")
            sha = refs.get(key)
            if sha is None:                        # e.g. repo `OpenROAD` vs ARG `OPENROAD`
                for k, v in refs.items():
                    if k.replace("_", "") == key.replace("_", ""):
                        sha = v
                        break
            if sha:
                pins[repo] = sha
    return pins


def declared_upstreams(eda_root: Path) -> Dict[str, str]:
    """tool -> upstream slug, as recorded in FORKS.json.

    GitHub only reports a `parent` for a repository created THROUGH the fork
    button; one populated by pushing a mirror has `fork=false` and no parent, and
    is indistinguishable here from a repo whose upstream nobody knows.  Both
    SAT solvers are in that state, so both had been reported unsurveyable since
    the survey was written — while FORKS.json recorded their upstreams the whole
    time, one file away.

    An empty mapping is returned only when the file is missing or unreadable; it
    is never used to mean "no upstream exists".
    """
    path = eda_root / "fork-gatekeeper" / "FORKS.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, str] = {}
    for entry in doc.get("forks", []):
        tool, upstream = entry.get("tool"), entry.get("upstream")
        if tool and upstream:
            out[tool] = upstream
    return out


def survey_one(repo: str, ref: str, declared: Optional[Dict[str, str]] = None) -> dict:
    """Upstream commits reachable from their default branch but not from `ref`."""
    meta = _gh_json(f"repos/vibeic/{repo}")
    # Measured: for a non-fork this endpoint OMITS `parent` entirely, so a plain
    # `.get("parent", {})` would do. `or {}` is kept because it also survives an
    # explicit `"parent": null`, and the two are indistinguishable to a reader
    # who only sees the field missing from the output.
    gh_parent = (meta or {}).get("parent") or {}
    parent = gh_parent.get("full_name")
    branch = gh_parent.get("default_branch")
    source = "github-parent"
    if not parent:
        parent = (declared or {}).get(repo)
        source = "forks-json"
    if not parent:
        return {"repo": repo, "state": UNMEASURABLE,
                "error": "no upstream recorded by GitHub or FORKS.json"}
    if not branch:
        # Resolved from the upstream itself rather than assumed: guessing
        # "master" on a repo that renamed to "main" turns a real survey into a
        # compare against a branch that does not exist, which reports as an
        # error rather than as the zero it would look like.
        branch = (_gh_json(f"repos/{parent}") or {}).get("default_branch", "master")
    owner = parent.split("/")[0]

    cmp_doc = _gh_json(f"repos/vibeic/{repo}/compare/{ref}...{owner}:{branch}")
    scope = "cross-repo"
    if cmp_doc is None:
        # A cross-repo compare 404s unless GitHub links the two as fork+parent,
        # which it only does for a repo created through the fork button. Our
        # mirrors were populated by pushing, so the endpoint refuses even though
        # the histories are shared — upstream resolves our cadical pin
        # c60730422 (2026-07-19) perfectly well.
        #
        # When upstream contains our pin, the same question can be asked inside
        # the upstream repository alone, which needs no relationship at all.
        # The narrower scope is recorded rather than hidden: this comparison
        # cannot see commits of OURS, so on a mirror it is exact and on a fork
        # carrying local work it would understate. `local_commits_invisible`
        # says so in the output instead of leaving the caller to assume.
        cmp_doc = _gh_json(f"repos/{parent}/compare/{ref}...{branch}")
        scope = "upstream-internal"
    if cmp_doc is None:
        # NAMED, not merely errored (vibeic-eda#101). The distinction the caller
        # has to make is "this fork was surveyed and the answer is N" versus "this
        # fork was not surveyed"; an `error` key alone made that a matter of which
        # fields the reader happened to check, and the headline did not check.
        return {"repo": repo, "upstream": parent, "upstream_source": source,
                "state": UNMEASURABLE,
                "error": "compare failed in both cross-repo and upstream-internal scope"}

    behind = cmp_doc.get("total_commits", 0)
    commits = cmp_doc.get("commits", []) or []
    truncated = len(commits) >= COMPARE_CAP or behind > len(commits)

    rel = RELEVANT_PATHS.get(repo, ("", ))
    fixes: List[dict] = []
    for c in commits:
        subject = (c.get("commit", {}).get("message") or "").split("\n")[0]
        if not _FIX_RE.search(subject):
            continue
        fixes.append({"sha": c.get("sha", "")[:9], "subject": subject[:120],
                      "date": (c.get("commit", {}).get("author", {})
                               .get("date", ""))[:10]})
    return {
        "repo": repo, "upstream": parent, "branch": branch, "pin": ref[:9],
        "state": SURVEYED,
        # Where the upstream came from. A slug GitHub vouches for and one we
        # asserted in FORKS.json carry different weight, and a reader that
        # cannot tell them apart is trusting our own claim as verification.
        "upstream_source": source,
        "compare_scope": scope,
        # True when the answer came from inside the upstream repo, which by
        # construction cannot observe commits that exist only in ours.
        "local_commits_invisible": scope == "upstream-internal",
        "behind": behind,
        "sampled": len(commits),
        # Named, not implied: a caller that reads `fix_candidates` without this
        # is reading a sample as a population.
        "truncated": truncated,
        "truncation_note": (
            f"upstream is {behind} ahead but GitHub's compare endpoint returned "
            f"{len(commits)}; counts below are over that sample only"
            if truncated else ""),
        "fix_candidates": len(fixes),
        "relevant_subtrees": list(rel),
        "commits": fixes,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", default=None)
    ap.add_argument("--top", type=int, default=8,
                    help="commits to print per fork (all go to --json)")
    a = ap.parse_args(argv)

    pins = pinned_refs(Path(a.eda_root))
    if not pins:
        print("[NOT CHECKED] no pinned refs found under tools/*/Dockerfile — "
              "nothing was surveyed, which is not a clean result",
              file=sys.stderr)
        return RC_NOTHING

    declared = declared_upstreams(Path(a.eda_root))
    results = [survey_one(repo, ref, declared) for repo, ref in sorted(pins.items())]
    # UNMEASURABLE is a STATE, not the absence of one. `errored` is kept as the
    # exit-code input it has always been; what changed (vibeic-eda#101) is that
    # these rows are no longer inside the population the headline describes.
    unmeasurable = [r for r in results if r.get("state") == UNMEASURABLE]
    errored = [r for r in results if r.get("error")]
    surveyed = [r for r in results if r.get("state") == SURVEYED]

    total_behind = sum(r.get("behind", 0) for r in surveyed)
    total_fixes = sum(r.get("fix_candidates", 0) for r in surveyed)
    any_trunc = any(r.get("truncated") for r in surveyed)

    # `len(surveyed)`, never `len(results)`. The old line said "24 fork(s), 12
    # upstream commit(s) our pins lack" on a tick that surveyed 21 and could not
    # ask about 3 — a denominator that includes the rows it has no answer for,
    # which is the same defect as a verdict column with nowhere to put them.
    print(f"inbound_survey: {len(surveyed)} of {len(results)} fork(s) surveyed, "
          f"{total_behind} upstream commit(s) our pins lack, {total_fixes} whose "
          f"subject reads as a defect fix")
    if unmeasurable:
        # COUNTED AND NAMED ON STDOUT, beside the number it is not part of. It was
        # only ever on stderr, so the file a reader opens showed a population of 24
        # with no sign that 3 of them were never asked. Not fatal: the exit code
        # below still says PARTIAL, and the round continues.
        print(f"  UNMEASURABLE: {len(unmeasurable)} fork(s) could not be surveyed "
              f"at all — {', '.join(r['repo'] for r in unmeasurable)}. Their gap is "
              f"NOT zero and is NOT included above")
    if any_trunc:
        print("  NOTE: at least one fork exceeded GitHub's 250-commit compare "
              "cap; its fix count is over a SAMPLE, not the whole gap")
    for r in results:
        if r.get("error"):
            print(f"  {r['repo']}: UNMEASURABLE (ERROR) {r['error']}", file=sys.stderr)
            continue
        flag = "  [SAMPLED]" if r["truncated"] else ""
        print(f"  {r['repo']:<12} pin {r['pin']}  behind {r['behind']:>4}  "
              f"fix-like {r['fix_candidates']:>3} of {r['sampled']}{flag}")
        for c in r["commits"][:a.top]:
            print(f"      {c['sha']}  {c['date']}  {c['subject'][:88]}")

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "inbound_survey", "forks": results,
             # The DENOMINATOR the totals below are over, on the document rather
             # than inferable from it. A consumer that reads `total_behind` beside
             # `len(forks)` computes an average over rows that carry no answer.
             "surveyed": len(surveyed), "unmeasurable": len(unmeasurable),
             "unmeasurable_repos": [r["repo"] for r in unmeasurable],
             "total_behind": total_behind, "total_fix_candidates": total_fixes,
             "any_truncated": any_trunc}, indent=2) + "\n", encoding="utf-8")

    if errored:
        print(f"[PARTIAL] {len(errored)} fork(s) UNMEASURABLE — not surveyed, and "
              f"not counted as zero", file=sys.stderr)
        return RC_PARTIAL
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
