# fork page has not published for 2 consecutive rounds

## The fork page has stopped publishing

`build_page` has not published for **2 consecutive rounds** (threshold 2).

* first silent round: `2026-09-27T09:17:51+08:00`
* latest silent round: `2026-09-27T12:34:04+08:00`

https://vibeic.ai/eda-forks.html is therefore serving the numbers from the last round that did publish. The page is not wrong by accident -- the round preserves the last good page on purpose when it cannot certify a new one -- but a reader cannot tell a quiet week from a stuck pipeline, which is the whole reason this PR exists.

### Why each round declined

* `2026-09-27T09:17:51+08:00` — current-round fork gap is open or NORECORD (rc=1)
* `2026-09-27T12:34:04+08:00` — release tick is not authorized/current (rc=6)

Each reason is the round's own verdict, not a diagnosis. Several different reasons across a streak usually means the pipeline is failing in more than one place, not that one bug recurs.
