#!/usr/bin/env python3
"""A test we wrote that no build system can reach has not been written.

WHY THIS EXISTS, AND WHY IT IS FLEET-WIDE
-----------------------------------------
The 2026-08-12 audit of the 18 forks carrying our patches found the SAME defect
in five of them, independently, each discovered by hand:

    magic      9 regression gates in no build system, no CI, no image step
    netgen    13 suites; `make test` exits 0 having run nothing
    ngspice   12 `vibeic_proofs` gates registered nowhere
    klayout    1 C++ unit test + 23 shell harnesses
    OpenSTA    4 C++ oracle gates, never compiled, never run
    sby       33 pytest tests with no target in any build system  (FIXED 2c2f04e)

That is ~1,300 lines of FAIL->PASS proof in netgen alone, executed by nothing.
It is also not new: OpenROAD's `check_test_registration_parity` exists because
27 of ITS tests had stopped being reachable. Six forks, one shape, six separate
hand discoveries -- which is the definition of something that belongs in a
program rather than in a reviewer's memory.

The cost is not the wasted effort. It is that `make test` returning 0 is an
ACTIVELY DECEPTIVE signal: a post-merge check written as `make test` would go
green on a fork whose entire regression suite is inert, and the fix those tests
guard could regress with nothing to notice.

WHAT IT ASKS
------------
For every file WE ADDED that is a test ENTRY POINT: does anything in the fork's
build/CI/test surface name it, or name a directory that would collect it?

  subject      `git diff --diff-filter=A <upstream>...HEAD`, filtered to entry
               points -- `run*.sh`, `check_*.sh`, `test_*.py`, `*_test.py`,
               `test_*.tcl`, `*_test.cc`, `*_gate.cc`, `*.sby`, or any added
               file carrying the executable bit. FIXTURES ARE NOT THE SUBJECT:
               a `.lef`, a `.spice` or a waiver `.json` is consumed BY a test
               and is correctly named nowhere. Counting them produced 90
               "orphans" in netgen where 15 is the true number.
  surface      every tracked Makefile / *.mk / CMakeLists.txt / BUILD / *.bzl /
               meson.build / setup.py / pyproject.toml / configure.ac /
               Makefile.am / .github workflow / conftest.py / *.list /
               run_all.sh / *.cmake in the tree.
  reached      the file's full path, its basename, or its parent DIRECTORY PATH
               appears in that surface.

THE PARENT MATCH IS A DIRECTORY PATH, NEVER A BARE DIRECTORY NAME, and that is
the whole difference between this working and not. Matching on the parent's
basename made `test/crosstalk_eco_gate.cc` look covered because the word "test"
appears in every CMakeLists in the repo -- OpenSTA and klayout both reported a
confident ZERO while carrying 4 and 25 orphans respectively. A false green, in
the one direction that must never happen. (OpenROAD's own checker records the
same lesson from a different angle: matching by source intersection made two
genuinely-unwired tests look covered.)

WHAT IT DOES NOT CLAIM
----------------------
Reachable is not the same as RUN. netgen's `make test` names its directories and
still executes nothing, because the target is empty. This check answers the
first question only -- is there a path at all -- and a fork can pass it while
its entry point is never invoked. That second question needs the build, which
this deliberately does not need: like the three OpenROAD checks, it is static
and runs in a merge worktree in seconds, which is what makes it affordable on
every tick. Saying so here rather than letting a PASS imply more than it means.

THE WAIVER IS A LIST OF NAMES, NEVER A COUNT
--------------------------------------------
The five forks above carry known orphans today and cannot all be fixed at once.
So the gate ships with a waiver that NAMES each file and the issue tracking it.
A count would let a new orphan hide behind a fixed one; naming them means the
gate fails the moment a SIXTH appears, and an entry that gets fixed must be
deleted from the waiver in the same commit -- an expired waiver is itself a
failure, so the list cannot quietly outlive its truth.

Exit: 0 every entry point is reachable (or waived by name)
      1 at least one unwaived orphan, or a waiver that no longer applies
      2 the question could not be put (no clone, no upstream ref, no surface)
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import progress_watchdog as watchdog  # noqa: E402

DEFAULT_FORKS_DIR = Path(os.environ.get("GK_FORKS_DIR") or "/home/reyerchu/vibe-ic-forks")

#: Files that constitute a fork's build / CI / test-runner surface.
RUNNER = re.compile(
    # Makefile.in is the CHECKED-IN source for an autotools project (configure
    # only substitutes @VARS@ into it), so a gate invoked there is genuinely
    # reachable. Omitting it made this checker report a false FAIL against
    # magic, whose test/check_gate_registration.py is wired at Makefile.in:204 --
    # i.e. it accused a fork of exactly the defect the fork had just fixed.
    r"^(Makefile|GNUmakefile|CMakeLists\.txt|BUILD|meson\.build|setup\.py"
    r"|pyproject\.toml|configure\.ac|Makefile\.am|Makefile\.in|conftest\.py"
    r"|run_all\.sh)$"
    r"|\.(mk|bazel|bzl|yml|yaml|list|cmake)$"
    r"|runtests")

#: A test ENTRY POINT — something that RUNS. Not a fixture it consumes.
ENTRY = re.compile(
    r"^run.*\.sh$|^check_.*\.sh$|^test_.*\.py$|.*_test\.py$|^test_.*\.tcl$"
    r"|.*_test\.(cc|cpp)$|.*_gate\.(cc|cpp)$|.*Test\.(cc|cpp)$|\.sby$")

#: Below this length a parent path is too generic to be evidence ("test",
#: "tests" appear in every build file in every repo).
MIN_PARENT = 5

#: pytest COLLECTS BY CONVENTION, so a `test_*.py` can be reachable while
#: nothing names it -- but only in a project that actually invokes pytest. That
#: distinction is the whole rule, and it is measured, not assumed:
#:
#:     sby @1230919 (before the fix)   pytest in the surface: 0   -> unreachable
#:     sby @2c2f04e (after the fix)    pytest in the surface: 1   -> reachable
#:     cocotb-coverage                 tox.ini invokes pytest: 1  -> reachable
#:     pyuvm                           4 places invoke pytest     -> reachable
#:
#: Without it, cocotb-coverage and pyuvm reported 7 and 2 orphans whose tests
#: demonstrably DO run -- pyuvm's are the very tests the audit found turning the
#: suite red on Python 3.9, which they could not do if nothing ran them.
PYTEST_COLLECTED = re.compile(r"^test_.*\.py$|.*_test\.py$")

#: A test-ish path segment. Used to keep the executable-bit fallback honest:
#: Trilinos' `packages/*/doc/build_docs` and `doc/DoxyfileWeb` carry the
#: executable bit and are not tests, and the epetra-restored merge reintroduced
#: sixteen of them.
TESTISH_PATH = re.compile(r"(^|/)(tests?|proofs?|vibeic[_-]proofs?|ivtest|regress\w*)(/|$)",
                          re.I)

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "COULD-NOT-CHECK"


GIT_STALL_GRACE_S = float(os.environ.get("GK_STATIC_GIT_STALL_GRACE", "60"))
GIT_PROGRESS_POLL_S = float(os.environ.get("GK_STATIC_GIT_PROGRESS_POLL", "1"))


@dataclass(frozen=True)
class GitMeasurementUnknown(RuntimeError):
    """A Git question produced no answer; absence must not be inferred from it."""

    command: tuple[str, ...]
    rc: int
    outcome: str
    detail: str

    def as_meta(self) -> dict:
        return {
            "reason": (f"git {' '.join(self.command)} did not answer; "
                       "test reachability is unknown, not empty"),
            "measurement": {
                "kind": "git_command",
                "command": list(self.command),
                "outcome": self.outcome,
                "exit_code": self.rc,
                "detail": self.detail,
                "total_runtime_ceiling_seconds": None,
            },
        }


def _git(clone: Path, *args) -> str:
    """Run a local graph query under a progress lease, never a wall-time verdict."""
    command = tuple(str(a) for a in args)
    result = watchdog.run_supervised(
        ["git", "-C", str(clone), *command],
        stall_grace_s=GIT_STALL_GRACE_S,
        poll_s=GIT_PROGRESS_POLL_S,
        output_progress=True,
        process_tree_cpu_progress=True,
    )
    if result.outcome != "natural" or result.rc != 0:
        detail = (result.err or result.out or result.outcome).strip()
        raise GitMeasurementUnknown(
            command=command,
            rc=result.rc,
            outcome=result.outcome,
            detail=detail[:500],
        )
    return result.out


def upstream_ref(clone: Path) -> str | None:
    """The fork's upstream mainline, resolved from the clone rather than guessed."""
    out = _git(clone, "branch", "-r", "--list", "upstream/*")
    refs = [l.strip() for l in out.splitlines() if l.strip() and "->" not in l]
    for want in ("upstream/master", "upstream/main"):
        if want in refs:
            return want
    return refs[0] if refs else None


def our_entry_points(clone: Path, up: str) -> list[str]:
    out = _git(clone, "diff", "--name-only", "--diff-filter=A", f"{up}...HEAD")
    keep = []
    for f in out.splitlines():
        f = f.strip()
        if not f:
            continue
        p = clone / f
        if ENTRY.search(os.path.basename(f)):
            keep.append(f)
        elif (p.is_file() and os.access(p, os.X_OK)
              and p.suffix in (".sh", ".py", "")
              and TESTISH_PATH.search(f)):
            keep.append(f)
    return keep


def declared_checks(tool: str, forks_json: Path) -> str:
    """The fork's `post_merge_check` commands, as text.

    THIS IS PART OF THE SURFACE, and leaving it out is a false red. A check
    declared in FORKS.json runs TWICE -- `daily_merge` runs it in the merge
    worktree before a push, and `check_fork_selftests` runs it every tick on the
    clone the image is built from. That is a stronger guarantee than most build
    wiring, not a weaker one: it executes daily whether or not anyone merged.

    Found immediately: OpenROAD's converter unit test was registered here (it is
    pure Python, needs no build, and passes in under a second) and the gate went
    on reporting it unreachable, because FORKS.json lives in vibeic-eda and this
    function only ever read the fork's own tree.
    """
    try:
        data = json.loads(forks_json.read_text())
    except (OSError, ValueError):
        return ""
    for entry in data.get("forks", []):
        if entry.get("tool") == tool:
            return json.dumps(entry.get("post_merge_check") or [])
    return ""


def surface(clone: Path) -> tuple[str, int]:
    """The concatenated text of every build/CI/test-runner file, and how many."""
    out = _git(clone, "ls-files")
    text, n = [], 0
    for rel in out.splitlines():
        if not RUNNER.search(os.path.basename(rel)) and not RUNNER.search(rel):
            continue
        try:
            text.append((clone / rel).read_text(encoding="utf-8", errors="replace"))
            n += 1
        except OSError:
            continue
    return "\n".join(text), n


#: Scripts that GENERATE a directory's build file. A test sitting beside one is
#: collected by a glob rather than named, which is the same shape as pytest's
#: convention one level in.
#:
#: MEASURED 2026-08-12, and it was a false positive of mine. yosys's two shell
#: tests were reported unreachable; in fact `tests/Makefile` runs
#: `cd $* && python3 generate_mk.py` per directory, and BOTH
#: techmap/generate_mk.py and various/generate_mk.py pass `--bash`, which globs
#: `*.sh`. Generating the Makefiles proves it: `grep -c "^merge_liberty.sh:"`
#: and `"^read_activity_err.sh:"` are each 1. They were collected all along.
GENERATOR = re.compile(r"^(generate_mk|gen_tests?_makefile|configure|autogen)\.(py|sh)$")


def generated_dir(clone: Path, parent: str, blob: str) -> bool:
    """Does a generator live in this directory, and is it named in the surface?

    Both halves are required. A generator the surface never invokes generates
    nothing, and a name in the surface that is not beside the test says nothing
    about the test.
    """
    d = clone / parent
    if not d.is_dir():
        return False
    for child in d.iterdir():
        if child.is_file() and GENERATOR.match(child.name) and child.name in blob:
            return True
    return False


def unreachable(clone: Path, declared: str = "") -> tuple[str, list[str], dict]:
    up = None
    try:
        up = upstream_ref(clone)
        if up is None:
            return UNKNOWN, [], {"reason": "no upstream/* ref in the clone"}
        eps = our_entry_points(clone, up)
        if not eps:
            # This is a PASS only because the diff command completed naturally
            # and authoritatively returned an empty set. A failed/stalled diff
            # raises above and can never reach this branch.
            return PASS, [], {"upstream": up, "entry_points": 0, "surface": 0}
        blob, nfiles = surface(clone)
    except GitMeasurementUnknown as exc:
        meta = exc.as_meta()
        if up is not None:
            meta["upstream"] = up
        return UNKNOWN, [], meta
    blob += "\n" + declared          # a declared check is a path, and a daily one
    if nfiles == 0 and not declared.strip():
        return UNKNOWN, [], {"upstream": up, "entry_points": len(eps),
                             "reason": "no build/CI/test file found — nothing to check against"}
    runs_pytest = "pytest" in blob
    orphans = []
    for f in eps:
        parent = os.path.dirname(f)
        if f in blob or os.path.basename(f) in blob:
            continue
        if parent and len(parent) >= MIN_PARENT and parent in blob:
            continue
        if runs_pytest and PYTEST_COLLECTED.match(os.path.basename(f)):
            continue                      # collected by convention — see the note above
        if parent and generated_dir(clone, parent, blob):
            continue                      # a generator in the surface builds this dir
        orphans.append(f)
    state = FAIL if orphans else PASS
    return state, orphans, {"upstream": up, "entry_points": len(eps), "surface": nfiles}


def load_waivers(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--forks-dir", default=str(DEFAULT_FORKS_DIR))
    ap.add_argument("--tool", action="append", help="only these tools")
    ap.add_argument("--forks-json", default=str(HERE / "FORKS.json"))
    ap.add_argument("--waivers", default=str(HERE / "TEST_REACH_WAIVERS.json"))
    ap.add_argument("--json", help="write the full verdict here")
    args = ap.parse_args(argv)

    forks_dir = Path(args.forks_dir)
    try:
        tools = [f["tool"] for f in json.loads(Path(args.forks_json).read_text())["forks"]]
    except (OSError, ValueError, KeyError) as exc:
        print(f"COULD NOT CHECK: cannot read {args.forks_json}: {exc}")
        return 2
    if args.tool:
        tools = [t for t in tools if t in args.tool]

    waivers = load_waivers(Path(args.waivers))
    rows, findings, unknowns, stale = [], [], [], []

    for tool in sorted(tools):
        clone = forks_dir / tool
        if not (clone / ".git").exists():
            continue                       # not cloned here; not this check's question
        state, orphans, meta = unreachable(
            clone, declared_checks(tool, Path(args.forks_json)))
        waived = set(waivers.get(tool, {}).get("files", []))
        live = [o for o in orphans if o not in waived]
        # An absent measurement says nothing about whether an existing waiver
        # expired. Calling every waiver stale when Git failed would turn one
        # UNKNOWN into a fabricated finding.
        gone = ([] if state == UNKNOWN
                else sorted(waived - set(orphans)))
        rows.append({"tool": tool, "state": state, "orphans": orphans,
                     "unwaived": live, "waiver_no_longer_applies": gone, **meta})
        if state == UNKNOWN:
            unknowns.append((tool, meta.get("reason", "")))
        if live:
            findings.append((tool, live))
        if gone:
            stale.append((tool, gone))

    for r in rows:
        mark = r["state"]
        if r["state"] == FAIL and not r["unwaived"]:
            mark = "PASS(waived)"
        print(f"  {mark:<15} {r['tool']:<18} "
              f"entry points {r.get('entry_points', 0):>3}"
              + (f"  unreachable {len(r['orphans'])}" if r["orphans"] else ""))

    n_ep = sum(r.get("entry_points", 0) for r in rows)
    n_or = sum(len(r["orphans"]) for r in rows)
    n_live = sum(len(r["unwaived"]) for r in rows)
    print(f"\ntest reachability: {len(rows)} fork(s), {n_ep} entry point(s) of ours, "
          f"{n_or} unreachable ({n_or - n_live} waived by name, {n_live} NOT waived)")

    if findings:
        print("\nFAIL — a test of ours that no build system can reach:")
        for tool, live in findings:
            print(f"  {tool}:")
            for f in live:
                print(f"      {f}")
        print("  Wire it into the build that produces the artefact, or waive it BY NAME "
              "in TEST_REACH_WAIVERS.json with the issue that tracks it.")

    if stale:
        print("\nFAIL — a waiver that no longer applies (delete it in the commit that "
              "fixed the file, or it will outlive its truth):")
        for tool, gone in stale:
            for f in gone:
                print(f"  {tool}: {f}")

    if unknowns:
        print("\nCOULD NOT CHECK — this is NOT a pass:")
        for tool, why in unknowns:
            print(f"  {tool}: {why}")

    if args.json:
        try:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(json.dumps(
                {"forks": rows, "entry_points": n_ep, "unreachable": n_or,
                 "unwaived": n_live, "unknown": len(unknowns)}, indent=2),
                encoding="utf-8")
        except OSError as exc:
            print(f"  (could not write --json: {exc})")

    if findings or stale:
        return 1
    if unknowns:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
