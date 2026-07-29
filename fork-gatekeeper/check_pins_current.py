#!/usr/bin/env python3
"""Is each pinned ref the TIP of the fork branch it comes from, or behind it?

WHY THIS EXISTS
===============
`check_pins_agree.py` asserts that a tool's commit is stated identically in all
three places that state it (the tool Dockerfile's `ARG`, `docker-bake.hcl`, and
the root Dockerfile's `IMG_*` tag). That catches DISAGREEMENT. It cannot catch
STALENESS, because three files agreeing on an old commit agree perfectly.

Measured instance, 2026-07-29: `vibeic/yosys#2` was reviewed and merged, and the
image kept shipping the pre-merge yosys for the rest of the day. All three pin
sites agreed. They agreed on the commit from before the merge. The merge was
real, the review was real, and none of it reached a single run — the owner found
it, not a gate:

    "容器映像還沒重建，跑的還是舊 yosys -> WHY YOU DIDNT REBUILD!!!"

That is the shape this checks. A fork commit becomes delivery only when three
things move in order: fork branch -> pin -> rebuilt image. This program checks
the first hop. `check_pins_agree` checks the pin is coherent; nothing before this
checked it was CURRENT.

WHAT EACH VERDICT MEANS
=======================
  CURRENT   the pin is the branch tip — the fork has nothing we are not building
  STALE     the branch has moved past the pin by N commits; those N commits are
            merged, reviewed, and NOT SHIPPING
  ORPHANED  no vibeic branch reaches the pin at all. Worse than stale: an
            unreachable commit is a GC candidate, so the image is pinned to
            something the fork may stop serving.

WHAT IT DOES NOT CHECK, STATED
==============================
That the IMAGE was rebuilt after the pin moved. A current pin with a stale image
is the same failure one hop later, and it is not visible from the API — it needs
the built artefact. `check_pins_agree` plus this plus a rebuild is the chain; two
of three still ships yesterday's tool.

Exit: 0 every pin is its branch tip, 1 one or more is not, 2 nothing compared.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RC_CURRENT, RC_STALE, RC_NOTHING = 0, 1, 2

#: Branch names that are not the build branch even when their tip equals the pin.
#: A dated merge branch and a temporary fix branch both sit at the same commit as
#: the build branch right after a fast-forward, and reporting "the pin is the tip
#: of vibeic/daily-merge-2026-07-29" answers a question nobody asked.
_TRANSIENT = re.compile(r"\d{4}-\d{2}-\d{2}|\b(tmp|temp|wip|test)\b", re.I)


def _sh(cmd, timeout=180):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                                   # noqa: BLE001
        return 1, "", str(exc)


def _gh(path: str, jq: Optional[str] = None, paginate: bool = False):
    cmd = ["gh", "api"] + (["--paginate"] if paginate else []) + [path]
    if jq:
        cmd += ["--jq", jq]
    rc, out, _ = _sh(cmd)
    return out if rc == 0 else ""


def pinned_refs(eda_root: Path) -> Dict[str, str]:
    """fork repo -> pinned SHA, over every Dockerfile that clones a vibeic fork.

    Pairs `ARG <TOOL>_REF=<sha>` with the `github.com/vibeic/<repo>` it feeds, by
    NAME rather than position: `tools/lvs/Dockerfile` builds magic AND netgen
    from one file, so a first-match parse silently drops one of them. My first
    pass at this in shell dropped eight of fourteen tools by name-matching alone,
    which is the under-report this file is written against.
    """
    pins: Dict[str, str] = {}
    files = sorted((eda_root / "tools").glob("*/Dockerfile"))
    if (eda_root / "Dockerfile").is_file():
        files.append(eda_root / "Dockerfile")
    for df in files:
        text = df.read_text(errors="replace")
        refs = dict(re.findall(r"^ARG\s+([A-Z0-9_]+)_REF=([0-9a-f]{40})",
                               text, re.M))
        repos = re.findall(
            r"github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?[\s\"'\\]", text)
        for repo in dict.fromkeys(repos):
            key = repo.upper().replace("-", "_").replace(".", "_")
            sha = refs.get(key)
            if sha is None:                    # repo `OpenROAD` vs ARG `OPENROAD`
                flat = key.replace("_", "")
                sha = next((v for k, v in refs.items()
                            if k.replace("_", "") == flat), None)
            if sha:
                pins[repo] = sha
    return pins


def _branches(repo: str) -> List[Tuple[str, str]]:
    """(name, tip) for every branch, paginated.

    Paginated deliberately: the endpoint returns 30 without it, cadical has 74,
    and a truncated branch list reads exactly like a missing branch.
    """
    out = _gh(f"repos/vibeic/{repo}/branches?per_page=100",
              jq='.[] | .name + " " + .commit.sha', paginate=True)
    return [(ln.split()[0], ln.split()[1]) for ln in out.splitlines()
            if len(ln.split()) == 2]


def _preference(b: str) -> tuple:
    """Build-branch ordering: durable first, `…integration` first among those."""
    # The `vibeic/` prefix is a RANKING key, not a filter. Filtering on it
    # dropped yosys's real build branch `satfix-integration` (no prefix), leaving
    # only `vibeic/daily-merge-2026-07-29` — so the check reported a throwaway
    # branch as the source of the pin. A convention that most repos follow is a
    # preference; treating it as a requirement discards the ones that do not.
    return (bool(_TRANSIENT.search(b)),
            not re.search(r"int(egration)?$", b, re.I),
            not b.startswith("vibeic/"),
            len(b))


def build_branch(repo: str, pin: str,
                 branches: Optional[List[Tuple[str, str]]] = None
                 ) -> Optional[str]:
    """The build branch that CONTAINS the pin — not the one whose TIP equals it.

    THE DIFFERENCE IS THE WHOLE CHECK. My first version looked for a branch whose
    tip WAS the pin, which is only true while the pin is current. The moment the
    build branch moves ahead — the exact condition this program exists to detect
    — no tip equals the pin any more, so it fell back to whatever leftover branch
    still sat at the old commit and reported CURRENT.

    Measured, 2026-07-29: minutes after `daily_merge` advanced
    `vibeic/openroad-integration`, `vibeic/parallel-regression-dispatch` and
    `vibeic/sv-tb-coverage`, this file reported all 16 pins CURRENT — because
    `vibeic/fin-bazel-fix` and `vibeic/daily-merge-2026-07-29` were still parked
    on the old commits. A staleness check that passes precisely when something
    goes stale is worse than no check: it is a PASS someone will trust.

    So: order candidates by convention, then ASK which of them contains the pin.
    """
    pool = branches if branches is not None else _branches(repo)
    if not pool:
        return None
    names = [n for n, _ in pool]
    return sorted(names, key=_preference)[0]


def check_one(repo: str, pin: str) -> dict:
    pool = _branches(repo)
    if not pool:
        return {"repo": repo, "pin": pin[:9], "verdict": "NO_BRANCHES",
                "detail": "the fork reports no branches at all"}
    names = [n for n, _ in pool]
    cands = sorted(names, key=_preference)

    checked = 0
    for branch in cands[:8]:
        doc = _gh(f"repos/vibeic/{repo}/compare/{pin}...{branch}")
        if not doc:
            continue
        try:
            d = json.loads(doc)
        except ValueError:
            continue
        checked += 1
        status = d.get("status", "")
        # `total_commits` is EXACT even though the `commits` array caps at 250 —
        # the count is safe to report, the list is not.
        behind = int(d.get("total_commits") or 0)
        if status == "identical":
            return {"repo": repo, "pin": pin[:9], "branch": branch,
                    "verdict": "CURRENT", "behind": 0,
                    "detail": f"pin is the tip of {branch}"}
        if status == "ahead":
            # `ahead` means the branch contains the pin and has moved past it.
            # This is the finding: those commits are merged and not shipping.
            return {"repo": repo, "pin": pin[:9], "branch": branch,
                    "verdict": "STALE", "behind": behind,
                    "detail": f"{branch} is {behind} commit(s) past the pin; "
                              f"those commits are merged and NOT shipping"}
        # `behind` or `diverged`: this branch does not contain the pin, so it is
        # not the branch the pin came from. Keep looking.

    if not checked:
        return {"repo": repo, "pin": pin[:9], "branch": cands[0],
                "verdict": "COMPARE_FAILED",
                "detail": "GitHub would not compare the pin to any candidate "
                          "branch — not a pass; the pin was not checked"}
    return {"repo": repo, "pin": pin[:9], "branch": cands[0],
            "verdict": "ORPHANED", "behind": 0,
            "detail": f"no vibeic branch reaches the pin ({len(cands[:8])} "
                      f"checked) — the pinned commit is on no branch, so the "
                      f"fork may garbage-collect it"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root",
                    default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    pins = pinned_refs(Path(a.eda_root))
    if not pins:
        print("[NOT CHECKED] no pinned refs found — nothing was compared, which "
              "is not a clean result", file=sys.stderr)
        return RC_NOTHING

    results = [check_one(r, s) for r, s in sorted(pins.items())]
    bad = [r for r in results if r["verdict"] != "CURRENT"]
    behind_total = sum(r.get("behind", 0) for r in results)

    print(f"check_pins_current: {len(results)} pin(s), "
          f"{len(results) - len(bad)} at their branch tip, {len(bad)} not, "
          f"{behind_total} merged commit(s) not shipping")
    for r in sorted(results, key=lambda x: (x["verdict"] == "CURRENT", x["repo"])):
        print(f"  {r['repo']:<22} {r['pin']}  {r['verdict']:<14} {r['detail']}")

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "check_pins_current", "pins": results,
             "commits_not_shipping": behind_total}, indent=2) + "\n",
            encoding="utf-8")

    if bad:
        print(f"[FAIL] {len(bad)} pin(s) are not their fork branch tip. A merged "
              f"fork commit that no pin points at was reviewed for nothing.",
              file=sys.stderr)
        return RC_STALE
    print("[PASS] every pin is the tip of its build branch (this does NOT prove "
          "the image was rebuilt — that needs the artefact)")
    return RC_CURRENT


if __name__ == "__main__":
    sys.exit(main())
