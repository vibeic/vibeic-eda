#!/usr/bin/env python3
"""Capture the 05:30 release learning loop as retained, machine-readable facts.

The daily text log answered *what command failed* but not the operational
question that matters next morning: did the round recover by itself, and if it
did not, which delivery hop kept the published image behind?  Worse, the live
release JSON used to keep yesterday's success on failure.

This program runs after `fork_gap_report` on every 05:30 round.  It joins the
release program's terminal record to the measured convergence result and keeps:

* `release-ll/latest.json` — the latest complete learning-loop record;
* `release-ll/rounds/<stamp>.json` — full per-round evidence, 400-day default;
* `release-ll/index.jsonl` — a narrow append-only record, never pruned.

"LL" here is evidence, not generated prose: trigger, control applied, attempts,
outcome, and the final measured lag.  A future gate can therefore learn from a
class of failure without scraping human-oriented logs or inheriting a story.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Optional

import gk_state

KEEP_DAYS = int(os.environ.get("GK_RELEASE_LL_KEEP_DAYS", "400"))


def _read(path: Path) -> tuple[Optional[dict], Optional[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"missing {path.name}"
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"unreadable {path.name}: {type(exc).__name__}: {exc}"
    if not isinstance(value, dict):
        return None, f"{path.name} is not a JSON object"
    return value, None


def _parse_when(value: Optional[str]) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _target(release: dict) -> Optional[str]:
    for key in ("version_candidate", "version"):
        value = release.get(key)
        if isinstance(value, dict) and value.get("to"):
            return str(value["to"])
    return None


def make_record(release: Optional[dict], gap: Optional[dict], round_at: str,
                tick_exit: Optional[int] = None,
                source_errors: Optional[list[str]] = None) -> dict:
    release = release or {}
    gap = gap or {}
    attempts = release.get("compose_attempts") or []
    failed_attempts = sum(1 for a in attempts
                          if isinstance(a, dict) and a.get("exit_code"))
    status = release.get("status") or "unmeasured"
    failure = release.get("failure") if isinstance(release.get("failure"), dict) else {}
    sync_lag = gap.get("q1_sync_lag_at_merge")
    live_sync_lag = gap.get("q1_sync_lag")
    if sync_lag is None:
        sync_lag = live_sync_lag
    image_lag = gap.get("q1_image_behind_upstream")
    release_lag = gap.get("q1_release_lag")
    past_pin = gap.get("q2_ours_past_the_pin")
    measured = all(isinstance(v, int) for v in (sync_lag, image_lag, past_pin))
    round_measured = all(isinstance(v, int)
                         for v in (sync_lag, release_lag, past_pin))
    terminal_ok = status in {"published", "noop", "completed", "recorded"}
    converged = bool(measured and terminal_ok and sync_lag == 0
                     and image_lag == 0 and past_pin == 0)
    round_converged = bool(round_measured and terminal_ok and sync_lag == 0
                           and release_lag == 0 and past_pin == 0)

    if source_errors:
        outcome = "could_not_measure_inputs"
    elif terminal_ok and failed_attempts:
        outcome = "self_healed_after_retry"
    elif status == "failed" and failure.get("retryable"):
        outcome = "blocked_after_bounded_retries"
    elif status == "failed":
        outcome = "blocked_non_transient"
    elif status == "running":
        outcome = "interrupted_without_terminal_verdict"
    elif status == "not_checked":
        outcome = "release_not_checked"
    elif converged:
        outcome = "converged"
    elif (round_converged and isinstance(live_sync_lag, int)
          and live_sync_lag > 0):
        outcome = "converged_at_boundary_with_late_upstream_drift"
    elif not measured:
        outcome = "could_not_measure_convergence"
    else:
        outcome = "finished_with_gap"

    category = failure.get("category")
    control = None
    if attempts:
        control = "classified bounded retry; reuse pushed artefacts and BuildKit cache"
    elif failure:
        control = "fail closed; publish no version and retain the terminal cause"

    return {
        "program": "release_ll",
        "schema_version": 1,
        "round_at": round_at,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "release": {
            "status": status,
            "target": _target(release),
            "moved_tools": [r.get("repo") for r in release.get("moved", [])
                            if isinstance(r, dict) and r.get("repo")],
            "built_tools": list(release.get("built") or []),
            "compose_attempts": attempts,
            "failure": failure or None,
            "tick_exit": tick_exit,
        },
        "convergence": {
            "measured": measured,
            "round_measured": round_measured,
            "sync_lag_at_merge": sync_lag,
            "live_sync_lag": live_sync_lag,
            "release_lag": release_lag,
            "image_behind_upstream": image_lag,
            "ours_past_pin": past_pin,
            "ours_past_pin_substantive": gap.get(
            "q2_ours_past_the_pin_substantive"),
            "converged": converged,
            "round_converged": round_converged,
        },
        "lesson": {
            "trigger": category,
            "control": control,
            "outcome": outcome,
            "principle": ("A fork sync is not delivery; the round converges "
                          "when every commit seen at its merge boundary reaches "
                          "the published image. Later upstream drift is retained "
                          "as live lag for the next round, never rewritten as "
                          "this round's failure or as zero."),
        },
        "source_errors": list(source_errors or []),
    }


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _stamp(when: datetime) -> str:
    return when.astimezone().strftime("%Y-%m-%dT%H%M%S%z")


def _prune(rounds: Path, now: datetime, keep_days: int) -> list[str]:
    if keep_days <= 0 or not rounds.is_dir():
        return []
    cutoff = now.timestamp() - keep_days * 86400
    removed = []
    for path in sorted(rounds.glob("*.json")):
        try:
            when = datetime.strptime(path.stem, "%Y-%m-%dT%H%M%S%z")
        except ValueError:
            continue
        if when.timestamp() < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


def retain(record: dict, state: Path, keep_days: int = KEEP_DAYS) -> dict:
    """Write the full record and append its narrow outcome; never infer facts."""
    root = Path(state) / "release-ll"
    gk_state.require_writable(root, "the retained 05:30 release learning loop")
    when = _parse_when(record.get("round_at"))
    stamp = _stamp(when)
    rounds = root / "rounds"
    full = rounds / f"{stamp}.json"
    _atomic(full, record)
    _atomic(root / "latest.json", record)
    narrow = {
        "round": stamp,
        "status": record["release"]["status"],
        "target": record["release"]["target"],
        "trigger": record["lesson"]["trigger"],
        "outcome": record["lesson"]["outcome"],
        "compose_attempts": len(record["release"]["compose_attempts"]),
        "sync_lag_at_merge": record["convergence"]["sync_lag_at_merge"],
        "live_sync_lag": record["convergence"]["live_sync_lag"],
        "release_lag": record["convergence"]["release_lag"],
        "image_behind_upstream": record["convergence"]["image_behind_upstream"],
        "ours_past_pin": record["convergence"]["ours_past_pin"],
        "converged": record["convergence"]["converged"],
        "round_converged": record["convergence"]["round_converged"],
        "source_errors": record["source_errors"],
    }
    index = root / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with index.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(narrow, sort_keys=True) + "\n")
    return {"stamp": stamp, "full": str(full), "index": str(index),
            "pruned": _prune(rounds, datetime.now(timezone.utc), keep_days)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", type=Path, default=gk_state.state_dir())
    ap.add_argument("--release-json", type=Path, required=True)
    ap.add_argument("--gap-json", type=Path, required=True)
    ap.add_argument("--round-at", required=True)
    ap.add_argument("--tick-exit", type=int, default=None)
    a = ap.parse_args(argv)

    release, release_err = _read(a.release_json)
    gap, gap_err = _read(a.gap_json)
    if release_err is None and (release.get("program") != "daily_release"
                                or not release.get("status")):
        release_err = ("daily-release.json has no current-run status; it is "
                       "a pre-schema or unrelated record, not this round's verdict")
        release = None
    if gap_err is None and "q1_image_behind_upstream" not in gap:
        gap_err = "fork_gap.json has no convergence headline"
        gap = None
    errors = [e for e in (release_err, gap_err) if e]
    record = make_record(release, gap, a.round_at, a.tick_exit, errors)
    written = retain(record, a.state_dir)
    print("release LL: "
          f"{record['lesson']['outcome']} · "
          f"image lag {record['convergence']['image_behind_upstream']} · "
          f"ours past pin {record['convergence']['ours_past_pin']} · "
          f"retained {written['stamp']}")
    if errors:
        for err in errors:
            print(f"  source error: {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
