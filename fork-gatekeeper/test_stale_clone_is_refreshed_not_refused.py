"""vibeic-eda#54 — a clone that was never fetched refused forever.

`_local_compare` refuses to answer from a clone that does not hold the CURRENT
upstream head, and that refusal is right: a stale clone reports fewer commits
behind than there are, and every consumer reads small numbers as health.

But nothing in `discover_forks` ever fetched. Only `prepare_merge_pr` and
`daily_merge` do, so a tool that never goes through a merge PR was stale
permanently and the refusal was permanent with it.

MEASURED on Trilinos, a mirror (`fork=false, parent=null`) that therefore never
reaches the merge path:

    clone could not answer          -> _local_compare None
    cross-repo API compare          -> cannot resolve for a mirror
    row printed                     -> behind_commits = null, "behind 1257 [SAMPLED]"

    with the branch fetched, same clone, seconds:
    git rev-list --count 5edda67161cc..upstream/master   ->  0

Our pin is LEVEL with upstream master (763aa751877), carrying 407 commits of
restored packages on top. The page was reporting an unmeasurable gap on a fork
that has no gap.
"""
from __future__ import annotations

import importlib.util
import sys
import types

from pathlib import Path as _Path

# RESOLVED AGAINST THIS FILE, not the working directory. As a bare
# "discover_forks.py" this collected only when pytest was invoked FROM this
# directory; from anywhere else it raised FileNotFoundError during collection and
# took the whole run to rc=2 — "could not even collect", which is not a test
# result at all. It went unnoticed because nothing ever ran the suite from
# anywhere else. Wiring it into the 05:30 round is what surfaced it.
_spec = importlib.util.spec_from_file_location(
    "df_s", str(_Path(__file__).resolve().parent / "discover_forks.py"))
df = importlib.util.module_from_spec(_spec)
sys.modules["df_s"] = df
_spec.loader.exec_module(df)


class _G(types.SimpleNamespace):
    pass


def _drive(monkeypatch, fetch_ok, holds_after_fetch, calls):
    """Clone that lacks the upstream head until a fetch supposedly succeeds.

    The fake shas are HEX. The first version of this fixture used `"m" * 40` for
    the merge-base, which `_local_compare` rejects with its own
    `re.fullmatch(r"[0-9a-f]{40}")` guard — so the accept case failed against
    correct code and the probe, not the fix, was what was broken."""
    monkeypatch.setattr(df, "_clone_for", lambda _t: "/nonexistent/clone")
    monkeypatch.setattr(df, "_ls_remote_head", lambda *_a, **_k: "a" * 40)

    state = {"holds": False}

    def fake_peel(_repo, revs):
        if not state["holds"]:
            return {"b" * 40: "b" * 40}          # our head only; upstream missing
        return {r: r for r in revs}

    def fake_git(_repo, *args, **kw):
        calls.append(args)
        if args and args[0] == "fetch":
            if fetch_ok:
                state["holds"] = holds_after_fetch
            return _G(ok=fetch_ok, out="")
        if args and args[0] == "merge-base":
            return _G(ok=True, out="c" * 40)
        if args and args[0] == "rev-list":
            return _G(ok=True, out="0 407")
        return _G(ok=True, out="")

    monkeypatch.setattr(df, "_peel", fake_peel)
    monkeypatch.setattr(df, "_git", fake_git)
    return df._local_compare("Trilinos", "trilinos/Trilinos", "master", "b" * 40)


def test_a_stale_clone_is_fetched_and_then_answers(monkeypatch):
    """THE DEFECT. Before this, the first `_peel` miss returned None and the row
    printed `behind_commits = null` on a fork with no gap."""
    calls: list = []
    got = _drive(monkeypatch, fetch_ok=True, holds_after_fetch=True, calls=calls)
    assert got is not None, "a fetchable clone still refused to answer"
    assert got["behind_by"] == 0 and got["ahead_by"] == 407, got
    fetches = [c for c in calls if c and c[0] == "fetch"]
    assert len(fetches) == 1, calls
    assert "https://github.com/trilinos/Trilinos.git" in fetches[0], fetches[0]
    assert "master" in fetches[0], fetches[0]


def test_a_FAILED_fetch_is_still_a_refusal_not_a_zero(monkeypatch):
    """LOAD-BEARING, and the whole reason the refusal existed. Turning a clone
    that could not be refreshed into an answer of 0 would print perfect health
    for a fork nobody measured — the exact shape the original refusal was
    written to prevent."""
    calls: list = []
    assert _drive(monkeypatch, fetch_ok=False, holds_after_fetch=False,
                  calls=calls) is None


def test_a_fetch_that_did_not_bring_the_head_is_also_a_refusal(monkeypatch):
    """A fetch can succeed and still not deliver the commit — a branch renamed
    upstream, a partial clone filter. Success of the command is not possession
    of the object, and only possession may be treated as an answer."""
    calls: list = []
    assert _drive(monkeypatch, fetch_ok=True, holds_after_fetch=False,
                  calls=calls) is None


def test_the_clone_is_fetched_at_most_once(monkeypatch):
    """A retry loop here would multiply a slow network by 36 forks on every
    tick."""
    calls: list = []
    _drive(monkeypatch, fetch_ok=True, holds_after_fetch=False, calls=calls)
    assert len([c for c in calls if c and c[0] == "fetch"]) == 1, calls
