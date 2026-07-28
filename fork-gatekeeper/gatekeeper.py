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
    python3 gatekeeper.py --verify [YYYY-MM-DD]
        Re-check an ALREADY-PUBLISHED day: does the daily report state the same counts
        as the assessment filed for the same date and tool, and do both name the same
        judgement? Reads only — no upstream fetch, no assessment, no PR. Exits 1 on a
        disagreement. Run it after any manual re-assessment: re-rendering an assessment
        over its date-stamped filename leaves the report that summarised the previous
        one in place, and that pair is what vibeic/vibeic-eda#7 was filed about.

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


class CountsDisagree(RuntimeError):
    """Two documents of one tick state different counts for the same assessment.

    vibeic/vibeic-eda#7. Raised INSTEAD of publishing either of them. The tick exits
    non-zero, cron.log records it, and the day's report is absent rather than wrong —
    an operator who finds no report goes looking; one who finds two contradicting
    numbers stops reading the report at all.
    """


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


def assessment_entry(rep: dict, nr: int, latest) -> dict:
    """The daily report's row for a fork that was ASSESSED — verdict, note, counts.

    A pure function of the assessment report so the sync-log summary can be exercised
    without a tick, a ledger, or the network. It used to be an inline branch of `tick()`,
    which is why the tests that were supposed to pin its arithmetic asserted on the
    SOURCE TEXT of this file instead of on what it computes — and a test that greps for
    `cc - safe` stays green while the number it produces is wrong, and goes red on a
    refactor that changes nothing.
    """
    if rep.get("error"):
        return {"verdict": "DEFERRED",
                "note": f"{nr} new release(s) → {latest}; assessment error: {rep['error']}"}
    # CONSUME the assessment's own classification — via the ONE derivation in
    # assess_release, never a private re-derivation. This block used to compute "needs
    # human" for itself as `commit_count - clearly_safe`, which silently discards the two
    # categories the assessment already resolved: commits our ref CARRIES (by ancestry or
    # cherry-pick patch-id) and commits with a RECORDED decision. Measured on magic
    # 8.3.674 → 8.3.676: 2 carried + 1 recorded skip = nothing outstanding, yet this note
    # reported "3 need human review" and the range was re-proposed on 07-23, 07-24, 07-26.
    #
    # Reading `outstanding` when present repaired the common case and left the split in
    # place (vibeic/vibeic-eda#7): the fallback here subtracted only `safe`, the one in
    # render_md subtracted `safe + carried + decided`, and pr_notify's never looked at
    # `outstanding` at all — so one tick could publish three different answers.
    # `summary_counts` is now the only place the question is answered, and it reaches
    # arithmetic only when neither the summary list nor the per-commit rows exist.
    n = assess_release.summary_counts(rep)
    cc, safe = n["commits"], n["clearly_safe"]
    carried, decided, n_open = n["carried"], n["decided"], n["outstanding"]
    # Commits the AI judge never reached a conclusion about. The operator must be told
    # the analysis did not run, rather than reading its absence as a triage result — see
    # the 2026-07-28 magic assessment, where a truncated judge reply published 105 rows
    # of fabricated "high risk".
    n_na = n["not_assessed"]
    # vibeic/vibeic-eda#5: adopt-candidates the model called relevant that nothing we run
    # can reach. DISCLOSED, never resolved away — the verdict may still be right, but its
    # stated evidence is not true of our fork.
    n_unreach = n["unreachable"]
    # vibeic/vibeic-eda#6: adopt-candidates whose judgement did not reproduce across
    # independent samples. DISCLOSED, never averaged into a majority.
    n_unconf = n["unconfirmed"]
    entry = {"verdict": "DEFERRED",
             "assessed": {"commits": cc, "clearly_safe": safe,
                          "carried": carried, "decided": decided,
                          "outstanding": n_open, "not_assessed": n_na,
                          "unreachable": n_unreach, "unconfirmed": n_unconf,
                          # WHICH assessment this report summarises. Without it the daily
                          # report and the assessment filed under the same date are
                          # indistinguishable when they are two different vintages — the
                          # 2026-07-28 pair was exactly that, and nothing on either
                          # document said so. `verify_documents` compares these stamps.
                          "assessor": rep.get("assessor"),
                          "assessed_at": rep.get("assessed_at"),
                          "replayed": bool(rep.get("cached")),
                          "derived": n["derived"]}}
    resolved = f"{carried} already carried, {decided} previously decided"
    entry["note"] = (f"{cc} upstream commit(s) {rep.get('base_release')} → {latest}: "
                     f"{safe} clearly-safe, {resolved}, {n_open} need human review — "
                     f"selective-merge assessment filed (not auto-merged)")
    if n_na:
        entry["note"] += (f" — WARNING: the AI judge did not complete, {n_na} "
                          f"commit(s) NOT ASSESSED (no classification made)")
    if n_unreach:
        entry["note"] += (f" — {n_unreach} commit(s) the judge called relevant are "
                          f"UNREACHABLE from any command our emitters issue "
                          f"(model/surface disagreement → human decision, not "
                          f"auto-proposed)")
    if n_unconf:
        entry["note"] += (
            f" — {n_unconf} commit(s) cleared every other auto-adopt condition but "
            f"their JUDGEMENT DID NOT REPRODUCE across independent samples "
            f"(every reading is on the row → human decision, not auto-proposed)")
    if n_open == 0 and safe == 0:
        # Nothing is outstanding: reporting DEFERRED here is what turned settled work
        # into a recurring proposal.
        entry["verdict"] = "RESOLVED"
        entry["note"] = (f"{cc} upstream commit(s) {rep.get('base_release')} → "
                         f"{latest}: {resolved} — nothing outstanding")
    return entry


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
    rendered = {}      # tool -> the assessment markdown, rendered ONCE (vibeic/vibeic-eda#7)
    if candidates and assess_release is not None:
        for led in candidates:
            try:
                rep = assess_release.assess(led["tool"])
                assessments[led["tool"]] = rep
                # Rendered here, PUBLISHED later — the assessment file, the daily report
                # and the review PR must be one atomic publication, so nothing is written
                # until every document has been checked against the others. Rendering
                # once (rather than again per consumer) is the other half: two renders of
                # "the" assessment are two chances to render two different reports.
                rendered[led["tool"]] = assess_release.render_md(rep)
            except Exception as e:  # noqa: BLE001 — assessment must never break the tick
                print(f"  [assess] {led['tool']} error (ignored): {e}")

    # An assessment replayed from cache (identical tool + upstream range + carried-patch
    # ref) is NOT news: we already filed its review PR and already offered its clearly-safe
    # set on the tick that first produced it. Re-filing would reopen the same PR every day.
    # Only FRESH assessments drive PR-opening; cached ones still show in the report/summary.
    fresh = {t: r for t, r in assessments.items() if not r.get("cached")}
    for t in sorted(set(assessments) - set(fresh)):
        a = assessments[t]
        print(f"  [assess] {t:16} unchanged range AND unchanged assessor "
              f"({a.get('assessor') or '?'}) — replayed from cache "
              f"(computed {a.get('assessed_at') or 'earlier'}), no new PR")
    # vibeic/vibeic-eda#4: the cache key now identifies the ASSESSOR as well as the
    # input, so editing the judge re-judges every cached range on the next tick. That
    # is the correct behaviour and it costs real API calls — say WHY, or it reads as an
    # unexplained spike and gets "fixed" by reverting the invalidation.
    for t, r in sorted(assessments.items()):
        if r.get("reassessed_because"):
            print(f"  [assess] {t:16} RE-JUDGED (cache intentionally missed): "
                  f"{r['reassessed_because']}")

    # Phase 3: open a cherry-pick MERGE PR (deterministic; holds token) for the clearly-safe
    # commits — real upstream commits, human-reviewed, never auto-merged, never force-push.
    # Gated on GK_MERGE_PR, which run_tick.sh (the cron entrypoint) defaults to 1 = ARMED.
    # Never breaks the tick.
    if fresh and os.environ.get("GK_MERGE_PR") in ("1", "true", "yes"):
        try:
            import prepare_merge_pr
            for r in prepare_merge_pr.prepare(fresh, date):
                print(f"  [merge-pr] {r.get('tool'):16} {r.get('status')} "
                      f"{r.get('url') or r.get('note') or ''}"[:110])
        except Exception as e:  # noqa: BLE001
            print(f"  [merge-pr] error (ignored): {e}")

    # LEGACY blind-rebase harness (rebase our branch onto the WHOLE release + build). Retired
    # as the default by the selective-merge pivot; kept for reference, runs ONLY if explicitly
    # re-enabled (GK_RUN_HARNESS=1) — the multi-hour docker rebuild is not run just to canary.
    hres = {}          # tool -> {status, detail, sha}
    not_configured = ("new upstream release(s) available; auto-merge (option B) rebuilds the "
                      "vibeic-eda image as the green gate, but image_build.cmd is not configured.")
    if candidates and cfg and os.environ.get("GK_RUN_HARNESS"):
        hres = _run_harness(cfg, candidates)

    results = []
    conflicts: list[str] = []    # cross-document count disagreements (vibeic/vibeic-eda#7)
    pending_ledgers: list[tuple] = []   # written only once the documents agree
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
            entry.update(assessment_entry(rep, nr, latest))
            # CROSS-DOCUMENT CHECK (vibeic/vibeic-eda#7). Both documents are now
            # rendered; parse the numbers back OUT of each and require them to agree.
            # Checking the rendered text rather than the ints they were built from is
            # the point — it is what a formatter that drops a field, or a caller that
            # pairs this report with a different assessment, cannot slip past.
            # Disagreement is collected, never reconciled: see the abort below.
            conflicts.extend(assess_release.cross_check(
                rep, {"assessment": rendered.get(tool) or "", "report": entry["note"]}))
        elif not cfg:
            entry["verdict"] = "DEFERRED"
            rels = ", ".join(r.get("tag") for r in (led.get("new_releases") or [])[:5] if r.get("tag"))
            entry["note"] = f"{nr} new upstream release(s) [{rels}] → target {latest}. {not_configured}"
        else:
            entry["verdict"] = "DEFERRED"
            entry["note"] = f"{nr} new release(s) → {latest}; harness returned no result for this tool"

        led.setdefault("sync_log", []).append(entry)
        led["last_sync"] = date
        pending_ledgers.append((p, led))
        results.append({"tool": tool, **entry})
        print(f"  {tool:16} {entry['verdict']:11} {entry['note'][:78]}")

    # ── THE GATE (vibeic/vibeic-eda#7) ───────────────────────────────────────
    # Nothing has been written yet. A tick whose documents contradict each other
    # publishes NOTHING and exits loudly: an operator who is shown two numbers for one
    # assessment learns to trust neither, and the fail-safe reading of a disagreement is
    # that the day's triage is unknown — not that one of the two answers may be picked.
    # Deliberately NOT wrapped in the "assessment must never break the tick" try: an
    # assessment that fails to compute degrades to a reported error, but an assessment
    # that computes two different results is a defect in this program.
    if conflicts:
        for c in conflicts:
            print(f"  [FATAL] cross-document count mismatch — {c}")
        raise CountsDisagree(
            f"{len(conflicts)} cross-document count mismatch(es) on {date}; "
            f"nothing published (no report, no assessment, no PR)")

    for p, led in pending_ledgers:
        p.write_text(json.dumps(led, indent=2, ensure_ascii=False) + "\n")
    if rendered:
        adir = STATE / "reports" / "assessments"
        adir.mkdir(parents=True, exist_ok=True)
        for t, md in rendered.items():
            (adir / f"{date}-{t}.md").write_text(md)
            (adir / f"{date}-{t}.json").write_text(
                json.dumps(assessments[t], ensure_ascii=False))
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
    # ROUND TRIP. The pre-publication gate compared what we MEANT to write; this
    # compares what is ON DISK, which is the only thing anyone reads. It costs a few
    # file reads and it is the half that survives a serialization bug — and, run later
    # as `--verify`, the half that catches a day whose assessment was regenerated
    # underneath a report that was not.
    disk = verify_documents(date)
    if disk:
        for c in disk:
            print(f"  [FATAL] published documents disagree — {c}")
        raise CountsDisagree(f"{len(disk)} mismatch(es) between the report and the "
                             f"assessments published for {date}")
    try:
        build_page.build(build_page.DEFAULT_OUT)
    except Exception as e:
        print(f"  (page rebuild failed: {e})")
    _maybe_notify(summary, fresh, rendered)   # cached (unchanged-range) assessments file no new PR
    return summary


def verify_documents(date: str) -> list[str]:
    """Do the documents PUBLISHED for `date` agree with each other?

    Re-reads `reports/<date>.json` and every `reports/assessments/<date>-<tool>.md` and
    compares, per tool, the counts the daily report states against the counts the
    assessment states — plus the PROVENANCE stamp, which is what tells two vintages of
    one date apart when their numbers happen to coincide.

    That last part is the 2026-07-28 case. The 05:32 tick wrote a report and an
    assessment that agreed with each other (0 clearly-safe, 105 needing review, from a
    judge reply that had been truncated). Four fixes landed by 06:53; the range was
    re-judged at 07:07 and the assessment for that date was re-rendered from the new
    verdict (1 clearly-safe, 2 needing review) — over the SAME date-stamped filename,
    while the daily report kept its 05:32 content. Neither file said which assessment it
    described, so the pair was indistinguishable from a tick that had contradicted
    itself. Run this after any manual re-assessment and it names the split.

    Returns [] when they agree; one line per disagreement otherwise. Never raises — a
    missing or unreadable document is reported, not thrown.
    """
    out: list[str] = []
    rp = REPORTS / f"{date}.json"
    try:
        summary = json.loads(rp.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [f"cannot read the daily report {rp}: {e}"]
    if assess_release is None:
        return ["assess_release is not importable — the published counts cannot be parsed"]
    adir = REPORTS / "assessments"
    for r in summary.get("results") or []:
        got = r.get("assessed")
        if not isinstance(got, dict):
            continue                       # this fork filed no assessment for that date
        tool = r.get("tool", "?")
        ap = adir / f"{date}-{tool}.md"
        try:
            md = ap.read_text()
        except OSError as e:
            out.append(f"{tool}: the report summarises an assessment that is not on disk "
                       f"at {ap} ({e})")
            continue
        want = assess_release.parse_headline("assessment", md)
        if want is None:
            out.append(f"{tool}: {ap.name} states no counts — the report summarises "
                       f"numbers the assessment does not carry")
            continue
        for field in assess_release.HEADLINE:
            if got.get(field) != want[field]:
                out.append(f"{tool}: the daily report says {field}={got.get(field)}, "
                           f"{ap.name} says {want[field]}")
        # PROVENANCE: same date, same tool, different judgement.
        stamp = assess_release.parse_provenance(md)
        for field in ("assessor", "assessed_at"):
            if stamp.get(field) and got.get(field) and stamp[field] != got[field]:
                out.append(f"{tool}: the daily report summarises the assessment computed "
                           f"{field}={got[field]}, but {ap.name} on disk is "
                           f"{field}={stamp[field]} — two vintages of one date")
    return out


def _maybe_notify(summary: dict, assessments: dict | None = None,
                  rendered: dict | None = None):
    """On an actionable day — a MERGED promote, or a new upstream release — open a PR on
    vibe-ic. When there are selective-merge assessments (behind forks), the PR carries the
    per-commit triage for human review (adopt the clearly-safe subset, decide the rest);
    otherwise it records the MERGED/DEFERRED sync row. All-CLEAN days do nothing (no PR
    noise). Never raises — a PR hiccup must not break the tick."""
    if pr_notify is None:
        return
    c = summary["counts"]
    assessments = assessments or {}
    # forks the ASSESSMENT PR covers: any assessed behind fork — with commits OR an
    # enumerate error (a new release we couldn't even read still needs surfacing).
    assess_tools = {t for t, a in assessments.items()
                    if (a.get("commit_count") or 0) > 0 or a.get("error")}
    # the SYNC/BACKLOG PR covers MERGED doc-bumps + any DEFERRED-with-new-release fork the
    # assessment PR did NOT already carry (e.g. assess_release import failed entirely).
    uncovered = any(r["verdict"] == "DEFERRED" and (r.get("new_releases") or 0) > 0
                    and r["tool"] not in assess_tools for r in summary["results"])
    outcomes = []
    try:
        if assess_tools and hasattr(pr_notify, "open_assessment_pr"):
            subset = {t: assessments[t] for t in assess_tools}
            # The PR carries the EXACT bytes that were checked and written, not a second
            # render of "the" assessment (vibeic/vibeic-eda#7). Two renders are two
            # chances to publish two reports; and re-rendering here would have re-run
            # the classifier's formatter after the gate that cleared it.
            md = {t: (rendered or {}).get(t) for t in subset}
            missing = [t for t, v in md.items() if not v]
            if missing and assess_release:
                for t in missing:
                    md[t] = assess_release.render_md(subset[t])
            outcomes.append(("assess",) + tuple(pr_notify.open_assessment_pr(summary, subset, md)))
        if c.get("MERGED", 0) > 0 or uncovered:
            outcomes.append(("sync",) + tuple(pr_notify.open_pr(summary, _report_md(summary))))
        if not outcomes:
            return
        for kind, ok, detail in outcomes:
            print(f"  [notify:{kind}] {'PR: ' if ok else 'skip: '}{detail}")
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
    if "--verify" in sys.argv:
        # Re-check an ALREADY-PUBLISHED day, without running a tick: no upstream fetch,
        # no assessment, no PR. `--verify [date]` (default: today).
        _rest = [a for a in sys.argv[1:] if not a.startswith("--")]
        _date = _rest[0] if _rest else _now_date()
        _bad = verify_documents(_date)
        for _c in _bad:
            print(f"[FATAL] {_date}: {_c}")
        print(f"{_date}: {'DISAGREE — ' + str(len(_bad)) + ' mismatch(es)' if _bad else 'documents agree'}")
        sys.exit(1 if _bad else 0)
    tick()
