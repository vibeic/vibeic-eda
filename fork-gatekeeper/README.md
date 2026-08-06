# fork-gatekeeper

CI/maintenance tooling that keeps the `vibeic` org's forked EDA tools in sync
with their upstreams and rebuilds the `vibeic-eda` Docker image when a fork
advances.

## What it does

1. **Discover** (`discover_forks.py`) — enumerates the vibeic org's forks and
   records each one's upstream parent → `FORKS.json` (the registry: 12 tools,
   e.g. OpenROAD→The-OpenROAD-Project/OpenROAD, klayout→KLayout/klayout, …).
2. **Track & gate** (`gatekeeper.py`, `run_tick.sh`) — checks each upstream for
   a new release; for a candidate, rebases the vibeic fork branch onto the new
   upstream, bumps the `vibeic-eda` Dockerfile ARG, docker-builds the image, and
   smoke-regresses it (`build_and_regress.sh`, `verify_yosys.sh`).
3. **Publish** (`build_page.py`) — renders the fork status page for the site.

### One derivation of the headline counts

A tick publishes the same four numbers — clearly-safe / already-carried / previously
decided / needing a human — in three places: the daily report, the per-tool assessment
table, and the review PR's body. They all come from `assess_release.summary_counts()`
and nowhere else. Each site used to derive them for itself and two of the three answered
by subtraction, so one tick could publish three different triage results for one range
(vibeic/vibeic-eda#7). Before anything is written the rendered documents are parsed back
and compared (`assess_release.cross_check`); a tick whose documents disagree publishes
NOTHING and exits non-zero. `python3 gatekeeper.py --verify [date]` runs the same
comparison over an already-published day — use it after any manual re-assessment, which
re-renders the assessment under its date-stamped filename and leaves the report that
summarised the previous one in place.

### One process may write the production state

There is ONE production cache, ONE ledger directory and ONE reports directory
(`~/.cache/eda-fork-gatekeeper`), and every guard above reads them. Until
vibeic/vibeic-eda#12 any process that imported these modules wrote them: on 2026-07-28
the cron ran 05:30:01→05:32:30 and the assessment cache gained an entry stamped 07:07:21
from a non-cron checkout, recorded nowhere. A poisoned cache entry does not make the
day's documents disagree — it makes them agree on the wrong thing.

`gk_state.py` now holds that policy for all five modules. **Reads are unchanged**:
`--verify`, `build_page.py` and a by-hand `assess_release.py <tool>` all still see the
production state, and a by-hand assessment still replays the cache for free. **Writes to
the production locations require the process to say it is the production runner** —
`run_tick.sh` exports `GK_PRODUCTION_WRITER=1`, and nothing else does. Every state file
written now carries a `written_by` block (checkout, commit, dirty, entrypoint, pid, host,
and whether it declared itself), stripped again at the publish boundary so it never
reaches the public monitor page.

Running anything by hand:

```
GK_STATE_DIR=/tmp/gk python3 gatekeeper.py          # own state, no permission needed
python3 build_page.py --out /tmp/page.html          # render, don't publish
GK_PRODUCTION_WRITER=1 python3 gatekeeper.py        # write the shared one on purpose
```

## Reviewer-side gate: redundancy precheck (`pr_precheck.py`)

Before landing ANY fork PR, run:

```
python3 pr_precheck.py vibeic/<tool> <pr#>
```

`mergeable=CLEAN` does **not** mean a PR is worth landing. A PR authored against
an OLDER base can duplicate a fix the fork's own working line has ALREADY landed
under a different commit — it merges cleanly and still adds dead-weight (2026-07-21:
2 of 5 fork PRs were exactly this — iverilog #1 duplicated `bedf375e9` closing
vibe-ic#125; yosys #1 duplicated `26bb283e58` closing vibe-ic#124). The tell is
cheap: the PR is BEHIND its base, and a base-only commit already Closes the same
issue. `pr_precheck.py` computes both from `gh api` (no clone, no mutation) and
emits `OK` (0) / `REVIEW` (1, base advanced — re-test on the rebased tree) /
`REDUNDANT_RISK` (2, base already closes a shared issue — prove it, then land
test-only or reject the code). Run it as the first step of every land.

## Modes (staged rollout)

`GK_MODE=verify` (default) proves the rebuild without touching production;
`GK_MODE=promote` fast-forwards the fork branch and pushes the new image on
green. Wired via `regression.json`.

## Environment knobs

| var | default | meaning |
|---|---|---|
| `GK_STATE_DIR` | `~/.cache/eda-fork-gatekeeper` | where cache/ledger/reports live |
| `GK_PRODUCTION_WRITER` | unset | declares this process the production runner, so it may WRITE the shared state above (`run_tick.sh` sets it; nothing else should) |
| `GK_FORKS_DIR` | `/home/reyerchu/vibe-ic-forks` | local clones of the fork repos |
| `GK_EDA_CLONE` | `/home/reyerchu/vibeic-eda` | this repo's working checkout |
| `GK_MODE` | `verify` | `verify` (staged) or `promote` (push on green) |
| `GK_RESULT` | `<host>/last_build_result.json` | last tick's result |
| `GK_ROUND_KEEP_DAYS` | `400` | how long a FULL retained round report is kept. The narrow per-round index is never pruned — see below |

## Deployment

Runs on the build host via cron (daily). Runtime output (`reports/`, `ledger/`,
`last_build_result.json`) is host-local and git-ignored — this directory holds
only the version-controlled source + the `FORKS.json` registry. To relocate the
deployment, point the cron entry at a checkout of this repo's `fork-gatekeeper/`
and set the env knobs above.

## The 05:30 round on 8HD-d, and the one decision the script refuses to make

`run_0530.sh` is the single cron entry at 05:30 Asia/Taipei on 8HD-d
(192.168.1.112). It runs `daily_0530.py --apply` first — upstream into each
fork's one line, our branches into that line, then prune — and only afterwards
the pre-existing `run_tick.sh`. Two pipelines, one entry, so the order is
guaranteed; the wrapper holds its own lock and captures each exit code directly
rather than through a pipe.

### Step 2b — the gatekeeper AI decides merge vs cherry-pick

Owner ruling, 2026-07-31: *"Merge 或者是 Cherry-pick 的話，是需要用到 AI 的，
用程式直接判斷是不夠的。"*

`daily_0530.py` deliberately does not resolve a merge conflict. It used to stop
there and print `needs_human`, which meant a conflicted branch simply sat while
the six steps reported themselves complete — the round finished and the work it
exists to consolidate did not.

So the script now does the mechanical half and hands the judgement half to this
host's gatekeeper. For each conflict it collects the evidence a decision needs
**before** aborting the merge, because aborting destroys it: the conflicting
files, our commits on that branch with their subjects and touched files, and
git's own message. Those land in `$GK_STATE_DIR/ai_decisions_pending.json` and
`ai_decisions_brief.txt`, and the brief is passed straight to a `claude -p`
turn. **That turn is the gatekeeper.** The call blocks — detaching it would end
the process before the turn produced anything.

The brief is self-contained: a gatekeeper turn needs no prior knowledge of this
document to do the job. It states the per-case evidence, asks for MERGE /
CHERRY-PICK / DECLINE with a reason, and carries the rules that bind the step:

* never force-push — report a rejection instead;
* never delete a branch that is the head of an **open upstream PR**. `step4_prune`
  guards this by asking GitHub, and prunes nothing at all when the API is
  unreachable. On 2026-07-31 the un-guarded prune closed our only upstream PR
  (steveicarus/iverilog#1455) by deleting its head branch;
* resolving a conflict by dropping our fix **is** a decision to abandon that fix
  — deliberate and recorded, or not at all;
* repo artifacts are English only.

`--no-ai` writes the brief without invoking, which is what a dry run does. A case
nobody decided still exits 1; a case the gatekeeper decided is decided.

Exercised end-to-end against a synthetic conflicting fork (`GK_FORKS_DIR`
overrides the forks root for exactly this reason — a step that only fires on a
real conflict is not tested by a morning that had none). Measured: the merge
landed on master naming the choice it made, our fix won the conflict, the source
branch was untouched, no branch was deleted, and when the push failed the turn
reported it rather than inventing a remote or forcing.

### "Has this ever fired?" — the retained round record (`round_record.py`)

`daily_0530.json` was written to ONE path and overwritten every morning, so the
only record of what a round decided was the record of what the **most recent**
round decided. Answering that question for #82 instead cost a reconstruction
from GitHub's Events API (~300 events/repo, so the busiest forks were already
down to a ~1-day window) and each clone's reflog (expires; a `git gc` can take
it) — and 14 of 36 forks were not measurable that way at all (#85).

Every round now also writes, under `$GK_STATE_DIR/rounds/`:

* `<stamp>/daily_0530.json` — the full report, kept `GK_ROUND_KEEP_DAYS` days;
* `index.jsonl` — one line per (round, fork), append-only and **never pruned**.
  A bounded store cannot answer "has this ever fired" beyond its bound, which
  is the failure being fixed. Measured 436 bytes/row over the real 36 forks:
  5.7 MB/year, 57 MB/decade.

A row carries the round's **observations**, not just its verdict — in
particular `confirmed_by` (`fetch_moved_ref` | `ls_remote` | `null`). A row
claiming `already current` with `confirmed_by: null` is a round that could not
tell, published as a round that could, and it is one `grep` away:

```
round_record.py --fired [--on YYYY-MM-DD]   # 0 none, 1 found, 2 no record kept
round_record.py --coverage                  # what window the record covers
```

Exit code 2 is not a spare: a window the record does not cover must not print
the same answer as a window it covers and found nothing in. Retention begins at
the first round after this landed; earlier mornings are **NO RECORD KEPT**,
which the first line of the index says in as many words.

Retention is best-effort by construction — `round_record.write` returns its
errors instead of raising, because retention that could take a round down would
be a worse defect than the one it closes.

## Merging upstream into a "restoration" branch (e.g. Trilinos epetra-restored)

A handful of forks carry a branch that exists to **revert** a class of upstream
commits — `vibeic/xyce-trilinos-17.2-epetra-restored` reverts the series that
removed the Epetra stack, because Xyce (also shipped) needs it. That branch's
NAME describes a permanent disagreement with upstream, and the first instinct
is to treat it as too delicate to touch automatically. It is not: `git log
--merges` on the branch shows upstream gets merged into it routinely (`owner
ruling 2026-07-29`), the same as any other fork. What changes is that **every
merge needs one extra audit step**, not that the merge is off-limits.

Measured 2026-08-07, 94 incoming commits / 1749 files: only 1 commit actually
reintroduced a reverted removal. The procedure that found it, cheaply:

1. `git merge --no-edit --no-commit upstream/<branch>` in a scratch worktree.
2. Derive the REAL protected-package list from the original restoration
   commits, not from memory — `git diff --name-only <before> <after>` over the
   commit range that did the original revert-and-restore. Guessing the package
   list from the branch name undercounts it (this fork's real list is 15
   packages, not the 7 named in the pin comment: also amesos2, anasazi, belos,
   nox, shylu, stokhos, thyra, trilinoscouplings).
3. `git diff --name-only HEAD | grep -E "^packages/($PKGS)/"` — which of THIS
   merge's files land inside that list.
4. For each: `git diff HEAD -- <file> | grep -iE epetra`. Empty means the file
   is in a protected package but the incoming change has nothing to do with
   Epetra — ordinary upstream work, safe to accept as-is. Non-empty means read
   it; if it removes Epetra content, find the offending commit
   (`git log --oneline HEAD..upstream/<branch> -- <file>`) and `git revert` it
   on top of the merge, using the SAME commit-message convention the original
   restoration used (`Revert <sha> '<upstream message>' (<package> ...)`).
5. Re-run step 3-4 until clean, THEN push.

This is cheap (a `git diff`/`grep` pass over a few dozen candidate files, not a
build) and it is the step that makes "merge routinely" safe — skipping it is
what would make the branch dangerous, not merging in the first place.
