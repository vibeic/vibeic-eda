# Fork Patch Audit — method

How to answer "is this patch of ours still doing anything?" on a fork that
carries many patches, without manufacturing a false answer.

Derived from the 2026-08-05 audit of `vibeic/OpenROAD` (54 fork commits) and
the parallel iverilog/ngspice/verilator audits. Every rule below is here
because breaking it produced a confident wrong answer first.

Prose, not a gate. The judgment is the point; a gate here would be one more
proxy standing in for a fact.

---

## 0. The two rules that generalise past any one tool

**0.1 — The population is defined by the mechanism, not by the place you
looked.**

> Every widening that "finds one more" is evidence the enumeration was never
> done.

In the OpenROAD audit this cost three corrections in a row. The question was
"which of our tests are registered with a build system". OpenROAD registers
tests in *three* places depending on module:

```
src/<mod>/CMakeLists.txt            # drt unit tests — one level UP from test/
src/<mod>/test/CMakeLists.txt       # the common case
src/<mod>/test/cpp/CMakeLists.txt   # reached via test/CMakeLists.txt add_subdirectory(cpp)
```

Grepping the first location gave "orphaned". Grepping the second retracted
some of it. Grepping the third retracted the rest. Each widening looked like
progress and was actually evidence the enumeration had never happened. The fix
is to find the *mechanism* (how does a test become registered?) once, then
enumerate from it — not to grep the directory you expect.

Same shape elsewhere in this repo: grepping `_LOCAL_WAIVERS` and missing
`LOCAL_WAIVERS`; searching a file for tables that live behind a runtime
`getattr`.

**0.2 — Define the slice by INSPECTION, not by EXCLUSION.**

To mutate a patch you need its *source* slice — the change minus its own
tests. The tempting definition is subtractive:

```
git show <C> -- . ':(exclude)src/*/test/*' ':(exclude)*/CMakeLists.txt' ':(exclude)*/BUILD'
```

That is "everything that is not a test or a build file", and it is wrong. On
`5b623c3dc` it let `README.md` through, so the "mutation" reverted a README,
nothing went red, and the result was filed as *alive but uncovered*. It was a
**null experiment recorded as a measurement** — the commit has no source at
all; it is a docs-and-tests commit.

Before mutating, look at what the commit actually changes. If the slice is a
README, there is nothing to measure.

---

## 1. The four steps

**Step 1 — blame filter.** Minutes, zero builds. Run it on every commit before
spending a single build.

For each of our commits compare `--numstat` additions against the lines it
still owns in `git blame HEAD`.

- **0% owned** → candidate. **Not a corpse by itself** — see §1.1.
- **<25%** → mostly rewritten; read the rewriter.

**§1.1 — what 0% actually means.** 0% ownership says *the lines are gone*,
which is the **opposite** of dead-code-still-present. Both of these produce
0%:

- *rewritten in place* — a later commit reimplemented the same behaviour, the
  code is live, attribution moved. (`e4a73c29e`, absorbed by `d791851be`
  "reconcile the two FL1 implementations into one".)
- *removed and superseded* — the real corpse shape.

Step 1 cannot tell them apart. **Only Step 3 can say whether what replaced
them still needs them.** A fork where old code were still present *and* dead
would look identical at Step 1.

**Step 2 — reverse-apply feasibility.** Minutes.
`git apply -R --check` each commit's source slice.

🔴 **Judge by exit code, not by grepping for "FAILED"** — `"Hunk #1
succeeded"` contains the word.

A conflict is not a failure of the method; it means a later patch rewrote that
region, and it is your **supersession shortlist**. On OpenROAD: 8 reversible,
3 empty-slice, 13 conflict.

**Step 3 — mutation, one patch at a time.**
Revert the **source slice only**. Reverting a patch's own test guarantees
nothing goes red and proves nothing.

Where reverse-apply conflicts, hand-craft a **surgical mutation that disables
the mechanism** rather than mangling the file — `return -1` from its
recogniser, `if (0)` on its branch. Good mutations leave the machinery running
and only remove the verdict:

```c
// fin: engine still measures density and still reports windows,
//      it simply can never declare a violation
if (false && r.min_limit >= 0.0 && r.density < r.min_limit) { ... }

// psm: engine still computes J and still reports segments
r.violated = false;              // was: r.violated = r.j > r.limit;

// psm transient: solver still runs every timestep and computes each extreme,
//                but only ever RECORDS step 1
if (k == 1) result.worst_value = step_extreme;
```

That is the *uncertain-becomes-pass* shape — the defect the code exists to
prevent — so an oracle that does not catch it is not guarding the rule.

**Step 4 — the rule that makes it honest.**
**Nothing red ⇒ dead OR untested, and you must say which.** Rebuild with the
patch out and re-run its original FAIL→PASS proof. Proof fails ⇒ alive but
**uncovered** (file a test). No proof exists to run ⇒ **NOT-RUN**, never "no
failure found".

---

## 2. Minimum evidence before calling a patch superseded

All four, or report and do not revert:

1. 0% blame ownership **or** a later commit reimplementing the same intent;
2. reverting leaves the suite green;
3. the replacement's own test still passes with it gone;
4. a spec/LRM basis if behaviour differs.

🔴 Criterion 2 is **unestablishable** for a patch with no oracle — that is not
the same as satisfied. A patch in the no-oracle set can never clear this bar,
and **an unverifiable revert of a working guard is a worse trade than carrying
a redundant one.**

---

## 3. Verdict vocabulary

Use these words; they are the distinctions the work actually needed.

| Verdict | Means |
|---|---|
| `LOAD-BEARING` | reverting turns an oracle's **assertions** red. Strongest. |
| `LOAD-BEARING (compile)` | reverting breaks the build because the patch's own test references a symbol the patch provides. **Weaker** — proves the source is *referenced*, not that its behaviour is right. Always label it as such. |
| `ALIVE BUT UNCOVERED` | full oracle set run, nothing red, but the code is live and has consumers. Action: file a test. |
| `EMPTY-SLICE` | the commit is entirely tests/goldens/docs/build files. Nothing to mutate. Not a finding. |
| `UNUSED-API` | real source, but no in-tree caller. Reverting breaks nothing because nothing uses it. A test here would be a test for a function nobody calls — **worse than no test, because it looks like coverage.** |
| `REDUNDANT-WITH-UPSTREAM` | upstream now carries the same fix. A **third category**, not a corpse: superseded by upstream, not by a later patch of ours. |
| `UNTESTED` | no signal, and no proof available to re-run. |
| `NOT-RUN` | not attempted. Say so; never let it read as a pass. |

---

## 4. Harness traps

Each of these produced a green that meant nothing.

- **`-no_splash`.** Without it every OpenROAD test "fails" on a 4-line startup
  banner. Cost: 17/17 false FAIL.
- **`TEST_CHECK_PASSFAIL=True`.** OpenROAD has three judging modes, not two:
  golden-diff, passfail last-line (`^(pass|OK)`), and none. A no-golden test
  run without the passfail flag runs with **no judgment at all** and can only
  "pass". Cost: 3 tests scored green while unjudged.
- **`puts "FAIL"` exits 0.** An exit-code oracle is vacuous for any test that
  reports failure by printing. 5 of the OpenROAD fork's tcl tests do this;
  only one uses `error "FAIL..."`. Generalises: **any harness that judges by
  exit code over a suite that signals by printing is measuring nothing.**
- **`eval` moving cwd.** A build step that `cd`s elsewhere leaves `git apply`
  running outside the repo, the restore silently fails, and the next mutations
  compile against the wreck. Use absolute `git -C <repo>`. Cost: 3 discarded
  BUILD-FAILEDs. This is the second independent occurrence in one day.
- **Killed-build leftovers are asymmetric across A/B.** A killed run only
  poisons the tests it reached, so the contamination itself is uneven. Clean
  build dirs before any differential comparison.
- **`pgrep -f "<script>"` matches its own command line.** A waiter can spin
  against itself indefinitely, indistinguishable from "still busy".
- **Never `cp -a` a git worktree.** The copy inherits the original's `.git`
  pointer, so `git status` inside the copy silently reports the **original**.
  Use `git worktree add`, or compare with `git archive HEAD` + byte-diff.

🔴 **CONTROL green before AND after every batch**, or none of it is
trustworthy. iverilog reported four false `BUILD-FAILED` findings from a
half-reverted file plus a `.rej` that every later mutation compiled against;
they were discarded only because a control run existed. Restore by
**forward-applying the inverse patch, never `git checkout -- <file>`** — that
is not an undo, it is "make this file equal HEAD", and it takes every
uncommitted change with it.

---

## 5. Honesty rules

- **NOT-RUN stated first**, before any result. It is the only thing that makes
  the positive verdicts mean anything.
- **Run the complete oracle set** before saying "nothing went red". One test
  is a guess; the full set is a measurement.
- **Report positives at the same standard as negatives.** A campaign hunting
  for checks that cannot fire makes it easy to report only absences. Driving a
  mechanism to prove seven oracles *do* fire is the same measurement pointed
  the other way, and it needs saying.
- **State the limit in the same breath as the finding.** "The cluster is
  covered" is supported by a chokepoint mutation; "this individual patch is
  load-bearing" is not.
- **Chokepoint vs per-patch is a property of the code, not the method.** A
  per-engine mutation is per-patch attribution only when each commit owns its
  own engine — `psm/em_signoff.h` does, so `b8230905f` got a real per-patch
  verdict; `fin`'s seven commits all feed one `classifyDensity`, and
  `psm`'s transient pair share seven files, so both are chokepoint-only.
- **Verify claims in commit messages rather than trusting them.** "Their
  measurement core wins and is now the ONLY one" is exactly the sort of claim
  a corpse hides behind. Trace the call path and check.

---

## 6. Scope — read this before running the method anywhere else

🔴 **The method's resolution is bounded by the oracle map.**

Build the patch→oracle map FIRST and publish it before any mutation result.
For each patch: which test would go red if it were reverted, and is that test
in the build system CI actually runs?

On `vibeic/OpenROAD` that map said:

- 54 fork commits, 27 ship an oracle, 27 ship none;
- of the 27 with an oracle, only **4** are visible to the bazel build that
  produces the shipped binary;
- ⇒ **50 of 54 patches can be reverted with the shipped-binary CI staying
  green.**

A mutation campaign there measures a minority *by construction*. Worse, a
runner who does not know this collects **confident false negatives**: "nothing
went red" from an absent oracle is indistinguishable from "nothing went red"
from a passing one. That is the false-corpse outcome, and it is why the map
comes first and why the person running the method has to be the person who
built it.

The underlying defect on OpenROAD — the fork's tests are written for the CMake
build while the image builds with bazel, so 29 of 34 oracles never execute in
the shipped pipeline — is tracked as **vibe-ic#813**. Two fork commits
(`bee1cf03c`, `6d97a7917`) already patched instances of it without anyone
noticing the scale, because the CMake run is green and nobody compares the two
target sets.

**Related defect class: a permanently-red oracle is a dead oracle.** Four
OpenROAD tests fail on stale goldens (upstream changed DPL/GPL log formatting).
Their own assertions still pass; only the log-vs-golden diff fails. While that
holds, reverting the patches behind them leaves the suite *exactly as red as
it already was* — indistinguishable, so those patches are unguarded.

🔴 **Do not refresh a golden to fix this mid-campaign.** Refreshing means
accepting current behaviour as correct, which defines away the question the
campaign is asking. It is a deliberate act with its own review: someone has to
look at the new output and say "this is right".
