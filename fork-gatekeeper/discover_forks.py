#!/usr/bin/env python3
"""discover_forks.py — build the per-tool FORK ledger from REAL state.

Source of truth for what we currently ship = the **vibeic-eda Dockerfile**: it pins
each fork with `ARG <TOOL>_REF=<sha>` (a commit on our `vibeic/*` enhancement branch)
and clones+checks-out that ref. So "our current version" is the pinned REF, and our
carried patches are the commits on that ref since its merge-base with upstream.

Tracking granularity is **releases, not commits** (owner directive): for each tool we
compare the release our pin is based on against the upstream's newer releases. A new
upstream release is the merge candidate; the daily gatekeeper rebases our branch onto
it, bumps the Dockerfile ARG, and rebuilds the vibeic-eda image (the green gate).

    python3 discover_forks.py            # refresh ledger/<tool>.json + ledger/index.json
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
LEDGER = HERE / "ledger"
FORKS = json.loads((HERE / "FORKS.json").read_text())["forks"]
ORG = "vibeic"
EDA_REPO = "vibeic/vibeic-eda"
CAP = 200


def gh(path: str):
    r = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {"_err": r.stderr.strip().splitlines()[-1][:160] if r.stderr else "error"}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"_err": "parse error"}


def _gh_file(repo: str, path: str) -> str | None:
    d = gh(f"repos/{repo}/contents/{path}")
    if isinstance(d, dict) and d.get("content"):
        try:
            return base64.b64decode(d["content"]).decode("utf-8", "replace")
        except Exception:
            return None
    return None


def parse_dockerfile_pins(text: str) -> dict:
    """tool(lowercased repo name) -> {'ref': sha, 'arg': 'YOSYS_REF', 'branch': 'vibeic/…'}."""
    args = dict(re.findall(r"ARG\s+(\w+_REF)\s*=\s*(\S+)", text))
    branches = {}
    for m in re.finditer(r"ARG\s+(\w+_REF)\s*=\s*\S+\s*#[^\n]*branch\s+(\S+)", text):
        branches[m.group(1)] = m.group(2)
    pins = {}
    for m in re.finditer(r"github\.com/vibeic/([A-Za-z0-9_.-]+?)\.git", text):
        tool = m.group(1)
        tail = text[m.end(): m.end() + 400]
        am = re.search(r"\$\{(\w+_REF)\}", tail)
        if am and am.group(1) in args:
            arg = am.group(1)
            pins[tool.lower()] = {"ref": args[arg], "arg": arg, "branch": branches.get(arg)}
    return pins


def _commit_brief(c: dict) -> dict:
    commit = c.get("commit", {})
    return {"sha": (c.get("sha") or "")[:12],
            "title": (commit.get("message") or "").splitlines()[0][:120] if commit else "",
            "date": ((commit.get("author") or {}).get("date", "") if commit else "")[:10],
            "url": c.get("html_url", "")}


def _releases(up_full: str) -> list[dict]:
    """Upstream releases newest-first: [{tag, date}]. Falls back to tags (no dates)."""
    rel = gh(f"repos/{up_full}/releases?per_page=30")
    out = []
    if isinstance(rel, list) and rel:
        for r in rel:
            out.append({"tag": r.get("tag_name"), "date": (r.get("published_at") or "")[:10]})
        return out
    tags = gh(f"repos/{up_full}/tags?per_page=30")
    if isinstance(tags, list):
        for t in tags:
            out.append({"tag": t.get("name"), "date": None})
    return out


def discover_one(fork: dict, pins: dict, image_version: str) -> dict:
    tool, up_full = fork["tool"], fork["upstream"]
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    led = {"tool": tool, "role": fork.get("role", ""),
           "upstream": up_full, "upstream_url": f"https://github.com/{up_full}",
           "fork_url": f"https://github.com/{ORG}/{tool}",
           "image_version": image_version, "generated_at": now}

    meta = gh(f"repos/{ORG}/{tool}")
    if meta.get("_err"):
        led["error"] = f"repo meta: {meta['_err']}"
        return led
    parent = meta.get("parent") or {}
    up_branch = parent.get("default_branch") or "main"
    up_owner = up_full.split("/")[0]
    led.update({"forked_at": (meta.get("created_at") or "")[:10],
                "upstream_default_branch": up_branch})

    pin = pins.get(tool.lower()) or {}
    ref = pin.get("ref")
    led["pinned_ref"] = (ref or "")[:12] if ref else None
    led["pinned_ref_full"] = ref
    led["vibeic_branch"] = pin.get("branch")
    led["dockerfile_arg"] = pin.get("arg")
    # A fork with no Dockerfile pin is forked but NOT layered into the image (e.g.
    # verilator — "no honest fix warranted, nothing to layer"). Track it honestly:
    # such a tool uses upstream directly, so there is nothing to sync into the image.
    led["integrated"] = bool(ref)

    # our carried patches + fork point: compare upstream default ... our pinned ref
    head = ref or meta.get("default_branch") or up_branch
    cmp = gh(f"repos/{ORG}/{tool}/compare/{up_owner}:{up_branch}...{head}")
    pin_date = None
    if not cmp.get("_err"):
        mb = cmp.get("merge_base_commit") or {}
        led["fork_point"] = _commit_brief(mb) if mb else None
        led["ahead"] = cmp.get("ahead_by", 0)                 # our patches on the pinned branch
        led["behind_commits"] = cmp.get("behind_by", 0)       # informational (commit granularity)
        led["carried_patches"] = [_commit_brief(c) for c in (cmp.get("commits") or [])][:CAP]
        # Classify releases by the FORK POINT (merge-base) date — the point where our
        # branch diverges from upstream = the release our patches are based on. Using a
        # patch's own author date is wrong: rebasing onto a new release preserves the
        # patch author dates, so the fork-point is the only reliable "we're based on X".
        pin_date = (led.get("fork_point") or {}).get("date")
    else:
        led["compare_error"] = cmp["_err"]

    # RELEASE tracking. Accurate "are we on the latest release" via ANCESTRY (one
    # compare: is the latest release tag contained in our pinned ref?) — date-based
    # classification is fragile for tools that release daily (magic) or whose tags
    # aren't on the default branch. Fall back to dates only when not current.
    rels = _releases(up_full)
    led["upstream_releases"] = rels[:15]
    led["upstream_latest_release"] = rels[0]["tag"] if rels else None
    new, base = [], None
    current = False
    if rels and ref:
        latest_tag = rels[0]["tag"]
        c = gh(f"repos/{ORG}/{tool}/compare/{up_owner}:{latest_tag}...{ref}")
        # behind_by == 0 → the latest release has no commit our pin lacks → we're current
        if not c.get("_err") and c.get("behind_by", 1) == 0:
            base, current = latest_tag, True
    if not current:
        new = [r for r in rels if r.get("date") and pin_date and r["date"] > pin_date]
        b = next((r for r in rels if r.get("date") and pin_date and r["date"] <= pin_date), None)
        base = b["tag"] if b else None
    led["new_releases"] = new
    led["behind_releases"] = len(new)
    led["base_release"] = base

    led.setdefault("last_sync", None)
    return led


def main():
    LEDGER.mkdir(parents=True, exist_ok=True)
    df = _gh_file(EDA_REPO, "Dockerfile") or ""
    pins = parse_dockerfile_pins(df)
    image_version = (_gh_file(EDA_REPO, "VERSION") or "").strip() or "unknown"
    if not pins:
        print("  WARNING: could not parse Dockerfile pins (falling back to default-branch tracking)")

    index = []
    for fork in FORKS:
        prev = LEDGER / f"{fork['tool']}.json"
        sync_log, last_sync = [], None
        if prev.is_file():
            try:
                old = json.loads(prev.read_text())
                sync_log, last_sync = old.get("sync_log", []), old.get("last_sync")
            except json.JSONDecodeError:
                pass
        led = discover_one(fork, pins, image_version)
        led["sync_log"], led["last_sync"] = sync_log, last_sync
        prev.write_text(json.dumps(led, indent=2, ensure_ascii=False) + "\n")
        index.append({k: led.get(k) for k in (
            "tool", "role", "upstream", "forked_at", "pinned_ref", "vibeic_branch",
            "ahead", "base_release", "upstream_latest_release", "behind_releases",
            "image_version", "last_sync")})
        tag = led.get("error") or (f"pin={led.get('pinned_ref')} patches={led.get('ahead','?')} "
                                   f"base={led.get('base_release')} latest={led.get('upstream_latest_release')} "
                                   f"new_releases={led.get('behind_releases','?')}")
        print(f"  {fork['tool']:16} {tag}")
    (LEDGER / "index.json").write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
         "image_version": image_version, "forks": index}, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(index)} ledgers · image {image_version} → {LEDGER}")


if __name__ == "__main__":
    main()
