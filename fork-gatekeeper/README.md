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
