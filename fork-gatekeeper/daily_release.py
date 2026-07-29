#!/usr/bin/env python3
"""Move every pin to its fork's tip, rebuild what changed, and cut a new image.

OWNER RULING, 2026-07-29
========================
    "只要有任何工具有新版本（有我們自己的改動，或者是 upstream merge 進來的
     commit），該工具就要重新 build 一次。只要有任何一個工具有新版，我們的
     image 也就重新再出一版。只要有任何新的 update，我們最晚會在隔天早上的
     5 點半，提供一個 daily 的新版 updated docker image。"

`daily_merge.py` takes upstream's commits into the forks. That is one hop of
three, and the other two are where the work has historically been lost:

    fork branch  ->  pin  ->  rebuilt artefact  ->  composed image

On 2026-07-29 `vibeic/yosys#2` was reviewed and merged and every run that day
still used the pre-merge yosys, because the pin never moved. The owner found it,
not a gate:

    "容器映像還沒重建，跑的還是舊 yosys -> WHY YOU DIDNT REBUILD!!!"

A merge that stops at the fork has changed nothing anyone runs. This program is
the rest of the chain, and it is deliberately the SAME program for both causes of
a new version — an upstream merge and one of our own commits are the same event
here, because the pin does not care which produced the tip.

WHAT IT REFUSES
===============
* It will not bump a pin it cannot resolve to a branch tip. A pin it cannot read
  is reported, never left alone silently.
* It will not cut an image version when no tool changed. A version number that
  moves without content is worse than no release — it makes "we shipped today"
  true and meaningless.
* It will not skip the multi-repo tag composition. `eda-tool-lvs` is tagged
  `<magic>-<netgen>`; bumping magic alone while leaving the tag would keep
  pulling the artefact built before the bump. The tag composition is READ from
  `docker-bake.hcl` rather than hard-coded, so a new pairing cannot drift.

WHAT IT DOES NOT PROVE
======================
That the rebuilt image works. It builds, and a build that fails stops the
release with the failure attached — but the regression suite is
`build_and_regress.sh`'s job, not this one's. A green build is not a green tool.

Exit: 0 released or nothing to do, 1 something needed a human, 2 nothing checked.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_pins_current import check_one, pinned_refs            # noqa: E402

RC_OK, RC_NEEDS_HUMAN, RC_NOTHING = 0, 1, 2

#: How many hex characters the artefact tag uses. `short()` in docker-bake.hcl.
SHORT = 7


def _sh(cmd, cwd=None, timeout=7200):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                                   # noqa: BLE001
        return 1, "", str(exc)


def _gh_tip(repo: str, branch: str) -> str:
    rc, out, _ = _sh(["gh", "api", f"repos/vibeic/{repo}/commits/{branch}",
                      "--jq", ".sha"], timeout=120)
    return out.strip() if rc == 0 else ""


def ref_arg_names(eda_root: Path) -> Dict[str, str]:
    """fork repo -> the `ARG <NAME>_REF` that pins it.

    Matched by name, not by position: `tools/lvs/Dockerfile` pins magic AND
    netgen, and a first-match parse silently drops one of them.
    """
    out: Dict[str, str] = {}
    files = sorted((eda_root / "tools").glob("*/Dockerfile"))
    if (eda_root / "Dockerfile").is_file():
        files.append(eda_root / "Dockerfile")
    for df in files:
        text = df.read_text(errors="replace")
        names = re.findall(r"^ARG\s+([A-Z0-9_]+)_REF=[0-9a-f]{40}", text, re.M)
        repos = re.findall(
            r"github\.com/vibeic/([A-Za-z0-9_.-]+?)(?:\.git)?[\s\"'\\]", text)
        for repo in dict.fromkeys(repos):
            key = repo.upper().replace("-", "_").replace(".", "_")
            if key in names:
                out[repo] = key
                continue
            flat = key.replace("_", "")
            hit = next((n for n in names if n.replace("_", "") == flat), None)
            if hit:
                out[repo] = hit
    return out


def bake_targets(eda_root: Path) -> Dict[str, List[str]]:
    """bake target -> the REF variable names its TAG is composed from, in order.

    Read rather than hard-coded. `sat-solvers` is tagged
    `${short(KISSAT_REF)}-${short(CADICAL_REF)}` and `lvs` is
    `${short(MAGIC_REF)}-${short(NETGEN_REF)}`; a future pairing would otherwise
    have to be remembered here, and would not be.
    """
    hcl = (eda_root / "docker-bake.hcl").read_text(errors="replace")
    out: Dict[str, List[str]] = {}
    for m in re.finditer(r'target\s+"([A-Za-z0-9_.-]+)"\s*\{(.*?)\n\}',
                         hcl, re.S):
        name, body = m.group(1), m.group(2)
        tags = re.search(r"tags\s*=\s*(.+)", body)
        if not tags:
            continue
        line = tags.group(1)
        refs = re.findall(r"short\(([A-Z0-9_]+_REF)\)", line)
        if not refs:                      # tool_tags("x", X_REF) helper form
            refs = re.findall(r"tool_tags\(\s*\"[^\"]+\"\s*,\s*([A-Z0-9_]+_REF)",
                              line)
        if refs:
            out[name] = refs
    return out


def rewrite_pin(eda_root: Path, arg: str, new: str) -> List[str]:
    """Write `new` at every site that states this pin. Returns the paths changed.

    Only the 40-hex is replaced; the rest of the line survives untouched. That
    matters: these lines carry trailing comments recording what the branch holds,
    and an earlier bumper rewrote whole lines with `sed`, eating the comments —
    after which a downstream parser that read the branch name FROM the comment
    lost two of fourteen tools and reported twelve without saying so.
    """
    changed: List[str] = []
    files = list((eda_root / "tools").glob("*/Dockerfile"))
    files += [eda_root / "Dockerfile", eda_root / "docker-bake.hcl"]
    pat_arg = re.compile(rf"(^ARG\s+{re.escape(arg)}_REF=)[0-9a-f]{{40}}", re.M)
    pat_var = re.compile(
        rf'(variable\s+"{re.escape(arg)}_REF"\s*\{{\s*default\s*=\s*")[0-9a-f]{{40}}')
    for f in files:
        if not f.is_file():
            continue
        text = f.read_text(errors="replace")
        new_text = pat_arg.sub(rf"\g<1>{new}", text)
        new_text = pat_var.sub(rf"\g<1>{new}", new_text)
        if new_text != text:
            f.write_text(new_text, encoding="utf-8")
            changed.append(str(f.relative_to(eda_root)))
    return changed


def retag_images(eda_root: Path, refs_now: Dict[str, str],
                 targets: Dict[str, List[str]]) -> List[str]:
    """Recompose every `ARG IMG_<TOOL>=...:<tag>` from the current pins."""
    root_df = eda_root / "Dockerfile"
    text = root_df.read_text(errors="replace")
    touched: List[str] = []
    for target, ref_vars in targets.items():
        shorts = [refs_now[v][:SHORT] for v in ref_vars if v in refs_now]
        if len(shorts) != len(ref_vars):
            continue
        tag = "-".join(shorts)
        argname = "IMG_" + target.upper().replace("-", "_")
        pat = re.compile(
            rf"(^ARG\s+{argname}=\S*/eda-tool-{re.escape(target)}:)\S+", re.M)
        new_text = pat.sub(rf"\g<1>{tag}", text)
        if new_text != text:
            touched.append(f"{argname} -> :{tag}")
            text = new_text
    if touched:
        root_df.write_text(text, encoding="utf-8")
    return touched


def _existing_versions(eda_root: Path) -> List[Tuple[int, int, int]]:
    """Every `x.y.z` this image has actually been tagged with, locally."""
    rc, out, _ = _sh(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                     timeout=120)
    seen = set()
    for ln in out.splitlines():
        m = re.search(r"vibeic-eda:(\d+)\.(\d+)\.(\d+)$", ln.strip())
        if m:
            seen.add(tuple(int(g) for g in m.groups()))
    return sorted(seen)


def bump_version(eda_root: Path) -> Tuple[str, str]:
    """Next version, above BOTH the VERSION file and every tag that exists.

    The file drifts. Measured 2026-07-29: `VERSION` read 0.2.30 while images
    0.2.31 and 0.2.32 were already built and tagged — two releases that never
    wrote the file back. Bumping the file alone would have re-issued 0.2.31 over
    a different image, which is the one thing a version number must never do.
    """
    vf = eda_root / "VERSION"
    old = vf.read_text().strip()
    cur = tuple(int(x) for x in (old.split(".") + ["0", "0"])[:3])
    top = max([cur] + _existing_versions(eda_root))
    maj, minor, patch = (str(top[0]), str(top[1]), str(top[2]))
    patch = int(patch) + 1
    if patch > 99:                       # patch 0-99, then roll the minor
        minor, patch = str(int(minor) + 1), 0
    new = f"{maj}.{minor}.{patch}"
    vf.write_text(new + "\n", encoding="utf-8")
    return old, new


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root",
                    default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-build", action="store_true",
                    help="move pins and stop; do not build or version")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    root = Path(a.eda_root)
    pins = pinned_refs(root)
    args_of = ref_arg_names(root)
    targets = bake_targets(root)
    if not pins:
        print("[NOT CHECKED] no pins found — nothing was released, which is not "
              "a clean result", file=sys.stderr)
        return RC_NOTHING

    # Resolution goes through `check_one`, which VERIFIES the branch contains the
    # pin. An earlier version called `build_branch` — which ranks branches by
    # naming convention and does not check containment — and then took its tip.
    # On the first real run that proposed moving cadical's pin onto an upstream
    # `sweep` branch, netgen's onto `vibeic/lvs-fidelity` and ALIGN's onto
    # `place_by_constraint`: four unrelated lines of development, presented as
    # "these tools have a new version". Nothing caught it but reading the diff.
    #
    # Only a STALE verdict moves a pin. CURRENT is nothing to do; anything else
    # is a pin we could not resolve, and an unresolved pin is reported, never
    # bumped on a guess.
    moved, unresolved = [], []
    for repo, pin in sorted(pins.items()):
        v = check_one(repo, pin)
        if v["verdict"] == "CURRENT":
            continue
        if v["verdict"] != "STALE":
            unresolved.append({"repo": repo, "why": f"{v['verdict']}: {v['detail']}"})
            continue
        tip = _gh_tip(repo, v["branch"])
        if not tip:
            unresolved.append({"repo": repo,
                               "why": f"tip of {v['branch']} unreadable"})
            continue
        moved.append({"repo": repo, "branch": v["branch"], "arg": args_of.get(repo),
                      "from": pin[:9], "to": tip[:9], "sha": tip,
                      "behind": v.get("behind")})

    print(f"daily_release: {len(pins)} pin(s), {len(moved)} tool(s) with a new "
          f"version, {len(unresolved)} unresolved")
    for m in moved:
        print(f"  {m['repo']:<20} {m['from']} -> {m['to']}  ({m['branch']})")
    for u in unresolved:
        print(f"  {u['repo']:<20} UNRESOLVED — {u['why']}", file=sys.stderr)

    result = {"program": "daily_release", "moved": moved,
              "unresolved": unresolved, "built": [], "version": None}

    if moved and not a.dry_run:
        for m in moved:
            if not m["arg"]:
                unresolved.append({"repo": m["repo"],
                                   "why": "no ARG <NAME>_REF pins it"})
                continue
            m["files"] = rewrite_pin(root, m["arg"], m["sha"])
        refs_now = {f"{args_of[r]}_REF": s for r, s in
                    {**pins, **{m["repo"]: m["sha"] for m in moved}}.items()
                    if r in args_of}
        result["retagged"] = retag_images(root, refs_now, targets)
        for t in result["retagged"]:
            print(f"  retag {t}")

        if not a.no_build:
            build = sorted({t for t, rv in targets.items()
                            if any(f"{m['arg']}_REF" in rv for m in moved
                                   if m.get("arg"))})
            for t in build:
                print(f"  building {t} …", flush=True)
                # `--push` is what makes the next hop incremental FOR ANYONE
                # ELSE. Without it the artefact exists on one machine, and the
                # release image can only be composed here, from this cache — a
                # per-tool architecture whose incrementality is an accident of
                # local state. Measured 2026-07-29: all 8 artefacts existed
                # locally and ghcr held none of them.
                rc, _, err = _sh(["docker", "buildx", "bake", "-f",
                                  str(root / "docker-bake.hcl"), "--push", t],
                                 cwd=root)
                if rc != 0:
                    print(f"[FAIL] {t} did not build; the release stops here so "
                          f"a broken tool is not tagged as a version:\n"
                          f"{err[-1500:]}", file=sys.stderr)
                    return RC_NEEDS_HUMAN
                result["built"].append(t)
            # COMPOSE THE IMAGE. Building the tool artefacts is not the
            # delivery — the image is what anyone runs, and a tool rebuilt
            # without recomposing is the same "merged but not shipping" failure
            # one hop further along. `eda-local` redirects each `FROM ${IMG_*}`
            # to the target just built, so the composed image cannot pull a
            # stale artefact from the registry.
            #
            # cocotb, pyuvm, sby and the ALIGN pair have no tool target at all —
            # the root Dockerfile installs them directly. For those, this compose
            # IS the whole rebuild, which is why it runs whenever anything moved
            # rather than only when a tool target was built.
            # Compose from the REGISTRY (`eda`), not from local targets
            # (`eda-local`). `eda` has no `contexts` block: every tool arrives as
            # the exact pinned artefact, so an unchanged tool is PULLED rather
            # than rebuilt, and the image is reproducible off this machine.
            # `eda-local` redirects each tool to a freshly built target — correct
            # for iterating on a tool, wrong for a daily release, because it
            # makes "only what changed was rebuilt" true only while the local
            # cache survives.
            print("  composing the release image from the pinned artefacts …",
                  flush=True)
            rc, _, err = _sh(["docker", "buildx", "bake", "-f",
                              str(root / "docker-bake.hcl"), "--load", "eda"],
                             cwd=root)
            if rc != 0:
                print(f"  compose from the registry failed; falling back to a "
                      f"LOCAL-ONLY image. This one is not reproducible "
                      f"elsewhere:\n{err[-600:]}", file=sys.stderr)
                result["registry_composed"] = False
                rc, _, err = _sh(["docker", "buildx", "bake", "-f",
                                  str(root / "docker-bake.hcl"), "--load",
                                  "eda-local"], cwd=root)
            else:
                result["registry_composed"] = True
            if rc != 0:
                print(f"[FAIL] the image did not compose; no version was cut so "
                      f"nothing claims to be a release:\n{err[-1500:]}",
                      file=sys.stderr)
                return RC_NEEDS_HUMAN
            result["built"].append("eda-local")

            old, new = bump_version(root)
            result["version"] = {"from": old, "to": new}
            rc, _, _ = _sh(["docker", "tag", "ghcr.io/vibeic/vibeic-eda:local",
                            f"ghcr.io/vibeic/vibeic-eda:{new}"])
            result["tagged"] = (rc == 0)
            print(f"  VERSION {old} -> {new}"
                  f"{'  (tagged)' if rc == 0 else '  (TAG FAILED)'}")

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(result, indent=2) + "\n",
                                encoding="utf-8")

    if unresolved:
        print(f"[NEEDS HUMAN] {len(unresolved)} pin(s) could not be resolved; "
              f"they were left alone and are NOT in this release", file=sys.stderr)
        return RC_NEEDS_HUMAN
    if not moved:
        print("[PASS] every pin is already its fork's tip — no tool has a new "
              "version, so no image version was cut")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
