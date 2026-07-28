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
| `GK_FORKS_DIR` | `/home/reyerchu/vibe-ic-forks` | local clones of the fork repos |
| `GK_EDA_CLONE` | `/home/reyerchu/vibeic-eda` | this repo's working checkout |
| `GK_MODE` | `verify` | `verify` (staged) or `promote` (push on green) |
| `GK_RESULT` | `<host>/last_build_result.json` | last tick's result |

## Deployment

Runs on the build host via cron (daily). Runtime output (`reports/`, `ledger/`,
`last_build_result.json`) is host-local and git-ignored — this directory holds
only the version-controlled source + the `FORKS.json` registry. To relocate the
deployment, point the cron entry at a checkout of this repo's `fork-gatekeeper/`
and set the env knobs above.
