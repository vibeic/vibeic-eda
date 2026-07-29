#!/usr/bin/env python3
"""The tool inventory, MEASURED at build time — never a pasted snapshot.

`build_page.py` renders three tables from this module:

  A  every directory in the image           `ls /foss/tools`
  B  every tool IIC-OSIC-TOOLS ships        upstream `_build/tool_metadata.yml`
  C  PDK data                               `ls /foss/pdks`

Table C exists because A and B are both built per TOOL, and PDKs are not tools —
so PDK data was outside the frame of both while the rule (owner, 2026-07-29)
explicitly covers it. A rule that no audit can see is a rule that is not being
audited.

WHAT IS MEASURED VS WHAT IS JUDGED, and why they are separate files:

  measured here     the directories in the image, the org's forks and each
                    fork's parent, the upstream tool metadata
  judged, in        what a tool is for, whether our flow uses it, why not if
  TOOL_NOTES.json   it does not

A pasted count goes stale silently — this page already shipped "all 15 forks"
above a 21-row ledger. A judgement cannot be derived at all. So they are joined
here, and every divergence is REPORTED rather than rendered as a normal row: a
directory with no note, and a note for a directory that no longer exists.

THREE WAYS THE FORK COLUMN LIED BEFORE, each fixed below, each having produced
output indistinguishable from a correct answer:

  1. `gh repo list` truncates and does not announce it — it just looks like a
     smaller org. The default returns 30 of the org's 53 repos; `--limit 100`
     returns all 53 only because 53 < 100 today, which is one repo-creation from
     going quietly wrong. -> paginate, which cannot truncate silently.

     An earlier draft blamed this for a "14 forks" reading. That does not hold:
     15 forks existed before the 2026-07-28 burst added 30 more (measured from
     each fork's created_at), so 14 was roughly the true count at the time, not
     a truncation artefact. The fix stands on its own; the causal story did not,
     and is corrected here rather than repeated.
  2. The repo-LIST endpoint returns `parent: null` on every row; only the
     single-repo endpoint populates it. Comparing against the list's `parent`
     yields "nothing is forked", byte-identical to a genuinely unforked org.
     -> one call per fork.
  3. Upstreams get renamed, so a renamed repo looks unforked (`povik/yosys-slang`
     is now `povik/sv-elab`, and we have forked it).
     -> resolve each slug's canonical `full_name` through the API, not through a
     hard-coded rename table, which is wrong again at the next rename.

Matching is by UPSTREAM REPO, never by name: `vibeic/<X>` existing does not mean
we forked X's upstream, and its absence does not mean we did not.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path

DIR = Path(__file__).resolve().parent
NOTES = DIR / "TOOL_NOTES.json"
ORG = os.environ.get("GK_ORG", "vibeic")
UPSTREAM_REPO = "iic-jku/IIC-OSIC-TOOLS"

#: Aggregate build targets in `_build/images/`, not tools.
AGGREGATES = {"base", "base-dev", "iic-osic-tools", "fpga-tools", "pulp-tools"}


def _sh(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception:
        return 1, ""


def _gh(path, jq=None):
    cmd = ["gh", "api", path] + (["-q", jq] if jq else [])
    rc, out = _sh(cmd)
    return out if rc == 0 else ""


def image_dirs(image: str, path: str) -> list[str] | None:
    """`ls <path>` inside the image, or None if it could not be listed.

    None is NOT an empty list. An image that is not present locally and a tool
    directory that is genuinely empty produce the same `[]` from a naive reader,
    and the caller must be able to tell "we did not measure" from "there is
    nothing there" — that distinction is the whole reason this returns None.
    """
    rc, out = _sh(["docker", "run", "--rm", "--entrypoint", "ls", image, path], timeout=300)
    if rc != 0:
        return None
    names = [x for x in out.split() if x]
    return names or None


def org_forks() -> dict[str, list[str]]:
    """upstream slug (lowercased) -> our fork names.

    Paginated, and `parent` read one repo at a time. See the module docstring for
    why each of those matters.
    """
    rc, out = _sh(["gh", "api", "--paginate", f"orgs/{ORG}/repos?per_page=100",
                   "-q", '.[] | select(.fork==true) | .name'], timeout=300)
    if rc != 0:
        return {}
    forks: dict[str, list[str]] = {}
    for n in [x for x in out.split() if x]:
        parent = _gh(f"repos/{ORG}/{n}", ".parent.full_name").strip()
        if parent and parent != "null":
            forks.setdefault(parent.lower(), []).append(n)
    for v in forks.values():
        v.sort()
    return forks


def upstream_tools() -> list[dict]:
    """The upstream tool list: metadata entries plus image dirs that have none."""
    raw = _gh(f"repos/{UPSTREAM_REPO}/contents/_build/tool_metadata.yml", ".content")
    tools, cur = [], {}
    if raw:
        try:
            text = base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            text = ""
        for line in text.split("\n"):
            m = re.match(r"^- name:\s*(\S+)", line)
            if m:
                if cur:
                    tools.append(cur)
                cur = {"name": m.group(1)}
            else:
                km = re.match(r"^\s+(\w+):\s*(.+?)\s*$", line)
                if km and cur:
                    cur[km.group(1)] = km.group(2)
        if cur:
            tools.append(cur)
    have = {t["name"] for t in tools}
    rc, out = _sh(["gh", "api", f"repos/{UPSTREAM_REPO}/contents/_build/images",
                   "-q", '.[] | select(.type=="dir") | .name'], timeout=120)
    if rc == 0:
        for d in [x for x in out.split() if x]:
            if d not in have and d not in AGGREGATES:
                tools.append({"name": d, "repo": ""})
    return [t for t in tools if t["name"] not in AGGREGATES]


_canon_cache: dict[str, str] = {}


def canonical(slug: str) -> str:
    """Follow a rename. A stale name is why a forked repo can look unforked."""
    if not slug:
        return slug
    if slug in _canon_cache:
        return _canon_cache[slug]
    v = _gh(f"repos/{slug}", ".full_name").strip().lower() or slug
    _canon_cache[slug] = v
    return v


def fork_of(repo_url: str, forks: dict) -> list[str]:
    if not repo_url:
        return []
    s = re.sub(r"^https://github\.com/", "", repo_url).replace(".git", "").lower()
    f = forks.get(s)
    if f:
        return f
    c = canonical(s)
    return forks.get(c, []) if c != s else []


def collect(image: str, base_image: str = "hpretl/iic-osic-tools:latest") -> dict:
    """Everything the three tables need, plus what could not be measured.

    `unmeasured` is returned rather than swallowed. A section rendered from a
    failed measurement must say so; the alternative is a table that looks
    complete because the thing it could not see contributed no rows.
    """
    notes = json.loads(NOTES.read_text())
    unmeasured: list[str] = []

    tools = image_dirs(image, "/foss/tools")
    if tools is None:
        unmeasured.append(f"could not list /foss/tools in {image}")
        tools = []
    pdks = image_dirs(image, "/foss/pdks")
    if pdks is None:
        unmeasured.append(f"could not list /foss/pdks in {image}")
        pdks = []
    base = image_dirs(base_image, "/foss/tools")
    if base is None:
        unmeasured.append(f"could not list /foss/tools in {base_image} "
                          f"— the origin column cannot distinguish base from ours")
        base = []

    forks = org_forks()
    if not forks:
        unmeasured.append("could not enumerate the org's forks — every fork "
                          "column below would read 'no' for the wrong reason")
    ups = upstream_tools()
    if not ups:
        unmeasured.append("could not read the upstream tool metadata")

    up_by_name = {t["name"]: t.get("repo", "") for t in ups}
    extra = notes.get("extra_upstream", {})
    alias = notes.get("upstream_aliases", {})
    pip = set(notes.get("pip_installed", []))
    not_tool = set(notes.get("not_a_tool", []))
    tnotes = notes.get("tools", {})

    rows_a, missing_notes = [], []
    for d in sorted(tools):
        n = tnotes.get(d)
        if n is None:
            missing_notes.append(d)
            n = {"desc_en": "", "desc_zh": "", "used": None}
        repo = extra.get(d) or up_by_name.get(alias.get(d, d), "") or up_by_name.get(d, "")
        if d in not_tool:
            state, f = "not-a-tool", []
        elif d in pip:
            state, f = "pip", []
        elif not repo:
            state, f = "unknown-upstream", []
        else:
            f = fork_of(repo, forks)
            state = "forked" if f else "no"
        rows_a.append({"dir": d, "origin": "ours" if base and d not in base else "base",
                       "upstream": repo, "state": state, "forks": f,
                       "used": n.get("used"), "desc_en": n.get("desc_en", ""),
                       "desc_zh": n.get("desc_zh", "")})

    rows_b = []
    for t in sorted(ups, key=lambda x: x["name"]):
        n = tnotes.get(t["name"], {})
        repo = t.get("repo", "")
        f = fork_of(repo, forks)
        rows_b.append({"tool": t["name"], "upstream": repo, "forks": f,
                       "used": n.get("used"), "desc_en": n.get("desc_en", ""),
                       "desc_zh": n.get("desc_zh", ""),
                       "reason_en": n.get("reason_en", ""),
                       "reason_zh": n.get("reason_zh", "")})

    pnotes = notes.get("pdks", {})
    rows_c = []
    for d in sorted(pdks):
        n = pnotes.get(d, {})
        up = n.get("upstream", "")
        rows_c.append({"dir": d, "upstream": up, "forks": fork_of(up, forks) if up else [],
                       "desc_en": n.get("desc_en", ""), "desc_zh": n.get("desc_zh", "")})

    stale_notes = [k for k in tnotes if k not in set(tools) | {t["name"] for t in ups}]

    return {"image": image, "a": rows_a, "b": rows_b, "c": rows_c,
            "n_fork_repos": sum(len(v) for v in forks.values()),
            "n_distinct_upstreams": len(forks),
            "dupes": {k: v for k, v in forks.items() if len(v) > 1},
            "missing_notes": sorted(missing_notes), "stale_notes": sorted(stale_notes),
            "unmeasured": unmeasured}


if __name__ == "__main__":
    import sys
    img = sys.argv[1] if len(sys.argv) > 1 else "ghcr.io/vibeic/vibeic-eda:latest"
    d = collect(img)
    print(json.dumps({k: (len(v) if isinstance(v, list) else v)
                      for k, v in d.items() if k != "image"},
                     ensure_ascii=False, indent=1)[:1200])
