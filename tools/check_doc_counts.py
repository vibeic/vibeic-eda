#!/usr/bin/env python3
"""Every count in the README states the command that produces it. This runs them.

A README count has no generator behind it, so it goes stale in silence. This one
did: `README.md` claimed **15 forked tool repos, 13 of them shipping, 12 pinned
as Dockerfile ARGs** while the org held 45 fork repos over 21 distinct upstreams,
`FORKS.json` listed 21, the per-tool split had moved the pins into three places
(20 `ARG *_REF` / 10 bake variables / 8 artefacts), and ALIGN — described there
as "forked but not yet shipped" — had been shipping since 0.2.27. Five sentences,
all confidently numbered, none reproducible. `fork-gatekeeper/inventory.py`
already exists because the *status page* shipped "all 15 forks" above a 21-row
ledger; the README was the same failure in the same week, one file over.

So the fix is not better numbers. It is that a count may not appear in the README
without the command that regenerates it, and that command runs here.

HOW THE BINDING WORKS. The README carries its counts in fenced tables:

    <!-- counts:local -->
    | what | count | reproduce (at the repo root) |
    |---|---|---|
    | per-tool build artefacts | **8** | `ls -d tools/*/ | wc -l` |
    <!-- /counts:local -->

This parses each row, runs column 3, and compares its output to column 2. The
README's own table is the test. A number nobody can regenerate cannot be written
down in the first place, and a number that drifts fails here instead of shipping.

TWO TABLES, BECAUSE THEY HAVE DIFFERENT FAILURE MODES:

  `counts:local`   derivable from this checkout. Always run. Offline.
  `counts:github`  needs the GitHub API (the org's fork list). Run only under
                   `--online`, and otherwise REPORTED AS UNVERIFIED rather than
                   passed over — a network-dependent number that quietly counts
                   as checked is worse than one openly marked as dated.

WHAT THIS CANNOT DO. It proves the README agrees with the repository. It cannot
prove the repository agrees with the built image, or that a count means what its
sentence claims — `check_image_provenance.py` covers the first and nothing
covers the second. A row could name a command that returns the right number for
the wrong reason: the first draft of the "vibeic repos cloned by the build" row
counted 15 by matching `.../ngspice` and `.../ngspice.git` as two repos, which is
the correct answer arrived at by a broken measurement, and the exact failure this
file exists to catch. Rows are therefore reviewed, not merely green.

TRUST MODEL, stated because this executes text from a file. The commands come
from a version-controlled document in this repo and run with the privileges of
whoever runs the checks. Anyone who can edit that table can already edit this
script; the table adds no privilege that `tools/*.py` did not already have. It is
still worth reading a diff that changes a command, for the same reason a diff
that changes a threshold is worth reading.

Usage:
    python3 tools/check_doc_counts.py                 # README.md, offline rows
    python3 tools/check_doc_counts.py --online        # also the GitHub rows
    python3 tools/check_doc_counts.py path/to/doc.md  # any doc carrying the fences

Exit: 0 agree, 1 drift (or a command that failed), 2 nothing was compared —
which is a gap in the check, not a pass.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: A fenced counts table. Non-greedy so two fences in one file stay separate.
FENCE = r"<!--\s*counts:{name}\s*-->(.*?)<!--\s*/counts:{name}\s*-->"

#: `| text | **12** | `cmd` |` — the count must be bold and the command must be
#: a code span. Both are required rather than tolerated: a bare number in the
#: table is a number nobody committed to regenerating, and this must not silently
#: skip it. Rows failing to parse are reported (see `parse_table`).
ROW = re.compile(r"^\|(?P<what>[^|]+)\|\s*\*\*(?P<count>[0-9,]+)\*\*\s*\|\s*`(?P<cmd>.+?)`\s*\|\s*$")

#: A markdown table's separator row, skipped without complaint.
SKIP = re.compile(r"^\|\s*[-: ]+\|")

#: What makes a line a COUNT CLAIM: a bold number. This, not "looks like a row",
#: is the trigger for the malformed-row complaint below. The asymmetry is
#: deliberate — a claim that lost its command is a number nobody can regenerate
#: (the whole defect), while a command that lost its claim asserts nothing. The
#: first must fail loudly; the second is merely tidy-able. Matching on "has a
#: backtick" instead flagged the header row `| in the `vibeic` org … |`, which is
#: a check crying wolf, and a guard that cries wolf gets deleted.
CLAIM = re.compile(r"\*\*[0-9,]+\*\*")


def parse_table(text: str, fence: str) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Rows of (label, expected, command), plus rows that looked like data and
    did not parse. The second return value is why this does not just filter:
    a row that stops matching after an edit would otherwise vanish from the
    check while still being read as checked by anyone looking at the README."""
    m = re.search(FENCE.format(name=re.escape(fence)), text, re.S)
    if not m:
        return [], []
    rows, malformed = [], []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|") or SKIP.match(line):
            continue
        r = ROW.match(line)
        if not r:
            if CLAIM.search(line):
                malformed.append(line)
            continue
        # Markdown escapes a literal pipe inside a cell. The shell wants it back.
        cmd = r.group("cmd").replace(r"\|", "|")
        rows.append((r.group("what").strip(), int(r.group("count").replace(",", "")), cmd))
    return rows, malformed


#: Anything path-shaped in a command string. Quotes and backslashes are not in
#: the class, so `open("fork-gatekeeper/FORKS.json")` still yields the path.
PATHISH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./*-]*")


def measures_something(cmd: str) -> bool:
    """Does this command actually look at the repository?

    THE BYPASS THIS EXISTS TO CLOSE. Every other check here compares a number to
    a command's output, so the cheapest way to make a failing row green is to
    replace the command with one that cannot fail: `echo 21`, `python3 -c
    'print(21)'`. The row still reads as measured, the checker still says ok, and
    the count is once again a number with nothing behind it — which is the exact
    defect this file was written to retire, reintroduced through the file itself.

    So a command must name something that exists in this repository. `gh` is the
    one exemption, because the GitHub rows measure the org and cannot name a
    local path; they are also the rows that only run under `--online`.
    """
    if re.match(r"^\s*gh\s", cmd):
        return True
    for tok in PATHISH.findall(cmd):
        if "/" not in tok and "*" not in tok and not (ROOT / tok).exists():
            continue  # a bare word like `grep` or `wc`; not a claim to measure anything
        try:
            if list(ROOT.glob(tok)) or (ROOT / tok).exists():
                return True
        except (ValueError, OSError):
            continue
    return False


def run_rows(rows, label: str) -> list[str]:
    bad = []
    for what, expected, cmd in rows:
        if not measures_something(cmd):
            bad.append(f"{label}: {what!r}: the command names nothing in this repository, "
                       f"so it cannot be measuring anything — a constant that agrees with "
                       f"the count is not evidence for it\n    $ {cmd}")
            continue
        p = subprocess.run(["bash", "-c", cmd], cwd=ROOT,
                           capture_output=True, text=True, timeout=300)
        out = p.stdout.strip()
        if p.returncode != 0:
            bad.append(f"{label}: {what!r}: the command FAILED (rc={p.returncode}), so "
                       f"the stated {expected} was not verified\n"
                       f"    $ {cmd}\n"
                       f"    {(p.stderr.strip() or '(no stderr)').splitlines()[0][:200]}")
            continue
        try:
            got = int(out.split()[-1]) if out else None
        except (ValueError, IndexError):
            got = None
        if got is None:
            bad.append(f"{label}: {what!r}: the command produced no number "
                       f"({out[:80]!r}), so the stated {expected} was not verified\n"
                       f"    $ {cmd}")
        elif got != expected:
            bad.append(f"{label}: {what!r}: doc says {expected}, the repository says {got}\n"
                       f"    $ {cmd}")
        else:
            print(f"  ok  {what}: {got}")
    return bad


def registry_invariants() -> list[str]:
    """Facts about `FORKS.json` no README row can state, because a doc can only
    quote a total and these are about its SHAPE.

    One tool per upstream. Two entries sharing an upstream would make the fleet
    list's length disagree with the number of projects tracked while every
    individual row still looked right — which is precisely how "45 forks" and
    "21 projects" became two true answers to one question in the org itself.
    """
    f = ROOT / "fork-gatekeeper" / "FORKS.json"
    if not f.is_file():
        return [f"fork-gatekeeper/FORKS.json is missing — the fleet list cannot be checked"]
    forks = json.loads(f.read_text())["forks"]
    bad = []
    seen: dict[str, str] = {}
    for e in forks:
        up = e.get("upstream", "").lower()
        if not up:
            bad.append(f"FORKS.json: {e.get('tool')!r} declares no upstream")
        elif up in seen:
            bad.append(f"FORKS.json: {e.get('tool')!r} and {seen[up]!r} both declare "
                       f"upstream {up!r} — the entry count is then not a project count")
        else:
            seen[up] = e.get("tool", "?")
    if not bad:
        print(f"  ok  FORKS.json: {len(forks)} entries, {len(seen)} distinct upstreams, no duplicate")
    return bad


#: Forks that reach the image without a clone URL in any Dockerfile, and the
#: route that gets them there. An entry here is an assertion that the tool is
#: shipped by some other mechanism; `shipping_invariants` fails if one stops
#: being needed, so this cannot quietly become a list of tools nobody rechecked.
SHIPPED_WITHOUT_A_CLONE_URL = {
    "OpenSTA": "OpenROAD's src/sta git submodule, via .gitmodules on the integration branch",
}

#: Forks we build and pin but deliberately do NOT install into the composed
#: image. Each entry is a decision someone must be able to find later.
BUILT_BUT_NOT_INSTALLED = (
    # yosys 0.67+ ships the slang frontend itself, so this plugin is a SECOND
    # copy of it in one process and the duplicated statics double-free at exit
    # (vibeic-eda#24). Nothing in the flow loads it; the fork stays tracked.
    "sv-elab",
)

CLONE_URL = re.compile(r"github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?(?=[\s\"'`]|$)")


def shipping_invariants(text: str) -> list[str]:
    """Which tracked forks do NOT reach the image — derived, then required to be
    named in the doc.

    The doc says "15 of the 21 reach this image" and lists the other six. Those
    two numbers are arithmetic on counts the tables already bind (14 clone URLs
    + 1 submodule; 21 - 15), so they are not re-derived here. The NAMES are:
    a fork that silently stops shipping, or a newly tracked fork nobody wired in,
    changes this set while every count in the table still reproduces.

    Deliberately one-directional. This fails when the repository knows about an
    unshipped fork the doc does not name; it does not parse the doc's prose to
    find names the repository no longer agrees with, because the sentence around
    them is English and a guard coupled to a wording is a guard that cries wolf
    at the first edit. The other direction is covered anyway: shipping a fork
    moves the "repos cloned by the build" row, which fails the table and lands
    the editor in this paragraph.
    """
    f = ROOT / "fork-gatekeeper" / "FORKS.json"
    dockerfiles = [ROOT / "Dockerfile"] + sorted(ROOT.glob("tools/*/Dockerfile"))
    if not f.is_file() or not any(p.is_file() for p in dockerfiles):
        return ["cannot derive what ships: FORKS.json or the Dockerfiles are missing"]

    cloned = set()
    for p in dockerfiles:
        if p.is_file():
            cloned |= {m.group(1).lower() for m in CLONE_URL.finditer(p.read_text())}

    tracked = [e["tool"] for e in json.loads(f.read_text())["forks"]]
    bad = []
    for tool, how in SHIPPED_WITHOUT_A_CLONE_URL.items():
        if tool not in tracked:
            bad.append(f"{tool!r} is listed as shipped via {how}, but no longer appears "
                       f"in FORKS.json — the exception outlived the thing it excepted")
        elif tool.lower() in cloned:
            bad.append(f"{tool!r} now has its own clone URL, so the exception for it "
                       f"({how}) is stale and is hiding a real count")

    # `cloned` answers "do we BUILD it". The sentence this feeds says "reach the
    # image". Two different claims, and BUILT_BUT_NOT_INSTALLED is where they
    # diverge — without it the doc reports a tool as reaching the image while its
    # COPY is commented out, which is the confidently-wrong number this file
    # exists to prevent.
    shipped = (cloned | {t.lower() for t in SHIPPED_WITHOUT_A_CLONE_URL}) \
              - {t.lower() for t in BUILT_BUT_NOT_INSTALLED}
    unshipped = [t for t in tracked if t.lower() not in shipped]
    unnamed = [t for t in unshipped if f"`{t}`" not in text]
    if unnamed:
        bad.append(f"tracked but not shipped, and not named in the doc: "
                   f"{', '.join(unnamed)} — a fork nobody can see is unshipped is "
                   f"how 'the two ALIGN repos are not shipped' outlived being true")
    if not bad:
        # len(shipped) would be wrong and was: `cloned` also holds vibeic sources
        # that are NOT tracked forks (mirrors of upstream data and solver repos,
        # which GitHub's fork API refused, so they carry fork=false and cannot be
        # in a fork registry). Counting those as "tracked forks that ship" printed
        # 21 of 21 alongside a list of six that do not ship. Subtract instead.
        print(f"  ok  shipping: {len(tracked) - len(unshipped)} of {len(tracked)} "
              f"tracked forks reach the image; the other {len(unshipped)} are named "
              f"in the doc ({', '.join(unshipped)})")
        print(f"  ok  the build clones {len(cloned)} vibeic sources, "
              f"{len(cloned & {t.lower() for t in tracked})} of which are tracked forks")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("doc", nargs="?", default=str(ROOT / "README.md"))
    ap.add_argument("--online", action="store_true",
                    help="also run the rows that need the GitHub API")
    a = ap.parse_args()

    doc = Path(a.doc)
    if not doc.is_file():
        print(f"check_doc_counts: {doc} does not exist — nothing was compared, "
              f"which is a gap in the check, not a pass", file=sys.stderr)
        return 2
    text = doc.read_text()

    local, mal_local = parse_table(text, "local")
    github, mal_github = parse_table(text, "github")
    bad = [f"{doc.name}: a counts row no longer parses, so it is not being checked "
           f"even though it still reads as a fact:\n    {ln}" for ln in mal_local + mal_github]

    if not local and not github:
        print(f"check_doc_counts: {doc} carries no <!-- counts:local --> or "
              f"<!-- counts:github --> table — nothing was compared, which is a "
              f"gap in the check, not a pass", file=sys.stderr)
        return 2

    bad += run_rows(local, "local")
    if a.online:
        bad += run_rows(github, "github")
    bad += registry_invariants()
    bad += shipping_invariants(text)

    if bad:
        sys.stdout.flush()
        print(f"\ncheck_doc_counts: {len(bad)} count(s) in {doc.name} do not "
              f"reproduce:", file=sys.stderr)
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        print("\n  Fix the document or fix the repository — but do not delete the "
              "command, which is the only reason this was catchable.", file=sys.stderr)
        return 1

    n = len(local) + (len(github) if a.online else 0)
    note = "" if a.online else (f"; {len(github)} GitHub row(s) NOT verified "
                                f"(needs --online) — they are dated in the doc")
    print(f"check_doc_counts: {n} count(s) in {doc.name} reproduce{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
