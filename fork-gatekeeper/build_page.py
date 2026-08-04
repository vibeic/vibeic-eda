#!/usr/bin/env python3
"""build_page.py — generate the vibeic.ai "EDA Forks" monitor subpage.

Reads the per-tool ledgers (ledger/*.json) and the latest daily report, and emits
a static, site-styled page into the vibeic.ai document root. The page records, per
forked tool: when we forked, from which upstream + fork point, the base vs current
version, the patches we carry, the upstream commits still pending, and the daily
Gatekeeper sync log. Data is embedded at build time (the site is a static server),
so the daily tick just regenerates this file.

    python3 build_page.py [--out /home/reyerchu/vibeic.ai/eda-forks.html]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import base64
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent          # version-controlled source
sys.path.insert(0, str(HERE))
import gk_state  # noqa: E402 — WHERE state lives and WHO may write it (vibeic/vibeic-eda#12)
import inventory  # noqa: E402 — the tool inventory, measured at build time

STATE = gk_state.state_dir()
LEDGER = STATE / "ledger"             # runtime state — outside the source tree
REPORTS = STATE / "reports"
DEFAULT_OUT = Path(os.environ.get("GK_PAGE_OUT") or gk_state.PRODUCTION_PAGE)

# --- NDA redaction at the publish boundary (BINDING) ---------------------------
# The ledgers are seeded from the forks' own commit messages, some of which name a
# commercial NDA foundry / process. That name must NEVER reach the public page, no
# matter what the ledger or commit text says. This build step is the single choke
# point where ledger data becomes a public artifact, so we sanitize the emitted
# HTML here — defense-in-depth, so a future commit message can't re-leak it.
# (Order matters: replace the specific compound tokens before the bare name.)
# The token PATTERNS are stored base64-encoded and decoded at run time. They
# were plain literals here until 2026-07-26 — in a file that is TRACKED and
# PUSHED to a PUBLIC repository. This block's own comment claimed to be
# "defense-in-depth so a future commit message can't re-leak it" while the
# block itself WAS the leak: a redactor that spells out what it redacts
# publishes it. Same encoded-store form the vibe-ic NDA checkers already use
# (programs/_commercial_pdk.py) so the two cannot drift apart in approach.
# Order still matters: compound tokens before the bare name.
#
# COVERAGE, measured 2026-07-26: this list held 5 patterns while the
# canonical store knows EIGHT token roles — a second foundry brand, an IP
# vendor and an IP part number were NOT redacted at all, so the published
# page could carry them. A parallel list drifts; it already had. All eight
# roles are covered now, and the real fix is a SHARED token module across
# the two repos rather than a copy that must be remembered.
from _nda_tokens import redact as _redact_nda_impl  # noqa: E402


def _redact_nda(s: str) -> str:
    return _redact_nda_impl(s)


def _load_ledgers() -> list[dict]:
    out = []
    for p in sorted(LEDGER.glob("*.json")):
        if p.name == "index.json":
            continue
        try:
            # The whole ledger dict is embedded into the published page, so the #12
            # provenance block — which names a local checkout path and a hostname —
            # comes off at the load boundary. What an internal state file records and
            # what a public page carries are two questions, exactly as for the NDA
            # redaction below.
            out.append(gk_state.strip_provenance(json.loads(p.read_text())))
        except json.JSONDecodeError:
            pass
    # OpenROAD/yosys first (most active), then by pending desc
    out.sort(key=lambda d: (-(d.get("pending_upstream_count") or d.get("behind") or 0), d.get("tool", "")))
    return out


def _latest_report() -> dict | None:
    js = sorted(REPORTS.glob("*.json"))
    if not js:
        return None
    try:
        return gk_state.strip_provenance(json.loads(js[-1].read_text()))
    except json.JSONDecodeError:
        return None


# The per-tool enhancement backlog (capability matrix vs commercial EDA). Unlike the
# ledgers — which are re-seeded into ~/.cache every day and would overwrite any data
# stored there — this file lives in the version-controlled source tree next to this
# script, so it survives the daily re-seed. Keyed by ledger tool name.
ENH_FILE = HERE / "ENHANCEMENTS.json"


def _load_enh() -> dict:
    try:
        return json.loads(ENH_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


NAV = """<nav>
    <div class="nav-inner">
        <a href="/" class="logo"><img src="img/logo-v5.svg" alt="vibeIC.ai" class="logo-img"></a>
        <div class="nav-links" id="navLinks">
__NAVLINKS__
            <a href="https://github.com/vibeic/vibe-ic" target="_blank" class="btn-nav">GitHub</a>
            <div class="lang-switch">
                <button class="lang-btn active" onclick="setLang('en')">EN</button>
                <button class="lang-btn" onclick="setLang('zh')">中</button>
            </div>
        </div>
        <button class="menu-toggle" onclick="document.getElementById('navLinks').classList.toggle('open')" aria-label="Menu">
            <svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
        </button>
    </div>
</nav>"""

FOOTER = """<footer>
    <div class="container">
        <div class="footer-inner">
            <div class="footer-brand">
                <span class="logo"><img src="img/logo-v5.svg" alt="vibeIC.ai" class="logo-img"></span>
                <p data-en="AI-Native IC Design Platform" data-zh="AI 原生 IC 設計平台">AI-Native IC Design Platform</p>
            </div>
            <div class="footer-cols">
                <div><h6 data-en="Site" data-zh="網站">Site</h6>__FOOTER_SITE__</div>
                <div><h6 data-en="Resources" data-zh="資源">Resources</h6><a href="https://github.com/vibeic/vibe-ic" target="_blank">GitHub</a><a href="https://github.com/vibeic/vibeic-bench" target="_blank" data-en="Run logs" data-zh="Run log">Run logs</a></div>
                <div><h6 data-en="Company" data-zh="公司">Company</h6><a href="https://vibeic.ai" target="_blank">vibeic.ai</a><a href="mailto:contact@vibeic.ai" data-en="Contact" data-zh="聯絡">Contact</a></div>
            </div>
        </div>
        <div class="footer-bottom"><p>&copy; 2026 vibeic.ai. <span data-en="All rights reserved." data-zh="保留所有權利。">All rights reserved.</span> | <a href="/privacy.html" data-en="Privacy" data-zh="隱私政策">Privacy</a> | <a href="/terms.html" data-en="Terms" data-zh="服務條款">Terms</a> | <a href="/disclaimer.html" data-en="Disclaimer" data-zh="免責聲明">Disclaimer</a></p></div>
    </div>
</footer>"""

# Honest commercial-gap self-assessment, from a top-down survey of every fork vs the
# leading commercial suites. The prose lives in the template, but its two COUNTS are
# substituted from ENHANCEMENTS.json at build time (__NFORKS__ / __NOPEN__).
# This used to read "all 12 forks ... ~63-item backlog" as literal text, and both
# numbers silently went stale: the fork count reached 15, and ~63 reconciled with no
# band of the ledger at all (open=143, open-with-priority=116, P0-P2=51, P0-P3=83).
# A hardcoded count beside the data that defines it will always drift, so it is derived.
#
# 2026-07-29: the same drift came back in a different form. The COUNT stayed
# correct and the WORD went wrong — "all __NFORKS__ forks" rendered as "all 15
# forks" directly above a 21-row ledger, because the survey covers 15 and the
# fork list grew to 21. A derived number inside a false quantifier is still a
# false sentence. Both counts are now substituted and "all" is gone; the prose
# says which forks are not yet surveyed instead of implying there are none.
GAP = """<section>
    <div class="fork-wrap">
        <div class="section-header" style="text-align:left">
            <p class="eyebrow" data-en="Honest self-assessment" data-zh="誠實自評">Honest self-assessment</p>
            <h2 data-en="What our forks can't do yet — vs commercial EDA" data-zh="我們的 fork 還做不到什麼 — 對照商用 EDA">What our forks can't do yet — vs commercial EDA</h2>
            <p data-en="We own the core engines; what we lack is the signoff + methodology layer on top. A systematic survey of the leading commercial suites (Synopsys / Cadence / Siemens EDA + Ansys / Keysight / Empyrean) against __NFORKS__ of the __NTOTAL__ forks produced an enhancement backlog of __NOPEN__ open items (every row not yet delivered, counted straight from the ledger below). The remaining __NUNSURVEYED__ forks carry no capability rows yet, so a tool with an empty backlog below has not been assessed — it is not a tool with no gaps. The single highest-leverage item is field-solver-accurate, coupling-aware parasitic extraction (PEX): it is a prerequisite for crosstalk/SI timing, dynamic IR-drop, electromigration, and point-to-point reliability — one keystone unblocks roughly five downstream signoff features across two tools. We publish this gap openly; honesty about the ceiling is how we earn trust." data-zh="我們擁有核心引擎，缺的是上面那層簽核 + 方法學。我們對三大廠（Synopsys／Cadence／Siemens EDA，加上 Ansys／Keysight／Empyrean）做了系統化調查，對照 __NTOTAL__ 個 fork 中的 __NFORKS__ 個，整理出一份強化 backlog，目前有 __NOPEN__ 項未交付（直接數下方帳本裡尚未完成的每一列）。其餘 __NUNSURVEYED__ 個 fork 尚無能力列，所以下方 backlog 是空的工具代表「還沒評估」，不是「沒有缺口」。最高槓桿的單一項目是 field-solver 級、耦合感知的寄生萃取（PEX）：它是串擾／SI timing、動態 IR-drop、電遷移、點對點可靠性的前置條件 — 一個拱心石解鎖橫跨兩個工具的約五個下游簽核功能。我們公開這份差距；對能力天花板誠實，正是我們贏得信任的方式。">We own the core engines; what we lack is the signoff + methodology layer on top. A systematic survey of the leading commercial suites (Synopsys / Cadence / Siemens EDA + Ansys / Keysight / Empyrean) against __NFORKS__ of the __NTOTAL__ forks produced an enhancement backlog of __NOPEN__ open items (every row not yet delivered, counted straight from the ledger below). The remaining __NUNSURVEYED__ forks carry no capability rows yet, so a tool with an empty backlog below has not been assessed — it is not a tool with no gaps. The single highest-leverage item is field-solver-accurate, coupling-aware parasitic extraction (PEX): it is a prerequisite for crosstalk/SI timing, dynamic IR-drop, electromigration, and point-to-point reliability — one keystone unblocks roughly five downstream signoff features across two tools. We publish this gap openly; honesty about the ceiling is how we earn trust.</p>
        </div>

        <div class="fork-scroll">
        <table class="fork-table">
            <thead><tr>
                <th data-en="Fork" data-zh="Fork">Fork</th>
                <th data-en="Commercial equivalent" data-zh="商用對標">Commercial equivalent</th>
                <th data-en="Headline gap — what it can't do yet" data-zh="主要差距 — 還做不到什麼">Headline gap — what it can't do yet</th>
            </tr></thead>
            <tbody>
                <tr><td class="fork-tool">OpenROAD</td><td class="fork-mono">Innovus · Tempus · Voltus / ICC2 · PrimeTime · RedHawk / Aprisa</td><td data-en="No crosstalk/SI timing, no coupling-aware SPEF, static-only IR (no dynamic/DvD), no EM, MCMM stuck at one mode, no UPF, no physical-aware signoff-ECO" data-zh="無串擾/SI timing、無耦合感知 SPEF、只有靜態 IR（無動態/DvD）、無 EM、MCMM 卡在單一 mode、無 UPF、無 physical-aware signoff-ECO">No crosstalk/SI timing, no coupling-aware SPEF, static-only IR (no dynamic/DvD), no EM, MCMM stuck at one mode, no UPF, no physical-aware signoff-ECO</td></tr>
                <tr><td class="fork-tool">yosys</td><td class="fork-mono">Design Compiler NXT · Fusion / Genus / Oasys-RTL</td><td data-en="Simplistic single-value delay (no real NLDM/CCS), no physical-aware synthesis, no DesignWare-grade datapath, no multi-Vth leakage opt, no ASIC DFT scan insertion" data-zh="單值延遲模型（無真正 NLDM/CCS）、無 physical-aware synthesis、無 DesignWare 級 datapath、無 multi-Vth 漏電優化、無 ASIC DFT scan 插入">Simplistic single-value delay (no real NLDM/CCS), no physical-aware synthesis, no DesignWare-grade datapath, no multi-Vth leakage opt, no ASIC DFT scan insertion</td></tr>
                <tr><td class="fork-tool">klayout</td><td class="fork-mono">Calibre nmDRC / IC Validator / Pegasus</td><td data-en="No field-solver PEX, no PERC reliability, no multi-patterning decomposition, no equation-based DRC engine, no smart/timing-aware fill, single-host DRC (no hyperscale cluster)" data-zh="無 field-solver PEX、無 PERC 可靠性、無 multi-patterning 分解、無 equation-based DRC 引擎、無 smart/timing-aware fill、單機 DRC（無 hyperscale 叢集）">No field-solver PEX, no PERC reliability, no multi-patterning decomposition, no equation-based DRC engine, no smart/timing-aware fill, single-host DRC (no hyperscale cluster)</td></tr>
                <tr><td class="fork-tool">magic</td><td class="fork-mono">Calibre xACT-3D / StarRC / Quantus</td><td data-en="Rule/table-based extraction only — no 3D field solver, no coupling-cap signoff SPEF, no golden correlation" data-zh="只有 rule/table-based 萃取 — 無 3D field solver、無耦合電容簽核 SPEF、無 golden 相關性">Rule/table-based extraction only — no 3D field solver, no coupling-cap signoff SPEF, no golden correlation</td></tr>
                <tr><td class="fork-tool">netgen</td><td class="fork-mono">Calibre nmLVS · PERC / IC Validator LVS</td><td data-en="Bus-heavy designs need manual normalization; zero PERC layer (voltage-aware DRC / ESD / latch-up / point-to-point)" data-zh="bus-heavy 設計需手動正規化；PERC 層完全沒有（voltage-aware DRC / ESD / latch-up / 點對點）">Bus-heavy designs need manual normalization; zero PERC layer (voltage-aware DRC / ESD / latch-up / point-to-point)</td></tr>
                <tr><td class="fork-tool">ngspice</td><td class="fork-mono">Spectre X · RF · FMC / PrimeSim / AFS / ADS / ALPS</td><td data-en="No native mismatch Monte-Carlo, no high-sigma, weaker convergence, no RF steady-state (PSS/HB/PNoise), no aging/EM, no FastSPICE/GPU" data-zh="無原生 mismatch Monte-Carlo、無 high-sigma、收斂較弱、無 RF 穩態（PSS/HB/PNoise）、無 aging/EM、無 FastSPICE/GPU">No native mismatch Monte-Carlo, no high-sigma, weaker convergence, no RF steady-state (PSS/HB/PNoise), no aging/EM, no FastSPICE/GPU</td></tr>
                <tr><td class="fork-tool">iverilog</td><td class="fork-mono">VCS / Xcelium / Questa</td><td data-en="Partial SystemVerilog, no constrained-random/UVM, no functional-coverage database, no full SVA (its true 4-state + SDF gate-level sim is a genuine asset)" data-zh="SystemVerilog 不完整、無 constrained-random/UVM、無功能覆蓋率資料庫、無完整 SVA（但其真 4-state + SDF gate-level 模擬是真正的資產）">Partial SystemVerilog, no constrained-random/UVM, no functional-coverage database, no full SVA (its true 4-state + SDF gate-level sim is a genuine asset)</td></tr>
                <tr><td class="fork-tool">verilator</td><td class="fork-mono">VCS · VC SpyGlass / Xcelium / Questa</td><td data-en="Mostly 2-state (no X-propagation), ignores SDF/timing, partial constrained-random, no production UVM, no UCIS coverage merge, no CDC/RDC" data-zh="幾乎 2-state（無 X-propagation）、忽略 SDF/timing、constrained-random 部分、無 production UVM、無 UCIS 覆蓋合併、無 CDC/RDC">Mostly 2-state (no X-propagation), ignores SDF/timing, partial constrained-random, no production UVM, no UCIS coverage merge, no CDC/RDC</td></tr>
                <tr><td class="fork-tool">sby / eqy</td><td class="fork-mono">JasperGold / VC Formal / Formality · Conformal</td><td data-en="Core BMC/induction engine only — none of the formal apps (CSR, connectivity, SEC, security, unreachability-coverage, superlint); eqy has no synthesis-guidance ingest and is weak on retiming, so it is not yet a trustworthy tape-out LEC" data-zh="只有核心 BMC/induction 引擎 — 沒有任何 formal app（CSR、connectivity、SEC、security、unreachability 覆蓋、superlint）；eqy 無合成 guidance 匯入、retiming 弱，尚不足以當 tapeout LEC">Core BMC/induction engine only — none of the formal apps (CSR, connectivity, SEC, security, unreachability-coverage, superlint); eqy has no synthesis-guidance ingest and is weak on retiming, so it is not yet a trustworthy tape-out LEC</td></tr>
                <tr><td class="fork-tool">cocotb</td><td class="fork-mono">UVM on VCS/Xcelium/Questa + vManager / Verdi Coverage</td><td data-en="No verification management, no UCIS coverage merge/rank/trend, weaker constraint solver, no protocol VIP libraries" data-zh="無 verification management、無 UCIS 覆蓋合併/排名/趨勢、約束求解器較弱、無 protocol VIP 函式庫">No verification management, no UCIS coverage merge/rank/trend, weaker constraint solver, no protocol VIP libraries</td></tr>
                <tr><td class="fork-tool">pyuvm</td><td class="fork-mono">SystemVerilog UVM + VIP libraries (Synopsys / Cadence / Siemens)</td><td data-en="Register abstraction layer (RAL) under development, no protocol VIP (AXI/PCIe/DDR/…), slower constraint solver, no portable stimulus" data-zh="RAL（register abstraction layer）開發中、無 protocol VIP（AXI/PCIe/DDR/…）、約束求解器較慢、無 portable stimulus">Register abstraction layer (RAL) under development, no protocol VIP (AXI/PCIe/DDR/…), slower constraint solver, no portable stimulus</td></tr>
            </tbody>
        </table>
        </div>
        <p class="fork-caption" data-en="Do-first spine: (Tier 0) field-solver PEX — the keystone; then (Tier 1, all P0) the signoff-integrity cluster (SI timing → dynamic IR → EM), the reliability layer (PERC), equivalence + formal sign-off (LEC/SEC + formal apps), verification methodology (constrained-random → SVA → coverage merge → UVM), synthesis QoR + DFT, and analog signoff (mismatch Monte-Carlo, high-sigma, hardened convergence). Advanced-node items (multi-patterning coloring, POCV/LVF, CCS/ECSM, GPU FastSPICE) are honestly deferred for a 180nm-class flow." data-zh="先做的主脊：（Tier 0）field-solver PEX — 拱心石；接著（Tier 1，全 P0）簽核完整性群組（SI timing → 動態 IR → EM）、可靠性層（PERC）、等價 + formal 簽核（LEC/SEC + formal apps）、驗證方法學（constrained-random → SVA → 覆蓋合併 → UVM）、合成 QoR + DFT，以及類比簽核（mismatch Monte-Carlo、high-sigma、強化收斂）。進階節點項目（multi-patterning coloring、POCV/LVF、CCS/ECSM、GPU FastSPICE）對 180nm 級流程誠實地延後。">Do-first spine: (Tier 0) field-solver PEX — the keystone; then (Tier 1, all P0) the signoff-integrity cluster (SI timing → dynamic IR → EM), the reliability layer (PERC), equivalence + formal sign-off (LEC/SEC + formal apps), verification methodology (constrained-random → SVA → coverage merge → UVM), synthesis QoR + DFT, and analog signoff (mismatch Monte-Carlo, high-sigma, hardened convergence). Advanced-node items (multi-patterning coloring, POCV/LVF, CCS/ECSM, GPU FastSPICE) are honestly deferred for a 180nm-class flow.</p>
        <p class="fork-caption" data-en="For the narrative behind this gap — a per-tool deep-dive on where open-source EDA stands versus commercial — see the blog: <a href='/blog/07-part3-backend-convergence-en.html' style='color:#63a8ea'>Open-source vs commercial EDA — where's the gap?</a>" data-zh="這份差距背後的完整敘事（逐工具深入分析開源 EDA 對比商用的現況），見部落格：<a href='/blog/07-part3-backend-convergence-zh.html' style='color:#63a8ea'>開源 vs 商業 EDA 的差距，到底還差在哪？</a>">For the narrative behind this gap — a per-tool deep-dive on where open-source EDA stands versus commercial — see the blog: <a href='/blog/07-part3-backend-convergence-en.html' style='color:#63a8ea'>Open-source vs commercial EDA — where's the gap?</a></p>
    </div>
</section>"""

# --- WHY a pin sits where it sits -------------------------------------------
# A release count cannot tell a pin that was NEGLECTED from one that is HELD ON
# PURPOSE, and rendering both in the same visual language tells the reader
# something false — the same reasoning the tracking-gap block already applies
# when it excludes the rows that are somebody's deliberate state. The ledger
# schema has no field for a RATIONALE, so until discover_forks.py records one it
# lives here, keyed by the ledger's `tool`. A tool with NO entry renders plain:
# an unexplained gap, which is the honest default.
#
#   kind "held" — deliberate ceiling. Taking the newer release breaks the build.
#
# WHAT WAS REMOVED HERE, AND WHY (2026-08-01). This table also carried a kind
# "on-it" — "measured to be sitting on the newest release already; the count is
# an artefact of the detector comparing dates" — for two tools, plus an `ours`
# override that replaced the ledger's `base_release` on all three rows. That was
# a DISPLAY-LAYER CORRECTION of a wrong MEASUREMENT, and it left the page arguing
# with itself: the table cell said 1, the prose under it said the 1 was not real,
# and the KPI at the top still counted it. The measurement is now made correctly
# where the number is produced (`discover_forks.classify_releases`), which
# reports 0 for those two and names the release we actually build as
# `base_release`, so the rows no longer appear and the overrides have nothing
# left to override. A note here may explain a real gap; it may never contradict
# a number this page prints.
PIN_NOTES = {
    "Trilinos": {
        "kind": "held",
        "en": "Frozen at 16.2.1 on purpose: Trilinos 17.x deletes AztecOO, Amesos, Ifpack, EpetraExt and Isorropia, which Xyce still needs in order to configure and link.",
        "zh": "刻意凍結在 16.2.1：Trilinos 17.x 已刪除 AztecOO、Amesos、Ifpack、EpetraExt、Isorropia，而 Xyce 至今仍需要它們才能 configure 與連結。",
        "checked": "gh api repos/trilinos/Trilinos/contents/packages/{aztecoo,ifpack,epetraext,isorropia,amesos}?ref=trilinos-release-16-2-1 -> all present; the same five paths at trilinos-release-17-1-1 -> HTTP 404. Xyce side: XyceSuperBuild.cmake:84,91,92 sets Trilinos_ENABLE_EpetraExt / Isorropia / AztecOO = ON.",
    },
}

STYLE = """<style>
.fork-wrap{max-width:1140px;margin:0 auto;padding:0 1.25rem}
.fork-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:2rem 0}
.fork-stale{background:#3a1d1d;border:1px solid #7a3030;border-radius:14px;padding:.9rem 1.15rem;margin:0 0 1rem;color:#ffd9d9;font-size:.95rem;line-height:1.5}
.fork-stale b{color:#fff}
.fork-kpi.is-stale{border-color:#7a3030;background:#221316}
.fork-kpi.is-stale .n{color:#ff9d9d}
.fork-kpi{background:#12161c;border:1px solid #232a33;border-radius:14px;padding:1.1rem 1.25rem}
.fork-kpi .n{font-size:1.9rem;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums;color:#f5f8fb;word-break:break-all}
.fork-kpi .l{font-size:.8rem;color:#9fb0c0;margin-top:.35rem;text-transform:uppercase;letter-spacing:.05em}
.fork-table{width:100%;border-collapse:collapse;font-size:.92rem;margin-top:1rem}
.fork-table th{text-align:left;font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted,#6b7684);font-weight:600;padding:.6rem .7rem;border-bottom:1px solid var(--border,#232a33)}
.fork-table td{padding:.7rem .7rem;border-bottom:1px solid var(--border,#232a33);vertical-align:middle}
.fork-table tr.trow{cursor:pointer;transition:background .12s}
.fork-table tr.trow:hover{background:rgba(120,150,180,.06)}
.fork-tool{font-weight:650}
.fork-tool .role{display:block;font-size:.74rem;color:var(--text-muted,#6b7684);font-weight:400;margin-top:.15rem}
.fork-mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:.82rem}
.pilln{display:inline-block;min-width:1.9rem;text-align:center;font-family:ui-monospace,monospace;font-size:.8rem;font-weight:700;padding:.12rem .5rem;border-radius:20px}
.pilln.zero{color:var(--text-muted,#6b7684);background:transparent;border:1px solid var(--border,#232a33)}
.pilln.behind{color:#fff;background:#c07d1e}
.pilln.ahead{color:#fff;background:#2f8f6b}
.fork-gap{margin:1.2rem 0 2rem;padding:1rem 1.2rem;border-left:3px solid #c07d1e;background:rgba(192,125,30,.07);border-radius:0 6px 6px 0}
.fork-gap h4{margin:0 0 .5rem;font-size:.98rem}
.fork-gap-list{margin:.3rem 0 .7rem;padding-left:1.2rem;font-size:.9rem;line-height:1.7}
.fork-gap-note{margin:0;font-size:.82rem;color:var(--text-muted,#6b7684);line-height:1.6}
.fork-rel{border-left-color:#63a8ea;background:rgba(99,168,234,.07)}
.fork-rel .fork-gap-list>li{margin-bottom:.55rem}
.fork-rel .enh-pill{margin-left:.3rem}
.fork-detail{background:rgba(120,150,180,.05)}
.fork-detail td{padding:0}
.fork-detail .inner{padding:1rem 1.2rem;display:none}
.fork-detail.open .inner{display:block}
.fork-detail h5{margin:.2rem 0 .5rem;font-size:.85rem}
.fork-detail h5.enh-hdr{color:var(--text,#0a0f1a);font-weight:700;font-size:.92rem;margin:.1rem 0 .6rem}
.fork-commit{font-family:ui-monospace,monospace;font-size:.78rem;color:var(--text-muted,#6b7684);padding:.2rem 0;display:flex;gap:.6rem}
.fork-commit a{color:inherit;text-decoration:none;border-bottom:1px dotted currentColor}
.fork-commit .sha{color:#63a8ea;flex:none}
.fork-verd{font-family:ui-monospace,monospace;font-size:.78rem}
.fork-verd.MERGED{color:#2f8f6b}.fork-verd.DEFERRED{color:#c07d1e}.fork-verd.SKIP{color:var(--text-muted,#6b7684)}.fork-verd.CLEAN{color:#63a8ea}
.fork-caption{color:var(--text-muted,#6b7684);font-size:.85rem;margin:.4rem 0 0}
.fork-scroll{overflow-x:auto}
@media(max-width:760px){.fork-hide-sm{display:none}}
/* enhancement backlog (capability matrix vs commercial) inside each tool detail */
.enh-wrap{margin-top:1rem;border-top:1px solid var(--border,#232a33);padding-top:.7rem}
.enh-wrap>summary{cursor:pointer;font-size:.85rem;font-weight:600;color:var(--text,#0a0f1a);list-style:none;user-select:none}
.enh-wrap>summary::-webkit-details-marker{display:none}
.enh-wrap>summary::before{content:"▸";display:inline-block;margin-right:.45rem;color:var(--text-muted,#6b7684);transition:transform .12s}
.enh-wrap[open]>summary::before{transform:rotate(90deg)}
.enh-mini{font-weight:400;color:var(--text-muted,#6b7684);font-size:.8rem}
.enh-counts{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:.7rem 0 .3rem}
.enh-pill{font-family:ui-monospace,monospace;font-size:.74rem;font-weight:700;padding:.1rem .5rem;border-radius:20px;border:1px solid var(--border,#232a33);white-space:nowrap}
.enh-pill.done{color:#2f8f6b}.enh-pill.todo{color:var(--text-secondary,#3b4259)}.enh-pill.deferred{color:#c07d1e}.enh-pill.external{color:var(--text-muted,#6b7684)}
.enh-summary{color:var(--text-muted,#6b7684);font-size:.8rem;flex:1 1 260px;min-width:200px;font-weight:400}
.enh-table{width:100%;border-collapse:collapse;font-size:.8rem;margin-top:.35rem}
.enh-table th{text-align:left;font-size:.66rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted,#6b7684);font-weight:600;padding:.35rem .55rem;border-bottom:1px solid var(--border,#232a33);white-space:nowrap}
.enh-table td{padding:.38rem .55rem;border-bottom:1px solid rgba(120,150,180,.09);vertical-align:top}
.enh-area td{font-weight:700;font-size:.72rem;color:var(--text-secondary,#3b4259);background:rgba(120,150,180,.06);text-transform:uppercase;letter-spacing:.04em;padding-top:.5rem}
.enh-feat{font-weight:550;color:var(--text,#0a0f1a);min-width:230px}
.enh-note{display:block;color:var(--text-muted,#6b7684);font-weight:400;font-size:.74rem;margin-top:.15rem;line-height:1.35}
.enh-comm{color:var(--text-muted,#6b7684);font-family:ui-monospace,monospace;font-size:.72rem;min-width:120px}
.enh-st{text-align:center;font-size:.9rem;white-space:nowrap}
.enh-pr{font-family:ui-monospace,monospace;font-size:.72rem;color:#63a8ea;white-space:nowrap}
.enh-cl{font-size:.72rem;color:var(--text-muted,#6b7684);white-space:nowrap}
.enh-r.enh-done .enh-feat{color:var(--text-secondary,#3b4259);font-weight:400}
</style>"""

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EDA Forks — Vibe-IC</title>
    <meta name="description" content="Upstream fork tracking for the open-source EDA tools Vibe-IC forks and enhances. A daily Gatekeeper checks each upstream, adversarially reviews new commits, auto-merges on a green full-EDA regression, and defers with a reason on red — full provenance per tool.">
    <link rel="stylesheet" href="css/style.css">
    <link rel="stylesheet" href="https://rsms.me/inter/inter.css">
    <link rel="icon" type="image/svg+xml" href="img/favicon.svg">
    __STYLE__
</head>
<body>
__NAV__

<section class="hero">
    <div class="hero-bg" aria-hidden="true"></div>
    <div class="container" style="position:relative;z-index:1;">
        <div class="hero-grid" style="grid-template-columns: 1fr;">
            <div class="hero-left" style="max-width: 900px; margin: 0 auto; text-align: center;">
                <p class="eyebrow" data-en="Open-source EDA · upstream fork tracking · daily gatekeeper" data-zh="開源 EDA · 上游 fork 追蹤 · 每日 gatekeeper">Open-source EDA · upstream fork tracking · daily gatekeeper</p>
                <h1 data-en="Every forked tool, tracked to upstream." data-zh="每個 fork 的工具，緊追上游。">Every forked tool, tracked to upstream.</h1>
                <p class="hero-sub" data-en="Vibe-IC forks the open-source EDA stack and enhances it. To avoid drifting from the projects we depend on, a Gatekeeper runs every day: it checks each upstream for new commits, adversarially reviews them, AUTO-MERGES on a green full-EDA regression, and defers with a written reason on red. Below is the full provenance — when each tool was forked, from which version, where it is now, and every commit merged in between." data-zh="Vibe-IC fork 了整套開源 EDA 並加以強化。為了不與所依賴的專案脫節，一個 Gatekeeper 每天執行：檢查各上游是否有新 commit、對抗式審查、在完整 EDA 回歸全綠時自動 merge、紅燈則附書面理由 defer。以下是完整履歷 — 每個工具何時 fork、基於哪個版本、現在到哪、以及中間 merge 了哪些 commit。">Vibe-IC forks the open-source EDA stack and enhances it. To avoid drifting from the projects we depend on, a Gatekeeper runs every day: it checks each upstream for new commits, adversarially reviews them, AUTO-MERGES on a green full-EDA regression, and defers with a written reason on red. Below is the full provenance — when each tool was forked, from which version, where it is now, and every commit merged in between.</p>
            </div>
        </div>
    </div>
    <div class="grid-lines" aria-hidden="true"></div>
</section>

<section>
    <div class="fork-wrap">
        <div class="section-header" style="text-align:left">
            <p class="eyebrow" data-en="Method" data-zh="做法">Method</p>
            <h2 data-en="Pristine upstream, minimal patches, gated auto-merge" data-zh="乾淨上游、最小補丁、閘門式自動合併">Pristine upstream, minimal patches, gated auto-merge</h2>
            <p data-en="Each fork keeps the upstream source pristine and carries our enhancements as a small, rebasable patch series (the Debian / kernel model). The daily Gatekeeper detects new upstream commits, rebases our patches on top, runs the full open-source EDA regression (build + the real benchmark ICs), and only auto-merges when that review is green — otherwise it defers the commit and records why. Every carried patch tracks whether it has been sent upstream, so our permanent delta stays minimal." data-zh="每個 fork 保持上游原始碼零修改，把我們的強化以一小疊可重貼的補丁序列揹著（Debian／kernel 模型）。每日 Gatekeeper 偵測上游新 commit、把我們的補丁重貼上去、跑完整開源 EDA 回歸（build + 真實 benchmark IC），只有審查全綠才自動 merge — 否則 defer 該 commit 並記錄原因。每個揹著的補丁都追蹤是否已送回上游，讓永久 delta 保持最小。">Each fork keeps the upstream source pristine and carries our enhancements as a small, rebasable patch series (the Debian / kernel model). The daily Gatekeeper detects new upstream commits, rebases our patches on top, runs the full open-source EDA regression (build + the real benchmark ICs), and only auto-merges when that review is green — otherwise it defers the commit and records why. Every carried patch tracks whether it has been sent upstream, so our permanent delta stays minimal.</p>
        </div>

        <div class="fork-stale" id="forkStale" hidden></div>
        <div class="fork-metrics" id="forkMetrics"></div>
<div class="fork-gap" id="forkGap"></div>

<div class="fork-gap fork-rel" id="forkRel"></div>
        <p class="fork-caption" id="forkUpdated"></p>
        <p class="fork-caption" id="enhSummary"></p>

        <div class="fork-scroll">
        <table class="fork-table">
            <thead><tr>
                <th data-en="Tool" data-zh="工具">Tool</th>
                <th data-en="Upstream" data-zh="上游">Upstream</th>
                <th class="fork-hide-sm" data-en="Pinned in image" data-zh="Image 內鎖定">Pinned in image</th>
                <th data-en="Our patches" data-zh="我們的補丁">Our patches</th>
                <th class="fork-hide-sm" data-en="On release" data-zh="目前 release">On release</th>
                <th data-en="Upstream latest" data-zh="上游最新">Upstream latest</th>
                <th data-en="In image" data-zh="有進 image">In image</th>
                <th data-en="Last check" data-zh="最後檢查">Last check</th>
            </tr></thead>
            <tbody id="forkRows"></tbody>
        </table>
        </div>
        <p class="fork-caption" data-en="Click a tool for its carried patches, the upstream commits still pending, and the daily sync log. Data refreshes every day." data-zh="點一個工具可看它揹著的補丁、仍待合的上游 commit、以及每日同步 log。資料每天更新。">Click a tool for its carried patches, the upstream commits still pending, and the daily sync log. Data refreshes every day.</p>
    </div>
</section>

__GAP__

__INVENTORY__

__FOOTER__

<script>
const LEDGERS = __DATA__;
const REPORT = __REPORT__;
const ENH = __ENH__;
const PINNOTES = __PINNOTES__;
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pill = (n, kind) => `<span class="pilln ${n?kind:'zero'}">${n||0}</span>`;
// THREE CLAIMS, THREE RENDERINGS. `behind_releases` is null under TWO different
// statuses and they are different sentences:
//
//   measured    — every upstream release was checked; the number is the answer,
//                 and 0 means "checked, nothing there".
//   unknown     — at least one release could not be decided. There is no number.
//   not-probed  — the question has no subject: nothing pins this tool into the
//                 image, or the upstream has published no release and no tag.
//                 ELEVEN rows on the corpus the day this was written.
//
// `(d.behind_releases)||0` collapsed the last two onto the first and printed a
// confident "0" pill for a row nobody compared against anything — the same
// fabrication as the count this whole page was rebuilt to stop making. A test
// greps the page for the coercion and executes these four readers in node
// against all three statuses.
const relStatus = d => {
  if(!d) return "not-probed";
  const s = d.behind_releases_status;
  if(s === "measured" || s === "unknown" || s === "not-probed") return s;
  if(d.behind_releases == null)
    return (d.undetermined_releases||[]).length > 0 ? "unknown" : "not-probed";
  return "measured";
};
const relUnknown = d => relStatus(d) === "unknown";
const relGap = d => relStatus(d) === "measured"
  ? (typeof (d && d.behind_releases) === "number" ? d.behind_releases : null)
  : null;
const relPill = d => {
  const s = relStatus(d);
  if(s === "unknown")
    return `<span class="pilln behind" title="containment undetermined — not a count">?</span>`;
  if(s === "not-probed")
    return `<span class="pilln zero" title="no release was probed — nothing pins this tool, or the upstream publishes no release or tag">n/a</span>`;
  return pill(relGap(d), 'behind');
};

// per-tool enhancement backlog (capability matrix vs commercial EDA), grouped by area
const ENH_ICON = {done:"✅", todo:"⬜", deferred:"🔷", external:"⚪"};
function enhBlock(tool){
  const e = ENH && ENH[tool];
  if(!e || !e.rows || !e.rows.length) return "";
  const c = e.counts || {};
  const openN = (c.todo||0) + (c.deferred||0) + (c.external||0);
  const prank = r => { const p=String(r.priority||'').match(/P(\d+)/); return p?+p[1]:99; };
  const thead = `<thead><tr>`
    + `<th data-en="Feature / Capability" data-zh="功能 / 能力">Feature / Capability</th>`
    + `<th data-en="Commercial equivalent" data-zh="商用對標">Commercial equivalent</th>`
    + `<th data-en="Status" data-zh="狀態">Status</th>`
    + `<th data-en="Priority" data-zh="優先">Priority</th>`
    + `<th data-en="Class" data-zh="類別">Class</th></tr></thead>`;
  // attr-safe double-escape: getAttribute()->innerHTML in setLang must render < > literally
  const escA = s => esc(s).replace(/&/g, "&amp;");
  // bilingual span: EN visible now, data-zh swapped in by the site's setLang; ZH falls back to EN
  const bil = (en, zh, cls) => `<span${cls?` class="${cls}"`:""} data-en="${escA(en)}" data-zh="${escA(zh||en)}">${esc(en)}</span>`;
  const areasZh = e.areas_zh || {};
  // grouped table body from a row-filter; open rows sort by priority within each area
  function tableBody(filter, sortByPrio){
    const groups = []; const idx = {};
    e.rows.filter(filter).forEach(r => {
      const a = r.area || "";
      if(!(a in idx)){ idx[a] = groups.length; groups.push([a, []]); }
      groups[idx[a]][1].push(r);
    });
    if(!groups.length) return "";
    return groups.map(([area, rs0]) => {
      const rs = sortByPrio ? rs0.slice().sort((x,y)=>prank(x)-prank(y)) : rs0;
      const rows = rs.map(r => {
        const note = r.notes ? `<span class="enh-note" data-en="${escA(r.notes)}" data-zh="${escA(r.notes_zh||r.notes)}">${esc(r.notes)}</span>` : "";
        return `<tr class="enh-r enh-${esc(r.status)}">`
          + `<td class="enh-feat">${bil(r.feature, r.feature_zh)}${note}</td>`
          + `<td class="enh-comm">${esc(r.commercial)}</td>`
          + `<td class="enh-st" title="${esc(r.status)}">${ENH_ICON[r.status]||""}</td>`
          + `<td class="enh-pr">${esc(r.priority||"")}</td>`
          + `<td class="enh-cl">${esc(r.class||"")}</td></tr>`;
      }).join("");
      return `<tr class="enh-area"><td colspan="5">${bil(area, areasZh[area])}</td></tr>${rows}`;
    }).join("");
  }
  const counts = `<div class="enh-counts">`
    + `<span class="enh-pill todo">⬜ ${c.todo||0}</span>`
    + `<span class="enh-pill deferred">🔷 ${c.deferred||0}</span>`
    + `<span class="enh-pill external">⚪ ${c.external||0}</span>`
    + `<span class="enh-pill done">✅ ${c.done||0}</span>`
    + `<span class="enh-summary" data-en="${escA(e.summary||"")}" data-zh="${escA(e.summary_zh||e.summary||"")}">${esc(e.summary||"")}</span></div>`;
  // OPEN (to-do / deferred / external) shown by DEFAULT, priority-sorted
  const openBody = tableBody(r => r.status !== 'done', true);
  const openTable = openBody
    ? `<div class="fork-scroll"><table class="enh-table">${thead}<tbody>${openBody}</tbody></table></div>`
    : `<p class="fork-caption" data-en="All tracked capabilities delivered." data-zh="所有追蹤的能力皆已完成。">All tracked capabilities delivered.</p>`;
  // DONE hidden by default — click to expand
  const doneBody = tableBody(r => r.status === 'done', false);
  const doneBlock = doneBody
    ? `<details class="enh-wrap enh-done"><summary><span data-en="✓ Completed (${c.done||0}) — click to expand" data-zh="✓ 已完成（${c.done||0}）— 點開展開">✓ Completed (${c.done||0})</span></summary>`
      + `<div class="fork-scroll"><table class="enh-table">${thead}<tbody>${doneBody}</tbody></table></div></details>`
    : "";
  const hdr = `<h5 class="enh-hdr" data-en="Enhancement backlog vs commercial EDA — ${openN} open · ${c.done||0} done" data-zh="對照商用 EDA 的強化 backlog — ${openN} 待處理 · ${c.done||0} 已完成">Enhancement backlog vs commercial EDA — ${openN} open · ${c.done||0} done</h5>`;
  return hdr + counts + openTable + doneBlock;
}

(function(){
  const imageVer = (LEDGERS[0]||{}).image_version || "—";
  const totalPatches = LEDGERS.reduce((a,d)=>a+(d.ahead||0),0);
  // THE TWO NUMBERS THIS PAGE EXISTS FOR.
  //
  // 1. How far behind upstream is each fork we ship? Measured in COMMITS, not releases:
  //    projects tag on wildly different conventions, and a tag says nothing about whether
  //    the hotfix we care about is in our image. A commit gap does.
  // 2. How much of our own work is in the image that upstream does not have?
  //
  // `behind_commits === null` means the comparison COULD NOT BE RUN (a fork with no
  // resolvable upstream branch, or one past GitHub's compare cap). That is not zero, and
  // folding it into zero would report a clean gap we never measured — so it is carried as
  // its own number and rendered beside the gap, never summed into it.
  //
  // …AND `behind_commits` IS NOT A GAP FOR EVERY ROW (vibeic-eda#79 / #81). It is a
  // true statement about two git histories on ALL of them; whether it names something
  // this image can CLOSE depends on what the Dockerfile value is. `pin_kind` is the
  // ledger's record of that:
  //
  //     "pin"                 a build INPUT — the build clones/checks out at it, so
  //                           being behind IS a gap and must be counted.
  //     "contents_assertion"  a CLAIM about a PREBUILT artefact nothing fetches
  //                           (`ARG <TOOL>_VOLUME_CONTENTS_SHA`). Advancing it rebuilds
  //                           nothing and turns a true statement false. There is no ref
  //                           for it to be behind, so it is not a gap.
  //
  // MEASURED 2026-08-04, and this card was the wrong side of it: it read
  // "39 across 7" while `fork_gap_report`, reading the same ledger at the same moment,
  // read "21 across 6". The entire difference was `open_pdks`' 18 — 86% of this
  // headline — and that number had already been cited as the reason to advance the ARG
  // in #74 and #78, both refused by the build guard for the same reason. The page is
  // the number people act on, so the page was the one doing the damage.
  //
  // READ, NOT RE-DERIVED. `pin_kinds.classify` is the single authority; `discover_forks`
  // puts its answer on the row. Re-deriving it here from the ARG NAME would be a second
  // copy of one rule, which is exactly how two programs came to say opposite things
  // about the same four pins (#29) — and it would get the corroboration wrong: an
  // assertion-named ARG that a fetch step reads is a MISNAMED PIN, comes back as `pin`,
  // and must keep being counted. This is a FIELD, not a list of tool names: the next
  // prebuilt artefact wired in is classified on the morning it appears, with no edit here.
  const isAssertion   = d => d.pin_kind === "contents_assertion";
  // EXCLUDED FROM BOTH the gap and the could-not-measure count, and the second half is
  // the one that takes thought — `fork_gap_report` records the same reasoning. Dropping
  // an assertion from the gap is obvious. Leaving it in "could not be measured" would
  // replace a false gap with a false open question that never closes. "Not measured"
  // means the question is still open; this question is answered.
  const gapRows       = LEDGERS.filter(d=>!isAssertion(d));
  // A row that carries no `pin_kind` AT ALL is a third state — a ledger written before
  // the field existed. It is COUNTED, deliberately: not having classified a row is not
  // evidence that it has no gap, and a page that dropped it would under-report the one
  // direction that must never read as healthy. It is counted AND named, so the silent
  // case that produced this defect cannot recur unseen.
  const kindUnrecorded = gapRows.filter(d=>!("pin_kind" in d)
                                        && (d.behind_commits||0) > 0);
  const behindKnown   = gapRows.filter(d=>typeof d.behind_commits === "number");
  const behindUnknown = gapRows.length - behindKnown.length;
  const commitsBehind = behindKnown.reduce((a,d)=>a+(d.behind_commits||0),0);
  const forksBehind   = behindKnown.filter(d=>(d.behind_commits||0)>0).length;
  // ONE NUMBER FOR TWO CONDITIONS WITH OPPOSITE FIXES was this card's remaining
  // defect after the contents-assertion one. `behind_commits` is pin->upstream,
  // which is SYNC LAG plus RELEASE LAG:
  //
  //     SYNC LAG     our fork trails upstream    -> merge upstream in
  //     RELEASE LAG  the image's pin trails us   -> bump the pin, rebuild
  //
  // MEASURED 2026-08-04: slang was `sync 0 · release 1`. Its fork was EXACTLY
  // LEVEL with upstream, and this card said "behind upstream by 1 commit" — which
  // reads as "go merge upstream", and merging would have changed nothing. The one
  // action that closes it, advancing `SLANG_REF` and rebuilding, was not on the
  // page. A number that sends the reader at the wrong lever is worse than no
  // number, because they act on it.
  //
  // READ FROM THE LEDGER, NOT PARTITIONED HERE. `discover_forks.lag_split` counts
  // both halves in the clone, against the same upstream commit `behind_commits`
  // was counted against. The browser has no clone; any split computed here would
  // be a guess dressed as a measurement, and a second implementation of a rule
  // `discover_forks` already owns (#29 is what two copies of one rule cost).
  //
  // SUMMED OVER THE ROWS THAT CARRY THEM, and counted separately when they do not:
  // a ledger written before these fields existed has no split, and rendering that
  // as `sync 0 · release 0` beside a non-zero gap would invent a clean answer for
  // a question never asked.
  const splitKnown    = behindKnown.filter(d=>typeof d.sync_lag === "number"
                                           && typeof d.release_lag === "number");
  const splitUnknown  = behindKnown.filter(d=>(d.behind_commits||0)>0
                                           && !(typeof d.sync_lag === "number"
                                             && typeof d.release_lag === "number")).length;
  const syncLag       = splitKnown.reduce((a,d)=>a+(d.sync_lag||0),0);
  const releaseLag    = splitKnown.reduce((a,d)=>a+(d.release_lag||0),0);
  // Named, because they are the rows whose FIX the page cannot yet state. A fork
  // that is behind only by release lag looks identical, in the combined number, to
  // one that needs an upstream merge.
  const releaseOnly   = splitKnown.filter(d=>(d.release_lag||0)>0 && !(d.sync_lag||0))
                                  .map(d=>d.tool||d.repo||"?");
  const syncOnly      = splitKnown.filter(d=>(d.sync_lag||0)>0 && !(d.release_lag||0))
                                  .map(d=>d.tool||d.repo||"?");
  // Kept for rendering, never summed into the number above: a row that vanishes is a
  // row nobody can audit, and WHICH upstream commit the shipped artefact carries is
  // the whole reason its ARG exists.
  const assertRows    = LEDGERS.filter(d=>isAssertion(d));
  const patchForks    = LEDGERS.filter(d=>(d.ahead||0)>0).length;
  // THE SECOND DAILY QUESTION: are our own commits actually IN the shipped image?
  // `totalPatches` answers "how many patches do we HOLD", which is not the same
  // thing. A fork can carry 57 of our commits and ship none of them, because the
  // image does not build from it — measured 2026-08-02: 4 of 36 forks
  // (ciel / open_pdks / sv2v / IHP-Open-PDK) are consumed from the BASE image, so
  // anything we land in them never reaches a user. Today they carry 0 patches, so
  // the shipped count happens to equal the held count — that is luck, not design,
  // and the day someone lands a fix in one of them the two numbers diverge
  // silently. See vibeic-eda#60. `integrated` is the ledger's own word for
  // "reaches the shipped image", by ARG pin or vendored inside one.
  // WRONG FIRST CUT, kept as the comment it earned. This keyed on `integrated`
  // — "does the image build from our fork at all" — and read
  // "345 shipped, 0 stranded". The image DOES build from our OpenROAD fork, and
  // three of our commits were still sitting past the pin, unshipped. `integrated`
  // is a fact about the Dockerfile; it says nothing about where the PIN stopped.
  // A card built to answer "do our commits reach the image" answered a
  // neighbouring question and said zero.
  //
  // `ours_unshipped` is now derived in the ledger as `pin..fork_tip` minus what
  // upstream already has — no author-name matching, so an outside contributor's
  // commit to our fork counts too.
  //
  // SUBSTANTIVE is the number shown, because all 5 unshipped commits on the day
  // this was written were MERGE commits whose content is upstream's. A merge of
  // ours carrying upstream work is not a fix of ours going unshipped, and
  // counting it would cry wolf on every sync.
  const unshipAll = LEDGERS.reduce((a,d)=>a+(typeof d.ours_unshipped==="number"?d.ours_unshipped:0),0);
  const unship    = LEDGERS.reduce((a,d)=>a+(typeof d.ours_unshipped_substantive==="number"?d.ours_unshipped_substantive:0),0);
  const unknownShip = LEDGERS.filter(d=>d.integrated && d.ours_unshipped==null).length;
  const totalOurs = LEDGERS.reduce((a,d)=>a+(d.ahead||0),0);
  const shipPatches = totalOurs - unshipAll;
  const notBuilt    = LEDGERS.filter(d=>!d.integrated).length;
  // THE TRACKING GAP. Forked, but neither worked on (ahead==0) nor kept current
  // (behind_commits>0) — the only rows that are nobody's deliberate state.
  // Measured on the PINNED ref each Dockerfile builds, NOT the fork default branch:
  // a fork whose default branch drifts while its pinned work branch is current is
  // fine by design, because the default branch takes part in no build.
  // `gapRows`, so a CONTENTS ASSERTION can never be listed here as a tracking gap
  // either. It does not qualify today only because it happens to carry patches of
  // ours (ahead=12); the day it does not, this list would name it under a heading
  // that says "behind upstream by N commits, carrying none of ours" — the same
  // wrong sentence the KPI above was printing, one block down.
  const gapTools = gapRows.filter(d=>(d.ahead||0)===0 && (d.behind_commits||0)>0)
                          .sort((a,b)=>(b.behind_commits||0)-(a.behind_commits||0));
  // Counted and stated SEPARATELY, never folded into the number beside it: a tool
  // whose release containment could not be decided is neither "has a new release"
  // nor "is level with upstream", and the KPI has to be able to say so.
  const unknownRel = LEDGERS.filter(d=>d.integrated && relUnknown(d)).length;
  // "Last daily check" must mean WHEN THE DATA ON THIS PAGE WAS GATHERED. It used to
  // read REPORT.date, which comes from a separate reports/YYYY-MM-DD.json written by a
  // different step — so when that step stopped running the card kept showing an old date
  // beside freshly-gathered numbers, and looked like the whole page was stale. The
  // ledger's own generated_at cannot drift from the ledger it stamps.
  const generatedAt = (LEDGERS[0]||{}).generated_at || (REPORT&&REPORT.date) || "";
  const lastCheck = generatedAt.slice(0,10) || "—";
  // STALENESS IS COMPUTED IN THE VIEWER'S BROWSER, not at build time, and that is
  // the whole point (vibeic-eda#58).
  //
  // The daily tick published NOTHING for two days. Both of its steps exited 1 into
  // a log nobody reads, and the only visible symptom was this page quietly
  // describing an older world — noticed by a person, not by anything we built.
  //
  // A build-time banner cannot catch that: this page is rebuilt BY the tick, so a
  // check that runs while building it is a check inside the process that stopped
  // running. It would never render. Computing the age when the page is VIEWED works
  // even when nothing has rebuilt it for a week, which is exactly the case that
  // needs to be loud.
  //
  // THRESHOLD 30 h — one daily round plus six hours of slack for a long one.
  //
  // Stated honestly, because the trade is real: a timestamp CANNOT distinguish "the
  // round ran and upstream was quiet" from "the round did not run" until enough time
  // has passed that no successful round could have left data this old. So this fires
  // on the morning AFTER a missed round, not at the moment one is missed, and there
  // is no threshold that does better from this signal alone. Measured against the
  // incident that prompted it (ledger frozen 2026-08-01T22:51): 30 h fires on 08-03,
  // 36 h fires the same day but later, and neither would have fired on 08-02.
  // Detecting the miss AS it happens needs the round to report its own liveness,
  // which is a separate mechanism and belongs to whatever runs the round.
  //
  // A clock-skewed FUTURE timestamp is reported as "could not tell" rather than as
  // freshness — the one direction that must never read as healthy.
  const genMs = Date.parse(generatedAt);
  const ageH  = isFinite(genMs) ? (Date.now() - genMs) / 3600000 : NaN;
  const stale = isFinite(ageH) && ageH > 30;
  const ahead = isFinite(ageH) && ageH < -1;
  const ageTxt = !isFinite(ageH) ? "" : (ageH < 48
      ? `${Math.round(ageH)} h` : `${Math.floor(ageH/24)} d`);
  const enhVals = Object.values(ENH||{});
  const enhRows = enhVals.reduce((a,e)=>a+((e.rows&&e.rows.length)||0),0);
  const enhDone = enhVals.reduce((a,e)=>a+(((e.counts&&e.counts.done)||0)),0);
  const enhOpen = enhVals.reduce((a,e)=>a+(((e.counts&&e.counts.todo)||0)+((e.counts&&e.counts.deferred)||0)),0);
  const kpis = [
    [LEDGERS.length, {en:"Tools tracked",zh:"追蹤工具"}],
    ["v"+imageVer, {en:"vibeic-eda version",zh:"vibeic-eda 版本"}],
    // The label states the SCOPE of the number, because the number's scope is what
    // was wrong (#81): a reader who takes it as "every commit any fork is behind"
    // will act on it, and two proposals already did. The assertion clause appears
    // only when there is one, and it names the block further down that carries them.
    [commitsBehind + (behindUnknown?` +${behindUnknown}?`:``)
       + (splitKnown.length ? ` (sync ${syncLag} · release ${releaseLag})` : ``),
     {en:`Commits behind upstream — sync ${syncLag} (fork trails upstream: merge upstream in) · release ${releaseLag} (image pin trails our fork: bump the pin, rebuild)`
         + ` — ${forksBehind} fork(s); +N? = could not be measured`
         + (releaseOnly.length ? `; RELEASE-ONLY, merging upstream would change nothing: ${esc(releaseOnly.join(", "))}` : ``)
         + (syncOnly.length ? `; sync-only: ${esc(syncOnly.join(", "))}` : ``)
         + (splitUnknown ? `; ${splitUnknown} fork(s) have a gap with NO recorded split — re-run discovery` : ``)
         + (assertRows.length ? `; ${assertRows.length} contents assertion(s) excluded — no ref to be behind, listed below` : ``),
      zh:`落後上游的 commit 數 — sync ${syncLag}（fork 落後上游：把上游 merge 進來）· release ${releaseLag}（image 的 pin 落後我們的 fork：bump pin 重建）`
         + ` —— ${forksBehind} 個 fork；+N? = 量不到，不等於零`
         + (releaseOnly.length ? `；純 RELEASE 落後，merge 上游不會有任何改變：${esc(releaseOnly.join(", "))}` : ``)
         + (syncOnly.length ? `；純 sync 落後：${esc(syncOnly.join(", "))}` : ``)
         + (splitUnknown ? `；有 ${splitUnknown} 個 fork 有缺口但沒有記錄拆分 —— 請重跑 discovery` : ``)
         + (assertRows.length ? `；另有 ${assertRows.length} 個內容宣告不計入 —— 沒有 ref 可以落後，列在下方` : ``)}],
    // Q2. `totalPatches` (how many patches we HOLD) was a second card here and
    // read 345 beside this one's 345 — the same number twice, which invites the
    // reader to think one of them means something else. Held-but-not-shipped is
    // the only interesting part of that difference, and it is already inside this
    // card as `(+N NOT shipped)`. So the inventory count is gone and the shipped
    // count stays.
    [`${shipPatches}/${totalOurs}` + (unship?` — ${unship} NOT shipped`:``) + (unknownShip?` +${unknownShip}?`:``),
     {en:`Our commits that reach the shipped image (${unshipAll} past the pin, ${unship} of them substantive; ${notBuilt} fork(s) not built from ours; +N? = could not be measured)`,
      zh:`真正進到出貨 image 的自有 commit（${unshipAll} 個卡在 pin 之後，其中 ${unship} 個是實質修改；${notBuilt} 個 fork 不是從我們的版本建置；+N? = 量不到）`}],
    // REMOVED: "Untracked forks (no patches, not synced)". It counted
    // `ahead==0 && behind>0` and today read 1 — OpenROAD-flow-scripts, which
    // FORKS.json itself describes as "a pure mirror… we carry NO commits of our
    // own here". Calling a deliberate mirror "lost contact" is a label that
    // manufactures an anomaly out of the intended state, and being behind is
    // already counted by the commits-behind card.
    // REMOVED: "Capabilities tracked" (566). That is capability coverage against
    // the commercial suites — a different question from either daily one, and on
    // a fork-sync page it reads as if 566 were something about upstream. It still
    // has its own explained section further down the page, where the sentence
    // around it says what it is.
    [lastCheck + (stale ? ` (${ageTxt} old)` : ``),
     {en:"Last daily check", zh:"最後每日檢查"}],
  ];
  document.getElementById("forkMetrics").innerHTML = kpis.map(([n,l])=>
    `<div class="fork-kpi${l.en==="Last daily check"&&stale?" is-stale":""}"><div class="n">${esc(n)}</div><div class="l" data-en="${l.en}" data-zh="${l.zh}">${l.en}</div></div>`).join("");
  const staleEl = document.getElementById("forkStale");
  if (staleEl && (stale || ahead || (generatedAt && !isFinite(genMs)))) {
    const en = ahead
      ? `This page's data is stamped in the FUTURE (${esc(lastCheck)}). Its age cannot be judged, so treat every number below as unverified.`
      : !isFinite(genMs)
      ? `This page's data carries no readable timestamp, so its age cannot be judged. Treat every number below as unverified.`
      : `<b>This page is ${esc(ageTxt)} out of date.</b> The daily round last gathered data on ${esc(lastCheck)}. Every number below describes that day, not today — a round that did not publish leaves these figures unchanged, which looks identical to upstream having been quiet.`;
    const zh = ahead
      ? `本頁資料的時間戳在未來（${esc(lastCheck)}），無法判斷新舊，以下數字請一律視為未經驗證。`
      : !isFinite(genMs)
      ? `本頁資料沒有可讀的時間戳，無法判斷新舊，以下數字請一律視為未經驗證。`
      : `<b>本頁已過期 ${esc(ageTxt)}。</b>最後一次每日彙集是 ${esc(lastCheck)}。以下每個數字描述的是那一天而不是今天 —— 一輪沒有發布會讓這些數字原封不動，看起來和「上游沒有變動」一模一樣。`;
    staleEl.innerHTML = `<span data-en="${en.replace(/"/g,'&quot;')}" data-zh="${zh.replace(/"/g,'&quot;')}">${en}</span>`;
    staleEl.hidden = false;
  }
  // CONTENTS ASSERTIONS — STATED, NOT SUBTRACTED (vibeic-eda#81). These rows leave
  // the gap count above because there is no ref for the artefact to be behind. They
  // are printed here, WITH their number, for the reason `fork_gap_report` prints its
  // own "CONTENTS ASSERTIONS (not pins, no gap to close)" line: a row that vanishes
  // is a row nobody can audit, and silently dropping it is #60's unverified pin
  // wearing the opposite mask. Everything rendered comes off the row itself —
  // `dockerfile_arg`, `pinned_ref`, `behind_commits` — so no tool is named in code.
  const assertBlock = !assertRows.length ? "" : (
    `<h4 data-en="Contents assertions — not a gap (${assertRows.length})" data-zh="內容宣告 —— 不是缺口（${assertRows.length}）">Contents assertions — not a gap (${assertRows.length})</h4>`
    + `<ul class="fork-gap-list">`
    + assertRows.map(d=>{
        const n = (typeof d.behind_commits === "number") ? d.behind_commits : null;
        const en = n === null
          ? `the image ships a PREBUILT artefact and <code>${esc(d.dockerfile_arg||"the ARG")}</code> records which upstream commit it carries`
          : `<b>${n}</b> upstream commit(s) exist beyond the PREBUILT artefact this image ships, and none of them is a gap this image can close`;
        const zh = n === null
          ? `image 出貨的是 PREBUILT 產物，<code>${esc(d.dockerfile_arg||"該 ARG")}</code> 記錄的是它帶著哪一個上游 commit`
          : `PREBUILT 產物之後還有 <b>${n}</b> 個上游 commit，但沒有任何一個是這個 image 補得掉的缺口`;
        return `<li><code>${esc(d.tool||d.repo||"?")}</code> <span class="fork-mono">${esc(d.pinned_ref||"")}</span> — <span data-en="${en.replace(/"/g,'&quot;')}" data-zh="${zh.replace(/"/g,'&quot;')}">${en}</span></li>`;
      }).join("")
    + `</ul>`
    + `<p class="fork-gap-note" data-en="Nothing fetches at these values. The artefact is built elsewhere and the image only ASSERTS what it carries, refusing to ship if the two disagree — so advancing the ARG would rebuild nothing and turn a true statement into a false one. Adopting newer upstream work here means CUTTING A NEW ARTEFACT, which is a decision rather than a sync. Classified from the ledger&#39;s own pin_kind, which comes from the Dockerfile text: an assertion-named value that a fetch step reads is a misnamed PIN and is counted as a gap above." data-zh="沒有任何步驟會去這些值抓東西。產物是別處建好的，image 只是 ASSERT 它帶著什麼，兩邊不一致就拒絕出貨 —— 所以把這個 ARG 往前推不會重建任何東西，只會把一句真話變成假話。要採用更新的上游工作，意思是重新切一份產物，那是一個決定而不是一次同步。分類來自 ledger 自己的 pin_kind，而它來自 Dockerfile 的內容：名字長得像宣告、卻被抓取步驟讀到的值，是命名錯誤的 PIN，會被算進上面的缺口。">Nothing fetches at these values — advancing the ARG would rebuild nothing and make a true statement false.</p>`);
  // A row nobody classified is counted above; here it is NAMED, so the state that
  // produced this defect can never again be invisible.
  const unrecBlock = !kindUnrecorded.length ? "" : (
    `<p class="fork-gap-note" data-en="${kindUnrecorded.length} row(s) above carry a commit gap but no recorded pin_kind, so it is not known whether their gap is closable. They are COUNTED — an unclassified row is not evidence of no gap — and named here: ${esc(kindUnrecorded.map(d=>d.tool||d.repo||"?").join(", "))}. Re-run the discovery pass to classify them." data-zh="上面有 ${kindUnrecorded.length} 列有 commit 缺口但沒有記錄 pin_kind，因此無法判斷那個缺口補不補得掉。它們有被計入（沒分類不等於沒缺口），並在這裡點名：${esc(kindUnrecorded.map(d=>d.tool||d.repo||"?").join(", "))}。重跑一次 discovery 就會分類。">${kindUnrecorded.length} row(s) carry a commit gap but no recorded pin_kind; they are counted, not assumed clean.</p>`);
  const gapEl = document.getElementById("forkGap");
  if (gapEl) {
    if (!gapTools.length) {
      gapEl.innerHTML = '<p data-en="Every fork is either carrying patches of ours or level with upstream." data-zh="每一個 fork 都不是揹著我們的補丁、就是跟上游齊平。">Every fork is either carrying patches of ours or level with upstream.</p>' + assertBlock + unrecBlock;
    } else {
      const rows = gapTools.map(d=>`<li><code>${esc(d.tool||d.repo||"?")}</code> — <span data-en="behind upstream by" data-zh="落後上游">behind upstream by</span> <b>${d.behind_commits}</b> <span data-en="commits, carrying none of ours" data-zh="個 commit，且沒有任何我們的補丁">commits, carrying none of ours</span></li>`).join("");
      gapEl.innerHTML = `<h4 data-en="The real tracking gap (${gapTools.length})" data-zh="真正的追蹤缺口（${gapTools.length}）">The real tracking gap (${gapTools.length})</h4><ul class="fork-gap-list">${rows}</ul><p class="fork-gap-note" data-en="Measured on the PINNED ref each Dockerfile builds (ARG &lt;TOOL&gt;_REF), not the fork default branch. A fork whose default branch drifts while its pinned work branch is current is fine by design — the default branch takes part in no build." data-zh="量的是每個 Dockerfile 實際建置的那個 PINNED ref（ARG &lt;TOOL&gt;_REF），不是 fork 的 default branch。一個 default branch 在漂、但 pinned 工作分支是最新的 fork，依設計就是正常的 —— default branch 不參與任何建置。">Measured on the PINNED ref each Dockerfile builds, not the fork default branch.</p>` + assertBlock + unrecBlock;
    }
  }

  // WHICH tools have a new release. The KPI above is a bare count, and a count the
  // reader cannot act on is not a report: it names no tool, no ref and no tag, so
  // there is nothing to go and look at. Rendered in the same idiom as the
  // tracking-gap block above. A row here is NOT automatically a gap — the marks
  // come from PIN_NOTES in build_page.py, where each one cites what was measured.
  //
  // Rows whose containment could not be DECIDED are listed here too, and they are
  // listed as undecided: no number, a distinct badge, and the literal error that
  // stopped the measurement. Leaving them out would publish "every other tool is
  // level with upstream" on evidence nobody has.
  const relTools = LEDGERS.filter(d=>relGap(d)>0 || (d.integrated && relUnknown(d)))
                          .sort((a,b)=>(relGap(b)==null?1e9:relGap(b))
                                      -(relGap(a)==null?1e9:relGap(a)));
  const relEl = document.getElementById("forkRel");
  if (relEl) {
    if (!relTools.length) {
      relEl.innerHTML = '<p data-en="Every tracked tool is on the newest upstream release." data-zh="每個追蹤中的工具都在上游最新的 release 上。">Every tracked tool is on the newest upstream release.</p>';
    } else {
      const BADGE = {
        "held":  {cls:"deferred", en:"HELD BY DESIGN", zh:"刻意凍結"}
      };
      const relRows = relTools.map(d=>{
        const tool = d.tool||d.repo||"?";
        const note = PINNOTES[tool];
        // `base_release` is now the newest release MEASURED to be contained in the
        // ref we build, so the page states what the ledger measured. The override
        // that used to sit in front of it here was a display-layer repair of a
        // wrong number, and it made the row disagree with its own count.
        const ours = d.base_release || d.pinned_ref || "?";
        const pin  = (d.pinned_ref && d.pinned_ref !== ours)
          ? ` <span class="fork-mono" style="color:var(--text-muted,#6b7684)">(${esc(d.pinned_ref)})</span>` : "";
        const latest = d.upstream_latest_release || "?";
        const unk = relUnknown(d);
        // A release EQUAL to the one we build is not a release we are missing, no
        // matter what the detector counted — listing it as "in between" would be a
        // plain falsehood on the page.
        const tags = (d.new_releases||[]).map(r=>r&&r.tag).filter(t=>t && t!==ours);
        const shown = tags.slice(0,6);
        const more = tags.length > shown.length ? ` +${tags.length-shown.length}` : "";
        const between = shown.length
          ? `<span class="enh-note"><span data-en="Tags in between:" data-zh="中間的 tag：">Tags in between:</span> <span class="fork-mono">${esc(shown.join(", ")+more)}</span></span>`
          : "";
        // The releases nobody could decide, named with the error that stopped each
        // one, so the reader's next move is a command rather than a guess.
        const und = (d.undetermined_releases||[]);
        const undTxt = und.length
          ? `<span class="enh-note"><span data-en="Undetermined:" data-zh="無法判定：">Undetermined:</span> <span class="fork-mono">${esc(und.slice(0,4).map(u=>`${u&&u.tag||"?"} — ${u&&u.error||"?"}`).join(" · "))}${und.length>4?` +${und.length-4}`:``}</span></span>`
          : "";
        const b = note && BADGE[note.kind];
        const badge = unk
          ? `<span class="enh-pill deferred" data-en="CONTAINMENT UNDETERMINED" data-zh="無法判定是否已包含">CONTAINMENT UNDETERMINED</span>`
          : (b ? `<span class="enh-pill ${b.cls}" data-en="${esc(b.en)}" data-zh="${esc(b.zh)}">${esc(b.en)}</span>` : "");
        const why = note ? `<span class="enh-note" data-en="${esc(note.en)}" data-zh="${esc(note.zh)}">${esc(note.en)}</span>` : "";
        // An undecided row states NO NUMBER. It used to be possible for this page
        // to print a count and then explain, in the prose beneath it, that the
        // count was not real; a page that argues with itself has already lost the
        // reader it was written for.
        const n = relGap(d);
        const tail = unk
          ? `<span data-en="the release gap could not be measured — ${und.length} upstream release(s) could not be checked for containment, so this is neither 0 nor a count" data-zh="這個 release 缺口量不出來 —— 有 ${und.length} 個上游 release 無法判定是否已包含，所以它既不是 0 也不是一個數字">the release gap could not be measured — ${und.length} upstream release(s) could not be checked for containment, so this is neither 0 nor a count</span>`
          : (b && note.kind==="held")
          ? `<b>${n}</b> <span data-en="measured to carry work we do not have — none of them adoptable" data-zh="經量測確實帶有我們沒有的東西 —— 但沒有一個能升上去">measured to carry work we do not have — none of them adoptable</span>`
          : `<b>${n}</b> <span data-en="release(s) ahead of us" data-zh="個 release 在我們前面">release(s) ahead of us</span>`;
        return `<li><code>${esc(tool)}</code> ${badge} — <span data-en="we build" data-zh="我們建置的是">we build</span> <span class="fork-mono">${esc(ours)}</span>${pin}, <span data-en="upstream latest" data-zh="上游最新">upstream latest</span> <span class="fork-mono">${esc(latest)}</span> — ${tail}${between}${undTxt}${why}</li>`;
      }).join("");
      relEl.innerHTML = `<h4 data-en="Tools with a new release (${relTools.length})" data-zh="有新 release 的工具（${relTools.length}）">Tools with a new release (${relTools.length})</h4><ul class="fork-gap-list">${relRows}</ul><p class="fork-gap-note" data-en="A release counts here only when it was MEASURED to carry work the ref each Dockerfile pins does not already contain — resolved to its target commit, deduplicated by commit, then tested by ancestry and by whether it changes any file relative to our pin. No publication date takes part. Not every row is a gap: HELD BY DESIGN is a deliberate ceiling — the newer release would break the build, so there is nothing here to close. CONTAINMENT UNDETERMINED means the check could not run for some release; that row has no count at all, and it is not zero. A row with no mark is an ordinary, unexplained gap." data-zh="一個 release 只有在「經量測確定帶有我們鎖定的 ref 尚未包含的內容」時才會被算進來 —— 先解析到它指向的 commit、依 commit 去重，再用 ancestry 以及「相對於我們的 pin 是否改到任何檔案」來測。完全不看發布日期。並不是每一列都是缺口：「刻意凍結」是刻意設下的天花板 —— 升上去會直接讓建置壞掉，這裡沒有東西要補。「無法判定是否已包含」代表某些 release 的檢查跑不起來；那一列根本沒有數字，而且它不等於零。沒有標記的那一列，才是一般的、還沒有人解釋的缺口。">A release counts here only when it was MEASURED to carry work our pinned ref does not contain. CONTAINMENT UNDETERMINED is not zero.</p>`;
    }
  }

  const rows = LEDGERS.map((d,i)=>{
    const ahead = d.ahead||0;
    const newRel = relGap(d);   // null = undetermined; never coerced to 0 here
    const last = (d.sync_log&&d.sync_log.length)?d.sync_log[d.sync_log.length-1]:null;
    const verd = last ? `<span class="fork-verd ${esc(last.verdict||'')}">${esc(last.verdict||'')}</span> <span style="color:var(--text-muted,#6b7684)">${esc((last.date||'').slice(0,10))}</span>` : '<span style="color:var(--text-muted,#6b7684)">—</span>';
    // HOW the ref is pinned into the image. Almost every fork is a Dockerfile
    // ARG; OpenSTA is the exception — it is pinned as OpenROAD's src/sta
    // SUBMODULE (.gitmodules repointed at the vibeic fork), which the ledger
    // records as `pinned_via`. Rendering it keeps the mechanism visible instead
    // of leaving one row silently unexplained.
    const pinHow = d.pinned_via
      ? esc(d.pinned_via)
      : (d.dockerfile_arg ? `Dockerfile ${esc(d.dockerfile_arg)}` : '');
    const pin = d.integrated
      ? `<span class="fork-mono">${esc(d.pinned_ref||'—')}</span>${d.vibeic_branch?`<br><span style="color:var(--text-muted,#6b7684);font-size:.72rem" class="fork-mono">${esc(d.vibeic_branch)}</span>`:''}${pinHow?`<br><span style="color:var(--text-muted,#6b7684);font-size:.68rem">${pinHow}</span>`:''}`
      : `<span style="color:var(--text-muted,#6b7684)" data-en="not layered" data-zh="未納入">not layered</span>`;
    // DOES OUR WORK IN THIS FORK ACTUALLY SHIP? A fork can carry N of our commits
    // and deliver none of them, because the image consumes the BASE image's copy
    // instead (vibeic-eda#60). Replacing the release-gap column with this one is
    // deliberate: release tagging conventions differ per project, so "behind by
    // N releases" measured something that was not the question, while "do our
    // fixes reach a user" is a question with an operational answer.
    const inImagePill = d => d.integrated
      ? `<span class="pill ok" data-en="yes" data-zh="有">yes</span>`
      : ((d.ahead||0) > 0
          ? `<span class="pill bad" data-en="NO — ${d.ahead} of our commit(s) not shipped" data-zh="沒有 —— 我們的 ${d.ahead} 個 commit 沒出貨">NO · ${d.ahead} stranded</span>`
          : `<span class="pill warn" data-en="no — image uses the base image's copy" data-zh="沒有 —— image 用的是 base image 的版本">no</span>`);
    const row = `<tr class="trow" data-i="${i}">
      <td class="fork-tool">${esc(d.tool)}<span class="role">${esc(d.role||'')}</span></td>
      <td class="fork-mono"><a href="${esc(d.upstream_url)}" target="_blank" rel="noopener" style="color:#63a8ea;text-decoration:none">${esc(d.upstream)}</a></td>
      <td class="fork-hide-sm">${pin}</td>
      <td>${pill(ahead,'ahead')}</td>
      <td class="fork-hide-sm fork-mono">${esc(d.base_release||d.pinned_ref||'—')}</td>
      <td class="fork-mono">${esc(d.upstream_latest_release||'—')}</td>
      <td>${inImagePill(d)}</td>
      <td>${verd}</td>
    </tr>`;
    const commit = c => `<div class="fork-commit"><a class="sha" href="${esc(c.url||'#')}" target="_blank" rel="noopener">${esc(c.sha)}</a><span>${esc(c.title)}</span><span style="margin-left:auto">${esc(c.date)}</span></div>`;
    const carried = (d.carried_patches&&d.carried_patches.length)
      ? `<h5 data-en="Patches we carry (${ahead}) — branch ${esc(d.vibeic_branch||'')}" data-zh="我們揹著的補丁（${ahead}）— 分支 ${esc(d.vibeic_branch||'')}">Patches we carry (${ahead})</h5>` + d.carried_patches.map(commit).join("")
      : (d.integrated
          ? `<h5 data-en="Patches we carry" data-zh="我們揹著的補丁">Patches we carry</h5><p class="fork-caption" data-en="Pinned to upstream with no local patches yet." data-zh="鎖定於上游，尚無本地補丁。">Pinned to upstream with no local patches yet.</p>`
          : `<h5 data-en="Not layered into the image" data-zh="未納入 image">Not layered into the image</h5><p class="fork-caption" data-en="Forked, but the image uses upstream directly (no fix warranted) — nothing to sync." data-zh="已 fork，但 image 直接用上游（無需修補）— 無需同步。">Forked, but the image uses upstream directly — nothing to sync.</p>`);
    // The releases nobody could DECIDE, in the detail drawer, with the literal
    // error each one failed on. Rendered BEFORE the "on the latest upstream
    // release" reassurance, and suppressing it: a fork with an undecided release
    // is not a fork that was checked and found level.
    const undRows = (d.undetermined_releases||[]);
    const undBlock = undRows.length
      ? `<h5 style="margin-top:1rem" data-en="Releases whose containment could not be determined (${undRows.length})" data-zh="無法判定是否已包含的 release（${undRows.length}）">Releases whose containment could not be determined (${undRows.length})</h5>`
        + undRows.map(r=>`<div class="fork-commit"><span class="sha">${esc(r&&r.tag||'?')}</span><span>${esc(r&&r.error||'undetermined')}</span><span style="margin-left:auto">${esc(r&&r.date||'')}</span></div>`).join("")
      : "";
    // Releases whose work is counted ONCE, under the release that carries it —
    // prereleases of a release we are already counting. They are NOT contained in
    // the ref we build (two of them measured 225 and 15 commits ahead of our pin),
    // so they get a heading that says what they are instead of one that says we
    // already have them.
    const foldRows = (d.folded_releases||[]);
    const foldBlock = foldRows.length
      ? `<h5 style="margin-top:1rem" data-en="Prereleases counted under a later release (${foldRows.length})" data-zh="併入後續 release 一起計算的 prerelease（${foldRows.length}）">Prereleases counted under a later release (${foldRows.length})</h5>`
        + foldRows.map(r=>`<div class="fork-commit"><span class="sha">${esc(r&&r.tag||'?')}</span><span>${esc((r&&r.why)||('counted under '+((r&&r.counted_under)||'?')))}</span><span style="margin-left:auto">${esc(r&&r.date||'')}</span></div>`).join("")
      : "";
    // Releases our pinned ref CARRIES — every commit they have exists in our ref
    // under a different sha — while our ref has since moved past them, so merging
    // one is not a no-op. They are not work we lack and are not counted; they are
    // also not "contained", which claims our tree is already theirs. MEASURED:
    // yices2 `yices-2.7.0`, cocotb `v1.5.0rc1` and klayout `v0.28.17-1` sat under
    // the contained heading while a three-way merge of each into our pin
    // CONFLICTS — on doc/sphinx/source/conf.py, documentation/source/release_notes.rst
    // and azure-pipelines.yml respectively, in every case because our pin is ahead
    // on that file. A row like this must not vanish from the page when it leaves
    // the contained bucket.
    const eqvRows = (d.patch_equivalent_releases||[]);
    const eqvBlock = eqvRows.length
      ? `<h5 style="margin-top:1rem" data-en="Releases our ref carries under different shas (${eqvRows.length})" data-zh="我們的 ref 以不同 sha 已帶著的 release（${eqvRows.length}）">Releases our ref carries under different shas (${eqvRows.length})</h5>`
        + `<p class="fork-caption" data-en="Every commit these releases have exists in the ref we build, patch-for-patch, under a different sha — and the ref has moved on since, so adopting one is not a no-op. Nothing here is work we are missing." data-zh="這些 release 的每一個 commit 都以不同的 sha 存在於我們建置的 ref 裡（patch 逐一比對相同），而我們的 ref 之後又往前走了，所以重新採用並不是零變更。這裡沒有我們缺少的工作。">Every commit these releases have exists in the ref we build under a different sha; nothing here is work we are missing.</p>`
        + eqvRows.map(r=>`<div class="fork-commit"><span class="sha">${esc(r&&r.tag||'?')}</span><span>${esc(r&&r.why||'')}</span><span style="margin-left:auto">${esc(r&&r.date||'')}</span></div>`).join("")
      : "";
    const relHead = relGap(d) == null ? (relUnknown(d) ? "?" : "n/a") : newRel;
    const rel = ((d.new_releases&&d.new_releases.length)
      ? `<h5 style="margin-top:1rem" data-en="New upstream releases to integrate (${relHead})" data-zh="待整合的上游新 release（${relHead}）">New upstream releases to integrate (${relHead})</h5>` + d.new_releases.map(r=>`<div class="fork-commit"><span class="sha">${esc(r.tag||'')}</span><span>${esc(r.why||'')}</span><span style="margin-left:auto">${esc(r.date||'')}</span></div>`).join("")
      : (d.integrated && !undRows.length
          // "Every upstream release was measured to be contained" is a claim about
          // a measurement, and a NOT-PROBED row made none: it has no pin, or the
          // upstream has published no release and no tag to compare against. The
          // reassurance is only printed where it is true.
          // "CONTAINED in the ref we build" is a claim about our TREE, and it is
          // not the claim a patch-equivalent row makes: our ref carries that work
          // under other shas and has since moved past it, so a merge of it is not
          // a no-op. Printing the stronger sentence over those rows would restate
          // on the page exactly the overstatement the bucket split removes from
          // the ledger, so where such a row exists the sentence says what was
          // actually measured.
          ? (relStatus(d) === "measured"
             ? (eqvRows.length
                ? `<h5 style="margin-top:1rem" data-en="Releases" data-zh="Release">Releases</h5><p class="fork-caption" data-en="Every upstream release was measured to be already in the ref we build — ${eqvRows.length} of them as work the ref carries under different shas rather than as an ancestor of it." data-zh="每一個上游 release 都經量測確認已在我們建置的 ref 裡 —— 其中 ${eqvRows.length} 個是以不同 sha 被我們的 ref 帶著，而不是它的祖先。">Every upstream release was measured to be already in the ref we build.</p>`
                : `<h5 style="margin-top:1rem" data-en="Releases" data-zh="Release">Releases</h5><p class="fork-caption" data-en="Every upstream release was measured to be contained in the ref we build." data-zh="每一個上游 release 都經量測確認已包含在我們建置的 ref 裡。">Every upstream release was measured to be contained in the ref we build.</p>`)
             : `<h5 style="margin-top:1rem" data-en="Releases" data-zh="Release">Releases</h5><p class="fork-caption" data-en="No upstream release was probed: this upstream publishes no release or tag, or nothing pins it into the image. That is not a gap of zero — nothing was compared." data-zh="沒有任何上游 release 被檢查過：這個上游沒有發布 release 或 tag，或是沒有東西把它鎖進 image。這不等於缺口為零 —— 根本沒有比較過。">No upstream release was probed — nothing was compared, which is not a gap of zero.</p>`)
          :"")) + eqvBlock + foldBlock + undBlock;
    const log = (d.sync_log&&d.sync_log.length)
      ? `<h5 style="margin-top:1rem" data-en="Daily sync log" data-zh="每日同步 log">Daily sync log</h5>` + d.sync_log.slice(-10).reverse().map(s=>`<div class="fork-commit"><span class="sha">${esc((s.date||'').slice(0,10))}</span><span class="fork-verd ${esc(s.verdict||'')}">${esc(s.verdict||'')}</span><span>${esc(s.note||'')}</span></div>`).join("")
      : "";
    const prov = `<details class="enh-wrap prov-wrap"><summary>`
      + `<span data-en="Carried patches · Releases · Daily sync log" data-zh="揹著的補丁 · Release · 每日同步 Log">Carried patches · Releases · Daily sync log</span>`
      + `</summary><div class="prov-inner">${carried}${rel}${log}</div></details>`;
    const detail = `<tr class="fork-detail" data-d="${i}"><td colspan="8"><div class="inner">${enhBlock(d.tool)}${prov}</div></td></tr>`;
    return row+detail;
  }).join("");
  document.getElementById("forkRows").innerHTML = rows;
  document.getElementById("forkUpdated").innerHTML = REPORT
    ? `<span data-en="Last Gatekeeper run: ${esc(REPORT.date||'')} · image vibeic-eda:${esc(imageVer)}" data-zh="最後 Gatekeeper 執行：${esc(REPORT.date||'')} · image vibeic-eda:${esc(imageVer)}">Last Gatekeeper run: ${esc(REPORT.date||'')}</span>`
    : `<span data-en="Ledger seeded from live state; the daily Gatekeeper has not run yet." data-zh="Ledger 由即時狀態種入；每日 Gatekeeper 尚未執行。">Ledger seeded from live state; the daily Gatekeeper has not run yet.</span>`;
  const enhEl = document.getElementById("enhSummary");
  if(enhEl && enhRows){
    enhEl.innerHTML = `<span data-en="Capability coverage vs commercial EDA: ${enhRows} capabilities tracked across ${enhVals.length} forks · ${enhDone} delivered · ${enhOpen} open (to-do + deferred). Click a tool to open its enhancement backlog." data-zh="對照商用 EDA 的能力覆蓋：跨 ${enhVals.length} 個 fork 追蹤 ${enhRows} 項能力 · 已交付 ${enhDone} · 待處理 ${enhOpen}（待做 + 延後）。點一個工具展開它的強化 backlog。">Capability coverage vs commercial EDA: ${enhRows} capabilities tracked across ${enhVals.length} forks · ${enhDone} delivered · ${enhOpen} open. Click a tool to open its enhancement backlog.</span>`;
  }

  document.querySelectorAll("tr.trow").forEach(tr => tr.addEventListener("click", ()=>{
    const d = document.querySelector(`tr.fork-detail[data-d="${tr.dataset.i}"]`);
    if(d) d.classList.toggle("open");
  }));
  // re-apply the site's language after injecting rows
  if(window.setLang){ try{ setLang(localStorage.getItem("lang")||"en"); }catch(e){} }
})();
</script>
<script src="js/main.js"></script>
</body>
</html>"""


# Canonical top-nav is OWNED BY THE OTHER PAGES, not by this generator. We EXTRACT the
# ---------------------------------------------------------------------------
# The tool inventory: three tables, MEASURED at build time by inventory.py.
#
# Deliberately not the same question as the ledger above. That one answers "what
# we forked and how far behind upstream it is"; this answers "what is actually in
# the image, what upstream ships that we skipped, and what PDK data we depend on"
# — and the fork column is the join between them.
#
# Nothing here is a pasted number. This page has already shipped a stale one:
# "all 15 forks" rendered above a 21-row ledger because the count was derived and
# the quantifier was not. Rows, counts and fork status are all recomputed per run;
# only the prose judgements (what a tool is for, whether we use it, why not) come
# from TOOL_NOTES.json, because measurement cannot answer those.
# ---------------------------------------------------------------------------

def _bi(en: str, zh: str, tag: str = "span", cls: str = "") -> str:
    """Bilingual cell, matching the page's existing data-en/data-zh switch.

    PASS TEXT, NOT MARKUP. Both arguments are escaped here, so an HTML entity
    written by the caller is escaped a second time: `&quot;` becomes `&amp;quot;`
    and the reader sees the literal characters `&quot;` on the page. The first
    draft of the three captions below did exactly that and it shipped -- measured
    on the live page, which carried `&amp;quot;no&amp;quot;` and `&amp;#39;s`.

    The trap is that the surrounding template is raw HTML where entities ARE
    correct, so the two conventions sit three lines apart. Anything routed
    through here takes a plain `"` and a plain `'`; only text written directly
    into a `data-en="..."` attribute in the template needs `&quot;`.
    `test_the_inventory_section_passes_text_not_markup_to__bi` pins it.
    """
    c = f' class="{cls}"' if cls else ""
    return (f'<{tag}{c} data-en="{_esc_attr(en)}" data-zh="{_esc_attr(zh)}">'
            f'{_esc_html(en)}</{tag}>')


def _esc_attr(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _esc_html(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _fork_cell(row: dict) -> tuple[str, str]:
    """The fork column, with three states kept distinct from 'no'.

    Collapsing them turns "could not determine" into "does not exist", which is
    the single most common way this column has been wrong.
    """
    st, f = row.get("state"), row.get("forks") or []
    if st == "not-a-tool":
        return "—", "—"
    if st == "pip":
        return "n/a — pip-installed", "不適用 — pip 安裝"
    if st == "unknown-upstream":
        return "? — upstream unconfirmed", "？— 上游未確認"
    if not f:
        return "no", "no"
    if len(f) == 1:
        return f"yes · vibeic/{f[0]}", f"yes · vibeic/{f[0]}"
    return (f"yes · vibeic/{f[0]} +{len(f)-1} duplicates",
            f"yes · vibeic/{f[0]} 另有 {len(f)-1} 份重複")


def render_inventory(inv: dict) -> str:
    a, b, c = inv["a"], inv["b"], inv["c"]
    n_used = sum(1 for r in a if r.get("used") and r["state"] != "not-a-tool")
    n_fork = sum(1 for r in a if r["state"] == "forked")
    gap_b = [r for r in b if r.get("used") and not r["forks"]]
    dupes = inv.get("dupes") or {}
    worst = max(dupes.items(), key=lambda kv: len(kv[1])) if dupes else None

    def rows_a():
        out = []
        for r in a:
            fe, fz = _fork_cell(r)
            used = ("—" if r["state"] == "not-a-tool"
                    else "yes" if r.get("used") else "no")
            out.append(
                "<tr><td><code>" + _esc_html(r["dir"]) + "</code></td><td>"
                + _bi(r["desc_en"], r["desc_zh"]) + "</td><td>"
                + _bi("added by us" if r["origin"] == "ours" else "base image",
                      "我們新增" if r["origin"] == "ours" else "base 映像")
                + "</td><td>" + _bi(fe, fz) + "</td><td>" + used + "</td></tr>")
        return "".join(out)

    def rows_b():
        out = []
        for r in b:
            f = r["forks"]
            fe = "no" if not f else f"yes · vibeic/{f[0]}" + (f" +{len(f)-1}" if len(f) > 1 else "")
            out.append(
                "<tr><td><code>" + _esc_html(r["tool"]) + "</code></td><td>"
                + _bi(r["desc_en"], r["desc_zh"]) + "</td><td>"
                + ("yes" if r.get("used") else "no") + "</td><td>" + _esc_html(fe)
                + "</td><td>" + _bi(r["reason_en"] or "—", r["reason_zh"] or "—")
                + "</td></tr>")
        return "".join(out)

    def rows_c():
        out = []
        for r in c:
            f = r["forks"]
            out.append(
                "<tr><td><code>" + _esc_html(r["dir"]) + "</code></td><td>"
                + _bi(r["desc_en"], r["desc_zh"]) + "</td><td><code>"
                + _esc_html(r["upstream"] or "—") + "</code></td><td>"
                + ("yes" if f else "<strong>no</strong>") + "</td></tr>")
        return "".join(out)

    # Anything the run could not measure is stated in the page, not swallowed.
    # A section that silently drops what it failed to read looks complete because
    # the missing thing contributed no rows.
    warn = ""
    problems = (inv.get("unmeasured") or []) + \
               [f"no note for image directory: {d}" for d in inv.get("missing_notes") or []] + \
               [f"a note exists for '{d}', which is not in the image measured "
                f"({inv.get('image')}) — either it was removed, or the image "
                f"predates it" for d in inv.get("stale_notes") or []]
    if problems:
        warn = ('<p class="fork-caption"><strong>' + _esc_html(
            "Not everything below could be measured, or measurement and notes "
            "disagree: " + "; ".join(problems)
            + ". A row affected by this is absent, not empty.") + "</strong></p>")

    dupe_note = ""
    if worst:
        up, names = worst
        dupe_note = "<p class=\"fork-caption\">" + _bi(
            f"{len(names)} of those forks are the same upstream ({up}) — "
            f"redundant copies, not coverage.",
            f"其中 {len(names)} 個是同一個上游（{up}）的重複 fork，不是覆蓋範圍。") + "</p>"

    # The three captions are bound HERE rather than inline below, and the reason is
    # not style: they are the only prose in this function that needs a literal quote
    # mark, an f-string expression part may not contain a backslash, and the one way
    # to write a quote inline is therefore an HTML entity — which `_bi` would escape
    # a second time and print as literal text. That is exactly the defect that
    # shipped. Bound out here they take a plain `"` and a plain `'`.
    cap_a = _bi(
        f"{len(a)} directories, {n_used} used by the flow, {n_fork} forked. Three "
        f'states are kept distinct from "no", because collapsing them turns "could '
        f'not determine" into "does not exist": pip-installed packages are not git '
        f"clones and forking their repo would not change what is installed; an "
        f"unconfirmed upstream is not an absent fork; and bin/sak/fpga are not tools.",
        f"{len(a)} 個目錄，流程使用 {n_used} 個，已 fork {n_fork} 個。三種狀態和「no」分開，"
        f"因為混在一起就是把「查不到」變成「不存在」：pip 安裝的不是 git clone，fork 它的 repo "
        f"不會改變安裝內容；上游未確認不等於沒有 fork；bin/sak/fpga 不是工具。")
    n_used_b = sum(1 for r in b if r.get("used"))
    gap_b_names = "" if not gap_b else " — " + ", ".join(r["tool"] for r in gap_b)
    cap_b = _bi(
        f"From the project's own metadata, not a README. We use {n_used_b} of "
        f"{len(b)}. Used but not forked: {len(gap_b)}{gap_b_names}.",
        f"來自該專案自己的 metadata，不是 README。{len(b)} 個中我們用 {n_used_b} 個。"
        f"用了但沒 fork：{len(gap_b)} 個{gap_b_names}。")
    cap_c = _bi(
        "Tables A and B are both built per tool, and PDKs are not tools — so PDK "
        "data sat outside the frame of both while the rule covering it was already "
        "in force. A rule that no audit can see is a rule that is not being audited. "
        "This table also states its own limit: it matches a directory to an upstream "
        "repository, and cannot establish which commit the data came from — PDKs "
        'carry no pin and no provenance file, so "which sky130A is this" is '
        'unanswerable beyond "open_pdks produced it".',
        "A 表和 B 表都是以工具為單位建的，而 PDK 不是工具——所以在涵蓋它的規則早已生效的情況下，"
        "PDK 資料一直在兩張表的視野之外。一個沒有任何稽核看得到的規則，就是沒在被稽核的規則。"
        "這張表也寫明自己的極限：它只能把目錄對到上游 repo，無法確認資料來自哪個 commit——"
        "PDK 沒有 pin 也沒有 provenance 檔，所以「這個 sky130A 是哪一版」目前答不出來，"
        "只能答「open_pdks 產的」。")

    return f'''<section>
    <div class="fork-wrap">
        <div class="section-header" style="text-align:left">
            <p class="eyebrow" data-en="Tool inventory" data-zh="工具清冊">Tool inventory</p>
            <h2 data-en="Everything in the image, and everything upstream ships that we skipped" data-zh="映像裡的全部，以及上游帶了而我們沒用的全部">Everything in the image, and everything upstream ships that we skipped</h2>
            <p data-en="The ledger above answers &quot;what we forked and how far behind it is&quot;. This answers a different question: what is actually installed, what upstream ships that we chose not to use and why, and what PDK data the flow depends on. Every row, count and fork status is measured when this page is built — the directories by listing them inside {_esc_attr(inv["image"])}, the forks and their parents from the GitHub API, the upstream list from the project&#39;s own tool metadata. Only the prose (what a tool is for, whether we use it, why not) is written by hand, because measurement cannot answer it." data-zh="上方帳本回答的是「我們 fork 了什麼、落後多少」。這裡回答另一個問題：實際裝了什麼、上游帶了什麼而我們選擇不用及其理由、流程依賴哪些 PDK 資料。每一列、每個數字、每個 fork 狀態都在產生這一頁時實測——目錄是進 {_esc_attr(inv["image"])} 裡列出來的，fork 與其上游來自 GitHub API，上游清單來自該專案自己的工具 metadata。只有敘述文字（工具做什麼、我們用不用、為什麼不用）是人工寫的，因為那不是量得出來的。">The ledger above answers "what we forked and how far behind it is". This answers a different question: what is actually installed, what upstream ships that we chose not to use and why, and what PDK data the flow depends on. Every row, count and fork status is measured when this page is built.</p>
        </div>
        {warn}
        <h3>{_bi("A · Every directory in the image", "A · 映像裡的每一個目錄", "span")} <span style="opacity:.6">({len(a)})</span></h3>
        <p class="fork-caption">{cap_a}</p>
        <div class="fork-wrap"><table class="fork-table"><thead><tr>
          <th>{_bi("directory","目錄")}</th><th>{_bi("function","功能")}</th><th>{_bi("origin","來源")}</th><th>{_bi("forked","有無 fork")}</th><th>{_bi("used","有用")}</th>
        </tr></thead><tbody>{rows_a()}</tbody></table></div>
        {dupe_note}

        <h3>{_bi("B · Every tool IIC-OSIC-TOOLS ships", "B · IIC-OSIC-TOOLS 帶的每一個工具", "span")} <span style="opacity:.6">({len(b)})</span></h3>
        <p class="fork-caption">{cap_b}</p>
        <div class="fork-wrap"><table class="fork-table"><thead><tr>
          <th>{_bi("tool","工具")}</th><th>{_bi("function","功能")}</th><th>{_bi("used","有用")}</th><th>{_bi("forked","有無 fork")}</th><th>{_bi("why not, if unused","不用的理由")}</th>
        </tr></thead><tbody>{rows_b()}</tbody></table></div>

        <h3>{_bi("C · PDK data", "C · PDK 資料", "span")} <span style="opacity:.6">({len(c)})</span></h3>
        <p class="fork-caption">{cap_c}</p>
        <div class="fork-wrap"><table class="fork-table"><thead><tr>
          <th>{_bi("PDK","PDK")}</th><th>{_bi("contents","內容")}</th><th>{_bi("upstream","上游")}</th><th>{_bi("forked","有無 fork")}</th>
        </tr></thead><tbody>{rows_c()}</tbody></table></div>
    </div>
</section>'''


# menu-anchor run from a sibling page at build time so eda-forks.html always matches the
# rest of the site (order, labels, zh text, item set) — regenerating can never drift it.
# Reference pages are tried in order; the first that parses wins. eda-forks.html is never
# its own reference. Fallback is the current canonical 7-item nav.
_NAV_REFERENCES = ("index.html", "evaluation.html", "flow.html", "platform.html", "manual.html")
_NAV_FALLBACK = "\n".join([
    '            <a href="/" data-en="System" data-zh="系統">System</a>',
    '            <a href="/flow.html" data-en="Flow" data-zh="流程">Flow</a>',
    '            <a href="/evaluation.html" data-en="Evaluation" data-zh="驗證">Evaluation</a>',
    '            <a href="/eda-forks.html" data-en="EDA Forks" data-zh="EDA 分叉">EDA Forks</a>',
    '            <a href="/platform.html" data-en="Platform" data-zh="開放平台">Platform</a>',
    '            <a href="/blog/" data-en="Blog" data-zh="部落格">Blog</a>',
    '            <a href="/manual.html" data-en="Manual" data-zh="使用手冊">Manual</a>',
])


def _extract_navlinks(html: str) -> list[str] | None:
    """Return the menu <a> anchors from a page's #navLinks, or None if not parseable.
    Only the leading site-menu anchors are taken — the run stops at the GitHub btn-nav
    or the lang-switch, so nav chrome (logo/lang/mobile toggle) is never pulled in."""
    i = html.find('id="navLinks"')
    if i < 0:
        return None
    j = html.find(">", i)
    tail = html[j + 1:]
    # cut at the first non-menu element inside the nav-links container
    for stop in ('class="btn-nav"', 'class="lang-switch"', "</div>"):
        k = tail.find(stop)
        if k != -1:
            tail = tail[:k]
    anchors = re.findall(r'<a\s+href="[^"]*"[^>]*\bdata-zh="[^"]*"[^>]*>.*?</a>', tail, re.S)
    return anchors or None


def _canonical_menu(out: Path) -> list[str]:
    """The site's canonical top-menu anchors (active stripped), from the first sibling
    page that parses; falls back to the built-in 7-item list on a first-ever build."""
    for ref in _NAV_REFERENCES:
        p = out.parent / ref
        if p.is_file():
            anchors = _extract_navlinks(p.read_text(encoding="utf-8"))
            if anchors:
                return [re.sub(r'\s+class="active"', "", a).strip() for a in anchors]
    return [a.strip() for a in _NAV_FALLBACK.strip().splitlines()]


def build_navlinks(out: Path, active_href: str = "/eda-forks.html") -> str:
    """Top-nav anchors from the canonical menu, with `active_href` marked active."""
    out_lines = []
    for a in _canonical_menu(out):
        href_m = re.search(r'href="([^"]*)"', a)
        if href_m and href_m.group(1) == active_href:
            a = re.sub(r"(<a\s+href=\"[^\"]*\")", r'\1 class="active"', a, count=1)
        out_lines.append("            " + a)
    return "\n".join(out_lines)


def build_footer_site(out: Path) -> str:
    """Footer 'Site' column mirrors the canonical top-menu (same items, no active),
    so the footer stays consistent with the nav and never re-drifts on regeneration."""
    return "".join(_canonical_menu(out))


def _image_ref() -> str:
    """The image the inventory measures, resolved to one that actually exists.

    Preference order is VERSION, then :latest. Not hard-coded: a literal tag
    would keep describing an old image after a release, and the tables would look
    measured while reporting the wrong one.

    The fallback matters because VERSION and the published image can disagree —
    they do right now, and the three numbers involved are all different. Measured
    2026-07-29 against the GHCR tag list rather than recalled:

        VERSION                     0.2.30
        newest tag on ghcr          0.2.31
        newest tag anywhere         0.2.32 — local only, from 595febb, unpublished

    So the image this resolves to is a release behind what is published and two
    behind what exists, and Table A renders 57 directories rather than 58 because
    0.2.30 predates `yices`. Whichever tag is used is printed in the section's own
    prose, so the page always names what it measured rather than implying it
    measured the current release. Falling back silently would be the worse
    failure: a table measured from :latest while the page claims to describe
    VERSION. The drift itself belongs to `version-sync`, which has never run
    (Actions disabled account-wide, vibe-ic#550).
    """
    forced = os.environ.get("GK_INVENTORY_IMAGE")
    if forced:
        return forced
    v = (HERE.parent / "VERSION").read_text().strip()
    for ref in (f"vibeic/vibeic-eda:{v}", "vibeic/vibeic-eda:latest"):
        r = subprocess.run(["docker", "image", "inspect", ref],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return ref
    return f"vibeic/vibeic-eda:{v}"          # let collect() report it as unmeasured


def build(out: Path):
    # vibeic/vibeic-eda#12. The published page is the third shared production artefact
    # with the same exposure as the cache and the ledgers: it is derived from whatever
    # state THIS process happened to read, so a run against an empty or scratch state
    # directory would republish the public page from it. `--out /tmp/page.html` is the
    # ordinary way to render one by hand and is unaffected.
    gk_state.require_writable(out, "the published monitor page",
                              "Render one somewhere else with --out /tmp/page.html")
    ledgers = _load_ledgers()
    report = _latest_report()
    enh = _load_enh()
    # Measured now, not pasted. If it cannot be measured the section says so
    # rather than vanishing: a section that disappears on failure looks exactly
    # like one that was never meant to be there.
    try:
        inv_html = render_inventory(inventory.collect(_image_ref()))
    except Exception as exc:
        inv_html = ('<section><div class="fork-wrap"><p class="fork-caption">'
                    '<strong>Tool inventory: not rendered — the measurement failed '
                    f'({_esc_html(str(exc)[:200])}). This is a gap in the page, not '
                    'an empty inventory.</strong></p></div></section>')
    data = json.dumps(ledgers, ensure_ascii=False)
    nav = NAV.replace("__NAVLINKS__", build_navlinks(out))
    footer = FOOTER.replace("__FOOTER_SITE__", build_footer_site(out))
    html = (PAGE.replace("__STYLE__", STYLE).replace("__NAV__", nav).replace("__FOOTER__", footer)
            .replace("__GAP__", GAP
                     .replace("__NFORKS__", str(len(enh)))
                     .replace("__NTOTAL__", str(len(ledgers)))
                     .replace("__NUNSURVEYED__", str(max(0, len(ledgers) - len(enh))))
                     .replace("__NOPEN__", str(sum(
                         1 for v in enh.values() for r in v.get("rows", [])
                         if r.get("status") in ("todo", "deferred")))))
            .replace("__INVENTORY__", inv_html)
            .replace("__DATA__", data)
            .replace("__ENH__", json.dumps(enh, ensure_ascii=False))
            .replace("__PINNOTES__", json.dumps(PIN_NOTES, ensure_ascii=False))
            .replace("__REPORT__", json.dumps(report, ensure_ascii=False)))
    html = _redact_nda(html)   # NDA redaction at the publish boundary — MUST be last
    out.write_text(html)
    enh_rows = sum(len(v.get("rows", [])) for v in enh.values())
    print(f"wrote {out}  ({len(html)//1024} KB, {len(ledgers)} tools, {enh_rows} enhancement rows)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    build(ap.parse_args().out)
