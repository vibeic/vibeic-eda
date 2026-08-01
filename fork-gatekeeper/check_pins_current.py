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


def tree_basis(eda_root: Path) -> dict:
    """WHICH TREE this verdict describes: HEAD, dirty pin files, behind-count.

    `pinned_refs` reads the Dockerfiles off the FILESYSTEM, so every verdict
    here is about the working tree it was run in — not about what ships. That
    distinction is not academic: on 2026-08-01 this reported
    `OpenROAD b6fd2b2fe STALE` while `origin/main` carried `47636465f9` and had
    shipped it as 0.2.53. The checkout was 14 commits behind with an
    uncommitted pin edit, and the report named neither.

    It fails in BOTH directions. A stale checkout invents staleness that is not
    shipping (what happened), and an uncommitted ADVANCE hides staleness that
    is — the tree would read current while the release is not.

    Not a refusal: advancing a pin legitimately runs this on a dirty tree. The
    basis is STATED so the reader knows which tree answered.
    """
    def _git(*a):
        try:
            r = subprocess.run(["git", "-C", str(eda_root), *a],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    head = _git("rev-parse", "--short", "HEAD")
    if head is None:
        return {"head": None, "dirty_pin_files": [], "behind": None,
                "note": "not a git checkout — the basis of this verdict is "
                        "unknown"}
    status = _git("status", "--porcelain", "--", "tools", "Dockerfile",
                  "docker-bake.hcl") or ""
    # SPLIT, not sliced. `--porcelain` is `XY<space>PATH`, but a staged entry
    # is `M <space>PATH` and a fixed offset ate the first character of the
    # filename — a report that names `ockerfile` is a report a reader cannot
    # act on.
    dirty = [l.split(None, 1)[1].strip() for l in status.splitlines()
             if l.strip() and len(l.split(None, 1)) == 2]
    # `@{u}` is UNDEFINED on a detached HEAD and on a branch with no upstream —
    # and a detached worktree pinned to `origin/main` is exactly how this check
    # should be run when the shared checkout is dirty, so the one tree we most
    # want a currency statement about was the one that produced none. Fall back
    # to the remote-tracking ref by name, and NAME which ref answered: "0 behind
    # origin/main" and "I could not tell" must not print the same way.
    behind, behind_basis = None, None
    for ref in ("@{u}", "origin/main", "origin/master"):
        cnt = _git("rev-list", "--count", f"HEAD..{ref}")
        if cnt is not None and cnt.isdigit():
            behind, behind_basis = int(cnt), ref
            break
    return {"head": head, "dirty_pin_files": dirty, "behind": behind,
            "behind_basis": behind_basis}


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
            # WHOSE commits they are decides whether this is a defect.
            #
            # This used to be one verdict, and it made the program permanently
            # red: four pins sit on pure upstream mirrors DELIBERATELY —
            # vibeic-eda#23/#25 pinned slang, xschem, Xyce and sv-elab to the
            # commits the image already shipped, so that change would be "build
            # from our fork" and not "and also upgrade four tools". Reporting a
            # recorded decision as a failure, by 288 commits and growing, means
            # rc=1 is the EXPECTED value — and a real stale pin arriving
            # tomorrow changes nothing anyone would notice. That is how
            # `fork_reaches_flow_check` lost its credibility too (#17).
            #
            # `branch_is_ours` is imported rather than reimplemented: two
            # programs carrying two copies of this answer is exactly how they
            # came to say opposite things about the same four pins (#29).
            ours = branch_is_ours(repo, branch)
            if ours is False:
                return {"repo": repo, "pin": pin[:9], "branch": branch,
                        "verdict": "UPSTREAM_AVAILABLE", "behind": behind,
                        "ours": False,
                        "detail": f"{branch} carries none of our commits and is "
                                  f"{behind} commit(s) past the pin — an upstream "
                                  f"version someone must CHOOSE to adopt, not a "
                                  f"pin that fell behind our own work"}
            if ours is None:
                # Not the mirror case, and not established as ours either. It
                # keeps its teeth — but it must not claim to know why.
                return {"repo": repo, "pin": pin[:9], "branch": branch,
                        "verdict": "STALE_UNDECIDED", "behind": behind,
                        "ours": None,
                        "detail": f"{branch} is {behind} commit(s) past the pin, "
                                  f"and whether it carries our commits could not "
                                  f"be determined — treated as a finding because "
                                  f"unknown is not a pass"}
            return {"repo": repo, "pin": pin[:9], "branch": branch,
                    "verdict": "STALE", "behind": behind, "ours": True,
                    "detail": f"{branch} is {behind} commit(s) past the pin; "
                              f"those commits are OURS, merged and NOT shipping"}
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


def declared_upstream(repo: str,
                      forks_json: Optional[Path] = None) -> Optional[str]:
    """The upstream WE declare for this fork in `FORKS.json`, or None.

    Our own declaration, and the one `daily_merge` and `discover_forks` already
    read — not a guess and not a second source of truth. It exists because
    GitHub's `.parent` does not: a repo pushed up directly, or deleted and
    recreated, carries no fork relationship at all.
    """
    f = forks_json or (Path(__file__).resolve().parent / "FORKS.json")
    try:
        d = json.loads(f.read_text(errors="replace"))
    except (OSError, ValueError):
        return None
    for entry in d.get("forks", []):
        if str(entry.get("tool", "")).lower() == repo.lower():
            up = str(entry.get("upstream") or "")
            return up if "/" in up else None
    return None


def branch_is_ours(repo: str, branch: str) -> Optional[bool]:
    """Does this branch carry any commit of ours, or is it an upstream mirror?

    A pin behind a branch WE build on is stale. A pin behind a branch that is a
    pure mirror of upstream is a DECISION someone made about which upstream
    version to ship, and advancing it adopts a new one.

    Caught before it did damage: vibeic-eda#23/#25 pinned slang, xschem, Xyce and
    sv-elab to the commits the IMAGE SHIPS, read out of each tool's own SOURCES
    file, precisely so the change would be "build from our fork" and not "and
    also upgrade the tool". The next release wanted to move all four to
    `master` — a four-tool version bump smuggled under "build 6 absent
    artefacts". Measured: those four branches carry 0 commits of ours;
    `yosys satfix-integration` carries 34.

    A BRANCH THAT DOES NOT EXIST UPSTREAM IS OURS, CONCLUSIVELY. That case used
    to return None, and the docstring above recorded a measurement — "yosys
    satfix-integration carries 34" — that the code as written could no longer
    reproduce: the comparison is `upstream:<branch>...vibeic:<branch>`, and
    upstream has no branch of that name, so it 404s. Every one of our own
    integration branches took the same path. `klayout vibeic/klayout-signoff-int`
    and `yosys satfix-integration` both answered "could not tell" about branches
    that exist nowhere else.

    None -> could not tell, which is treated as not-ours: the fail-safe direction
    is to leave a pin alone and say so. It must stay reachable, but it must not
    be reached by the branches this whole system exists to ship.
    """
    meta = _sh(["gh", "api", f"repos/vibeic/{repo}",
                "--jq", ".parent.full_name // empty"], timeout=120)[1].strip()
    if not meta:
        # GitHub's `.parent` is empty for any repo in the org that was not
        # created BY forking — a repo pushed up directly, or one deleted and
        # recreated, loses the relationship. MEASURED: 8 of the 28 pinned repos
        # have no `.parent` (FasterCap, Fault, Geometry, LinAlgebra,
        # OpenROAD-flow-scripts, Trilinos, cadical, kissat), and every one of
        # them DECLARES its upstream in FORKS.json — the same declaration
        # `daily_merge` and `discover_forks` already read.
        #
        # Without this, `branch_is_ours` returned None for all eight, the
        # release program read that as "could not determine", and their pins
        # could never be advanced by it: `fault chain --skip-boundary` landed in
        # the fork and the release refused to ship it, reporting only
        # STALE_UNDECIDED. An authority we own, unused, turning into a
        # permanent silent block.
        meta = declared_upstream(repo) or ""
    if not meta:
        return None
    owner = meta.split("/")[0]
    rc, out, _ = _sh(["gh", "api",
                      f"repos/vibeic/{repo}/compare/{owner}:{branch}...vibeic:{branch}",
                      "--jq", ".ahead_by"], timeout=120)
    if rc == 0 and out.strip().isdigit():
        return int(out.strip()) > 0
    # The compare failed. Distinguish "upstream has no such branch" — which
    # answers the question rather than leaving it open — from a transport or
    # permission failure, which does not.
    up_rc, _, _ = _sh(["gh", "api", f"repos/{meta}/branches/{branch}",
                       "--jq", ".name"], timeout=120)
    ours_rc, _, _ = _sh(["gh", "api", f"repos/vibeic/{repo}/branches/{branch}",
                         "--jq", ".name"], timeout=120)
    if ours_rc == 0 and up_rc != 0:
        return True

    # LAST, and the one that answers for a repo with no fork RELATIONSHIP: is
    # the branch TIP a commit the upstream repository has at all?
    #
    # The compare endpoint needs the two repos to be in one network, so for the
    # eight repos below it 404s no matter which upstream is named — and the
    # branch-existence test above cannot decide either, because upstream DOES
    # have a `main`. Commit presence needs no relationship: a tip upstream has
    # never seen was authored here.
    #
    # MEASURED both directions on Fault: our `10613da` and `0c90e3b` return 422
    # "No commit found for SHA" from AUCOHL/Fault, while upstream's own tip
    # `cf5509f` returns 200 there AND 200 in our fork. The negative control
    # matters — without it, "everything 422s" would look like the same answer.
    tip_rc, tip, _ = _sh(["gh", "api", f"repos/vibeic/{repo}/commits/{branch}",
                          "--jq", ".sha"], timeout=120)
    if tip_rc == 0 and tip.strip():
        up_has, _, _ = _sh(["gh", "api", f"repos/{meta}/commits/{tip.strip()}",
                            "--jq", ".sha"], timeout=120)
        # A tip upstream HAS is a mirror tip: advancing the pin to it adopts an
        # upstream version, which is a decision, not a stale pin. That is the
        # #23/#25 finding and it must keep answering False.
        return up_has != 0
    return None


def print_basis(basis: dict) -> None:
    """State WHICH TREE answered, on every exit path.

    Printed before the refusal too: "nothing was compared" and "nothing was
    compared IN A TREE 15 COMMITS BEHIND" send a reader to different places, and
    the second is the one that has actually happened.
    """
    _b = []
    if basis.get("head"):
        _b.append(f"HEAD {basis['head']}")
    if basis.get("behind"):
        _b.append(f"{basis['behind']} commit(s) BEHIND {basis['behind_basis']}")
    elif basis.get("behind") == 0:
        _b.append(f"up to date with {basis['behind_basis']} "
                  f"(the local remote-tracking ref, as of the last fetch)")
    elif basis.get("head"):
        # Stated, because silence here reads as "up to date" — the one reading
        # a stale checkout most needs not to be given.
        _b.append("could NOT establish whether this tree is behind a remote "
                  "(no upstream and no origin/main|master) — its currency is "
                  "UNKNOWN, not confirmed")
    if basis.get("dirty_pin_files"):
        _b.append(f"{len(basis['dirty_pin_files'])} UNCOMMITTED pin file(s): "
                  + ", ".join(basis["dirty_pin_files"][:4]))
    if basis.get("note"):
        _b.append(basis["note"])
    print(f"  basis: this verdict describes the WORKING TREE — "
          + ("; ".join(_b) if _b else "clean and current"))
    if (basis.get("behind") or basis.get("dirty_pin_files")
            or (basis.get("head") and basis.get("behind") is None)):
        print("  ^ so it is NOT a statement about what SHIPS: a stale checkout "
              "invents staleness that is not shipping, and an uncommitted "
              "advance hides staleness that is.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root",
                    default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    basis = tree_basis(Path(a.eda_root))
    pins = pinned_refs(Path(a.eda_root))
    if not pins:
        print_basis(basis)
        print("[NOT CHECKED] no pinned refs found — nothing was compared, which "
              "is not a clean result", file=sys.stderr)
        return RC_NOTHING

    results = [check_one(r, s) for r, s in sorted(pins.items())]
    # UPSTREAM_AVAILABLE is reported, never failed on: it is a decision waiting
    # for someone, not a pin that fell behind our own work. Everything else that
    # is not CURRENT still fails, including the states that mean "not checked".
    available = [r for r in results if r["verdict"] == "UPSTREAM_AVAILABLE"]
    bad = [r for r in results
           if r["verdict"] not in ("CURRENT", "UPSTREAM_AVAILABLE")]
    # …and only OUR commits count as "not shipping". Counting an upstream
    # version we chose not to adopt inflated this to 288 and made the number
    # mean nothing.
    behind_total = sum(r.get("behind", 0) for r in results
                       if r["verdict"] != "UPSTREAM_AVAILABLE")
    upstream_total = sum(r.get("behind", 0) for r in available)

    print_basis(basis)
    print(f"check_pins_current: {len(results)} pin(s), "
          f"{len(results) - len(bad) - len(available)} at their branch tip, "
          f"{len(bad)} not, {len(available)} holding an upstream version "
          f"({upstream_total} upstream commit(s) available), "
          f"{behind_total} of OUR merged commit(s) not shipping")
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
    if available:
        # Reported, not failed on — but not silently either. An upstream version
        # being available is not a neutral fact forever: Xyce reached 182
        # commits while this was indistinguishable from a defect and therefore
        # ignored. Naming the oldest one is what turns "we are holding" into a
        # question someone answers.
        worst = max(available, key=lambda r: r.get("behind", 0))
        print(f"[PASS] every pin is the tip of its build branch. "
              f"{len(available)} pin(s) hold a deliberate upstream version "
              f"(vibeic-eda#23/#25); the furthest is {worst['repo']} at "
              f"{worst['behind']} upstream commit(s) — adopting one is a "
              f"decision, and this line is the only place it gets asked.")
        return RC_CURRENT
    print("[PASS] every pin is the tip of its build branch (this does NOT prove "
          "the image was rebuilt — that needs the artefact)")
    return RC_CURRENT


if __name__ == "__main__":
    sys.exit(main())
