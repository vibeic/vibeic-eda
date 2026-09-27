"""vibeic-eda#253 -- a fix the image ships as a recipe step on a CONTENTS ASSERTION.

WHAT WAS MEASURED. vibeic/open_pdks#1/#2 (bccc9cd / merge 1e2380a: gf180mcuD
LibreLane LAYERS_RC in ohm/um) is in 0.3.80: `config.tcl` reads `Metal2 res
0.385861` there and `3.85861E-04` in 0.3.79. It got there through vibeic-eda#250,
a declared step in tools/pdk/local_mods.json that the build applies to the
prebuilt ciel volume. The page's open_pdks row listed carried patches only up to
the asserted `b344c97eacc2`, so the fix was nowhere on it.

WHY THE REF DOES NOT MOVE. `OPEN_PDKS_VOLUME_CONTENTS_SHA` states what the
volume was CUT FROM; advancing it rebuilds nothing and turns a true statement
false (#74, #78, #79). The truthful addition is the other half of what the image
ships: the manifest the build reconciles against that assertion.

WHAT THESE TESTS HOLD.
  * the derivation reads the link off the build (`--upstream-sha "${ARG}"` +
    `--manifest` + the COPY that put it there), not off a table;
  * the committed manifest attributes #250 to vibeic/open_pdks#2/#1, and #249
    to NO fork commit, with the reason;
  * `discover_one` puts the list on a contents-assertion row and on no other;
  * the rendered page lists each entry on that row, and a row without the field
    renders byte-for-byte as it did;
  * a certified page refuses a row whose list is not the one the certified tree
    derives.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import pin_kinds  # noqa: E402

NOW = "2026-09-27T09:00:00+08:00"


def _asserted_open_pdks() -> str:
    text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^ARG OPEN_PDKS_VOLUME_CONTENTS_SHA=([0-9a-f]{40})", text, re.M)
    assert m, "the composing Dockerfile no longer asserts the open_pdks volume"
    return m.group(1)


# ------------------------------------------------------------ the derivation


def test_the_committed_tree_attributes_the_layers_rc_fix_to_the_open_pdks_row():
    res = pin_kinds.recipe_patches(REPO, _asserted_open_pdks())
    assert res["error"] is None, res["error"]
    assert res["arg"] == "OPEN_PDKS_VOLUME_CONTENTS_SHA"
    by_id = {p["id"]: p for p in res["patches"]}
    manifest = json.loads((REPO / "tools/pdk/local_mods.json").read_text())
    # EVERY declared entry, not a hand-picked one: the build reconciles the whole
    # manifest against this assertion, so the row lists the whole manifest.
    assert list(by_id) == [m["id"] for m in manifest["modifications"]]

    rc = by_id["gf180mcu-librelane-layers-rc-ohm"]
    assert rc["recipe_pr"] == "vibeic/vibeic-eda#250"
    assert rc["paths"] == [
        "gf180mcu/versions/*/gf180mcuD/libs.tech/librelane/config.tcl"]
    assert [(c["pr"], c["sha"][:7]) for c in rc["fork_commits"]] == [
        ("vibeic/open_pdks#2", "1e2380a"), ("vibeic/open_pdks#1", "bccc9cd")]

    xy = by_id["gf180mcu-xyce-defines-the-5v-cell-devices"]
    assert xy["recipe_pr"] == "vibeic/vibeic-eda#249"
    assert xy["fork_commits"] == [], "#249 corresponds to no fork commit"
    assert xy["fork_note"] and "no commit or PR" in xy["fork_note"]
    for p in by_id.values():
        assert p["manifest"] == "tools/pdk/local_mods.json"


def test_a_sha_nothing_asserts_has_nothing_to_add():
    res = pin_kinds.recipe_patches(REPO, "0" * 40)
    assert res == {"arg": None, "patches": None, "error": None}


def _tree(tmp: Path, *, verify_arg: str = "VOL_VOLUME_CONTENTS_SHA",
          manifest: object | None = None, copy_line: bool = True) -> str:
    sha = "a" * 40
    (tmp / "tools" / "pdk").mkdir(parents=True, exist_ok=True)
    (tmp / "Dockerfile").write_text(
        f"ARG VOL_VOLUME_CONTENTS_SHA={sha}\n"
        f"ARG OTHER_VOLUME_CONTENTS_SHA={'b' * 40}\n"
        "FROM scratch\n"
        "ARG VOL_VOLUME_CONTENTS_SHA\nARG OTHER_VOLUME_CONTENTS_SHA\n"
        'RUN test -n "${VOL_VOLUME_CONTENTS_SHA}" && test -n "${OTHER_VOLUME_CONTENTS_SHA}"\n'
        + ("COPY tools/pdk/ /vibeic/pdk/\n" if copy_line else "")
        + "RUN set -e; \\\n    python3 /vibeic/pdk/pdk_local_mods.py verify \\\n"
        "        --manifest /vibeic/pdk/local_mods.json \\\n"
        f'        --upstream-sha "${{{verify_arg}}}"\n', encoding="utf-8")
    if manifest is None:
        manifest = {"modifications": [
            {"id": "one", "issue": "x", "what": "w", "paths": ["a/*.tcl"],
             "recipe_pr": "vibeic/vibeic-eda#9",
             "fork_commits": [{"pr": "vibeic/f#1", "sha": "c" * 40}]},
            {"id": "two", "issue": "x", "what": "w", "paths": ["b/c"]}]}
    (tmp / "tools" / "pdk" / "local_mods.json").write_text(
        json.dumps(manifest) if not isinstance(manifest, str) else manifest)
    return sha


def test_the_link_is_read_off_the_reconcile_step(tmp_path):
    sha = _tree(tmp_path)
    res = pin_kinds.recipe_patches(tmp_path, sha)
    assert res["error"] is None and res["arg"] == "VOL_VOLUME_CONTENTS_SHA"
    assert [p["id"] for p in res["patches"]] == ["one", "two"]
    assert res["patches"][0]["fork_commits"][0]["pr"] == "vibeic/f#1"
    assert res["patches"][1]["fork_commits"] is None, \
        "absent is 'not recorded', never 'none'"
    # The OTHER assertion in the same file has no manifest verified against it.
    assert pin_kinds.recipe_patches(tmp_path, "b" * 40)["patches"] is None


def test_a_manifest_verified_against_another_assertion_is_not_attributed(tmp_path):
    sha = _tree(tmp_path, verify_arg="OTHER_VOLUME_CONTENTS_SHA")
    assert pin_kinds.recipe_patches(tmp_path, sha)["patches"] is None
    assert [p["id"] for p in
            pin_kinds.recipe_patches(tmp_path, "b" * 40)["patches"]] == ["one", "two"]


@pytest.mark.parametrize("manifest,copy_line,needle", [
    ("{not json", True, "local_mods.json"),
    (None, False, "reaches the image by no COPY"),
    ({"modifications": [{"id": "e", "paths": ["p"], "fork_commits": []}]},
     True, "no 'fork_note'"),
    ({"modifications": [{"id": "e", "paths": ["p"],
                         "fork_commits": [{"pr": "open_pdks 2", "sha": "c" * 40}]}]},
     True, "owner/repo#N"),
])
def test_a_manifest_the_build_names_but_nobody_can_read_is_an_error_not_an_empty_list(
        tmp_path, manifest, copy_line, needle):
    sha = _tree(tmp_path, manifest=manifest, copy_line=copy_line)
    res = pin_kinds.recipe_patches(tmp_path, sha)
    assert res["patches"] is None
    assert res["error"] and needle in res["error"], res


# ------------------------------------------------------------ discover_one


def _stub_gh(monkeypatch, df):
    def fake(path):
        if path == "repos/vibeic/Tool":
            return {"created_at": "2026-01-01T00:00:00Z", "default_branch": "main"}
        if path == "repos/them/Tool":
            return {"default_branch": "main"}
        if "compare" in path:
            return {"ahead_by": 0, "behind_by": 0, "merge_base_commit": {},
                    "commits": []}
        return []
    monkeypatch.setattr(df, "gh", fake)


@pytest.mark.parametrize("kind", ["contents_assertion", "pin"])
def test_discover_one_puts_the_list_on_an_assertion_row_only(monkeypatch, kind):
    import discover_forks as df
    _stub_gh(monkeypatch, df)
    sha = _asserted_open_pdks()
    led = df.discover_one({"tool": "Tool", "upstream": "them/Tool", "role": "r"},
                          {"tool": {"ref": sha, "pin_kind": kind}}, "0.3.80")
    if kind == "contents_assertion":
        ids = [p["id"] for p in led["recipe_patches"]]
        assert "gf180mcu-librelane-layers-rc-ohm" in ids
        assert led["recipe_patches_error"] is None
        # The reference is NOT moved to reach the fix (#74/#78/#79).
        assert led["pinned_ref_full"] == sha
    else:
        assert "recipe_patches" not in led and "recipe_patches_error" not in led


# ------------------------------------------------------------ the rendered page


def _row(tool: str, **kw) -> dict:
    d = {"tool": tool, "role": "test", "upstream": f"up/{tool}",
         "upstream_url": f"https://github.com/up/{tool}",
         "fork_url": f"https://github.com/vibeic/{tool}",
         "image_version": "0.3.80", "generated_at": NOW,
         "pinned_ref": "0" * 12, "pinned_ref_full": "0" * 40,
         "pin_kind": "pin", "integrated": True, "ahead": 1,
         "behind_commits": 0, "behind_measured_at": NOW,
         "sync_lag": 0, "release_lag": 0, "lag_split_exact": True,
         "carried_patches": [{"sha": "1" * 12, "title": f"{tool} patch",
                              "date": "2026-09-01", "url": "#"}]}
    d.update(kw)
    return d


def _assertion_row(with_recipes: bool) -> dict:
    sha = _asserted_open_pdks()
    d = _row("open_pdks", pin_kind="contents_assertion",
             dockerfile_arg="OPEN_PDKS_VOLUME_CONTENTS_SHA",
             pinned_ref=sha[:12], pinned_ref_full=sha, ahead=12,
             behind_commits=None, behind_measured_at=None, sync_lag=None,
             release_lag=None, lag_split_exact=None)
    if with_recipes:
        res = pin_kinds.recipe_patches(REPO, sha)
        d["recipe_patches"], d["recipe_patches_error"] = res["patches"], res["error"]
    return d


def _rendered_rows(tmp: Path, rows: list[dict]) -> dict[str, str]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available; the renderer under test is the page's JavaScript")
    led = tmp / "state" / "ledger"
    led.mkdir(parents=True)
    for r in rows:
        (led / f"{r['tool']}.json").write_text(json.dumps(r), encoding="utf-8")
    (tmp / "nobin").mkdir()
    out = tmp / "page.html"
    env = dict(os.environ, PATH=str(tmp / "nobin"), GK_STATE_DIR=str(tmp / "state"),
               GK_INVENTORY_IMAGE="absent:test")
    r = subprocess.run([sys.executable, str(HERE / "build_page.py"), "--out", str(out)],
                       capture_output=True, text=True, timeout=300, env=env, cwd=str(HERE))
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run([node, str(HERE / "jstests" / "render_page.mjs"), str(out), NOW],
                       capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, RENDER_IDS="forkRows"))
    assert r.returncode == 0, r.stdout + r.stderr
    html = r.stdout.split("===== #forkRows =====\n", 1)[1]
    # One <tr class="trow"> + its detail <tr> per tool.
    per: dict[str, str] = {}
    for chunk in re.split(r'(?=<tr class="trow")', html):
        m = re.search(r'<td class="fork-tool">([^<]+)<', chunk)
        if m:
            # The row index `data-i`/`data-d` depends on sort order, not content.
            per[m.group(1)] = re.sub(r'data-[id]="\d+"', "", chunk)
    return per


def test_the_assertion_row_lists_each_recipe_patch(tmp_path):
    rows = _rendered_rows(tmp_path, [_assertion_row(True), _row("plain")])
    op = rows["open_pdks"]
    assert "Applied by the image build to this artefact (6)" in op
    assert "+ 6 applied by the build" in op
    for needle in ("gf180mcu-librelane-layers-rc-ohm",
                   "gf180mcu/versions/*/gf180mcuD/libs.tech/librelane/config.tcl",
                   "vibeic/open_pdks#2", "1e2380a8fbe5",
                   "vibeic/open_pdks#1", "bccc9cd1c088",
                   "https://github.com/vibeic/open_pdks/commit/bccc9cd1c0881a135b2a2ea71d4c64f00fd24624",
                   "https://github.com/vibeic/vibeic-eda/pull/250",
                   "gf180mcu-xyce-defines-the-5v-cell-devices",
                   "https://github.com/vibeic/vibeic-eda/pull/249",
                   "no commit or PR"):
        assert needle in op, needle
    # The asserted reference is still the one rendered: nothing moved it.
    assert _asserted_open_pdks() in op
    assert "Applied by the image build" not in rows["plain"]


def test_a_row_without_recipe_patches_renders_exactly_as_before(tmp_path):
    with_ = _rendered_rows(tmp_path / "a", [_assertion_row(True), _row("plain")])
    without = _rendered_rows(tmp_path / "b", [_assertion_row(False), _row("plain")])
    assert with_["plain"] == without["plain"]
    assert "Applied by the image build" not in without["open_pdks"]
    assert "applied by the build" not in without["open_pdks"]


def test_an_unreadable_manifest_is_stated_not_rendered_as_zero(tmp_path):
    d = _assertion_row(False)
    d["recipe_patches"], d["recipe_patches_error"] = None, "tools/pdk/x.json: boom"
    op = _rendered_rows(tmp_path, [d])["open_pdks"]
    assert "Could not be read: tools/pdk/x.json: boom" in op
    assert "That is not a list of zero." in op


# ------------------------------------------------------------ the certificate


def _certified_assertion_fixture(tmp_path, monkeypatch):
    import build_page as PAGE
    import test_page_certified_boundary as B
    import test_page_publication_current_round as CERT_FIX
    checkout = B._clean_checkout(tmp_path, monkeypatch)
    gap = B._normalized_special_gap("contents-assertion")
    sha = _asserted_open_pdks()
    gap["rows"][0]["asserted_contents"] = sha
    gap["assertions"] = [{"tool": gap["rows"][0]["tool"], "contents": sha}]
    paths = B._strict_paths(tmp_path, checkout, gap=gap)
    evidence = CERT_FIX._load_strict(paths)
    state = CERT_FIX._page_state(tmp_path, monkeypatch)
    gap_row = evidence.gap["rows"][0]
    ledger_path = state / "ledger" / f"{gap_row['tool']}.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    res = pin_kinds.recipe_patches(checkout, sha)
    ledger.update(
        integrated=True, ahead=gap_row["ahead"], pin_kind="contents_assertion",
        pinned_ref=sha, pinned_ref_full=sha, behind_commits=None, sync_lag=None,
        release_lag=None, ours_unshipped=None, ours_unshipped_substantive=None,
        recipe_patches=res["patches"], recipe_patches_error=res["error"])
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    PAGE.build(tmp_path / "control.html", publication=evidence)
    assert "gf180mcu-librelane-layers-rc-ohm" in (tmp_path / "control.html").read_text()
    return PAGE, evidence, ledger_path


@pytest.mark.parametrize("mutation", ["drop-one", "absent", "invent-fork-commit"])
def test_a_certified_page_refuses_a_recipe_list_the_tree_does_not_derive(
        tmp_path, monkeypatch, mutation):
    PAGE, evidence, ledger_path = _certified_assertion_fixture(tmp_path, monkeypatch)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if mutation == "drop-one":
        ledger["recipe_patches"] = ledger["recipe_patches"][:-1]
    elif mutation == "absent":
        del ledger["recipe_patches"]
    else:
        patched = copy.deepcopy(ledger["recipe_patches"])
        patched[0]["fork_commits"] = [{"pr": "vibeic/open_pdks#9", "sha": "e" * 40,
                                       "what": ""}]
        ledger["recipe_patches"] = patched
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")
    with pytest.raises(PAGE.PublicationEvidenceError, match="recipe_patches"):
        PAGE.build(out, publication=evidence)
    assert out.read_text(encoding="utf-8") == "LAST GOOD"


def test_a_certified_pin_row_may_not_carry_recipe_patches(tmp_path, monkeypatch):
    import build_page as PAGE
    import test_page_certified_boundary as B
    import test_page_publication_current_round as CERT_FIX
    checkout = B._clean_checkout(tmp_path, monkeypatch)
    paths = B._strict_paths(tmp_path, checkout)
    evidence = CERT_FIX._load_strict(paths)
    state = CERT_FIX._page_state(tmp_path, monkeypatch)
    tool = evidence.gap["rows"][0]["tool"]
    ledger_path = state / "ledger" / f"{tool}.json"
    PAGE.build(tmp_path / "control.html", publication=evidence)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["recipe_patches"] = [{"id": "x", "paths": ["p"]}]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(PAGE.PublicationEvidenceError, match="recipe_patches"):
        PAGE.build(tmp_path / "eda-forks.html", publication=evidence)
