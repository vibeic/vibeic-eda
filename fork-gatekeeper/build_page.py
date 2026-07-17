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
import re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent          # version-controlled source
STATE = Path(os.environ.get("GK_STATE_DIR") or os.path.expanduser("~/.cache/eda-fork-gatekeeper"))
LEDGER = STATE / "ledger"             # runtime state — outside the source tree
REPORTS = STATE / "reports"
DEFAULT_OUT = Path(os.environ.get("GK_PAGE_OUT") or "/home/reyerchu/vibeic.ai/eda-forks.html")

# --- NDA redaction at the publish boundary (BINDING) ---------------------------
# The ledgers are seeded from the forks' own commit messages, some of which name a
# commercial NDA foundry / process. That name must NEVER reach the public page, no
# matter what the ledger or commit text says. This build step is the single choke
# point where ledger data becomes a public artifact, so we sanitize the emitted
# HTML here — defense-in-depth, so a future commit message can't re-leak it.
# (Order matters: replace the specific compound tokens before the bare name.)
_NDA_SUBS = [
    (re.compile(r"real-HP18E80-deck", re.I), "real-commercial-PDK-deck"),
    (re.compile(r"real-HP18E80", re.I), "real-commercial-PDK"),
    (re.compile(r"HP18E80", re.I), "a commercial 180nm NDA PDK"),
    (re.compile(r"Key ?Foundry", re.I), "a commercial foundry"),
    (re.compile(r"\bm18e80\w*", re.I), "commercial-180nm-pdk"),
]


def _redact_nda(s: str) -> str:
    for pat, rep in _NDA_SUBS:
        s = pat.sub(rep, s)
    return s


def _load_ledgers() -> list[dict]:
    out = []
    for p in sorted(LEDGER.glob("*.json")):
        if p.name == "index.json":
            continue
        try:
            out.append(json.loads(p.read_text()))
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
        return json.loads(js[-1].read_text())
    except json.JSONDecodeError:
        return None


NAV = """<nav>
    <div class="nav-inner">
        <a href="/" class="logo"><img src="img/logo-v5.svg" alt="vibeIC.ai" class="logo-img"></a>
        <div class="nav-links" id="navLinks">
            <a href="/" data-en="System" data-zh="系統">System</a>
            <a href="/flow.html" data-en="Flow" data-zh="流程">Flow</a>
            <a href="/evaluation.html" data-en="Evaluation" data-zh="驗證">Evaluation</a>
            <a href="/platform.html" data-en="Platform" data-zh="開放平台">Platform</a>
            <a href="/eda-forks.html" class="active" data-en="EDA Forks" data-zh="工具追蹤">EDA Forks</a>
            <a href="/manual.html" data-en="Manual" data-zh="使用手冊">Manual</a>
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
                <div><h6 data-en="Site" data-zh="網站">Site</h6><a href="/" data-en="System" data-zh="系統">System</a><a href="/flow.html" data-en="Flow" data-zh="流程">Flow</a><a href="/evaluation.html" data-en="Evaluation" data-zh="驗證">Evaluation</a><a href="/platform.html" data-en="Platform" data-zh="開放平台">Platform</a><a href="/eda-forks.html" data-en="EDA Forks" data-zh="工具追蹤">EDA Forks</a></div>
                <div><h6 data-en="Resources" data-zh="資源">Resources</h6><a href="https://github.com/vibeic/vibe-ic" target="_blank">GitHub</a><a href="https://github.com/vibeic/vibeic-bench" target="_blank" data-en="Run logs" data-zh="Run log">Run logs</a></div>
                <div><h6 data-en="Company" data-zh="公司">Company</h6><a href="https://vibeic.ai" target="_blank">vibeic.ai</a><a href="mailto:contact@vibeic.ai" data-en="Contact" data-zh="聯絡">Contact</a></div>
            </div>
        </div>
        <div class="footer-bottom"><p>&copy; 2026 vibeic.ai. <span data-en="All rights reserved." data-zh="保留所有權利。">All rights reserved.</span> | <a href="/privacy.html" data-en="Privacy" data-zh="隱私政策">Privacy</a> | <a href="/terms.html" data-en="Terms" data-zh="服務條款">Terms</a> | <a href="/disclaimer.html" data-en="Disclaimer" data-zh="免責聲明">Disclaimer</a></p></div>
    </div>
</footer>"""

# Static (non-data-driven) section: an honest commercial-gap self-assessment, from a
# top-down survey of all 12 forks vs the leading commercial suites. Persists across
# every regeneration because it lives in the template, not the ledger.
GAP = """<section>
    <div class="fork-wrap">
        <div class="section-header" style="text-align:left">
            <p class="eyebrow" data-en="Honest self-assessment" data-zh="誠實自評">Honest self-assessment</p>
            <h2 data-en="What our forks can't do yet — vs commercial EDA" data-zh="我們的 fork 還做不到什麼 — 對照商用 EDA">What our forks can't do yet — vs commercial EDA</h2>
            <p data-en="We own the core engines; what we lack is the signoff + methodology layer on top. A systematic survey of the leading commercial suites (Synopsys / Cadence / Siemens EDA + Ansys / Keysight / Empyrean) against all 12 forks produced a prioritized ~63-item enhancement backlog. The single highest-leverage item is field-solver-accurate, coupling-aware parasitic extraction (PEX): it is a prerequisite for crosstalk/SI timing, dynamic IR-drop, electromigration, and point-to-point reliability — one keystone unblocks roughly five downstream signoff features across two tools. We publish this gap openly; honesty about the ceiling is how we earn trust." data-zh="我們擁有核心引擎，缺的是上面那層簽核 + 方法學。我們對三大廠（Synopsys／Cadence／Siemens EDA，加上 Ansys／Keysight／Empyrean）做了系統化調查，對照全部 12 個 fork，整理出一份排序過、約 63 項的強化 backlog。最高槓桿的單一項目是 field-solver 級、耦合感知的寄生萃取（PEX）：它是串擾／SI timing、動態 IR-drop、電遷移、點對點可靠性的前置條件 — 一個拱心石解鎖橫跨兩個工具的約五個下游簽核功能。我們公開這份差距；對能力天花板誠實，正是我們贏得信任的方式。">We own the core engines; what we lack is the signoff + methodology layer on top. A systematic survey of the leading commercial suites (Synopsys / Cadence / Siemens EDA + Ansys / Keysight / Empyrean) against all 12 forks produced a prioritized ~63-item enhancement backlog. The single highest-leverage item is field-solver-accurate, coupling-aware parasitic extraction (PEX): it is a prerequisite for crosstalk/SI timing, dynamic IR-drop, electromigration, and point-to-point reliability — one keystone unblocks roughly five downstream signoff features across two tools. We publish this gap openly; honesty about the ceiling is how we earn trust.</p>
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
        <p class="fork-caption" data-en="For the narrative behind this gap — a per-tool deep-dive on where open-source EDA stands versus commercial — see the blog: <a href='/blog/11-oss-vs-commercial-gap-en.html' style='color:#63a8ea'>Open-source vs commercial EDA — where's the gap?</a>" data-zh="這份差距背後的完整敘事（逐工具深入分析開源 EDA 對比商用的現況），見部落格：<a href='/blog/11-oss-vs-commercial-gap-zh.html' style='color:#63a8ea'>開源 vs 商業 EDA 的差距，到底還差在哪？</a>">For the narrative behind this gap — a per-tool deep-dive on where open-source EDA stands versus commercial — see the blog: <a href='/blog/11-oss-vs-commercial-gap-en.html' style='color:#63a8ea'>Open-source vs commercial EDA — where's the gap?</a></p>
    </div>
</section>"""

STYLE = """<style>
.fork-wrap{max-width:1140px;margin:0 auto;padding:0 1.25rem}
.fork-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:2rem 0}
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
.fork-detail{background:rgba(120,150,180,.05)}
.fork-detail td{padding:0}
.fork-detail .inner{padding:1rem 1.2rem;display:none}
.fork-detail.open .inner{display:block}
.fork-detail h5{margin:.2rem 0 .5rem;font-size:.85rem}
.fork-commit{font-family:ui-monospace,monospace;font-size:.78rem;color:var(--text-muted,#6b7684);padding:.2rem 0;display:flex;gap:.6rem}
.fork-commit a{color:inherit;text-decoration:none;border-bottom:1px dotted currentColor}
.fork-commit .sha{color:#63a8ea;flex:none}
.fork-verd{font-family:ui-monospace,monospace;font-size:.78rem}
.fork-verd.MERGED{color:#2f8f6b}.fork-verd.DEFERRED{color:#c07d1e}.fork-verd.SKIP{color:var(--text-muted,#6b7684)}.fork-verd.CLEAN{color:#63a8ea}
.fork-caption{color:var(--text-muted,#6b7684);font-size:.85rem;margin:.4rem 0 0}
.fork-scroll{overflow-x:auto}
@media(max-width:760px){.fork-hide-sm{display:none}}
</style>"""

PAGE = """<!DOCTYPE html>
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

        <div class="fork-metrics" id="forkMetrics"></div>
        <p class="fork-caption" id="forkUpdated"></p>

        <div class="fork-scroll">
        <table class="fork-table">
            <thead><tr>
                <th data-en="Tool" data-zh="工具">Tool</th>
                <th data-en="Upstream" data-zh="上游">Upstream</th>
                <th class="fork-hide-sm" data-en="Pinned in image" data-zh="Image 內鎖定">Pinned in image</th>
                <th data-en="Our patches" data-zh="我們的補丁">Our patches</th>
                <th class="fork-hide-sm" data-en="On release" data-zh="目前 release">On release</th>
                <th data-en="Upstream latest" data-zh="上游最新">Upstream latest</th>
                <th data-en="New releases" data-zh="新 release">New releases</th>
                <th data-en="Last check" data-zh="最後檢查">Last check</th>
            </tr></thead>
            <tbody id="forkRows"></tbody>
        </table>
        </div>
        <p class="fork-caption" data-en="Click a tool for its carried patches, the upstream commits still pending, and the daily sync log. Data refreshes every day." data-zh="點一個工具可看它揹著的補丁、仍待合的上游 commit、以及每日同步 log。資料每天更新。">Click a tool for its carried patches, the upstream commits still pending, and the daily sync log. Data refreshes every day.</p>
    </div>
</section>

__GAP__

__FOOTER__

<script>
const LEDGERS = __DATA__;
const REPORT = __REPORT__;
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pill = (n, kind) => `<span class="pilln ${n?kind:'zero'}">${n||0}</span>`;

(function(){
  const imageVer = (LEDGERS[0]||{}).image_version || "—";
  const totalPatches = LEDGERS.reduce((a,d)=>a+(d.ahead||0),0);
  const withNewRel = LEDGERS.filter(d=>(d.behind_releases||0)>0).length;
  const lastCheck = REPORT ? (REPORT.date||"") : "—";
  const kpis = [
    [LEDGERS.length, {en:"Tools tracked",zh:"追蹤工具"}],
    ["v"+imageVer, {en:"vibeic-eda version",zh:"vibeic-eda 版本"}],
    [totalPatches, {en:"Patches carried",zh:"揹著的補丁"}],
    [withNewRel, {en:"Tools with a new release",zh:"有新 release 的工具"}],
    [lastCheck, {en:"Last daily check",zh:"最後每日檢查"}],
  ];
  document.getElementById("forkMetrics").innerHTML = kpis.map(([n,l])=>
    `<div class="fork-kpi"><div class="n">${esc(n)}</div><div class="l" data-en="${l.en}" data-zh="${l.zh}">${l.en}</div></div>`).join("");

  const rows = LEDGERS.map((d,i)=>{
    const ahead = d.ahead||0;
    const newRel = d.behind_releases||0;
    const last = (d.sync_log&&d.sync_log.length)?d.sync_log[d.sync_log.length-1]:null;
    const verd = last ? `<span class="fork-verd ${esc(last.verdict||'')}">${esc(last.verdict||'')}</span> <span style="color:var(--text-muted,#6b7684)">${esc((last.date||'').slice(0,10))}</span>` : '<span style="color:var(--text-muted,#6b7684)">—</span>';
    const pin = d.integrated
      ? `<span class="fork-mono">${esc(d.pinned_ref||'—')}</span>${d.vibeic_branch?`<br><span style="color:var(--text-muted,#6b7684);font-size:.72rem" class="fork-mono">${esc(d.vibeic_branch)}</span>`:''}`
      : `<span style="color:var(--text-muted,#6b7684)" data-en="not layered" data-zh="未納入">not layered</span>`;
    const row = `<tr class="trow" data-i="${i}">
      <td class="fork-tool">${esc(d.tool)}<span class="role">${esc(d.role||'')}</span></td>
      <td class="fork-mono"><a href="${esc(d.upstream_url)}" target="_blank" rel="noopener" style="color:#63a8ea;text-decoration:none">${esc(d.upstream)}</a></td>
      <td class="fork-hide-sm">${pin}</td>
      <td>${pill(ahead,'ahead')}</td>
      <td class="fork-hide-sm fork-mono">${esc(d.base_release||d.pinned_ref||'—')}</td>
      <td class="fork-mono">${esc(d.upstream_latest_release||'—')}</td>
      <td>${pill(newRel,'behind')}</td>
      <td>${verd}</td>
    </tr>`;
    const commit = c => `<div class="fork-commit"><a class="sha" href="${esc(c.url||'#')}" target="_blank" rel="noopener">${esc(c.sha)}</a><span>${esc(c.title)}</span><span style="margin-left:auto">${esc(c.date)}</span></div>`;
    const carried = (d.carried_patches&&d.carried_patches.length)
      ? `<h5 data-en="Patches we carry (${ahead}) — branch ${esc(d.vibeic_branch||'')}" data-zh="我們揹著的補丁（${ahead}）— 分支 ${esc(d.vibeic_branch||'')}">Patches we carry (${ahead})</h5>` + d.carried_patches.map(commit).join("")
      : (d.integrated
          ? `<h5 data-en="Patches we carry" data-zh="我們揹著的補丁">Patches we carry</h5><p class="fork-caption" data-en="Pinned to upstream with no local patches yet." data-zh="鎖定於上游，尚無本地補丁。">Pinned to upstream with no local patches yet.</p>`
          : `<h5 data-en="Not layered into the image" data-zh="未納入 image">Not layered into the image</h5><p class="fork-caption" data-en="Forked, but the image uses upstream directly (no fix warranted) — nothing to sync." data-zh="已 fork，但 image 直接用上游（無需修補）— 無需同步。">Forked, but the image uses upstream directly — nothing to sync.</p>`);
    const rel = (d.new_releases&&d.new_releases.length)
      ? `<h5 style="margin-top:1rem" data-en="New upstream releases to integrate (${newRel})" data-zh="待整合的上游新 release（${newRel}）">New upstream releases to integrate (${newRel})</h5>` + d.new_releases.map(r=>`<div class="fork-commit"><span class="sha">${esc(r.tag||'')}</span><span style="margin-left:auto">${esc(r.date||'')}</span></div>`).join("")
      : (d.integrated?`<h5 style="margin-top:1rem" data-en="Releases" data-zh="Release">Releases</h5><p class="fork-caption" data-en="On the latest upstream release." data-zh="已在上游最新 release。">On the latest upstream release.</p>`:"");
    const log = (d.sync_log&&d.sync_log.length)
      ? `<h5 style="margin-top:1rem" data-en="Daily sync log" data-zh="每日同步 log">Daily sync log</h5>` + d.sync_log.slice(-10).reverse().map(s=>`<div class="fork-commit"><span class="sha">${esc((s.date||'').slice(0,10))}</span><span class="fork-verd ${esc(s.verdict||'')}">${esc(s.verdict||'')}</span><span>${esc(s.note||'')}</span></div>`).join("")
      : "";
    const detail = `<tr class="fork-detail" data-d="${i}"><td colspan="8"><div class="inner">${carried}${rel}${log}</div></td></tr>`;
    return row+detail;
  }).join("");
  document.getElementById("forkRows").innerHTML = rows;
  document.getElementById("forkUpdated").innerHTML = REPORT
    ? `<span data-en="Last Gatekeeper run: ${esc(REPORT.date||'')} · image vibeic-eda:${esc(imageVer)}" data-zh="最後 Gatekeeper 執行：${esc(REPORT.date||'')} · image vibeic-eda:${esc(imageVer)}">Last Gatekeeper run: ${esc(REPORT.date||'')}</span>`
    : `<span data-en="Ledger seeded from live state; the daily Gatekeeper has not run yet." data-zh="Ledger 由即時狀態種入；每日 Gatekeeper 尚未執行。">Ledger seeded from live state; the daily Gatekeeper has not run yet.</span>`;

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


def build(out: Path):
    ledgers = _load_ledgers()
    report = _latest_report()
    data = json.dumps(ledgers, ensure_ascii=False)
    html = (PAGE.replace("__STYLE__", STYLE).replace("__NAV__", NAV).replace("__FOOTER__", FOOTER)
            .replace("__GAP__", GAP)
            .replace("__DATA__", data).replace("__REPORT__", json.dumps(report, ensure_ascii=False)))
    html = _redact_nda(html)   # NDA redaction at the publish boundary — MUST be last
    out.write_text(html)
    print(f"wrote {out}  ({len(html)//1024} KB, {len(ledgers)} tools)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    build(ap.parse_args().out)
