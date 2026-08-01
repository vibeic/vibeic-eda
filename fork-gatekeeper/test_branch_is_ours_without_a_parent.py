"""`branch_is_ours` could not answer for 8 of the 28 pinned repos.

It asked GitHub for `.parent.full_name` and returned None when that was empty —
and `.parent` is empty for any repo in the org that was not created BY forking:
one pushed up directly, or deleted and recreated, carries no fork relationship.

MEASURED over the pinned set:

    28 pins, 8 with no GitHub .parent
      FasterCap, Fault, Geometry, LinAlgebra,
      OpenROAD-flow-scripts, Trilinos, cadical, kissat

and every one of them DECLARES its upstream in `FORKS.json` — the same
declaration `daily_merge` and `discover_forks` already read.

WHAT THAT COST. `branch_is_ours` returning None is read by `daily_release` as
STALE_UNDECIDED and the pin is left alone: correct as a fail-safe, permanent as a
state. `fault chain --skip-boundary` landed in the fork and the release refused
to ship it, reporting only "whether it carries our commits could not be
determined". An authority we own, unused, turning into a silent block that no
amount of re-running clears.

THE COMPARE ENDPOINT CANNOT BE THE ANSWER EITHER: it needs the two repos in one
network, so naming the declared upstream still 404s. And the existing
"upstream has no such branch" test cannot decide, because upstream does have a
`main`. What needs no relationship is COMMIT PRESENCE: a branch tip the upstream
repository has never seen was authored here.

MEASURED both directions on Fault, against the live API:

    AUCOHL/Fault  commits/10613da…  -> 422 No commit found   (ours)
    AUCOHL/Fault  commits/0c90e3b…  -> 422 No commit found   (ours)
    AUCOHL/Fault  commits/cf5509f…  -> 200                   (upstream's own tip)
    vibeic/Fault  commits/cf5509f…  -> 200                   (we carry it too)

and over five real repos through the finished function:

    Fault    main    True      cadical  master  False
    Trilinos master  True      kissat   master  False
    yosys    main    True   (has .parent — the original path, unchanged)

The two Falses are the load-bearing half: #23/#25 found four tools whose pins
were about to be advanced onto pure upstream mirrors, smuggling a four-tool
version bump under "build 6 absent artefacts". A fallback that answered True for
everything would re-open exactly that.
"""
from __future__ import annotations

import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import check_pins_current as C  # noqa: E402


# ── the declaration we already own ──────────────────────────────────────────
def test_the_declared_upstream_is_read_from_forks_json(tmp_path):
    f = tmp_path / "FORKS.json"
    f.write_text(json.dumps({"forks": [
        {"tool": "Fault", "upstream": "AUCOHL/Fault"},
        {"tool": "yosys", "upstream": "YosysHQ/yosys"}]}))
    assert C.declared_upstream("Fault", f) == "AUCOHL/Fault"
    assert C.declared_upstream("fault", f) == "AUCOHL/Fault", "case-insensitive"
    assert C.declared_upstream("nope", f) is None


def test_a_declaration_that_is_not_a_repo_path_is_not_used(tmp_path):
    """A bare name cannot address a repository, and passing it on would produce
    a 404 that reads like "upstream has no such branch" — a wrong answer rather
    than no answer."""
    f = tmp_path / "FORKS.json"
    f.write_text(json.dumps({"forks": [{"tool": "x", "upstream": "somename"}]}))
    assert C.declared_upstream("x", f) is None


def test_the_real_declaration_resolves():
    """Pinned against the shipped FORKS.json: the eight parentless repos are
    only answerable because it declares them."""
    if not (_HERE / "FORKS.json").is_file():
        return
    assert C.declared_upstream("Fault") == "AUCOHL/Fault"


# ── the decision, driven through a scripted API ─────────────────────────────
def _api(monkeypatch, responses):
    """Drive `branch_is_ours` with a scripted `gh api`. Keyed on a substring of
    the path so the test states WHICH question it is answering."""
    calls = []

    def fake_sh(cmd, timeout=180):
        path = cmd[2] if len(cmd) > 2 else ""
        calls.append(path)
        for frag, (rc, out) in responses.items():
            if frag in path:
                return rc, out, ""
        return 1, "", "unscripted"

    monkeypatch.setattr(C, "_sh", fake_sh)
    return calls


_NO_PARENT = {"repos/vibeic/Fault": (0, "\n")}          # .parent -> empty


def test_no_parent_and_no_declaration_still_answers_nothing(monkeypatch, tmp_path):
    """The fail-safe survives: an undeclared, unparented repo is still None."""
    _api(monkeypatch, _NO_PARENT)
    monkeypatch.setattr(C, "declared_upstream", lambda *a, **k: None)
    assert C.branch_is_ours("Fault", "main") is None


def test_a_tip_upstream_has_never_seen_is_ours(monkeypatch):
    """THE #50 CASE. No `.parent`, the compare 404s, upstream HAS a `main` — and
    the branch tip is a commit upstream does not have."""
    _api(monkeypatch, {
        "repos/vibeic/Fault?": (0, "\n"),
        "repos/vibeic/Fault/compare": (1, ""),
        "repos/AUCOHL/Fault/branches/main": (0, "main"),
        "repos/vibeic/Fault/branches/main": (0, "main"),
        "repos/vibeic/Fault/commits/main": (0, "10613da\n"),
        "repos/AUCOHL/Fault/commits/10613da": (1, ""),
    })
    monkeypatch.setattr(C, "declared_upstream", lambda *a, **k: "AUCOHL/Fault")
    assert C.branch_is_ours("Fault", "main") is True


def test_a_tip_upstream_HAS_is_a_mirror_and_is_not_ours(monkeypatch):
    """LOAD-BEARING, and the whole reason this question exists. #23/#25 caught
    four pins about to be advanced onto pure upstream mirrors — a four-tool
    version bump smuggled under "build 6 absent artefacts". A fallback that
    answered True for everything re-opens it."""
    _api(monkeypatch, {
        "repos/vibeic/cadical?": (0, "\n"),
        "repos/vibeic/cadical/compare": (1, ""),
        "repos/arminbiere/cadical/branches/master": (0, "master"),
        "repos/vibeic/cadical/branches/master": (0, "master"),
        "repos/vibeic/cadical/commits/master": (0, "c607304\n"),
        "repos/arminbiere/cadical/commits/c607304": (0, "c607304"),
    })
    monkeypatch.setattr(C, "declared_upstream",
                        lambda *a, **k: "arminbiere/cadical")
    assert C.branch_is_ours("cadical", "master") is False


def test_a_tip_that_cannot_be_read_is_still_undecided(monkeypatch):
    """No tip, no answer. Guessing here would be guessing about what ships."""
    _api(monkeypatch, {
        "repos/vibeic/Fault?": (0, "\n"),
        "repos/vibeic/Fault/compare": (1, ""),
        "repos/AUCOHL/Fault/branches/main": (0, "main"),
        "repos/vibeic/Fault/branches/main": (0, "main"),
        "repos/vibeic/Fault/commits/main": (1, ""),
    })
    monkeypatch.setattr(C, "declared_upstream", lambda *a, **k: "AUCOHL/Fault")
    assert C.branch_is_ours("Fault", "main") is None


def test_a_parented_repo_still_takes_the_compare_path(monkeypatch):
    """THE ACCEPT CASE: 20 of the 28 have a `.parent` and must be unaffected —
    the compare answers and nothing below it runs."""
    calls = _api(monkeypatch, {
        "repos/vibeic/yosys?": (0, "YosysHQ/yosys\n"),
        "repos/vibeic/yosys/compare": (0, "34\n"),
    })
    assert C.branch_is_ours("yosys", "main") is True
    assert not any("/commits/" in c for c in calls), (
        "the commit probe ran on a repo whose compare already answered")


def test_a_branch_absent_upstream_is_still_conclusively_ours(monkeypatch):
    """The earlier fix stays reachable: our own integration branches exist
    nowhere else, and that answers the question rather than leaving it open."""
    calls = _api(monkeypatch, {
        "repos/vibeic/klayout?": (0, "KLayout/klayout\n"),
        "repos/vibeic/klayout/compare": (1, ""),
        "repos/KLayout/klayout/branches/vibeic-signoff": (1, ""),
        "repos/vibeic/klayout/branches/vibeic-signoff": (0, "vibeic-signoff"),
    })
    assert C.branch_is_ours("klayout", "vibeic-signoff") is True
    assert not any("/commits/" in c for c in calls)
