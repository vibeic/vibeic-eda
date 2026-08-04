#!/usr/bin/env python3
"""Per-round retention for the 05:30 round (vibeic-eda#85).

THE DEFECT THIS CLOSES
======================
`daily_0530.json` and `daily-merge.json` were each written to ONE path and
overwritten every morning. The only record of what a round decided was the
record of what the MOST RECENT round decided, so a question of the form "has
this ever fired?" was unanswerable by construction.

Answering exactly that question for vibeic-eda#82 cost a reconstruction from two
instruments that are not ours and do not last: GitHub's PushEvents API (capped
at ~300 events per repo, so the busiest forks were already down to a ~1-day
window) and each clone's own reflog (expires, and a `git gc` can take it). It
produced a usable answer — 22 forks, 91 round instants, 0 confirmed firings —
and it worked only because the program under review was five days old. Next
month the same question over the same window is not reconstructible.

WHAT IS RETAINED, AND WHY IT IS TWO THINGS
==========================================
`rounds/<stamp>/daily_0530.json`   the round's FULL report, verbatim.
                                   Bounded: `KEEP_DAYS` (default 400), because
                                   a full report carries conflict bodies and AI
                                   decision blobs and is the part that grows.

`rounds/index.jsonl`               ONE LINE per (round, fork), append-only,
                                   NEVER pruned.

The split is the point. A bounded store cannot answer "has this EVER fired"
beyond its bound, and that is the exact failure being fixed here; an unbounded
store of everything is a different way to lose. So the narrow row that answers
the question is the one that is kept forever, and the bulky context expires.

THE GROWTH BOUND, MEASURED 2026-08-05 — not estimated
=====================================================
An unbounded store on a cache directory is a promise about disk, so the number
is measured rather than asserted. Both figures below come from the production
report at `~/.cache/eda-fork-gatekeeper/daily_0530.json` (36 forks, the widest
row shape this module emits), not from a guess:

    index row      400 / 436 / 453 bytes   min / mean / max over the real 36
    per round      15.7 kB                 36 rows
    per year       5.7 MB                  365 rounds
    per decade     57 MB

A row is fixed-shape — shas, a state string, a pin — so this scales with the
FLEET, not with what happened that morning; doubling the fleet doubles it and
nothing else does. 57 MB per decade is the price of being able to answer "has
this ever fired", and it is worth paying. Should the fleet reach a size where
it is not, the compaction to reach for is per-(fork, state) run-length
encoding — consecutive identical rows collapse to a first/last pair — which
preserves every answer this module is asked for. It is deliberately NOT done
now: it would be a second representation of a row, and the record would stop
being greppable, which is most of why anyone will trust it.

The FULL reports are the half that varies. The quiet morning measured above is
8.4 kB, so the 400-day bound holds ~3.3 MB; a conflicted morning is larger
(conflict file lists, up to 20 commit subjects per case, 800 bytes of merge
stderr, plus the AI decision blob) and is the reason that store is bounded at
all. No conflicted morning could be measured here, because none was retained —
which is the defect this file closes.

WHAT A ROW HAS TO CARRY — the acceptance test
=============================================
The row must let a reader decide, from the record ALONE, whether a fork the
round called `already current` really was. That needs the round's OBSERVATIONS,
not its conclusion:

    state            what the round said
    fetch            ok | failed | no_answer | no_upstream_remote
    confirmed_by     fetch_moved_ref | ls_remote | null   <- the load-bearing one
    tip_seen         the sha the comparison was made against
    remote_tip       upstream's tip as the ROUND itself observed it
    behind           the number it computed

`confirmed_by: null` on a row claiming currency is precisely "the round could
not tell", and it is a `grep` away. Recording only `state` would reproduce the
original defect one layer up: the next reader would again be unable to separate
*current* from *could not tell*, which is what this whole campaign exists to
remove.

WHAT THIS WILL NOT ANSWER
=========================
Retention starts the first round after this lands. Every round BEFORE that is
still reconstructible only from the perishable instruments above, and 14 of 36
forks were not measurable that way at all. An empty window before the first
`rounds/` stamp means NO RECORD WAS KEPT — it does not mean nothing happened,
and `describe_coverage()` says so in as many words so that nobody has to
remember it.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

#: Full per-round reports older than this are deleted. The index row survives.
KEEP_DAYS = int(os.environ.get("GK_ROUND_KEEP_DAYS", "400"))
ROUNDS_DIRNAME = "rounds"
INDEX_NAME = "index.jsonl"
#: Written into the first index line so a reader can date the boundary between
#: "no record kept" and "nothing happened" without consulting anything else.
COVERAGE_NOTE = (
    "Retention began at this stamp. Rounds before it were never recorded; an "
    "absence earlier than this is NO RECORD KEPT, not evidence of no event.")


def _now() -> datetime:
    return datetime.now().astimezone()


def rounds_dir(state_dir: Path) -> Path:
    return Path(state_dir) / ROUNDS_DIRNAME


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H%M%S%z")


def _fork_rows(report: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """One row per fork. `_`-prefixed keys are round-level, not forks."""
    for name, rep in sorted(report.items()):
        if name.startswith("_") or not isinstance(rep, dict):
            continue
        ev = rep.get("upstream_evidence") or {}
        yield {
            "fork": name,
            "mainline": rep.get("main"),
            "state": rep.get("upstream") or rep.get("error"),
            "fetch": ev.get("fetch"),
            "confirmed_by": ev.get("confirmed_by"),
            "ref": ev.get("ref"),
            "tip_before_fetch": ev.get("tip_before_fetch"),
            "tip_seen": ev.get("tip_seen"),
            "remote_tip": ev.get("remote_tip"),
            "behind": ev.get("behind"),
            "pin": (rep.get("pin") or None),
            "needs_human": bool(rep.get("needs_human")),
        }


def write(report: Dict[str, Any], state_dir: Path,
          when: Optional[datetime] = None,
          keep_days: Optional[int] = None) -> Dict[str, Any]:
    """Retain one round. Returns a summary of what was written.

    Never raises into the round: retention that takes the round down would be a
    worse defect than the one it fixes. Failures are returned, not thrown.
    """
    when = when or _now()
    out: Dict[str, Any] = {"stamp": _stamp(when), "written": [], "error": None}
    try:
        root = rounds_dir(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        d = root / out["stamp"]
        d.mkdir(parents=True, exist_ok=True)
        full = d / "daily_0530.json"
        full.write_text(json.dumps(report, indent=2) + "\n")
        out["written"].append(str(full))

        idx = root / INDEX_NAME
        first = not idx.exists()
        lines: List[str] = []
        if first:
            lines.append(json.dumps({"round": out["stamp"],
                                     "note": COVERAGE_NOTE}) + "\n")
        for row in _fork_rows(report):
            row["round"] = out["stamp"]
            lines.append(json.dumps(row, sort_keys=True) + "\n")
        with idx.open("a") as fh:                # append-only, never rewritten
            fh.writelines(lines)
        out["written"].append(str(idx))
        out["rows"] = len(lines) - (1 if first else 0)
        out["pruned"] = _prune(root, keep_days if keep_days is not None
                               else KEEP_DAYS, when)
    # BROAD ON PURPOSE, and the docstring above is the reason: the caller is a
    # round that must still finish. `OSError` alone would have covered the
    # unwritable-directory case and left the one this call actually INTRODUCES —
    # `json.dumps` on a report carrying something it cannot serialise. The
    # `--json` write in `main_` is optional; this one is not, so a report that
    # the old code would merely have declined to save now had a path to killing
    # the morning. Retention that takes a round down is a worse defect than the
    # one it closes.
    except Exception as exc:                                # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _prune(root: Path, keep_days: int, when: datetime) -> List[str]:
    """Delete FULL round directories older than `keep_days`. The index is never
    touched — it is the part that answers "ever"."""
    gone: List[str] = []
    if keep_days <= 0:
        return gone
    cutoff = when.timestamp() - keep_days * 86400
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            ts = datetime.strptime(d.name, "%Y-%m-%dT%H%M%S%z").timestamp()
        except ValueError:
            continue                    # not one of ours; leave it alone
        if ts < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d.name)
    return gone


# ── reading it back ─────────────────────────────────────────────────────────
def read_index(state_dir: Path) -> List[Dict[str, Any]]:
    idx = rounds_dir(state_dir) / INDEX_NAME
    if not idx.is_file():
        return []
    rows = []
    for ln in idx.read_text(errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return rows


#: A row that CLAIMS currency. The two spellings the round can emit for it.
_CURRENCY_CLAIMS = ("already current", "merged")
#: `--fired` exit code for "the window holds no rows at all". Distinct from 0
#: (asked, and nothing fired) and from 1 (asked, and something did).
RC_NO_RECORD = 2


def unconfirmed_currency_claims(rows: Iterable[Dict[str, Any]]
                                ) -> List[Dict[str, Any]]:
    """Rows where the round reported a fork's upstream state WITHOUT having
    confirmed the ref it compared against.

    This is the vibeic-eda#82 question, asked of the record instead of of
    GitHub's API and a reflog: a row claiming `already current` with
    `confirmed_by: null` is a round that could not tell, published as a round
    that could. On a tree carrying the #82 fix this list should be empty; a
    non-empty one names the fork and the morning.
    """
    hits = []
    for r in rows:
        if "fork" not in r:
            continue                    # the coverage note line
        state = (r.get("state") or "")
        if not any(state.startswith(c) for c in _CURRENCY_CLAIMS):
            continue
        if r.get("confirmed_by"):
            continue
        hits.append(r)
    return hits


def describe_coverage(rows: Iterable[Dict[str, Any]]) -> str:
    rows = list(rows)
    stamps = sorted({r["round"] for r in rows if "round" in r})
    if not stamps:
        return ("NO ROUNDS RETAINED. Nothing has been recorded yet — which is "
                "not the same as nothing having happened.")
    return (f"retained rounds: {len(stamps)}  first {stamps[0]}  last "
            f"{stamps[-1]}\n{COVERAGE_NOTE}")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", default=None,
                    help="defaults to gk_state.state_dir()")
    ap.add_argument("--fired", action="store_true",
                    help="list rounds where a fork's upstream state was "
                         "reported without the ref having been confirmed")
    ap.add_argument("--on", default=None, metavar="YYYY-MM-DD",
                    help="restrict to one day")
    ap.add_argument("--coverage", action="store_true")
    a = ap.parse_args(argv)

    if a.state_dir:
        state = Path(a.state_dir)
    else:
        import gk_state
        state = gk_state.state_dir()

    rows = read_index(state)
    if a.on:
        rows = [r for r in rows if str(r.get("round", "")).startswith(a.on)]

    if a.coverage or not a.fired:
        print(describe_coverage(rows))
    if a.fired:
        # FORK rows only. The coverage note is one line in the same file and is
        # not an observation of anything; counting it would inflate the very
        # number a reader uses to judge how much the answer rests on.
        examined = [r for r in rows if "fork" in r]
        hits = unconfirmed_currency_claims(rows)
        scope = f" on {a.on}" if a.on else ""
        if not examined:
            # THE ANSWER IS NOT `0`. A window with no retained rows is the #85
            # failure itself, and printing "0 unconfirmed currency claims"
            # for it would render NO RECORD KEPT as the reassuring answer —
            # the exact substitution this whole file exists to stop. Its own
            # exit code, because "did not fire" and "cannot say" are different
            # answers and a caller must be able to branch on which it got.
            print(f"NO ROWS RETAINED{scope}. This is not 'nothing fired' — it "
                  f"is 'no record was kept', which is a different answer.")
            print(describe_coverage(read_index(state)))
            return RC_NO_RECORD
        if not hits:
            print(f"0 unconfirmed currency claims{scope} "
                  f"across {len(examined)} retained fork row(s).")
            return 0
        print(f"{len(hits)} unconfirmed currency claim(s){scope}:")
        for h in hits:
            print(f"  {h['round']}  {h['fork']:<24} state={h['state']!r} "
                  f"fetch={h.get('fetch')} confirmed_by=None")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
