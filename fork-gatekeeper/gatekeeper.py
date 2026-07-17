#!/usr/bin/env python3
"""gatekeeper.py — daily upstream-sync tick for the forked EDA tools.

Owner directives:
  · daily check ALL forks
  · track RELEASES, not every commit  → a new upstream *release* is the merge trigger
  · if we merge, BUILD A NEW vibeic-eda Docker image (option B: auto-merge on green)

Flow each day (only for the forks in FORKS.json):
  1. re-seed the ledgers from live state — the vibeic-eda Dockerfile is the source of
     truth for what we ship (ARG <TOOL>_REF pins each fork's vibeic branch); we compare
     the release our pin is based on against the upstream's newer releases.
  2. per fork verdict:
       NOT_LAYERED — forked but not pinned into the image (e.g. verilator); informational
       CLEAN       — pinned + already on the latest upstream release; filtered out
       candidate   — a newer upstream release exists → try to integrate
  3. GATE (option B): integrating a candidate = rebase our vibeic branch onto the new
     release, bump the Dockerfile ARG, and **rebuild the vibeic-eda image**. That image
     build (+ the benchmark-IC regression it runs) IS the green signal. It is wired via
     `image_build.cmd` in regression.json. Until that is configured the candidate is
     DEFERRED with the reason — never a merge/image-bump without a verified green build.
       MERGED   — image rebuilt green with the new release(s); fork branch + image pushed
       DEFERRED — new release(s) available but the build gate isn't green (reason recorded)
  4. append a sync_log entry per fork + write reports/<date>.{md,json}
  5. regenerate the vibeic.ai monitor page

    python3 gatekeeper.py            # one tick

regression.json (optional): {"image_build": {"cmd": "bash build_and_regress.sh", "cwd": "…"}}
The cmd should: rebase each candidate's vibeic branch onto its new release, bump the
Dockerfile ARGs, `docker build` the image, run the benchmark-IC regression, and exit 0
ONLY if the new image is green.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent          # the (version-controlled) source location
# Runtime state lives OUTSIDE the source tree so the checked-in copy can run in-place
# without dirtying the repo. Override with GK_STATE_DIR; defaults to the user cache.
STATE = Path(os.environ.get("GK_STATE_DIR") or os.path.expanduser("~/.cache/eda-fork-gatekeeper"))
LEDGER = STATE / "ledger"
REPORTS = STATE / "reports"
REG_CFG = HERE / "regression.json"    # config ships WITH the source
sys.path.insert(0, str(HERE))
import discover_forks as disc  # noqa: E402
import build_page  # noqa: E402
try:
    import pr_notify  # opens a vibe-ic PR on actionable ticks (replaced email)
except Exception:  # noqa: BLE001
    pr_notify = None
try:
    import assess_release  # selective-merge assessment engine (per-commit triage)
except Exception:  # noqa: BLE001
    assess_release = None


def _now_date() -> str:
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _image_build_cfg() -> dict | None:
    if not REG_CFG.is_file():
        return None
    try:
        return json.loads(REG_CFG.read_text()).get("image_build")
    except (OSError, json.JSONDecodeError):
        return None


def _run_harness(cfg: dict, candidates: list[dict]) -> dict:
    """Run the integration harness (rebase → build → smoke → gated promote). Returns a
    per-candidate {tool: {status, detail, sha}} map read from GK_RESULT."""
    import os as _os
    cmd, cwd = cfg.get("cmd"), cfg.get("cwd") or str(HERE)   # run the harness from the source dir
    result_path = cfg.get("result", str(STATE / "last_build_result.json"))
    if not cmd:
        return {}
    env = {**_os.environ,
           "GK_RESULT": result_path,
           "GK_STATE_DIR": str(STATE),
           "GK_MODE": _os.environ.get("GK_MODE", cfg.get("mode", "verify")),
           "VIBEIC_CANDIDATES": json.dumps(
               [{"tool": c["tool"], "arg": c.get("dockerfile_arg"), "branch": c.get("vibeic_branch"),
                 "release": c.get("upstream_latest_release"), "upstream": c.get("upstream")}
                for c in candidates])}
    try:
        subprocess.run(cmd, shell=True, cwd=cwd, timeout=cfg.get("timeout", 21600), env=env)
    except subprocess.TimeoutExpired:
        return {c["tool"]: {"status": "timeout", "detail": "harness timed out"} for c in candidates}
    try:
        arr = json.loads(Path(result_path).read_text())
        return {r["tool"]: r for r in arr}
    except (OSError, json.JSONDecodeError):
        return {}


def tick() -> dict:
    print(f"[{_now_iso()}] gatekeeper tick — re-seeding ledgers…")
    disc.main()
    date = _now_date()
    cfg = _image_build_cfg()

    leds = {}
    candidates = []
    for p in sorted(LEDGER.glob("*.json")):
        if p.name == "index.json":
            continue
        led = json.loads(p.read_text())
        leds[p] = led
        if led.get("integrated") and (led.get("behind_releases") or 0) > 0:
            candidates.append(led)

    # SELECTIVE-MERGE doctrine (owner 2026-07-17): a behind fork gets ASSESSED, not blindly
    # rebased. Per candidate, enumerate the upstream commits and triage each (category /
    # relevance / risk / conflict-with-our-patches / clean-cherry-pick / reproduce plan);
    # the clearly-safe subset is flagged, everything else is a human decision. Assessments
    # are filed as a vibe-ic review PR (see _maybe_notify).
    assessments = {}   # tool -> assessment report
    if candidates and assess_release is not None:
        adir = STATE / "reports" / "assessments"
        adir.mkdir(parents=True, exist_ok=True)
        for led in candidates:
            try:
                rep = assess_release.assess(led["tool"])
                assessments[led["tool"]] = rep
                (adir / f"{date}-{led['tool']}.md").write_text(assess_release.render_md(rep))
            except Exception as e:  # noqa: BLE001 — assessment must never break the tick
                print(f"  [assess] {led['tool']} error (ignored): {e}")

    # LEGACY blind-rebase harness (rebase our branch onto the WHOLE release + build). Retired
    # as the default by the selective-merge pivot; kept for reference, runs ONLY if explicitly
    # re-enabled (GK_RUN_HARNESS=1) — the multi-hour docker rebuild is not run just to canary.
    hres = {}          # tool -> {status, detail, sha}
    not_configured = ("new upstream release(s) available; auto-merge (option B) rebuilds the "
                      "vibeic-eda image as the green gate, but image_build.cmd is not configured.")
    if candidates and cfg and os.environ.get("GK_RUN_HARNESS"):
        hres = _run_harness(cfg, candidates)

    results = []
    for p, led in leds.items():
        tool = led["tool"]
        nr = led.get("behind_releases") or 0
        latest = led.get("upstream_latest_release")
        entry = {"date": date, "verdict": None, "note": "", "new_releases": nr,
                 "latest_release": latest, "merged_release": None}

        if not led.get("integrated"):
            entry["verdict"] = "NOT_LAYERED"
            entry["note"] = "forked but not pinned into the image (uses upstream directly) — nothing to sync"
        elif nr == 0:
            entry["verdict"] = "CLEAN"
            entry["note"] = f"on the latest upstream release ({led.get('base_release') or led.get('pinned_ref')})"
        elif tool in hres:
            st = hres[tool]
            s, detail = st.get("status", "?"), st.get("detail", "")
            if s == "promoted":
                entry["verdict"], entry["merged_release"] = "MERGED", latest
                entry["note"] = f"integrated {latest} + image pushed: {detail}"
            elif s == "promote_failed":
                entry["verdict"] = "DEFERRED"
                entry["note"] = f"promote attempted for {latest} but FAILED (nothing shipped) — {detail}"
            elif s == "built_green":
                entry["verdict"] = "DEFERRED"
                entry["note"] = (f"rebased onto {latest} + image build VERIFIED GREEN — enable "
                                 f"GK_MODE=promote to auto-merge + push. {detail}")
            else:  # rebase_conflict / tag_missing / built_red / worktree_fail / no_clone / no_vibeic_branch
                entry["verdict"] = "DEFERRED"
                entry["note"] = f"{s} → target {latest}: {detail}"
        elif tool in assessments:
            rep = assessments[tool]
            entry["verdict"] = "DEFERRED"
            if rep.get("error"):
                entry["note"] = f"{nr} new release(s) → {latest}; assessment error: {rep['error']}"
            else:
                cc, safe = rep.get("commit_count", 0), len(rep.get("clearly_safe") or [])
                entry["assessed"] = {"commits": cc, "clearly_safe": safe}
                entry["note"] = (f"{cc} upstream commit(s) {rep.get('base_release')} → {latest}: "
                                 f"{safe} clearly-safe, {cc - safe} need human review — "
                                 f"selective-merge assessment filed (not auto-merged)")
        elif not cfg:
            entry["verdict"] = "DEFERRED"
            rels = ", ".join(r.get("tag") for r in (led.get("new_releases") or [])[:5] if r.get("tag"))
            entry["note"] = f"{nr} new upstream release(s) [{rels}] → target {latest}. {not_configured}"
        else:
            entry["verdict"] = "DEFERRED"
            entry["note"] = f"{nr} new release(s) → {latest}; harness returned no result for this tool"

        led.setdefault("sync_log", []).append(entry)
        led["last_sync"] = date
        p.write_text(json.dumps(led, indent=2, ensure_ascii=False) + "\n")
        results.append({"tool": tool, **entry})
        print(f"  {tool:16} {entry['verdict']:11} {entry['note'][:78]}")

    REPORTS.mkdir(parents=True, exist_ok=True)
    # the ledgers were seeded BEFORE promote; a successful promote ships a NEW image
    # version, so surface the actually-shipped version (parsed from the 'promoted' note)
    # in the report header instead of the stale pre-bump one.
    img_ver = leds and next(iter(leds.values())).get("image_version")
    for r in results:
        if r.get("verdict") == "MERGED":
            m = re.search(r"vibeic-eda:([0-9]+\.[0-9]+\.[0-9]+)", r.get("note", "") or "")
            if m:
                img_ver = m.group(1)
    summary = {"date": date, "generated_at": _now_iso(),
               "image_version": img_ver,
               "counts": {v: sum(1 for r in results if r["verdict"] == v)
                          for v in ("MERGED", "DEFERRED", "CLEAN", "NOT_LAYERED")},
               "results": results}
    (REPORTS / f"{date}.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    (REPORTS / f"{date}.md").write_text(_report_md(summary))
    try:
        build_page.build(build_page.DEFAULT_OUT)
    except Exception as e:
        print(f"  (page rebuild failed: {e})")
    _maybe_notify(summary, assessments)
    return summary


def _maybe_notify(summary: dict, assessments: dict | None = None):
    """On an actionable day — a MERGED promote, or a new upstream release — open a PR on
    vibe-ic. When there are selective-merge assessments (behind forks), the PR carries the
    per-commit triage for human review (adopt the clearly-safe subset, decide the rest);
    otherwise it records the MERGED/DEFERRED sync row. All-CLEAN days do nothing (no PR
    noise). Never raises — a PR hiccup must not break the tick."""
    if pr_notify is None:
        return
    c = summary["counts"]
    has_assess = bool(assessments) and any(
        (a.get("commit_count") or 0) > 0 for a in assessments.values())
    actionable = c.get("MERGED", 0) > 0 or has_assess or any(
        r["verdict"] == "DEFERRED" and (r.get("new_releases") or 0) > 0
        for r in summary["results"])
    if not actionable:
        return
    try:
        if has_assess and hasattr(pr_notify, "open_assessment_pr"):
            ok, detail = pr_notify.open_assessment_pr(summary, assessments,
                                                      {t: assess_release.render_md(a)
                                                       for t, a in assessments.items()}
                                                      if assess_release else {})
        else:
            ok, detail = pr_notify.open_pr(summary, _report_md(summary))
        print(f"  [notify] {'PR: ' if ok else 'skip: '}{detail}")
    except Exception as e:  # noqa: BLE001
        print(f"  [notify] error (ignored): {e}")


def _report_md(s: dict) -> str:
    c = s["counts"]
    lines = [f"# EDA Fork Gatekeeper — daily report {s['date']}", "",
             f"Generated {s['generated_at']}. Image `vibeic/vibeic-eda:{s.get('image_version')}`. "
             f"Policy: track **releases** (not commits); a new upstream release triggers an "
             f"image rebuild; **option B** — auto-merge on a green rebuild, defer on red.", "",
             f"**MERGED {c['MERGED']} · DEFERRED {c['DEFERRED']} · CLEAN {c['CLEAN']} · "
             f"NOT_LAYERED {c['NOT_LAYERED']}**", "",
             "| Tool | Verdict | New releases | Target | Note |", "|---|---|---|---|---|"]
    order = {"MERGED": 0, "DEFERRED": 1, "CLEAN": 2, "NOT_LAYERED": 3}
    for r in sorted(s["results"], key=lambda r: (order.get(r["verdict"], 9), r["tool"])):
        lines.append(f"| {r['tool']} | {r['verdict']} | {r['new_releases']} | "
                     f"{r.get('latest_release') or '—'} | {r['note']} |")
    lines += ["", "> CLEAN = already on the latest upstream release. NOT_LAYERED = forked but "
              "not in the image. DEFERRED tools have a new upstream release staged; the image "
              "auto-rebuilds + merges once image_build.cmd is wired and the rebuild is green."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    tick()
