#!/usr/bin/env python3
"""Every command the base image provided must still resolve in ours.

WHY THIS EXISTS
===============
Replacing a tool takes its co-tenants with it, and this has now happened three
times in one week:

  * `rm -rf /foss/tools/yosys` before installing ours removed eqy, mcy and sby,
    which the base kept in that prefix, and left five symlinks pointing at
    nothing (vibeic-eda#19);
  * the same `rm -rf` removed the yices solvers, worked around by copying them
    aside under a comment reading "they are not ours to rebuild" (#25);
  * our yosys upgrade left the base's `slang.so` plugin in place, where it now
    loads and corrupts the heap on teardown (#24).

Each was found by hand, months apart, by someone tripping over it. None of the
existing checks looks at the question they share: **did we take something away?**

`check_fork_only` asks whether sources are ours. `check_pins_agree` asks whether
a pin is coherent. `fork_reaches_flow_check` asks whether the flow runs OUR build
of a tool we claim. All three are about what we ADD. This one is about what we
subtract.

THE PREDICATE IS RESOLVABILITY, NOT LOCATION
============================================
My first version compared file lists per directory and reported five losses in
0.2.37 — `yosys/sby` and four `yices*`. All five are fine: yices was rescued to
`/foss/tools/yices/bin` and sby comes from our own build at `/usr/local/bin`.
"Not in that directory any more" is not "gone"; a tool that moved is still a tool
you have. So the question asked here is whether the NAME still resolves on PATH,
which is what a user or a flow actually depends on.

WHAT IT CANNOT SEE, STATED
==========================
A command that resolves but no longer works — #24 exactly, where `slang.so`
loads and then aborts. Resolvability is a floor, not a guarantee, and the smoke
in `daily_release` is the layer that runs things. It also only covers prefixes we
replace: a capability the base provides somewhere we never touch cannot be lost
by us and is not this program's business.

Exit: 0 nothing was lost, 1 something was, 2 nothing compared.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

RC_OK, RC_LOST, RC_NOTHING = 0, 1, 2

#: Prefixes our composing image removes or writes over. Read from the Dockerfile
#: where possible; this is the fallback for the ones expressed as a bare COPY.
_FALLBACK_PREFIXES = ("yosys", "ngspice", "magic", "netgen", "iverilog",
                      "verilator", "klayout")


def _sh(cmd, timeout=600):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                                   # noqa: BLE001
        return 1, "", str(exc)


def base_image(dockerfile: Path) -> str:
    """The pinned base, read from the Dockerfile rather than assumed."""
    m = re.search(r"^ARG\s+BASE_IMAGE=(\S+)", dockerfile.read_text(errors="replace"),
                  re.M)
    return m.group(1) if m else ""


def replaced_prefixes(dockerfile: Path) -> List[str]:
    """Tool directories we delete, copy over, or move aside in the composing image.

    `mv` counts, and leaving it out was a real hole. vibeic-eda#17 replaces the
    base's klayout by moving its tree to `klayout-base` and symlinking the
    canonical path at ours — no `rm -rf`, no `COPY` to that path, so this
    function did not consider `/foss/tools/klayout` replaced at all and the
    check silently compared nothing for the tool it most needed to watch.
    """
    text = dockerfile.read_text(errors="replace")
    found = set(re.findall(r"rm -rf[^\n]*?/foss/tools/([A-Za-z0-9_.-]+)", text))
    for m in re.finditer(r"^COPY --from=img-\S+\s+\S+\s+/foss/tools/([A-Za-z0-9_.-]+)",
                         text, re.M):
        found.add(m.group(1))
    # `rm -rf a b c` names several in one line; the regex above catches the first
    for line in text.splitlines():
        if "rm -rf" in line and "/foss/tools/" in line:
            found.update(re.findall(r"/foss/tools/([A-Za-z0-9_.-]+)", line))
        # `mv /foss/tools/x /foss/tools/x-base` — the SOURCE is what was replaced
        for m in re.finditer(r"\bmv\s+/foss/tools/([A-Za-z0-9_.-]+)\s+/foss/tools/",
                             line):
            found.add(m.group(1))
    return sorted(found) or list(_FALLBACK_PREFIXES)


def command_names(image: str, prefixes: List[str]) -> List[str]:
    """Executables the base ships under each replaced prefix.

    BOTH `<prefix>/bin` AND `<prefix>` itself. Only the first was listed, and
    that is a layout assumption dressed up as a rule: yosys, magic and netgen
    put their binaries in `bin/`, klayout puts `klayout` and twelve `strm2*`
    buddies at the top of its prefix. Measured before this fix — 55 commands
    compared, and `klayout`, `strm2gds` and `strmxor` were in none of them. The
    check that exists to catch "did we take something away" was blind to the
    one tool whose replacement was actually being contemplated.

    `! -name '*.so*'` because a shared object under the prefix is not a command;
    listing them would compare version-suffixed sonames as if they were tools
    and report a loss on every version bump.
    """
    script = "; ".join(
        f'[ -d /foss/tools/{p}/bin ] && ls /foss/tools/{p}/bin; '
        f'[ -d /foss/tools/{p} ] && find /foss/tools/{p} -maxdepth 1 -type f '
        f'-executable ! -name "*.so*" -printf "%f\\n"'
        for p in prefixes)
    rc, out, _ = _sh(["docker", "run", "--rm", "--entrypoint", "bash", image,
                      "-lc", script])
    if rc != 0 and not out.strip():
        return []
    return sorted({ln.strip() for ln in out.splitlines()
                   if ln.strip() and not ln.startswith("[INFO]")})


def unresolvable(image: str, names: List[str]) -> Optional[List[str]]:
    """Names that do NOT resolve in `image`, under a LOGIN shell.

    RETURNS None WHEN THE IMAGE COULD NOT BE PROBED AT ALL, and that distinction
    is the whole reason this signature is not simply `List[str]`.

    Measured 2026-08-05, while wiring vibeic-eda#88:

        $ python3 check_no_capability_lost.py vibeic-nonexistent-image:doesnotexist
        check_no_capability_lost: 78 command(s) ...; 0 no longer resolve in ...
        [PASS] nothing the base provided under those prefixes was lost
        $ echo $?
        0

    An image that does not exist reported a clean bill of health. `docker run`
    failed, its exit status was discarded, `out` was empty, and an empty output
    means "every name resolved" to a reader that only looks at the text. The
    probe breaking and the answer being good produce the IDENTICAL result, and
    the identical result is the reassuring one.

    That mattered more than a cosmetic bug: #88 asked for this check to become
    blocking in the tick, and a blocking gate whose dominant failure mode is a
    silent PASS is exactly the "blocking-looking check that silently is not one"
    the issue exists to remove. Making the call site fail-closed while the
    program itself cannot fail would have moved the defect rather than fixed it.

    The script is a `;`-joined chain ending in `command -v N || echo N`, which
    exits 0 whichever branch runs — so a non-zero status here is the container
    failing to start or bash failing to run, never a name that was absent.

    Login on BOTH sides, deliberately. My first version listed the base's files
    with `bash -lc` and then probed ours with `sh -c` — two different questions —
    and reported that 0.2.5 had "lost" `yosys` itself. It had not: the base only
    puts /foss/tools/* on PATH from profile.d, so nothing there resolves without
    a login shell, and 0.2.5 predates the `ENV PATH` enhancement that fixed that
    for `docker exec`. The asymmetric comparison turned a property the base
    shares into a loss we caused.

    A like-for-like comparison is the only one that can attribute anything. That
    our image ALSO resolves these without a login shell is a separate, additive
    improvement, and not what this program measures.
    """
    script = "; ".join(f'command -v {n} >/dev/null 2>&1 || echo {n}'
                       for n in names)
    rc, out, err = _sh(["docker", "run", "--rm", "--entrypoint", "bash", image,
                        "-lc", script])
    if rc != 0:
        print(f"[probe] docker run on {image} exited {rc}: "
              f"{(err or out).strip().splitlines()[-1][:200] if (err or out).strip() else 'no output'}",
              file=sys.stderr)
        return None
    return sorted({ln.strip() for ln in out.splitlines()
                   if ln.strip() and not ln.startswith("[INFO]")})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", help="our composed image")
    ap.add_argument("--dockerfile",
                    default=str(Path(__file__).resolve().parents[1] / "Dockerfile"))
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    df = Path(a.dockerfile)
    if not df.is_file():
        print(f"[NOT CHECKED] no Dockerfile at {df}", file=sys.stderr)
        return RC_NOTHING
    base = base_image(df)
    if not base:
        print("[NOT CHECKED] the Dockerfile names no ARG BASE_IMAGE, so there is "
              "nothing to compare against — not a pass", file=sys.stderr)
        return RC_NOTHING

    prefixes = replaced_prefixes(df)
    names = command_names(base, prefixes)
    if not names:
        print(f"[NOT CHECKED] listed no commands under {len(prefixes)} prefix(es) "
              f"in {base} — nothing was compared", file=sys.stderr)
        return RC_NOTHING

    lost = unresolvable(a.image, names)
    if lost is None:
        # WRITE THE JSON ANYWAY. Returning before the write leaves YESTERDAY's
        # file in place, and a consumer reading a stale `"lost": []` sees a pass
        # that nothing produced today — the same substitution of an old answer
        # for a missing one that this branch exists to refuse.
        if a.json:
            Path(a.json).parent.mkdir(parents=True, exist_ok=True)
            Path(a.json).write_text(json.dumps(
                {"program": "check_no_capability_lost", "base": base,
                 "image": a.image, "prefixes": prefixes,
                 "commands": len(names), "lost": None,
                 "error": f"could not probe {a.image}"}, indent=2) + "\n",
                encoding="utf-8")
        print(f"[NOT CHECKED] could not probe {a.image} — {len(names)} command(s) "
              f"were listed from the base and NONE of them were compared. This is "
              f"not a pass.", file=sys.stderr)
        return RC_NOTHING

    print(f"check_no_capability_lost: {len(names)} command(s) the base provides "
          f"under {len(prefixes)} replaced prefix(es); {len(lost)} no longer "
          f"resolve in {a.image}")
    for t in lost:
        print(f"    LOST: {t}")

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(
            {"program": "check_no_capability_lost", "base": base,
             "image": a.image, "prefixes": prefixes,
             "commands": len(names), "lost": lost}, indent=2) + "\n",
            encoding="utf-8")

    if lost:
        print(f"[FAIL] {len(lost)} capability the base image shipped is gone from "
              f"ours. Rescue it the way yices and eqy/mcy are rescued, or remove "
              f"what advertises it.", file=sys.stderr)
        return RC_LOST
    print("[PASS] nothing the base provided under those prefixes was lost "
          "(resolvability only — a command that resolves and then aborts is the "
          "smoke's job)")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
