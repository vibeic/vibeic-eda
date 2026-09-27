"""Q2 for an OVERLAY-installed tool counts what the overlay ships, not pin..tip.

Measured 2026-09-27 on librelane: the image keeps the base image's LibreLane and
overlays an explicit list of fork commits (the Dockerfile's `for c in <sha>...`
loop). `LIBRELANE_REF` moves to the fork tip on every release, so `pin..tip`
reads 0 right after it -- while 2678800 (#16), 2f32449 (#17), ea9d541 (#23)
and 81fb28d (#24) were on fork main and in NO overlay. The report called them
shipped. These tests drive the real `analyse()` over real git repositories.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

import build_page as PAGE
import fork_gap_report as F
import overlay_ship as O
import test_fork_github_convergence_ll as GH_FIX
from test_page_publication_current_round import (
    _load_strict, _page_state, _strict_fixture)

HERE = Path(__file__).resolve().parent


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, files: dict, msg: str) -> str:
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        _git(repo, "add", name)
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _dockerfile(pin: str, listed: list, overlay: bool = True) -> str:
    head = ("FROM base:1 AS demo-src\n"
            f"ARG DEMO_REF={pin}\n"
            "RUN git clone https://example.invalid/demo.git /src; "
            "cd /src; git checkout ${DEMO_REF}\n\n"
            "FROM base:1\n")
    if not overlay:
        return head + ("RUN --mount=type=bind,from=demo-src,source=/src,"
                       "target=/tmp/demo,ro \\\n    cp -r /tmp/demo/lib /opt/\n")
    loop = " \\\n             ".join(listed)
    return head + (
        "RUN --mount=type=bind,from=demo-src,source=/src,target=/tmp/demo,ro \\\n"
        "    set -e; \\\n"
        "    install -m 0755 -o root -g root /vibeic/x.sh /usr/bin/x.sh; \\\n"
        "    cd /tmp/demo; \\\n"
        "    install -m 0644 lib/w.py /usr/lib/demo/lib/w.py; \\\n"
        f"    for c in {loop}; do \\\n"
        "        git show --format= $c -- lib/ > /tmp/d-$c.diff; \\\n"
        "        patch -p1 -d /usr/lib/demo < /tmp/d-$c.diff; \\\n"
        "    done\n")


@pytest.fixture
def fleet(tmp_path):
    """A fork whose main carries seven commits of ours, three of them shipped
    three different ways, and a pin that the release already moved to the tip."""
    clone = tmp_path / "forks" / "demo"
    _init(clone)
    base = _commit(clone, {"lib/a.py": "a0\n", "lib/w.py": "w0\n",
                           "lib/b.py": "b0\n", "lib/c.py": "c0\n"}, "upstream")
    _git(clone, "update-ref", "refs/remotes/upstream/main", base)
    listed = _commit(clone, {"lib/a.py": "a1\n"}, "listed: a")          # listed
    unlisted = _commit(clone, {"lib/b.py": "b1\n"}, "not listed: b")    # counted
    tests_only = _commit(clone, {"test/t.py": "t\n"}, "tests only")    # scope
    whole = _commit(clone, {"lib/w.py": "w1\n"}, "whole file w")       # whole
    held = _commit(clone, {"lib/c.py": "c1\n"}, "held: c")             # held
    # a PR branch squashed into ONE listed commit, then the branch merged too
    fork_point = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "-q", "-b", "pr5")
    b1 = _commit(clone, {"lib/s.py": "s1\n"}, "pr5 part 1")
    b2 = _commit(clone, {"lib/s.py": "s2\n", "test/s.py": "t\n"}, "pr5 part 2")
    _git(clone, "checkout", "-q", "main")
    squash = _commit(clone, {"lib/s.py": "s2\n"}, "pr5 squashed (#5)")  # listed
    _git(clone, "merge", "-q", "--no-ff", "-s", "ours", "-m", "consolidate pr5",
         "pr5")
    tip = _git(clone, "rev-parse", "HEAD")
    _git(clone, "update-ref", "refs/remotes/origin/main", tip)
    assert fork_point

    repo = tmp_path / "eda"
    _init(repo)
    (repo / "Dockerfile").write_text(_dockerfile(tip, [listed, squash]))
    _git(repo, "add", "Dockerfile")
    _git(repo, "commit", "-qm", "d")
    _git(repo, "update-ref", "refs/remotes/origin/main",
         _git(repo, "rev-parse", "HEAD"))

    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "demo.json").write_text(json.dumps({
        "tool": "demo", "integrated": True, "ahead": 9,
        "vibeic_branch": "main", "upstream_default_branch": "main",
        "pinned_ref_full": tip}))
    return {"tmp": tmp_path, "repo": repo, "forks": tmp_path / "forks",
            "clone": clone, "ledger": ledger, "tip": tip,
            "sha": {"listed": listed, "unlisted": unlisted,
                    "tests_only": tests_only, "whole": whole, "held": held,
                    "b1": b1, "b2": b2, "squash": squash}}


def _forks_json(fleet, held: list) -> Path:
    p = fleet["tmp"] / "FORKS.json"
    p.write_text(json.dumps({"forks": [
        {"tool": "demo", "upstream": "them/demo", "held": held}]}))
    return p


def _analyse(fleet, held=None, state="open"):
    fj = _forks_json(fleet, held or [])
    return F.analyse(fleet["repo"], fleet["forks"], fleet["ledger"], False,
                     forks_json=fj, tracker_state=lambda ref: state)


def _short(*shas):
    return [s[:12] for s in shas]


def test_the_overlay_decides_what_shipped_not_the_pin(fleet):
    rep = _analyse(fleet)
    row = rep["rows"][0]
    s = fleet["sha"]
    assert row["ship_model"] == "overlay"
    assert row["release_lag"] == 0          # the pin IS the fork tip
    assert [c["sha"] for c in row["unshipped_commits"]] == _short(
        s["unlisted"], s["held"])
    assert row["ours_unshipped_substantive"] == 2
    assert rep["q2_ours_past_the_pin_substantive"] == 2
    ov = row["overlay"]
    assert ov["listed"] == 2
    assert ov["shipped_how"] == {
        s["listed"][:12]: "listed", s["squash"][:12]: "listed",
        s["whole"][:12]: "whole file at the pin",
        s["b1"][:12]: f"squash listed as {s['squash'][:12]}",
        s["b2"][:12]: f"squash listed as {s['squash'][:12]}"}
    assert [c["sha"] for c in ov["out_of_scope"]] == _short(s["tests_only"])


def test_negative_control_the_pin_model_reads_zero_on_the_same_fork(fleet):
    """Same fork, same pin, the Dockerfile builds the pinned tree: 0 IS right
    there, and it is exactly the number the overlay tool was being given."""
    (fleet["repo"] / "Dockerfile").write_text(
        _dockerfile(fleet["tip"], [], overlay=False))
    _git(fleet["repo"], "commit", "-qam", "no overlay")
    _git(fleet["repo"], "update-ref", "refs/remotes/origin/main",
         _git(fleet["repo"], "rev-parse", "HEAD"))
    rep = _analyse(fleet)
    assert rep["rows"][0]["ship_model"] == "pin"
    assert rep["q2_ours_past_the_pin_substantive"] == 0


def test_a_new_fork_commit_is_unshipped_until_it_is_listed(fleet):
    clone, s = fleet["clone"], fleet["sha"]
    new = _commit(clone, {"lib/a.py": "a2\n"}, "new fix")
    _git(clone, "update-ref", "refs/remotes/origin/main", new)
    (fleet["repo"] / "Dockerfile").write_text(
        _dockerfile(new, [s["listed"], s["squash"]]))
    _git(fleet["repo"], "commit", "-qam", "pin moves to the tip")
    _git(fleet["repo"], "update-ref", "refs/remotes/origin/main",
         _git(fleet["repo"], "rev-parse", "HEAD"))
    rep = _analyse(fleet)
    assert new[:12] in [c["sha"] for c in rep["q2_unshipped_commits"]]

    (fleet["repo"] / "Dockerfile").write_text(
        _dockerfile(new, [s["listed"], s["squash"], new]))
    _git(fleet["repo"], "commit", "-qam", "list it")
    _git(fleet["repo"], "update-ref", "refs/remotes/origin/main",
         _git(fleet["repo"], "rev-parse", "HEAD"))
    rep = _analyse(fleet)
    assert new[:12] not in [c["sha"] for c in rep["q2_unshipped_commits"]]


def test_a_cherry_pick_of_a_listed_commit_is_shipped_by_patch_id(fleet):
    clone, s = fleet["clone"], fleet["sha"]
    _git(clone, "checkout", "-q", "-b", "side", s["unlisted"] + "^")
    _git(clone, "cherry-pick", "-x", s["unlisted"])
    picked = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "-q", "main")
    (fleet["repo"] / "Dockerfile").write_text(
        _dockerfile(fleet["tip"], [s["listed"], s["squash"], picked]))
    _git(fleet["repo"], "commit", "-qam", "list the pick")
    _git(fleet["repo"], "update-ref", "refs/remotes/origin/main",
         _git(fleet["repo"], "rev-parse", "HEAD"))
    rep = _analyse(fleet)
    row = rep["rows"][0]
    assert s["unlisted"][:12] not in [c["sha"] for c in row["unshipped_commits"]]
    assert row["overlay"]["shipped_how"][s["unlisted"][:12]] == \
        f"patch-id of {picked[:12]}"


def _held(fleet, **kw):
    d = {"commit": fleet["sha"]["held"], "reason": "known DRC defect",
         "tracked_by": "vibeic/demo#7"}
    d.update(kw)
    return [d]


def test_a_held_commit_with_an_open_tracker_is_listed_not_counted(fleet):
    rep = _analyse(fleet, _held(fleet), "open")
    assert rep["q2_ours_past_the_pin_substantive"] == 1
    assert [c["sha"] for c in rep["q2_held_commits"]] == _short(
        fleet["sha"]["held"])
    h = rep["q2_held_commits"][0]
    assert (h["reason"], h["tracked_by"], h["tracker_state"], h["tool"]) == (
        "known DRC defect", "vibeic/demo#7", "open", "demo")
    assert rep["rows"][0]["held_commits"] == [
        {k: v for k, v in h.items() if k != "tool"}]


@pytest.mark.parametrize("state,needle", [
    ("closed", "is closed -- counted"),
    (None, "is UNREADABLE -- counted"),
])
def test_a_held_commit_counts_again_once_its_tracker_is_not_open(
        fleet, state, needle):
    rep = _analyse(fleet, _held(fleet), state)
    assert rep["q2_ours_past_the_pin_substantive"] == 2
    assert rep["q2_held_commits"] == []
    assert any(needle in n for n in rep["q2_held_notes"])


@pytest.mark.parametrize("patch", [
    {"reason": ""}, {"tracked_by": "see chat"}, {"tracked_by": None}])
def test_a_held_declaration_without_reason_or_tracker_holds_nothing(
        fleet, patch):
    rep = _analyse(fleet, _held(fleet, **patch), "open")
    assert rep["q2_ours_past_the_pin_substantive"] == 2
    assert any("needs commit, reason and tracked_by" in n
               for n in rep["q2_held_notes"])


def test_holding_a_shipped_commit_is_named_not_silently_accepted(fleet):
    rep = _analyse(fleet, _held(fleet, commit=fleet["sha"]["listed"]), "open")
    assert rep["q2_ours_past_the_pin_substantive"] == 2
    assert any("not an unshipped commit of ours" in n
               for n in rep["q2_held_notes"])


def test_held_works_on_the_pin_model_too(fleet):
    """A held declaration is about a commit, not about how a tool is built."""
    s = fleet["sha"]
    (fleet["repo"] / "Dockerfile").write_text(
        _dockerfile(s["unlisted"], [], overlay=False))
    _git(fleet["repo"], "commit", "-qam", "pin model, pin behind")
    _git(fleet["repo"], "update-ref", "refs/remotes/origin/main",
         _git(fleet["repo"], "rev-parse", "HEAD"))
    open_ = _analyse(fleet, _held(fleet), "open")
    closed = _analyse(fleet, _held(fleet), "closed")
    assert (closed["q2_ours_past_the_pin_substantive"]
            - open_["q2_ours_past_the_pin_substantive"]) == 1
    assert [c["sha"] for c in open_["q2_held_commits"]] == _short(s["held"])


def test_the_real_dockerfile_overlay_is_parsed_whole():
    """The parser must see every sha in the librelane loop, or a listed commit
    would read as unshipped (and an unlisted one could never be told apart)."""
    text = (HERE.parent / "Dockerfile").read_text(encoding="utf-8")
    ov = O.parse_overlays(text).get("LIBRELANE")
    assert ov is not None, "librelane overlay not found in the Dockerfile"
    loop = re.search(r"for c in (.*?); do", text.replace("\\\n", " "), re.S)
    assert ov.commits == re.findall(r"[0-9a-f]{40}", loop.group(1))
    assert ov.scope == ["librelane/"]
    assert "librelane/steps/netgen.py" in ov.whole_files
    assert all(p.startswith("librelane/") for p in ov.whole_files)


def test_a_tool_built_at_its_pin_has_no_overlay():
    text = (HERE.parent / "Dockerfile").read_text(encoding="utf-8")
    ovs = O.parse_overlays(text)
    assert O.overlay_for(ovs, "OpenROAD") is None
    assert O.overlay_for(ovs, "librelane") is ovs["LIBRELANE"]


def test_forks_json_held_entries_are_well_formed():
    """Every held declaration in the real FORKS.json names a commit, a reason
    and a tracker; the tracker being OPEN is checked at measurement time."""
    for tool, decls in O.load_held(HERE / "FORKS.json").items():
        for d in decls:
            assert re.fullmatch(r"[0-9a-f]{7,40}", d.get("commit") or ""), (tool, d)
            assert (d.get("reason") or "").strip(), (tool, d)
            assert re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", d.get("tracked_by") or ""), (tool, d)


def test_the_page_refuses_a_ledger_whose_held_list_differs(
        tmp_path, monkeypatch):
    paths = _strict_fixture(tmp_path)
    evidence = _load_strict(paths)
    state = _page_state(tmp_path, monkeypatch)
    changed = state / "ledger" / f"{GH_FIX.REGISTERED_FORKS[0]}.json"
    row = json.loads(changed.read_text(encoding="utf-8"))
    row["held_commits"] = [{"sha": "0" * 12, "subject": "x", "reason": "r",
                            "tracked_by": "vibeic/x#1",
                            "tracker_state": "open"}]
    changed.write_text(json.dumps(row), encoding="utf-8")
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")
    with pytest.raises(PAGE.PublicationEvidenceError,
                       match="held_commits differs"):
        PAGE.build(out, publication=evidence)
    assert out.read_text(encoding="utf-8") == "LAST GOOD"


def test_both_producers_run_the_one_derivation():
    """discover_forks (the ledger the page renders) and fork_gap_report (the
    certificate) must not drift apart: both call overlay_ship.classify, and
    neither keeps a private `pin..tip` count."""
    for name in ("discover_forks.py", "fork_gap_report.py"):
        src = (HERE / name).read_text(encoding="utf-8")
        assert "overlay_ship.classify(" in src, name
        assert not re.search(r'rev-list"?,\s*f"\{_?pin', src), name


def test_the_page_shows_a_held_commit_on_its_row(tmp_path):
    """HELD is visible on the page, not only in a JSON nobody opens."""
    import test_page_lists_recipe_patches as R
    held = [{"sha": "2f324495ed8a", "merge": False, "author": "a",
             "date": "2026-09-27",
             "subject": "FillInsertion: an optional FILL_INSERTION_AREA",
             "reason": "DRC/LVS defect when the area is set",
             "tracked_by": "vibeic/librelane#26", "tracker_state": "open"}]
    rows = R._rendered_rows(tmp_path, [R._row("librelane", held_commits=held),
                                       R._row("plain")])
    ll = rows["librelane"]
    for needle in ("HELD 1", "HELD out of the image (1)", "2f324495ed8a",
                   "DRC/LVS defect when the area is set",
                   "https://github.com/vibeic/librelane/issues/26",
                   "vibeic/librelane#26", "(open)"):
        assert needle in ll, needle
    assert "HELD" not in rows["plain"]
