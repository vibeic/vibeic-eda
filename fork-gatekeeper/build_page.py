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
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
LEDGER = HERE / "ledger"
REPORTS = HERE / "reports"
DEFAULT_OUT = Path("/home/reyerchu/vibeic.ai/eda-forks.html")


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
            .replace("__DATA__", data).replace("__REPORT__", json.dumps(report, ensure_ascii=False)))
    out.write_text(html)
    print(f"wrote {out}  ({len(html)//1024} KB, {len(ledgers)} tools)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    build(ap.parse_args().out)
