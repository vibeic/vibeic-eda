#!/usr/bin/env python3
"""gk_state.py — WHERE the runtime state lives, WHO may write it, and WHO wrote each entry.

vibeic/vibeic-eda#12. Five modules each carried their own copy of

    STATE = Path(os.environ.get("GK_STATE_DIR") or os.path.expanduser("~/.cache/eda-fork-gatekeeper"))

so there was one production cache, one production ledger directory and one production
reports directory, and *any* process that imported one of those modules wrote them. On
2026-07-28 the cron tick ran 05:30:01→05:32:30 and `assessment-cache/magic.json` gained an
entry stamped 07:07:21 written by a non-cron process at 1b36787. Nothing in the file said
so; it was inferable only from the mtime and from which key SHAPE the entry used.

That input is now load-bearing. #4/#11 made a cache key a claim about which judge answered
which question; #10 made the tick refuse to publish on a fleet list no commit contains;
#7/#9 made it refuse to publish documents that disagree. None of those guards look at the
provenance of a cache entry — a poisoned entry does not make the documents disagree, it
makes them agree on the wrong thing.

WHY AN OPT-IN TO WRITE, RATHER THAN A CHECKOUT-DERIVED DEFAULT
--------------------------------------------------------------
The alternative on the table was to derive the default state directory from the checkout
instead of from `$HOME`. Three measurements on this repository argue against it:

  * THE CRON RUNS FROM THE SHARED CHECKOUT. crontab line: `30 5 * * *
    /home/reyerchu/vibeic-eda/fork-gatekeeper/run_tick.sh`, and `run_tick.sh` cd's to its
    own directory. That is the same tree a human opens to run something by hand — so a
    checkout-derived default would hand the cron and the most likely offender the SAME
    directory. It isolates worktrees, which were not the process that wrote the 07:07
    entry, and leaves the shared checkout exactly as exposed as before. "Production
    runner" is not a property of a path; it has to be declared.

  * IT WOULD TURN A WRITE HAZARD INTO A READ HAZARD. `gatekeeper.py --verify`,
    `build_page.py` and a by-hand `assess_release.py <tool>` all READ this state and are
    correct to. Moving the default moves their reads too: `--verify` would report "no
    report" for a day that published fine, and `build_page.py` would regenerate the public
    monitor page from an empty directory.

  * IT WOULD SPEND API BUDGET. A by-hand assessment today READS the production cache and
    replays it for free. Point the default somewhere else and every such run misses the
    cache and buys a real judge call — a hygiene fix with an invalidation bill attached,
    which vibeic/vibeic-eda#11 is explicit that a hygiene fix must not have.

So: READS are untouched — everyone still reads the production state, and no cache entry is
invalidated by this change. WRITES to the shared production locations require the process
to say it is the production runner (`GK_PRODUCTION_WRITER=1`, exported by `run_tick.sh`).
This is NOT "read-only for everyone": the production runner writes exactly as before, and
anything else may write it too — it just has to ask, and the entry then records that it
did. A run that does not want to ask points `GK_STATE_DIR` at a scratch directory and owns
its own state, at which point none of this applies.

FAIL-CLOSED. A path whose relation to the production locations cannot be determined is
treated as production, i.e. refused. The failure mode of guessing "not production" is a
silently poisoned cache, which is the defect; the failure mode of guessing "production" is
a loud refusal with a one-line remedy.
"""
from __future__ import annotations

import datetime
import functools
import os
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

#: Overrides WHERE state lives. Unchanged by #12 — the cron still exports it, and pointing
#: it at a scratch directory is still the way to run against your own state.
STATE_ENV = "GK_STATE_DIR"
#: The env var by which a process DECLARES it is the production runner. Absence is not an
#: accusation — it is the ordinary case, and the ordinary case must not write.
WRITER_ENV = "GK_PRODUCTION_WRITER"
_TRUE = ("1", "true", "yes")

#: The key every state file carries its provenance under.
PROVENANCE_KEY = "written_by"

#: The one shared production state directory — the built-in default, i.e. what a process
#: that sets nothing resolves to.
PRODUCTION_STATE = "~/.cache/eda-fork-gatekeeper"
#: The published monitor page. Not part of the state directory, but the same exposure:
#: it is one shared production artefact any checkout could overwrite. `build_page` takes
#: its default from here so the path exists as ONE literal.
PRODUCTION_PAGE = "/home/reyerchu/vibeic.ai/eda-forks.html"


class ProductionStateWriteRefused(RuntimeError):
    """A process that did not declare itself the production runner tried to write the
    shared production state the daily cron reads."""


def state_dir() -> Path:
    """WHERE state lives for this process. Resolution is exactly what it was before #12."""
    return Path(os.environ.get(STATE_ENV) or os.path.expanduser(PRODUCTION_STATE))


def production_state_dir() -> Path:
    """The shared production directory, regardless of what THIS process resolved to."""
    return Path(os.path.expanduser(PRODUCTION_STATE))


def _real(p) -> str:
    try:
        return os.path.realpath(str(p))
    except OSError:
        return str(p)


def is_production_path(p) -> bool:
    """Is `p` one of the shared production locations, or inside one?

    Compares RESOLVED paths, so `~/.cache/…`, `$HOME/.cache/…` and a symlinked equivalent
    are one location — an env var spelled a different way must not buy a different answer.
    Fails CLOSED (see the module docstring).
    """
    try:
        target = _real(p)
        for root in (production_state_dir(), Path(PRODUCTION_PAGE)):
            r = _real(root)
            if target == r or target.startswith(r.rstrip(os.sep) + os.sep):
                return True
        return False
    except Exception:  # noqa: BLE001 — undeterminable means production, means refused
        return True


def declared_production_writer() -> bool:
    return (os.environ.get(WRITER_ENV) or "").strip().lower() in _TRUE


def may_write(p) -> bool:
    """May THIS process write `p`? Anything outside the shared production locations, yes.
    A production location only if this process declared itself the production runner."""
    return declared_production_writer() or not is_production_path(p)


#: How to run this WITHOUT touching production. Per-artefact, because the state directory
#: and the published page do not move by the same lever, and a refusal that names a
#: remedy which does not apply teaches the reader to skip to the override.
ELSEWHERE = f"Point {STATE_ENV} at a scratch directory to run against your own state"


def refusal_reason(p, what: str, remedy: str = ELSEWHERE) -> str:
    return (f"refusing to write {what} at {p}: that is the SHARED PRODUCTION state the "
            f"05:30 cron reads, and this process did not declare itself the production "
            f"runner (vibeic/vibeic-eda#12). {remedy}, or set {WRITER_ENV}=1 to write the "
            f"shared one on purpose — the entry then records that you did.")


def require_writable(p, what: str = "shared production state",
                     remedy: str = ELSEWHERE) -> None:
    """Raise unless this process may write `p`. The message names the remedy, because a
    guard whose message does not is a guard that gets worked around with an env var
    someone guessed."""
    if not may_write(p):
        raise ProductionStateWriteRefused(refusal_reason(p, what, remedy))


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@functools.lru_cache(maxsize=1)
def _checkout_facts() -> tuple[str | None, bool | None]:
    """(short commit, dirty) of the checkout this code was loaded from.

    Memoised: `discover_forks` writes one ledger per fork, and shelling out to git per file
    would put a dozen subprocesses in the hot path of a tick to answer a question whose
    answer cannot change while the process runs.
    """
    def _git(*args) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(HERE), *args],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    head = _git("rev-parse", "--short=12", "HEAD")
    status = _git("status", "--porcelain")
    return head, (bool(status) if status is not None else None)


def provenance(**extra) -> dict:
    """WHO wrote this entry: which checkout, which commit, and whether it claimed to be
    the production runner.

    The 07:07 entry of 2026-07-28 carried an assessor id — which says which JUDGE answered
    — and nothing at all about which process stored it. That it was not the cron was
    reconstructed from an mtime and from the key shape the entry used; neither survives a
    file being copied, and neither is something a program can check. This block is what
    makes such an entry say so itself.

    Never raises, and an undeterminable field is null rather than absent: a reader must be
    able to tell "we could not find out" from "this shape predates the question".
    """
    commit, dirty = _checkout_facts()
    return {"at": _now_iso(),
            # The one field a guard could act on: did this process ASK to write production?
            "production": declared_production_writer(),
            "entrypoint": os.path.basename(sys.argv[0] or "") or None,
            "checkout": str(HERE),
            "commit": commit,
            "dirty": dirty,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            **extra}


def describe(prov) -> str:
    """One line naming who wrote an entry — including for entries written before this
    existed, which must read as "unknown", never as "the cron"."""
    if not isinstance(prov, dict):
        return "provenance unknown (written before entries recorded who wrote them)"
    who = "the production runner" if prov.get("production") else "a NON-production process"
    return (f"{who} · {prov.get('entrypoint') or '?'} @ {prov.get('commit') or '?'}"
            f"{'+dirty' if prov.get('dirty') else ''} · {prov.get('at') or '?'}")


def strip_provenance(obj):
    """The provenance block, removed — for the PUBLISH boundary.

    `build_page` embeds whole ledger dicts and the whole latest report into the public
    monitor page, so provenance (which names a local checkout path and a hostname) must
    come off there. Same reasoning as the NDA redaction that already guards that boundary:
    what an internal file records and what a public page carries are two questions.
    """
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k != PROVENANCE_KEY}
    return obj
