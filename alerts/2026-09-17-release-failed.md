# release failed on the 2026-09-17 round (run_tick exit 5)

`run_tick` exited **5** (state `ran`) on the 2026-09-17T05:30:01+08:00 round, so no image was published and `build_page` was skipped.

## What the release said

```
failure.summary: ERROR: failed to solve: process "/bin/sh -c git clone \"${OPENROAD_REPO}\" /src  && cd /src && git checkout ${OPENROAD_REF}  && git submodule update --init --recursive --depth 1  && printf 'build --stamp\\nbuild --distdir=/distdir\\n' > /root/.bazelrc  && ./etc/Build.sh -threads=$(nproc)       -prefix=/vibeic-openroad" did not complete successfully: exit code: 1
failure.category: build_failed
failure.stage: tool_build
failure.retryable: False
```

## Why this is a PR and not a page banner

The page cannot report that the page did not update. Until this alarm existed the only signal was `check_publish_silence`, which measures the page going stale — a consequence, counted in DAYS. A build that fails the same way every round could sit for most of a week first.

## What to check

- The archived build output under `.cache/eda-fork-gatekeeper/failed-releases/`.
- Whether the failure is a moving target (an upstream merge advanced something a fixed assertion pinned) — that shape fails at the moment of success and repeats every round until the assertion is fixed.
- Whether the working tree was left with uncommitted pin files; the round's own `recover_pin_only_dirty_tree` handles that at the next start.

---
_Opened by the round itself. Operational alert, not an auto-merge._
