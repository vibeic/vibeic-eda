#!/usr/bin/env python3
"""fleet_config.py — is the configuration this tick runs on the COMMITTED one?

vibeic/vibeic-eda#10. `FORKS.json` is the fleet list: it decides which forks are
audited at all, and each entry's `role` is handed to the judge as context. For months
the cron read a working-tree copy carrying three forks (OpenSTA, ALIGN-public,
ALIGN-pdk-sky130) that no commit contained. Every daily report was a faithful summary
of a premise that existed on exactly one machine — and a fresh clone would have
produced a 12-row report whose own headline count agreed with itself.

This module answers two questions and refuses to let the tick publish while either is
answered badly:

  1. Is the on-disk configuration the one at HEAD?  `config_status()`
  2. Do the rows about to be published come from that configuration?  `phantom_rows()`

The second is what keeps the first from becoming decoration. The report's rows are not
read from `FORKS.json` — they are read from the ledger directory, which
`discover_forks.main()` writes one file per fork into and never prunes. So a fleet list
that is perfectly committed can still sit beside ledgers for forks it does not name, and
those rows publish yesterday's verdict forever with nothing marking them stale. Stamping
a commit sha onto a report whose rows did not come from that commit is the false
certificate this repo keeps producing, one layer further down.

SEVERITY — deliberate, because a check that fires on a state the operator considers
normal gets commented out, and a commented-out check is worse than none:

  modified / absent-from-head  → FATAL. The tick is auditing a different fleet than the
        repository declares, or a fleet the repository does not declare at all. This is
        #10 itself. The remedy is one commit, and "I added a fork and did not commit it"
        must not be a normal state — it is the defect.
  formatting                   → WARN + published stamp. The bytes differ but the parsed
        configuration is identical, so nothing about what is audited, or asked, can have
        changed. Killing the day's report over a re-indent teaches operators that this
        check is noise.
  unversioned                  → WARN + published stamp. git could not be consulted at
        all (no repository, no HEAD, no git binary). Fatal here would break every
        deployment that is not a checkout, which is the fastest route to deletion; and
        vibeic/vibeic-eda#10 asks for exactly this marker ("commit sha, or an explicit
        'uncommitted' marker"). The mitigation is that the marker is loud and permanent:
        the report says, in its header, that nothing vouches for its configuration.

`GK_ALLOW_UNVERSIONED_FLEET=1` downgrades the FATAL states to a warning for operators
who must ship before they can commit. It does not buy a clean report: the stamp still
says the configuration was not committed and additionally records that the override was
used. An escape hatch that leaves no mark is just a slower way of deleting the check.

NOT DONE HERE, deliberately: this module never writes `FORKS.json`. A tick that
regenerated its own fleet list from discovery would make the list a derived artefact,
take away the operator's ability to say "track this fork", and make every check above
vacuous — a self-populating input cannot disagree with itself.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).parent

FLEET_FILE = "FORKS.json"
#: Configuration files the tick reads out of its own source directory. `FORKS.json`
#: governs the audit itself (which forks, and the `role` the judge is told); its drift is
#: fatal. `ENHANCEMENTS.json` is hand-authored too and is read by `build_page` for the
#: published monitor page's fork/backlog counts — drift there mis-states a public page
#: but cannot change a verdict, so it is reported, not fatal.
WATCHED = (FLEET_FILE, "ENHANCEMENTS.json")

#: States in which the configuration that produced the tick is not the committed one in a
#: way that could change what the report says.
FATAL_STATES = ("modified", "absent-from-head")

OVERRIDE_ENV = "GK_ALLOW_UNVERSIONED_FLEET"


class FleetConfigUnversioned(RuntimeError):
    """The tick's configuration is not the committed configuration.

    vibeic/vibeic-eda#10. Raised INSTEAD of publishing, for the same reason
    `CountsDisagree` is: a report produced from a fleet list that exists in no commit
    cannot be reproduced, cannot be reviewed, and looks exactly like one that can.
    """


def _git(*args: str) -> tuple[int, str]:
    """Run git in the source directory. Never raises — a missing or broken git is a
    state this module reports, not an exception it propagates."""
    try:
        r = subprocess.run(("git", "-C", str(HERE)) + args,
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
        return 127, str(e)
    return r.returncode, (r.stdout if r.returncode == 0 else r.stderr).strip()


def head_commit() -> str | None:
    """The commit the source directory is checked out at, or None if unknowable."""
    rc, out = _git("rev-parse", "HEAD")
    return out if rc == 0 and out else None


def _head_bytes(name: str) -> tuple[bytes | None, str]:
    """(blob at HEAD, why-not). `name` is resolved relative to THIS directory's path
    inside the repository, so the check works wherever the repository is cloned and
    whatever the checkout is named."""
    rc, prefix = _git("rev-parse", "--show-prefix")
    if rc != 0:
        return None, f"not a git checkout ({prefix.splitlines()[0][:80] if prefix else 'git failed'})"
    if head_commit() is None:
        return None, "no HEAD (unborn branch or bare repository)"
    try:
        r = subprocess.run(("git", "-C", str(HERE), "show", f"HEAD:{prefix}{name}"),
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
        return None, str(e)
    if r.returncode != 0:
        return None, "absent-from-head"
    return r.stdout, ""


def _entries(name: str, doc) -> dict:
    """Normalize a configuration document to {entry-name: entry}.

    `FORKS.json` is `{"forks": [{"tool": …}]}`; `ENHANCEMENTS.json` is keyed by tool at
    the top level. Both are per-tool registries, so both compare the same way.
    """
    if isinstance(doc, dict) and isinstance(doc.get("forks"), list):
        return {e.get("tool", f"<entry {i}>"): e
                for i, e in enumerate(doc["forks"]) if isinstance(e, dict)}
    if isinstance(doc, dict):
        return {k: v for k, v in doc.items() if not k.startswith("_")}
    return {}


def _short(v, n: int = 60) -> str:
    if isinstance(v, (list, dict)):
        return f"<{type(v).__name__} of {len(v)}>"
    s = repr(v)
    return s if len(s) <= n else s[:n] + "…"


def describe(name: str, committed, on_disk) -> list[str]:
    """One line per way the on-disk configuration differs from the committed one.

    Named entries, named fields, both readings — "the fleet list differs" sends the
    reader back to diffing two files by hand, which is the state this check exists to
    end.
    """
    out: list[str] = []
    ca, cb = _entries(name, committed), _entries(name, on_disk)
    for k in cb:
        if k not in ca:
            e = cb[k]
            up = e.get("upstream") if isinstance(e, dict) else None
            out.append(f"+ {k}" + (f" (upstream {up})" if up else "") +
                       " — on disk, in no commit")
    for k in ca:
        if k not in cb:
            out.append(f"- {k} — committed, missing from the file on disk")
    for k in ca:
        if k not in cb or not (isinstance(ca[k], dict) and isinstance(cb[k], dict)):
            if k in cb and ca[k] != cb[k]:
                out.append(f"~ {k}: value differs")
            continue
        for f in sorted(set(ca[k]) | set(cb[k])):
            av, bv = ca[k].get(f, "<absent>"), cb[k].get(f, "<absent>")
            if av != bv:
                out.append(f"~ {k}.{f}: committed {_short(av)} → on disk {_short(bv)}")
    # top-level keys that are not the entry registry (e.g. FORKS.json's `org`)
    if isinstance(committed, dict) and isinstance(on_disk, dict):
        skip = set(ca) | set(cb) | {"forks"}
        for f in sorted((set(committed) | set(on_disk)) - skip):
            av, bv = committed.get(f, "<absent>"), on_disk.get(f, "<absent>")
            if av != bv:
                out.append(f"~ (top-level) {f}: committed {_short(av)} → on disk {_short(bv)}")
    return out


def config_status(name: str, root: Path | None = None) -> dict:
    """State of one configuration file: is the copy this tick reads the committed one?

    Returns {file, state, commit, entries, detail}. `state` is one of
    committed / formatting / modified / absent-from-head / unversioned / unreadable.
    """
    path = (root or HERE) / name
    st = {"file": name, "state": "unversioned", "commit": head_commit(),
          "entries": None, "detail": []}
    try:
        disk = path.read_bytes()
    except OSError as e:
        st["state"] = "unreadable"
        st["detail"] = [f"cannot read {path}: {e}"]
        return st
    try:
        disk_doc = json.loads(disk)
        st["entries"] = len(_entries(name, disk_doc))
    except json.JSONDecodeError as e:
        st["state"] = "unreadable"
        st["detail"] = [f"{name} on disk is not valid JSON: {e}"]
        return st

    blob, why = _head_bytes(name)
    if blob is None:
        st["state"] = "absent-from-head" if why == "absent-from-head" else "unversioned"
        st["detail"] = [f"{name} is not in HEAD — the tick is running on a file the "
                        f"repository does not carry"] if why == "absent-from-head" else [why]
        return st
    if blob == disk:
        st["state"] = "committed"
        return st
    try:
        head_doc = json.loads(blob)
    except json.JSONDecodeError as e:
        st["state"] = "modified"
        st["detail"] = [f"{name} at HEAD is not valid JSON ({e}); the on-disk copy "
                        f"cannot be shown to match it"]
        return st
    if head_doc == disk_doc:
        st["state"] = "formatting"
        st["detail"] = [f"{name} differs from HEAD in formatting only "
                        f"({len(blob)} → {len(disk)} bytes); the parsed configuration "
                        f"is identical, so nothing audited can have changed"]
        return st
    st["state"] = "modified"
    st["detail"] = describe(name, head_doc, disk_doc)
    return st


def fleet_tools(root: Path | None = None) -> set[str]:
    """Tool names the ON-DISK fleet list authorises a row for."""
    try:
        doc = json.loads(((root or HERE) / FLEET_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return set(_entries(FLEET_FILE, doc))


def phantom_rows(tools, root: Path | None = None) -> list[str]:
    """Tools about to be published that the fleet list does not name, sorted.

    `discover_forks.main()` writes one ledger per fork and never deletes one, so a fork
    dropped from the fleet list keeps its ledger and keeps publishing a row — frozen at
    whatever the last tick that still tracked it discovered, and indistinguishable from a
    live one. Checked in this direction only: the opposite (a fleet entry with no ledger)
    is written by `discover_forks.main()` on the same pass that reads the list, and it
    records a per-fork `error` rather than skipping the file, so it cannot go missing
    quietly the way a leftover can persist quietly.
    """
    fleet = fleet_tools(root)
    if not fleet:                      # unreadable list — config_status already says so
        return []
    return sorted(t for t in tools if t not in fleet)


def check(root: Path | None = None) -> dict:
    """Full configuration verdict for one tick.

    Returns a stamp dict; `fatal` is the list of reasons the tick must not publish
    (empty when the override is set, which is then recorded in the stamp).
    """
    statuses = {n: config_status(n, root) for n in WATCHED}
    fleet = statuses[FLEET_FILE]
    override = os.environ.get(OVERRIDE_ENV) in ("1", "true", "yes")
    fatal: list[str] = []
    if fleet["state"] in FATAL_STATES:
        fatal.append(
            f"{FLEET_FILE} is {fleet['state']}: the fleet list this tick read is not the "
            f"one committed at {fleet['commit'] or 'HEAD'}"
            + (" — " + "; ".join(fleet["detail"]) if fleet["detail"] else ""))
    return {"commit": fleet["commit"], "state": fleet["state"],
            "entries": fleet["entries"], "detail": fleet["detail"],
            "files": statuses, "override": override and bool(fatal),
            "fatal": [] if override else fatal}


def stamp_line(st: dict) -> str:
    """The configuration provenance row for the daily report (vibeic/vibeic-eda#10).

    vibeic/vibeic-eda#7 made the report name the ASSESSOR; this names the CONFIGURATION,
    for the same reason: two reports of one fleet, produced from two fleet lists, are
    otherwise indistinguishable.
    """
    sha = (st.get("commit") or "")[:12]
    n = st.get("entries")
    who = f"`{FLEET_FILE}`" + (f" ({n} forks)" if n is not None else "")
    state = st.get("state")
    if state == "committed":
        return f"Fleet list {who} @ `{sha or 'unknown'}` — committed."
    tail = (" ".join(st.get("detail") or []))[:400]
    if state == "formatting":
        return (f"Fleet list {who} — **UNCOMMITTED (formatting only)** vs "
                f"`{sha or 'unknown'}`. {tail}")
    if state == "unversioned":
        return (f"Fleet list {who} — **UNVERSIONED**: nothing vouches for the "
                f"configuration this report was produced from ({tail}).")
    over = " Published under " + OVERRIDE_ENV + "." if st.get("override") else ""
    return (f"Fleet list {who} — **UNCOMMITTED** vs `{sha or 'unknown'}`: {tail}{over}")
