#!/usr/bin/env python3
"""The patch->oracle map, asking BOTH questions instead of one.

WHY THIS EXISTS (vibeic-eda#93, measured 2026-08-05)
====================================================
`FORK_PATCH_AUDIT.md` §6 says the map comes first: for each of our patches,
which test would go red if it were reverted, and does that test run in the
build that produces the shipped binary. That map was built by hand, and it
asked exactly one question:

    does this commit SHIP a test?

`ee778e7ced` ("drt: post-route additive min-area repair", 494 adds) answered
"no" and was filed **NO-ORACLE — substantive code, no test at all**.

That is wrong, and wrong in a way the question could not see. The commit HAS
an oracle: `drt:top_level_term2`, an upstream golden-diff test registered in
both CMake and bazel. Our patch changed the routed DEF that test compares
against, the `.defok` was never regenerated, and the test has been
**permanently red** ever since. An oracle that is always red can never signal,
so the patch is unguarded — but it is unguarded for the OPPOSITE reason from a
patch nobody wrote a test for.

    "does this commit SHIP a test"  cannot see  "does this commit BREAK one".

Two conditions, two remedies. The first needs a test written. The second needs
a golden regenerated, and until it is, every later mutation experiment in that
module reads "nothing went red" from a suite that was already red — a
confident false negative, which is the exact failure §6 warns about.

WHAT THIS PROGRAM ADDS
----------------------
Question 2, explicitly, with its own bucket:

    BROKE-EXISTING-ORACLE   this commit is a suspect for a pre-existing test
                            that is currently FAILING. Remedy: regenerate the
                            golden (or fix the patch) — NOT "write a test".

and the state that must exist for the answer to stay honest:

    COULD-NOT-MEASURE       no verdict was recorded for the tests this commit
                            could have broken. NO-ORACLE asserts "breaks
                            nothing", which is a MEASUREMENT claim; without a
                            measurement it may not be made. A test that is
                            permanently red and a test that never executes are
                            indistinguishable from outside, so an absent
                            verdict NEVER renders as a pass.

VERDICTS ARE INPUT, NOT INFERENCE
---------------------------------
Question 2 cannot be answered from git alone: whether a test passes is a fact
about running it. So the program consumes measured verdict ledgers

    {"tool": ..., "measured_at_sha": ..., "measured_by": ..., "measured_on": ...,
     "verdicts": {"drt:top_level_term2": "FAIL", ...}}

and refuses to invent the ones it was not given. Pass `--verdicts` more than
once with ledgers measured at different shas and the flip range narrows: a test
PASSing at X and FAILing at Y makes the suspects exactly our commits in X..Y.
With only one ledger there is no flip range, and attribution falls back to
"our commits touching that module's source since the golden was last written" —
reported as `golden-relative`, which is weaker and is labelled weaker.

FLOOR vs TOTAL
--------------
The headline count says which it is, always. If the ledger does not cover every
registered test, more dead oracles can be hiding in the unmeasured remainder and
the number is a FLOOR. Only a ledger covering every registered test earns TOTAL.
A corrected number presented as complete is the same defect one layer up.

Exit: 0 map produced and clean, 1 at least one BROKE-EXISTING-ORACLE,
      2 nothing could be measured.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

PROGRAM = "oracle_map"

#: Author substrings that mark a commit as OURS — same definition as
#: `check_our_commits_ship.py`, deliberately, so the two programs cannot
#: disagree about which commits the fork owns.
OURS = ("reyer", "vibeic")

#: Buckets, most-specific first. The order is the precedence order used for the
#: headline `bucket`; every condition that held is also listed in `flags`, so a
#: commit that both ships a test and breaks another is not silently reduced.
BROKE = "BROKE-EXISTING-ORACLE"
SHIPS = "SHIPS-ORACLE"
NO_ORACLE = "NO-ORACLE"
EMPTY_SLICE = "EMPTY-SLICE"
UNMEASURED = "COULD-NOT-MEASURE"

#: A golden is the file a test's output is diffed against. OpenROAD spells them
#: `<test>.ok`, `.defok`, `.drcok`, `.guideok`, …; the shared shape is a name
#: ending in "ok". `.golden` is accepted for tools that use that convention.
GOLDEN_RE = re.compile(r"\.(?:[a-z0-9]*ok|golden)$")

BUILD_FILE_NAMES = ("CMakeLists.txt", "BUILD", "BUILD.bazel", "Makefile.am",
                    "meson.build", "configure.ac")
DOC_SUFFIXES = (".md", ".rst", ".txt", ".png", ".svg")


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def sh_ok(*args: str) -> bool:
    return subprocess.run(args, capture_output=True, text=True).returncode == 0


def is_test_path(path: str) -> bool:
    """A path is a test if it lives under a test directory or is named like one.

    Deliberately path-shaped and not build-system-shaped: this answers "is this
    file part of a test", which is a different question from "does this test
    run", answered by `registered_tests()` below from the build files.
    """
    parts = path.split("/")
    return ("test" in parts or "tests" in parts
            or parts[-1].startswith("test_")
            or parts[-1].endswith("_test.py"))


def module_of(path: str) -> Optional[str]:
    """`src/drt/src/TritonRoute.cpp` -> `drt`. None when the layout is unknown.

    Returning None matters: an unknown layout must reach COULD-NOT-MEASURE, not
    a confident bucket derived from a guess.
    """
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "src":
        return parts[1]
    return None


def _cmake_tests(text: str) -> Set[str]:
    """Test names inside every `or_integration_tests(<mod> TESTS … )` call.

    Keyword-sectioned, bare identifiers, no quotes — so it is parsed by walking
    the call's own parentheses rather than by grepping for `TESTS`, which also
    matches inside the macro name `or_integration_tests`.
    """
    names: Set[str] = set()
    for m in re.finditer(r"or_integration_tests\s*\(", text):
        depth, end = 0, len(text)
        for j in range(m.end() - 1, len(text)):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        body = text[m.end():end]
        section = None
        for tok in re.findall(r'"[^"]*"|[A-Za-z0-9_./-]+', body):
            if tok.isupper():
                section = tok if tok.endswith("TESTS") else None
                continue
            if section:
                names.add(tok.strip('"'))
    return names


def _bazel_tests(text: str) -> Set[str]:
    names: Set[str] = set()
    for key in ("COMPULSORY_TESTS", "PASSFAIL_TESTS", "TESTS"):
        for m in re.finditer(rf"^{key}\s*=\s*\[(.*?)\]", text, re.S | re.M):
            names.update(re.findall(r'"([^"]+)"', m.group(1)))
    return names


def registered_tests(repo: Path, module: str) -> Dict[str, Set[str]]:
    """Tests the build systems register for `module`, per build system.

    FORK_PATCH_AUDIT §0.1: enumerate from the MECHANISM, not from the directory
    you expect. OpenROAD registers in `src/<mod>/test/CMakeLists.txt` (the
    common case), `src/<mod>/CMakeLists.txt` (drt's unit tests) and
    `src/<mod>/test/cpp/CMakeLists.txt`. bazel registers in
    `src/<mod>/test/BUILD`. Both are read; a test present in one and absent from
    the other is exactly the vibe-ic#813 gap and is reported, not averaged.
    """
    out: Dict[str, Set[str]] = {"cmake": set(), "bazel": set()}
    for rel in (f"src/{module}/test/CMakeLists.txt",
                f"src/{module}/CMakeLists.txt",
                f"src/{module}/test/cpp/CMakeLists.txt"):
        p = repo / rel
        if p.is_file():
            out["cmake"].update(_cmake_tests(p.read_text(errors="replace")))
    p = repo / f"src/{module}/test/BUILD"
    if p.is_file():
        out["bazel"].update(_bazel_tests(p.read_text(errors="replace")))
    return out


def golden_files(repo: Path, module: str, test: str) -> List[str]:
    d = repo / f"src/{module}/test"
    if not d.is_dir():
        return []
    return sorted(f"src/{module}/test/{f.name}" for f in d.iterdir()
                  if f.is_file() and f.name.startswith(test + ".")
                  and GOLDEN_RE.search(f.name))


def last_touch(repo: Path, path: str, head: str) -> Optional[dict]:
    line = sh("git", "-C", str(repo), "log", "-1", "--format=%H%x1f%an%x1f%ad",
              "--date=short", head, "--", path)
    if not line:
        return None
    sha, author, date = line.split("\x1f")
    return {"sha": sha, "author": author, "date": date,
            "ours": any(o in author.lower() for o in OURS)}


def our_commits(repo: Path, upstream_ref: str, head: str) -> List[dict]:
    """Commits on `head` that upstream does not have and that we authored."""
    rng = f"{upstream_ref}..{head}" if sh_ok(
        "git", "-C", str(repo), "rev-parse", "--verify", upstream_ref) else head
    log = sh("git", "-C", str(repo), "log", "--no-merges",
             "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=short", rng)
    out = []
    for line in log.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, author, date, subject = parts
        if not any(o in author.lower() for o in OURS):
            continue
        out.append({"sha": sha, "short": sha[:9], "author": author,
                    "date": date, "subject": subject})
    return out


def slice_of(repo: Path, sha: str) -> dict:
    """What the commit changes, partitioned. `added` is status-A paths only."""
    raw = sh("git", "-C", str(repo), "show", "--no-renames", "--name-status",
             "--format=", sha)
    source, tests, builds, docs, added = [], [], [], [], set()
    for line in raw.splitlines():
        bits = line.split("\t")
        if len(bits) < 2:
            continue
        status, path = bits[0], bits[-1]
        if status.startswith("A"):
            added.add(path)
        name = path.split("/")[-1]
        if name in BUILD_FILE_NAMES:
            builds.append(path)
        elif is_test_path(path):
            tests.append(path)
        elif path.endswith(DOC_SUFFIXES):
            docs.append(path)
        else:
            source.append(path)
    return {"source": source, "tests": tests, "builds": builds, "docs": docs,
            "added": added}


def load_ledgers(paths: Sequence[str]) -> List[dict]:
    out = []
    for p in paths:
        data = json.loads(Path(p).read_text())
        if "verdicts" not in data:
            raise ValueError(f"{p}: no 'verdicts' key — a ledger without "
                             f"measurements is not a ledger")
        data["_path"] = p
        out.append(data)
    return out


def flip_range(repo: Path, ledgers: List[dict], test: str
               ) -> Tuple[Optional[str], Optional[str], str]:
    """(pass_sha, fail_sha, how) for the narrowest measured PASS->FAIL flip.

    `how` is `measured-flip` when two ledgers bracket the change and
    `golden-relative` when only a FAIL was ever measured — the weaker
    attribution, named so it cannot be read as the stronger one.
    """
    fails = [l for l in ledgers if l["verdicts"].get(test) == "FAIL"]
    passes = [l for l in ledgers if l["verdicts"].get(test) == "PASS"]
    if not fails:
        return None, None, "not-failing"
    fail_sha = fails[-1].get("measured_at_sha")
    for cand in reversed(passes):
        p = cand.get("measured_at_sha")
        if p and fail_sha and sh_ok("git", "-C", str(repo),
                                    "merge-base", "--is-ancestor", p, fail_sha):
            return p, fail_sha, "measured-flip"
    return None, fail_sha, "golden-relative"


def build_map(repo: Path, upstream_ref: str, head: str,
              ledgers: List[dict]) -> dict:
    commits = our_commits(repo, upstream_ref, head)
    slices = {c["sha"]: slice_of(repo, c["sha"]) for c in commits}
    newest = ledgers[-1] if ledgers else None
    verdicts: Dict[str, str] = dict(newest["verdicts"]) if newest else {}

    # --- the dead-oracle table: every measured FAIL, and who is a suspect ---
    dead: List[dict] = []
    suspects_by_commit: Dict[str, List[str]] = {}
    for test, verdict in sorted(verdicts.items()):
        if verdict != "FAIL":
            continue
        module = test.split(":")[0] if ":" in test else None
        name = test.split(":", 1)[1] if ":" in test else test
        goldens = golden_files(repo, module, name) if module else []
        gold = None
        for g in goldens:
            t = last_touch(repo, g, head)
            if t and (gold is None or t["date"] > gold["date"]):
                gold = dict(t, path=g)
        pass_sha, fail_sha, how = flip_range(repo, ledgers, test)
        lo = pass_sha or (gold or {}).get("sha")
        cand: List[dict] = []
        for c in commits:
            if module and not any(module_of(p) == module for p in
                                  slices[c["sha"]]["source"]):
                continue
            if lo and not sh_ok("git", "-C", str(repo), "merge-base",
                                "--is-ancestor", lo, c["sha"]):
                continue
            if fail_sha and not sh_ok("git", "-C", str(repo), "merge-base",
                                      "--is-ancestor", c["sha"], fail_sha):
                continue
            cand.append(c)
            suspects_by_commit.setdefault(c["sha"], []).append(test)
        reg = registered_tests(repo, module) if module else {}
        dead.append({
            "test": test, "verdict": verdict, "attribution": how,
            "flip_range": {"passing_at": pass_sha, "failing_at": fail_sha},
            "golden": gold,
            "golden_is_upstreams": bool(gold and not gold["ours"]),
            "registered": {k: (name in v) for k, v in reg.items()},
            "suspects": [{"sha": c["short"], "subject": c["subject"]}
                         for c in cand],
        })

    # --- which modules were actually MEASURED, not merely sampled ---
    #
    # FORK_PATCH_AUDIT §5: "Run the complete oracle set before saying 'nothing
    # went red'. One test is a guess; the full set is a measurement." So a module
    # counts as measured only when EVERY test its build systems register has a
    # verdict. A partial ledger that promoted a commit out of COULD-NOT-MEASURE
    # would reintroduce the defect this program exists to remove, one level down:
    # "no failure found" from four tests out of ninety-two is not "breaks
    # nothing", it is four tests out of ninety-two.
    measured_modules: Set[str] = set()
    for mod in {t.split(":")[0] for t in verdicts if ":" in t}:
        reg = registered_tests(repo, mod)
        names = {f"{mod}:{n}" for n in (reg["cmake"] | reg["bazel"])}
        if names and names <= set(verdicts):
            measured_modules.add(mod)

    # --- per-commit classification ---
    rows = []
    for c in commits:
        sl = slices[c["sha"]]
        flags: List[str] = []
        broke = suspects_by_commit.get(c["sha"], [])
        ships = [p for p in sl["tests"] if p in sl["added"]]
        if broke:
            flags.append(BROKE)
        if ships:
            flags.append(SHIPS)


        modules = {module_of(p) for p in sl["source"]} - {None}
        # Can we even SAY "breaks nothing"? Only if EVERY module this commit
        # touches had its complete registered test set run.
        #
        # Source that maps to NO module is the same answer, not an exemption.
        # `1bade74e7` changes only `.gitmodules` — it repoints src/sta at another
        # OpenSTA — and an unmappable slice used to fall out of this test with an
        # empty module set and land in NO-ORACLE, i.e. "breaks nothing", about a
        # submodule bump that can change anything the timer touches. An unknown
        # layout must reach COULD-NOT-MEASURE, never a confident bucket derived
        # from a guess.
        unmapped = any(module_of(p) is None for p in sl["source"])
        unmeasured = bool(sl["source"]) and (
            unmapped or not (modules <= measured_modules))

        if unmeasured:
            flags.append(UNMEASURED)

        # PRECEDENCE, and the reason for it: only NO-ORACLE is a claim that needs
        # a measurement. SHIPS-ORACLE says what the commit CONTAINS, which git
        # answers on its own, so an unmeasured module does not erase it — the
        # flags carry both. NO-ORACLE says "breaks nothing", which git cannot
        # answer, so it yields to COULD-NOT-MEASURE every time.
        if not sl["source"]:
            bucket = EMPTY_SLICE
        elif broke:
            bucket = BROKE
        elif ships:
            bucket = SHIPS
        elif unmeasured:
            bucket = UNMEASURED
        else:
            bucket = NO_ORACLE
        rows.append({
            "sha": c["short"], "date": c["date"], "subject": c["subject"],
            "adds": sum(1 for _ in sl["source"]), "bucket": bucket,
            "flags": flags or [bucket], "broke": broke,
            "ships_tests": ships, "modules": sorted(modules),
        })

    # --- floor or total ---
    all_registered: Set[str] = set()
    touched = {m for r in rows for m in r["modules"]}
    for m in sorted(touched):
        reg = registered_tests(repo, m)
        for name in reg["cmake"] | reg["bazel"]:
            all_registered.add(f"{m}:{name}")
    covered = all_registered & set(verdicts)
    complete = bool(all_registered) and covered == all_registered

    return {
        "program": PROGRAM, "repo": str(repo), "head": head,
        "upstream_ref": upstream_ref,
        "ledgers": [{"path": l["_path"], "measured_at_sha": l.get("measured_at_sha"),
                     "measured_by": l.get("measured_by"),
                     "measured_on": l.get("measured_on"),
                     "verdicts": len(l["verdicts"])} for l in ledgers],
        "counting": "TOTAL" if complete else "FLOOR",
        "coverage": {"registered_tests_in_touched_modules": len(all_registered),
                     "with_a_recorded_verdict": len(covered)},
        "dead_oracles": dead,
        "commits": rows,
        "counts": {b: sum(1 for r in rows if r["bucket"] == b)
                   for b in (BROKE, SHIPS, NO_ORACLE, EMPTY_SLICE, UNMEASURED)},
    }


def render(m: dict) -> str:
    out = [f"{PROGRAM}: {len(m['commits'])} commit(s) of ours on {m['head'][:12]}"]
    if not m["ledgers"]:
        out.append("  NO VERDICT LEDGER GIVEN — question 2 (\"did this commit "
                   "BREAK an existing test?\") was NOT asked.")
    for l in m["ledgers"]:
        out.append(f"  ledger {Path(l['path']).name}: {l['verdicts']} verdict(s) "
                   f"measured at {str(l['measured_at_sha'])[:12]} "
                   f"by {l['measured_by']} on {l['measured_on']}")
    c = m["counts"]
    out.append(f"  {c[BROKE]} {BROKE} · {c[SHIPS]} {SHIPS} · "
               f"{c[NO_ORACLE]} {NO_ORACLE} · {c[EMPTY_SLICE]} {EMPTY_SLICE} · "
               f"{c[UNMEASURED]} {UNMEASURED}")
    cov = m["coverage"]
    out.append(f"  counting: {m['counting']} — "
               f"{cov['with_a_recorded_verdict']}/"
               f"{cov['registered_tests_in_touched_modules']} registered test(s) "
               f"in the touched modules have a recorded verdict")
    if m["dead_oracles"]:
        out.append("\n  DEAD ORACLES (measured FAIL — cannot signal for anyone):")
        for d in m["dead_oracles"]:
            g = d["golden"]
            who = ("upstream's" if d["golden_is_upstreams"] else "ours") if g else "?"
            out.append(f"    {d['test']}  attribution={d['attribution']}")
            if g:
                out.append(f"      golden {Path(g['path']).name} last written by "
                           f"{g['sha'][:9]} ({g['author']}, {g['date']}) — {who}")
            out.append(f"      registered: " + ", ".join(
                f"{k}={'yes' if v else 'NO'}" for k, v in d["registered"].items()))
            for s in d["suspects"]:
                out.append(f"      suspect {s['sha']}  {s['subject'][:70]}")
    broke_rows = [r for r in m["commits"] if r["bucket"] == BROKE]
    if broke_rows:
        out.append(f"\n  {BROKE} — remedy is REGENERATE THE GOLDEN, not "
                   f"\"write a test\":")
        for r in broke_rows:
            out.append(f"    {r['sha']}  {r['subject'][:70]}")
            out.append(f"              breaks: {', '.join(r['broke'])}")
    unm = [r for r in m["commits"] if r["bucket"] == UNMEASURED]
    if unm:
        out.append(f"\n  {UNMEASURED} — no test of these modules was run, so "
                   f"\"breaks nothing\" is unavailable. NOT a pass:")
        for r in unm[:15]:
            out.append(f"    {r['sha']}  {r['subject'][:70]}  "
                       f"[{', '.join(r['modules'])}]")
        if len(unm) > 15:
            out.append(f"    … and {len(unm) - 15} more")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--upstream-ref", default="upstream/master")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--verdicts", action="append", default=[],
                    help="measured verdict ledger; repeat, oldest sha first")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    repo = Path(args.repo)
    if not (repo / ".git").exists():
        print(f"{PROGRAM}: {repo} is not a git checkout — nothing measured",
              file=sys.stderr)
        return 2
    try:
        ledgers = load_ledgers(args.verdicts)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"{PROGRAM}: {e}", file=sys.stderr)
        return 2

    m = build_map(repo, args.upstream_ref, args.head, ledgers)
    if args.json:
        Path(args.json).write_text(json.dumps(m, indent=2) + "\n")
    print(render(m))
    if not m["commits"]:
        print(f"{PROGRAM}: no commit of ours found — nothing measured",
              file=sys.stderr)
        return 2
    return 1 if m["counts"][BROKE] else 0


if __name__ == "__main__":
    sys.exit(main())
