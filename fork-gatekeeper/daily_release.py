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
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_pins_current import (branch_is_ours, check_one,       # noqa: E402
                                pinned_refs)

RC_OK, RC_NEEDS_HUMAN, RC_NOTHING = 0, 1, 2

#: How many hex characters the artefact tag uses. `short()` in docker-bake.hcl.
SHORT = 7


#: Environment every buildx invocation gets.
#:
#: buildkit truncates a single step's log at 2 MiB by default and says so only
#: in a line that is itself easy to miss. The ALIGN step blows past that — CBC
#: builds serially and is verbose — so exactly the step that has wedged four
#: times is the one whose log stops. `-1` removes the limit.
#:
#: BUT ONLY WHERE BUILDKIT READS IT, WHICH IS NOT HERE BY DEFAULT. The variable
#: is read by the buildkit DAEMON, not by the buildx client. With the `docker`
#: driver — the default, and what this host uses — buildkit lives inside
#: dockerd, so exporting it into the client process does exactly nothing.
#: Measured: the klayout build with this env in place still printed
#: `[output clipped, log limit 2MiB reached]`.
#:
#: It is kept because it IS the correct setting for the `docker-container`
#: driver, where buildx passes the env through to the builder container. What
#: is not kept is the silence: `log_limit_effective()` below reports when this
#: cannot work, so the setting cannot look like it is doing something.
BUILD_ENV = {"BUILDKIT_STEP_LOG_MAX_SIZE": "-1",
             "BUILDKIT_PROGRESS": "plain"}


#: A builder whose buildkit CAN honour BUILDKIT_STEP_LOG_MAX_SIZE.
#:
#: With the default `docker` driver, buildkit runs inside dockerd and never sees
#: the client's environment, so the variable is a no-op — measured, and the
#: klayout build printed `[output clipped, log limit 2MiB reached]` with it set.
#: The obvious fix is a dockerd drop-in plus a restart, and a restart here would
#: have killed a running ibex route, a long-lived container, and someone else's
#: Discourse. A `docker-container` builder lifts the limit for our builds and
#: touches nothing else:
#:
#:     docker buildx create --name vibeic-builder --driver docker-container \
#:         --driver-opt env.BUILDKIT_STEP_LOG_MAX_SIZE=-1 \
#:         --driver-opt env.BUILDKIT_STEP_LOG_MAX_SPEED=-1
#:
#: Measured on a step emitting 90 000 lines:
#:
#:     default builder    kept to line   3120,  1 clip message, 232 KB
#:     vibeic-builder     kept to line  90000,  0 clip messages, 6.6 MB
#:
#: Used when it exists, ignored when it does not — a release must not fail
#: because a builder was never created on this host.
PREFERRED_BUILDER = "vibeic-builder"


def preferred_builder() -> Optional[str]:
    """`PREFERRED_BUILDER` if it exists and is usable, else None."""
    rc, out, _ = _sh(["docker", "buildx", "inspect", PREFERRED_BUILDER],
                     timeout=120)
    if rc != 0 or "running" not in out.lower():
        return None
    return PREFERRED_BUILDER


def log_limit_effective(builder: Optional[str] = None) -> Optional[bool]:
    """Can BUILDKIT_STEP_LOG_MAX_SIZE actually take effect here?

    None when the driver cannot be read — not-known, which must not read as a
    pass. This is the second time in two days that a fix of mine changed the
    layer I control while the behaviour lived one layer further out (the first
    removed our COPY of a directory the BASE image ships). The cheap defence is
    the same both times: have the program say which layer it reached.

    Asks about the builder the release WILL USE, not whichever one is current.
    Those differ the moment a preferred builder exists, and reporting on the
    wrong one is the same class of error as the setting it reports on.
    """
    cmd = ["docker", "buildx", "inspect"] + ([builder] if builder else [])
    rc, out, _ = _sh(cmd, timeout=60)
    if rc != 0 or not out.strip():
        return None
    for line in out.splitlines():
        if line.lower().startswith("driver:"):
            return line.split(":", 1)[1].strip() != "docker"
    return None

#: Deadlines, set from measurement rather than from caution.
#:
#: Observed composes run 13–20 minutes when every artefact is already pulled,
#: and the longest legitimate one is a cold ALIGN build, whose CBC dependency
#: compiles serially upstream. 90 minutes clears that with room and still turns
#: an overnight wedge into a morning failure instead of a morning mystery.
#: Tool builds get longer: klayout with Qt is the slowest of them.
COMPOSE_TIMEOUT = 5400
TOOL_BUILD_TIMEOUT = 10800


def _sh(cmd, cwd=None, timeout=7200, stream=False, env=None):
    """Run a command. `stream=True` lets its output through to OUR stdout.

    Capturing is right for the short API calls, and wrong for the two builds:
    with `capture_output=True` nothing reaches the log until the process exits,
    so `--progress plain` — added precisely to make a build watchable — changed
    nothing. A compose wedged twice with no CPU, no disk and no network, and the
    only way to place it was reading dockerd's journal, because its own progress
    was sitting in a buffer that would only be flushed by the exit that never
    came.

    Streaming gives up the captured text for the failure message. The output is
    in the log instead, which is more than the tail we were printing.

    A TIMEOUT IS REPORTED AS A TIMEOUT. The wedge has cost four manual
    interventions, each of which began with me deciding by hand whether a silent
    build was working; `subprocess` already knows when it has waited too long,
    and the only reason that never turned into a failure is that the deadline was
    two hours and nobody waited for it.
    """
    full = None
    if env:
        full = dict(os.environ)
        full.update(env)
    try:
        if stream:
            sys.stdout.flush()
            p = subprocess.run(cmd, cwd=cwd, timeout=timeout, env=full)
            return p.returncode, "", "(output streamed to the log above)"
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=full)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 1, "", (f"TIMED OUT after {timeout}s with no exit. This is the "
                       f"signature recorded in vibeic-eda#26: buildx alive, no "
                       f"compiler running, no disk write, no network. Treated as "
                       f"a failure so the release stops instead of hanging.")
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
        full = artefact_tag(eda_root, target, ref_vars, refs_now)
        if full is None:
            continue
        tag = full.split(":", 1)[1]
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


def pins_fingerprint(pins: Dict[str, str]) -> str:
    """A stable digest of the whole pin set — what a released image was built from."""
    blob = ";".join(f"{k}={v}" for k, v in sorted(pins.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def released_record(eda_root: Path) -> dict:
    f = eda_root / "RELEASED.json"
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text())
    except ValueError:
        return {}


def write_released_record(eda_root: Path, version: str,
                          targets: Dict[str, List[str]]) -> dict:
    """THE ONE writer of `RELEASED.json`. Returns the record it wrote.

    vibeic-eda#51. This used to be an inline block inside the publish path, so
    the ledger was written only by a release that went THROUGH that path.
    0.2.53 was cut by hand in response to #45/#46 — VERSION advanced, the image
    was built and published, and the record still described 0.2.52. By the
    file's own contract ("a pin set not matching this has never been released")
    main's shipped pin set had never shipped, and the next tick would have
    published 0.2.54 byte-identical to 0.2.53.

    Extracted so a hand release can record itself through the SAME code — see
    `--record-release`. A second, hand-derived fingerprint would be the same
    defect wearing a different hat: the fingerprint MUST be computed the way the
    reader recomputes it or the record is unreproducible (measured on 0.2.45:
    the shipped tree hashed to e4e0a5f6 while the file recorded 94d85fda, and
    the next run started composing 0.2.46 with nothing changed).

    MEASURED AFTER every edit this run made, so a later run can reproduce it
    from the tree that shipped — `compose_recipe_hash` reads the root Dockerfile
    that `rewrite_pin` and `retag_images` have already edited.
    """
    rec = {
        "_comment": "What the last PUBLISHED image was built from. A pin set "
                    "not matching this has never been released, however current "
                    "the pins look. The fingerprint is measured AFTER every edit "
                    "this run made, so a later run can reproduce it from the "
                    "tree that shipped.",
        "version": version,
        "pins_fingerprint": pins_fingerprint({
            **pinned_refs(eda_root),
            **{f"recipe:{k}": recipe_hash(eda_root, k) for k in targets},
            "recipe:__compose__": compose_recipe_hash(eda_root)}),
        "pins": {k: v for k, v in sorted(pinned_refs(eda_root).items())},
    }
    (eda_root / "RELEASED.json").write_text(
        json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return rec


def commit_release_record(eda_root: Path, version: str) -> Tuple[bool, str]:
    """Commit VERSION + RELEASED.json. vibeic-eda#71.

    THE PUBLISH HALF IS DURABLE AND THE RECORD HALF WAS NOT. `docker push` puts
    the image in a registry, where it stays; `write_released_record` writes a
    file in the WORKING TREE and this program never shells out to git — measured
    by reading it, `_sh(["git", ...])` appears nowhere. So a checkout, a landing
    sequence, or anyone tidying a dirty tree discards the record, and the
    registry and the repo then disagree about what the current release is.

    Observed: 0.2.57 was built, smoke-tested and pushed, and `VERSION` +
    `RELEASED.json` on main both still said 0.2.56 — with a fingerprint that no
    longer reproduced, so `check_release_recorded` read "this pin set has never
    been released" about a pin set that had.

    COMMITS ONLY THE TWO RECORD FILES, by explicit path. A release run's tree
    also holds the pin edits it made; those are a separate decision that goes
    through review, and sweeping them in would publish an unreviewed pin change
    under a release commit. Never `-A`.

    Does not push: whether the record reaches origin is the caller's call, and a
    failed push must not look like a failed release. Returns (committed, note)
    and never raises — a release that published successfully must not be
    reported as failed because a commit could not be made.
    """
    # README.md carries this repo's OWN install pointers, rewritten by
    # `sync_image_version --set` immediately before this call. It is listed here so
    # the docs land in the same commit as the VERSION they describe; left out, the
    # sync would run every release and be discarded every release. Still explicit,
    # still never `-A`.
    #
    # THE PIN SITES ARE HERE TOO, and their absence was a real defect. The run
    # that published 0.2.65 moved OPENROAD_REF to b64a496b9 and composed the
    # image from it, then committed VERSION/RELEASED.json/README.md and left the
    # pin uncommitted -- so HEAD recorded f396ce8ee while the published image
    # was built from b64a496b9. Anyone cloning main got pins that do not
    # describe the image the same commit says was released, and RELEASED.json's
    # whole purpose is to answer "what was this built from".
    #
    # Listed by name, never `-A`: the three places a pin is written, plus the
    # composing Dockerfile's IMG_ tags. `git status --porcelain -- <paths>`
    # below already drops the ones this run did not touch, so naming them all is
    # safe and does not sweep in unrelated edits.
    files = ["VERSION", "RELEASED.json", "README.md",
             "Dockerfile", "docker-bake.hcl"]
    files += sorted(str(f.relative_to(eda_root))
                    for f in (eda_root / "tools").glob("*/Dockerfile"))
    present = [f for f in files if (eda_root / f).is_file()]
    if not present:
        return False, "neither VERSION nor RELEASED.json exists"
    rc, out, err = _sh(["git", "-C", str(eda_root), "status", "--porcelain",
                        "--"] + present, timeout=60)
    if rc != 0:
        return False, f"git status failed: {(err or out).strip()[:120]}"
    if not out.strip():
        return False, "record already matches HEAD — nothing to commit"
    rc, out, err = _sh(["git", "-C", str(eda_root), "add", "--"] + present,
                       timeout=60)
    if rc != 0:
        return False, f"git add failed: {(err or out).strip()[:120]}"
    msg = (f"release: record {version} as published\n"
           f"\n"
           f"Written by daily_release after the image was pushed. The publish is "
           f"durable (it is in a registry); this is the half that was not "
           f"(vibeic-eda#71).\n")
    rc, out, err = _sh(["git", "-C", str(eda_root), "commit", "-m", msg,
                        "--"] + present, timeout=120)
    if rc != 0:
        return False, f"git commit failed: {(err or out).strip()[:160]}"
    return True, f"recorded {version} in a commit"


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


def recipe_hash(eda_root: Path, target: str) -> str:
    """Digest of the Dockerfile that BUILDS this tool.

    An artefact's identity is (source commit, build recipe). The tag carried only
    the first, so a change to HOW a tool is built produced no new tag and the
    release reused the artefact built from the old recipe (#21).

    Demonstrated rather than argued: fixing #18 — verilator's install prefix, a
    pure recipe change with no pin move — left `daily_release` reporting
    "nothing to do", and the corrected Dockerfile could never reach an image.
    Worse, the root `COPY` had already moved to the new path, so the next compose
    would have failed against an artefact that does not contain it.

    Rebuilding under the SAME tag was the tempting shortcut and is the one thing
    that must not happen: every guarantee here rests on a tag naming exactly one
    artefact. So the recipe joins the tag, and a changed recipe simply produces a
    tag that does not exist yet — which is the absence trigger that already works.
    """
    f = eda_root / "tools" / target / "Dockerfile"
    if not f.is_file():
        return "nofile"
    return hashlib.sha256(f.read_bytes()).hexdigest()[:6]


def bake_recipe_vars(eda_root: Path) -> Dict[str, str]:
    """What `docker-bake.hcl` currently believes each tool's recipe digest is."""
    hcl = (eda_root / "docker-bake.hcl").read_text(errors="replace")
    return {m.group(1): m.group(2) for m in re.finditer(
        r'variable\s+"([A-Z0-9_]+)_RECIPE"\s*\{\s*default\s*=\s*"([^"]*)"', hcl)}


def write_recipe_vars(eda_root: Path, targets: Dict[str, List[str]]) -> List[str]:
    """Publish each tool's recipe digest into bake. Returns the ones that moved.

    The digest has to live somewhere bake can read because TWO expressions
    compose a tool tag — `tool_tags()` and `eda-local`'s `contexts` map — and a
    digest known only to this program made those two disagree, silently
    disabling the local-build redirect (#21). One value, written here, read by
    both.
    """
    hcl = eda_root / "docker-bake.hcl"
    text = hcl.read_text(errors="replace")
    moved: List[str] = []
    for target in targets:
        var = target.upper().replace("-", "_") + "_RECIPE"
        want = recipe_hash(eda_root, target)
        pat = re.compile(rf'(variable\s+"{var}"\s*\{{\s*default\s*=\s*")[^"]*(")')
        new = pat.sub(rf"\g<1>{want}\g<2>", text)
        if new != text:
            moved.append(f"{var}={want}")
            text = new
    if moved:
        hcl.write_text(text, encoding="utf-8")
    return moved


def compose_recipe_hash(eda_root: Path) -> str:
    """Digest of what builds the COMPOSED image: the root Dockerfile and bake.

    Three levels of this file have now had the same hole. The tool artefacts were
    keyed on the source commit and ignored the tool Dockerfile (#21). The release
    fingerprint was keyed on pins and ignored the tool recipes. And it still
    ignored the root Dockerfile and `docker-bake.hcl`, which are what turn eight
    artefacts into an image — so a fix touching only those, which is what #19 and
    #20 both are, would leave the release reporting "already released" forever.

    Measured before fixing, by appending one comment line to the root Dockerfile:
    the fingerprint did not move.

    `docker-bake.hcl` is included even though `write_recipe_vars` writes to it —
    that write runs first and is idempotent, so the digest is stable across runs
    that change nothing.
    """
    h = hashlib.sha256()
    for name in ("Dockerfile", "docker-bake.hcl"):
        f = eda_root / name
        h.update(f.read_bytes() if f.is_file() else b"missing")
    return h.hexdigest()[:6]


def artefact_tag(eda_root: Path, target: str, ref_vars: List[str],
                 refs: Dict[str, str]) -> Optional[str]:
    """The full artefact reference: every source commit, then the recipe."""
    shorts = [refs[v][:SHORT] for v in ref_vars if v in refs]
    if len(shorts) != len(ref_vars):
        return None
    return (f"ghcr.io/vibeic/eda-tool-{target}:"
            f"{'-'.join(shorts)}-{recipe_hash(eda_root, target)}")


def missing_artefacts(eda_root: Path, refs_now: Dict[str, str],
                     targets: Dict[str, List[str]]) -> Dict[str, str]:
    """target -> tag, for every pinned artefact that does not exist yet.

    THE REBUILD TRIGGER IS ABSENCE, NOT MOVEMENT. An earlier version rebuilt
    whatever moved DURING THIS RUN, which is not the same question and fails in
    the one case that matters: a run interrupted between moving the pin and
    building the artefact. That happened on 2026-07-29 — the pins were rewritten,
    the OpenROAD build was killed, and the next run said "every pin is already
    its fork's tip, nothing to do" while the artefact for that pin did not exist
    anywhere. Exactly the state the whole chain is built to prevent, produced by
    the chain itself.

    Asking whether the artefact EXISTS makes the release idempotent and
    self-healing: interrupt it anywhere, run it again, and it resumes.
    """
    missing: Dict[str, str] = {}
    for target, ref_vars in targets.items():
        ref = artefact_tag(eda_root, target, ref_vars, refs_now)
        if ref is None:
            continue
        if _sh(["docker", "image", "inspect", ref], timeout=60)[0] == 0:
            continue
        if _sh(["docker", "manifest", "inspect", ref], timeout=180)[0] == 0:
            continue
        missing[target] = ref
    return missing


#: What each tool must do inside the composed image before it is called a
#: release. Each entry RUNS the tool and asserts the binary is the one WE built:
#: a bare `command -v` passes on the base image's copy, and `klayout -v` prints a
#: version string for whichever klayout answers first. Measured 2026-07-29:
#: `command -v klayout` resolved the base image's binary while our fork lives at
#: /foss/tools/klayout-vibeic — the check was green and the fork was not loaded.
SMOKE = {
    "yosys":     "yosys -V | grep -qi yosys",
    "openroad":  "openroad -version",
    # Our klayout fork ships as libraries + add-on engines, not as a `klayout`
    # binary — see vibeic-eda#17: the `klayout` on PATH is the BASE image's build
    # and links the base library. So this asserts our fork's library is present;
    # it deliberately does NOT claim the fork is what `klayout` loads, because it
    # is not.
    "klayout":   "test -e /foss/tools/klayout-vibeic/libklayout_db.so",
    "iverilog":  "iverilog -V >/dev/null",
    "verilator": "verilator --version | grep -qi verilator",
    "ngspice":   "ngspice --version >/dev/null 2>&1 || command -v ngspice",
    "magic":     "magic --version >/dev/null 2>&1 || command -v magic",
    "netgen":    "command -v netgen",
    "cocotb":    "python3 -c 'import cocotb; print(cocotb.__version__)'",
    "pyuvm":     "python3 -c 'import pyuvm'",
    "sby":       "sby --help >/dev/null 2>&1 || command -v sby",
}


def smoke_image(image: str) -> List[dict]:
    """Run every tool in the composed image. Returns the FAILURES only.

    This proves each tool starts and is the build we shipped. It does not prove
    any of them is correct — that is the regression suite's job. A release that
    ships a tool which cannot start is a different and much cheaper failure to
    catch, and until now nothing caught it before the version was cut.
    """
    bad: List[dict] = []
    for tool, cmd in sorted(SMOKE.items()):
        rc, _, err = _sh(["docker", "run", "--rm", "--entrypoint", "bash",
                          image, "-lc", cmd], timeout=300)
        if rc != 0:
            bad.append({"tool": tool, "cmd": cmd, "err": err.strip()[-200:]})
    return bad


def peek_version(eda_root: Path) -> Tuple[str, str]:
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
    # DECIDED, NOT WRITTEN. The file moves only after the image is smoked and
    # pushed — a VERSION that advanced for an image nobody can pull is how the
    # file drifted from reality in the first place (0.2.30 on disk, 0.2.32 built).
    return old, f"{maj}.{minor}.{patch}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eda-root",
                    default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-build", action="store_true",
                    help="move pins and stop; do not build or version")
    ap.add_argument("--json", default=None)
    ap.add_argument("--smoke-only", default=None, metavar="IMAGE",
                    help="run the tool smoke against an existing image and stop")
    ap.add_argument("--record-release", default=None, metavar="VERSION",
                    help="write RELEASED.json for a version published OUTSIDE "
                         "this program (vibeic-eda#51), through the same writer "
                         "the publish path uses. Refuses when VERSION does not "
                         "match the VERSION file, because the record describes "
                         "the tree it is written from.")
    a = ap.parse_args(argv)

    if a.record_release:
        root = Path(a.eda_root)
        # The record is computed FROM THE TREE, so recording a version this tree
        # is not is recording a pin set that version was never built from — the
        # #51 defect with the numbers swapped.
        vf = (root / "VERSION")
        cur = vf.read_text().strip() if vf.is_file() else None
        if cur != a.record_release:
            print(f"[REFUSED] --record-release {a.record_release} but VERSION "
                  f"says {cur!r}. The record is computed from THIS tree; "
                  f"writing another version's name onto it records a pin set "
                  f"that version was never built from.", file=sys.stderr)
            return RC_NEEDS_HUMAN
        rec = write_released_record(root, a.record_release, bake_targets(root))
        print(f"RELEASED.json <- {rec['version']} "
              f"fingerprint={rec['pins_fingerprint']} "
              f"({len(rec['pins'])} pin(s))")
        print("  NOTE: this records what THIS TREE would build. Verify the "
              "published image was built from it — `check_release_recorded` "
              "asks the registry, and the image's own "
              "/vibeic/provenance/*.json name the refs it actually used.")
        return RC_OK

    if a.smoke_only:
        bad = smoke_image(a.smoke_only)
        for b in bad:
            print(f"  FAIL {b['tool']}: {b['cmd']}\n    {b['err']}",
                  file=sys.stderr)
        print(f"smoke: {len(SMOKE) - len(bad)}/{len(SMOKE)} tools run in "
              f"{a.smoke_only}")
        return RC_NEEDS_HUMAN if bad else RC_OK

    root = Path(a.eda_root)

    # COHERENCE BEFORE ANYTHING ELSE (vibeic-eda#75 follow-up). A pin stated
    # three different ways must state the same thing before it is worth asking
    # whether it is CURRENT — otherwise this program moves a variable while the
    # image keeps pulling the old tag, which is what it just did to sv-elab.
    #
    # `check_pins_agree` already existed and already caught it. It was wired
    # ONLY into `.github/workflows/release.yml`, and Actions is disabled at the
    # account level (vibe-ic#550) — MEASURED: `gh run list` reports ZERO runs of
    # any workflow in this repo, ever. So the gate produced no verdict, the
    # dry-run said "nothing to do", and the disagreement was found by running it
    # by hand. A gate wired to a rail that never moves is the #693 shape.
    _agree = subprocess.run([sys.executable, str(root / "tools" / "check_pins_agree.py")],
                            capture_output=True, text=True)
    if _agree.returncode != 0:
        sys.stdout.write(_agree.stdout)
        sys.stderr.write(_agree.stderr)
        print("[REFUSED] the pins disagree with themselves; a release cut here "
              "ships something other than what it says.", file=sys.stderr)
        return RC_NEEDS_HUMAN

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
    moved, unresolved, upstream_bump, undecidable = [], [], [], []
    for repo, pin in sorted(pins.items()):
        v = check_one(repo, pin)
        if v["verdict"] == "CURRENT":
            continue
        if v["verdict"] == "UPSTREAM_AVAILABLE":
            # A REGRESSION I INTRODUCED ONE COMMIT EARLIER. vibeic-eda#29 split
            # `check_one`'s STALE into STALE / UPSTREAM_AVAILABLE /
            # STALE_UNDECIDED, and this branch tests `!= "STALE"`, so the new
            # verdict fell into "could not resolve" and the release started
            # reporting `[NEEDS HUMAN] 4 pin(s) could not be resolved` where it
            # had printed nothing to do.
            #
            # The state already exists here — `upstream_bump` is this program's
            # own name for the same fact — so the fix is to route it there, not
            # to widen the STALE test. Fixing a shared vocabulary in one program
            # and matching on the old vocabulary in the other is the same defect
            # #29 was about, one commit later.
            upstream_bump.append({"repo": repo, "branch": v.get("branch"),
                                  "from": pin[:9], "to": "(not moved)",
                                  "behind": v.get("behind"),
                                  "branch_is_ours": False})
            continue
        if v["verdict"] != "STALE":
            unresolved.append({"repo": repo, "why": f"{v['verdict']}: {v['detail']}"})
            continue
        tip = _gh_tip(repo, v["branch"])
        if not tip:
            unresolved.append({"repo": repo,
                               "why": f"tip of {v['branch']} unreadable"})
            continue
        ours = branch_is_ours(repo, v["branch"])
        row = {"repo": repo, "branch": v["branch"], "arg": args_of.get(repo),
               "from": pin[:9], "to": tip[:9], "sha": tip,
               "behind": v.get("behind"), "branch_is_ours": ours}
        if ours:
            moved.append(row)
        elif ours is False:
            # Not stale — a deliberate pin on upstream history. Reported so it is
            # visible, never advanced on its own.
            upstream_bump.append(row)
        else:
            # UNKNOWN IS NOT A NEGATIVE. This used to fall in with the branch
            # above and print "that branch carries none of our commits" — a
            # statement of fact asserted from a `None`, about branches like
            # `yosys satfix-integration` that exist nowhere but our fork. Both
            # outcomes leave the pin alone; only one of them is entitled to say
            # why.
            undecidable.append(row)

    print(f"daily_release: {len(pins)} pin(s), {len(moved)} tool(s) with a new "
          f"version, {len(upstream_bump)} awaiting an upstream decision, "
          f"{len(undecidable)} undecidable, {len(unresolved)} unresolved")
    for u in upstream_bump:
        print(f"  {u['repo']:<20} {u['from']} -> {u['to']}  ({u['branch']}) "
              f"NOT MOVED — that branch carries none of our commits, so this is "
              f"an upstream version bump, not a stale pin")
    for u in undecidable:
        print(f"  {u['repo']:<20} {u['from']} -> {u['to']}  ({u['branch']}) "
              f"NOT MOVED — could not determine whether that branch carries our "
              f"commits. The pin is left alone because unknown is not a licence "
              f"to move it, NOT because the branch was found to be upstream's.",
              file=sys.stderr)
    for m in moved:
        print(f"  {m['repo']:<20} {m['from']} -> {m['to']}  ({m['branch']})")
    for u in unresolved:
        print(f"  {u['repo']:<20} UNRESOLVED — {u['why']}", file=sys.stderr)

    result = {"program": "daily_release", "moved": moved,
              "upstream_bump_available": upstream_bump,
              "undecidable": undecidable,
              "unresolved": unresolved, "built": [], "version": None}

    # THE COMPOSED IMAGE IS AN ARTEFACT TOO. Checking only the per-tool
    # artefacts left the exact hole this program is about, one level up: after a
    # run built and published both tools and was then interrupted before
    # composing, the next run said "every pin is its fork's tip and every pinned
    # artefact exists — nothing to rebuild" while no image had ever been made
    # from those pins. Reproduced it on 2026-07-29, in this file, hours after
    # fixing the same shape for tool artefacts.
    #
    # An image tag is a version number, not a content hash, so "does the image
    # exist" cannot be asked of the registry. RELEASED.json records what the last
    # published image was built FROM; a pin set that does not match it has never
    # been released.
    # Publish the recipe digests BEFORE anything reads a tag, so bake and this
    # program cannot disagree about what an artefact is called.
    stale = write_recipe_vars(root, targets)
    for s in stale:
        print(f"  recipe changed: {s}")

    # The fingerprint covers pins AND recipes, because an image is built from
    # both. Over pins alone, 0.2.33 and 0.2.34 — identical pins, different
    # verilator recipe, different bytes — would share a fingerprint, and
    # RELEASED.json would be unable to say which one it recorded. It would also
    # report a recipe-only change as "already released", which is exactly the
    # #18 shape one layer up.
    fp = pins_fingerprint({**pins,
                           **{f"recipe:{k}": recipe_hash(root, k)
                              for k in targets},
                           "recipe:__compose__": compose_recipe_hash(root)})
    rec = released_record(root)
    unreleased = rec.get("pins_fingerprint") != fp
    if unreleased:
        print(f"  pin set {fp} has not been released "
              f"(last released: {rec.get('pins_fingerprint') or 'never'})")

    refs_all = {f"{args_of[r]}_REF": s for r, s in pins.items() if r in args_of}
    # Computed on a dry run too: `docker image/manifest inspect` only reads, and
    # a preview that cannot see a missing artefact previews a rosier release than
    # the real one.
    absent = missing_artefacts(root, refs_all, targets)
    if absent:
        print(f"  {len(absent)} pinned artefact(s) do not exist yet: "
              f"{', '.join(sorted(absent))}")
    result["absent_artefacts"] = sorted(absent)

    if (moved or absent or unreleased) and not a.dry_run:
        for m in moved:
            if not m["arg"]:
                unresolved.append({"repo": m["repo"],
                                   "why": "no ARG <NAME>_REF pins it"})
                continue
            m["files"] = rewrite_pin(root, m["arg"], m["sha"])
        refs_now = {f"{args_of[r]}_REF": s for r, s in
                    {**pins, **{m["repo"]: m["sha"] for m in moved}}.items()
                    if r in args_of}
        # RE-PUBLISH the recipe digests, because `rewrite_pin` just edited the
        # very files they are the hash OF.
        #
        # The call above runs before anything reads a tag, which is right for
        # everything between there and here. But a pin lives INSIDE
        # `tools/<t>/Dockerfile`, so moving it necessarily moves
        # `sha256(tools/<t>/Dockerfile)[:6]` — and the digest published a moment
        # ago is now describing a file that no longer exists in that form.
        #
        # `retag_images` below recomputes the tag from the CURRENT file, so
        # without this the two sites disagree by construction on every release
        # that moves a pin: `docker-bake.hcl` keeps the pre-edit digest and the
        # composing Dockerfile gets the post-edit one. That is the exact
        # `check_pins_agree` failure the 2026-08-05 tick reported for openroad,
        # yosys and verilator — "bake would publish a tag the composing
        # Dockerfile does not pull" — reported AFTER the release, by which point
        # the round had already failed to compose.
        for s2 in write_recipe_vars(root, targets):
            print(f"  recipe changed (after pin move): {s2}")

        result["retagged"] = retag_images(root, refs_now, targets)
        for t in result["retagged"]:
            print(f"  retag {t}")

        if not a.no_build:
            builder = preferred_builder()
            bflag = ["--builder", builder] if builder else []
            if builder:
                print(f"  building on `{builder}` (docker-container), so a "
                      f"long step's log is kept in full")
            eff = log_limit_effective(builder)
            if eff is False:
                print("  NOTE: the buildx driver is `docker`, so buildkit runs "
                      "inside dockerd and BUILDKIT_STEP_LOG_MAX_SIZE cannot "
                      "reach it from here — a long step's log WILL be clipped "
                      "at 2 MiB. To lift it, put the variable in dockerd's own "
                      "environment, or build on a `docker-container` builder "
                      "created with --driver-opt env.BUILDKIT_STEP_LOG_MAX_SIZE=-1.")
            elif eff is None:
                print("  NOTE: could not read the buildx driver, so whether the "
                      "step-log limit is lifted is UNKNOWN — not assumed lifted.")
            build = sorted(missing_artefacts(root, refs_now, targets))
            if build:
                print(f"  artefacts to build (absent, not merely moved): "
                      f"{', '.join(build)}")
            for t in build:
                print(f"  building {t} …", flush=True)
                # `--push` is what makes the next hop incremental FOR ANYONE
                # ELSE. Without it the artefact exists on one machine, and the
                # release image can only be composed here, from this cache — a
                # per-tool architecture whose incrementality is an accident of
                # local state. Measured 2026-07-29: all 8 artefacts existed
                # locally and ghcr held none of them.
                # `--set` because the bake file's own tag expression knows only
                # the source commit. Overriding here keeps ONE definition of an
                # artefact's identity — this function's — rather than two that
                # can disagree.
                # refs_NOW, not refs_all: refs_all is the pre-move reading, so
                # a run that moved a pin would build the artefact and tag it with
                # the commit it replaced.
                want = artefact_tag(root, t, targets[t], refs_now)
                # `--progress plain` here too. I added it to the compose after a
                # wedged build was indistinguishable from a quiet one, and left
                # the TOOL builds — the LONGER of the two — still writing nothing
                # to the log. Fixing one instance of a defect and not its sibling
                # is how the sibling gets found the hard way.
                rc, _, err = _sh(["docker", "buildx", "bake", *bflag, "-f",
                                  str(root / "docker-bake.hcl"), "--push",
                                  "--progress", "plain",
                                  "--set", f"{t}.tags={want}", t], cwd=root,
                                 stream=True, env=BUILD_ENV,
                                 timeout=TOOL_BUILD_TIMEOUT)
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
            #
            # THE VERSION IS DECIDED FIRST AND THE COMPOSE IS TAGGED WITH IT.
            # An earlier version composed `eda` — which tags `:${TAG}` (default
            # `dev`) and `:latest` — then smoked `vibeic-eda:local` and tagged
            # THAT as the new version. `:local` is `eda-local`'s tag, so it was
            # whatever the last local build left behind: the release would have
            # smoked one image and published a different, older one under the
            # new number. Caught by reading the bake file, not by any output —
            # every step succeeds.
            #
            # `--set` overrides the target's tags so the compose cannot move
            # `:latest` onto an image that has not passed the smoke yet.
            old, new = peek_version(root)
            tag = f"ghcr.io/vibeic/vibeic-eda:{new}"
            print(f"  composing {tag} from the pinned artefacts …", flush=True)
            # `--progress plain` because the default TTY renderer emits almost
            # nothing to a redirected log. A compose that wedged after
            # `task-delete` was indistinguishable from one making quiet progress:
            # no CPU, no disk, no network, three lines of log, and the only way to
            # tell was reading dockerd's journal. A build that cannot be watched
            # cannot be diagnosed.
            bake = ["docker", "buildx", "bake", *bflag, "-f",
                    str(root / "docker-bake.hcl"), "--load", "--progress",
                    "plain", "--set", f"eda.tags={tag}", "eda"]
            rc, _, err = _sh(bake, cwd=root, stream=True, env=BUILD_ENV,
                             timeout=COMPOSE_TIMEOUT)
            if rc != 0:
                print("  registry compose failed once; retrying before "
                      "downgrading (a transient must not cost reproducibility)",
                      flush=True)
                # The RETRY needs the deadline more than the first attempt does,
                # not less: it runs after something already went wrong, and an
                # unbounded retry turns a failure into a hang. Found by the
                # wiring test, which is the whole reason that test asserts about
                # every call site instead of the one I was editing.
                rc, _, err = _sh(bake, cwd=root, stream=True, env=BUILD_ENV,
                                 timeout=COMPOSE_TIMEOUT)
            if rc != 0:
                # NO LOCAL FALLBACK. There used to be one — compose `eda-local`,
                # which redirects each tool to a freshly built target — and it
                # stopped working the moment the artefact tag gained its recipe
                # component (#21): `eda-local`'s `contexts` map reconstructs tool
                # tags from the bake expression, which now produces
                # `eda-tool-iverilog:fe9dfab` while the root Dockerfile asks for
                # `eda-tool-openroad:92b079b-7444a2`. The keys no longer match the
                # FROM refs, so the redirect does not apply and `eda-local`
                # resolves from the registry — the same thing that just failed.
                #
                # A fallback that does not fall back is worse than none: it turns
                # one loud failure into two quiet ones and, when it did work, it
                # produced an image that could not be reproduced anywhere else.
                # A failed compose is a failed release, said once, loudly.
                print(f"[FAIL] the image did not compose after a retry; no "
                      f"version was cut, so nothing claims to be a release:"
                      f"\n{err[-6000:]}", file=sys.stderr)
                result["registry_composed"] = False
                return RC_NEEDS_HUMAN
            result["registry_composed"] = True

            # SMOKE BEFORE THE VERSION IS REAL. A tag is a claim, and cutting one
            # over an image whose tools have never been started makes the claim
            # on nothing. The tag is removed on failure, so a bad image cannot be
            # left carrying a version number.
            bad = smoke_image(tag)
            result["smoke_failures"] = bad
            if bad:
                _sh(["docker", "rmi", tag])
                print(f"[FAIL] {len(bad)} tool(s) do not run in the composed "
                      f"image; the tag was removed and NO version was cut:",
                      file=sys.stderr)
                for b in bad:
                    print(f"    {b['tool']}: {b['cmd']}\n      {b['err']}",
                          file=sys.stderr)
                return RC_NEEDS_HUMAN
            print(f"  smoke: all {len(SMOKE)} tools run in {tag}")

            prc, _, perr = _sh(["docker", "push", tag], timeout=7200)
            pushed = prc == 0
            if not pushed:
                print(f"  PUSH FAILED — {new} exists here and nowhere else:"
                      f"\n{perr[-800:]}", file=sys.stderr)
            result["pushed"] = pushed
            result["version"] = {"from": old, "to": new}

            if pushed:
                # `latest` moves only after a smoke pass AND a successful push,
                # so it can never point at something nobody could pull.
                _sh(["docker", "tag", tag, "ghcr.io/vibeic/vibeic-eda:latest"])
                _sh(["docker", "push", "ghcr.io/vibeic/vibeic-eda:latest"],
                    timeout=7200)
                (root / "VERSION").write_text(new + "\n", encoding="utf-8")
                # RECOMPUTE. `fp` above was measured before `rewrite_pin` and
                # `retag_images` edited the root Dockerfile, and those edits feed
                # `compose_recipe_hash`. Recording the pre-edit value made the
                # record IRREPRODUCIBLE: the shipped tree hashes to something
                # else, so every later run reads "this pin set has never been
                # released" and cuts another version. Measured on 0.2.45 — the
                # released tree computes e4e0a5f6 while the file it shipped
                # records 94d85fda, and the next run started composing 0.2.46
                # with nothing whatsoever changed.
                #
                # This is the program's own stated refusal — "it will not cut an
                # image version when no tool changed" — defeated by the order of
                # two writes. The decision fingerprint (early) and the RECORDED
                # fingerprint (here) answer different questions and only the
                # second has to match the tree that shipped.
                write_released_record(root, new, targets)
                # #71 — and COMMIT it. Only when the image was actually pushed:
                # a LOCAL ONLY build has published nothing, so recording it as
                # released would assert a release nobody can pull.
                if pushed:
                    # THIS REPO'S OWN DOCS, BEFORE THE RECORD COMMIT. VERSION is
                    # written directly above, which is why README.md's five
                    # `docker pull` / `docker run` commands sat at 0.2.56 while
                    # VERSION said 0.2.63 — seven releases telling a reader to pull
                    # an image the release had superseded.
                    # `sync_image_version.py` is the tool that knows where those
                    # pointers are, and writing VERSION without it is what let them
                    # drift. It runs AFTER the push because it refuses to point at a
                    # tag `docker pull` cannot resolve, and BEFORE the record commit
                    # so README.md lands in the same commit as VERSION rather than
                    # as a stray edit nobody stages.
                    _rc, _dout, _derr = _sh(
                        ["python3", str(root / "sync_image_version.py"),
                         "--set", new], cwd=str(root))
                    print(f"  docs: {'synced to ' + new if _rc == 0 else 'SYNC FAILED — ' + (_dout or '').strip()[-160:]}")
                    _ok, _note = commit_release_record(root, new)
                    print(f"  record: {_note}")
            print(f"  VERSION {old} -> {new}  "
                  f"{'published' if pushed else 'LOCAL ONLY'}")
            # vibe-ic#754 — PUBLISHING AND ANCHORING ARE ONE ACTION.
            #
            # They were two, with nothing linking them, so `:latest` moved off the
            # version vibe-ic pins on every single release and the two were reunited
            # only when a landing gate happened to look. Applied by hand four times
            # before this. Only on a real publish: a LOCAL ONLY build has nothing to
            # anchor TO, and pointing the repo at an unpullable tag is worse than
            # leaving it behind a real one.
            #
            # Advisory by construction — it opens a PR, it does not push to main, and
            # a failure here must not fail a release that already succeeded. The
            # landing gate that used to be the only line of defence is still there and
            # is still authoritative; this just stops it being the FIRST time anyone
            # notices.
            if pushed:
                try:
                    import pr_notify as _prn
                    _aok, _anote = _prn.open_anchor_pr(new)
                except Exception as _e:      # noqa: BLE001
                    _aok, _anote = False, f"{_e.__class__.__name__}: {_e}"
                print(f"  anchor: {_anote}")
                result.setdefault("anchor_pr", {})["ok"] = bool(_aok)
                result["anchor_pr"]["note"] = _anote

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(result, indent=2) + "\n",
                                encoding="utf-8")

    if unresolved:
        print(f"[NEEDS HUMAN] {len(unresolved)} pin(s) could not be resolved; "
              f"they were left alone and are NOT in this release", file=sys.stderr)
        return RC_NEEDS_HUMAN
    if not moved and not absent and not unreleased:
        print("[PASS] every pin is its fork's tip, every pinned artefact exists, "
              "and this pin set is already released — nothing to do")
    elif a.dry_run:
        # A release follows from ANY of the three, not from `unreleased` alone —
        # an absent artefact means the image cannot already contain it.
        print(f"[DRY RUN] would move {len(moved)} pin(s), build {len(absent)} "
              f"absent artefact(s), and "
              f"{'cut a release' if (moved or absent or unreleased) else 'cut no release'}")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
