"""The image has to state BOTH halves of what its PDK volume is (vibe-ic#768
landing on top of vibeic-eda#79).

#79 established that the open_pdks value is an ASSERTION about a prebuilt ciel
volume rather than a build input, and made the build refuse a value the volume
does not carry. It is enforced by resolving `/foss/pdks/<fam>` and requiring the
sha in the resolved PATH.

#768 then rewrites 12 tech LEFs INSIDE the tree that path resolves to. MEASURED
in the base image this Dockerfile pins:

    readlink -f /foss/pdks/sky130A
      -> /foss/pdks/ciel/sky130/versions/b344c97e.../sky130A
    the patched files
      -> /foss/pdks/ciel/sky130/versions/b344c97e.../sky130A/libs.ref/...

so the symlink does not move, the assertion still passes, and the sentence it
stands for — "this is what open_pdks <sha> produced" — has stopped being true.
That is #79's own defect class arriving from the other side: not a claim
advanced past its evidence, but evidence moved out from under a claim.

These tests pin the reconciliation. The load-bearing one is
`test_an_undeclared_modification_fails`: a patch step nobody declares must fail
the build, so declaring is the only way to get a green build rather than
something a maintainer has to remember.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROG = ROOT / "tools" / "pdk" / "pdk_local_mods.py"
MANIFEST = ROOT / "tools" / "pdk" / "local_mods.json"
DOCKERFILE = ROOT / "Dockerfile"

_spec = importlib.util.spec_from_file_location("pdk_local_mods", str(PROG))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# --------------------------------------------------------------- fixtures

def _volume(tmp_path: Path) -> Path:
    """A miniature of the real shape: <fam>/versions/<sha>/<pdk>/..."""
    root = tmp_path / "ciel"
    d = root / "sky130" / "versions" / "abc123" / "sky130A" / "libs.ref" / "lib" / "techlef"
    d.mkdir(parents=True)
    (d / "a.tlef").write_text("RECT -0.71 -0.71 0.71 0.71 ;\n")
    (d / "b.tlef").write_text("RECT -0.71 -0.71 0.71 0.71 ;\n")
    other = root / "gf180mcu" / "versions" / "abc123" / "gf180mcuD" / "libs.ref"
    other.mkdir(parents=True)
    (other / "c.tlef").write_text("untouched\n")
    return root


def _manifest(tmp_path: Path, **over) -> Path:
    entry = {
        "id": "e1", "issue": "x#1", "what": "w", "step": "s",
        "paths": ["sky130/versions/*/sky130A/libs.ref/lib/techlef/*.tlef"],
        "expect_modified": 2, "expect_added": 0, "expect_removed": 0,
    }
    entry.update(over)
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"root": "x", "modifications": [entry]}))
    return p


def _run(*args):
    return subprocess.run([sys.executable, str(PROG), *args],
                          capture_output=True, text=True)


# --------------------------------------------------------------- digesting

def test_a_content_edit_moves_the_digest_but_not_the_path(tmp_path):
    """The whole reason the path assertion cannot see #768."""
    root = _volume(tmp_path)
    f = root / "sky130/versions/abc123/sky130A/libs.ref/lib/techlef/a.tlef"
    before = mod.digest_tree(root)
    assert "abc123" in str(f.resolve())          # the sha is in the PATH
    f.write_text("RECT -0.8 -0.8 0.8 0.8 ;\n")
    after = mod.digest_tree(root)
    assert "abc123" in str(f.resolve())          # ...and it still is
    assert set(before) == set(after)             # no path changed
    assert mod.diff_trees(before, after)["modified"]   # the bytes did


def test_a_retargeted_symlink_is_a_difference(tmp_path):
    """Hashing regular-file content only would miss it, and the volume is
    reached THROUGH symlinks."""
    root = _volume(tmp_path)
    link = root / "here"
    link.symlink_to("sky130")
    before = mod.digest_tree(root)
    link.unlink()
    link.symlink_to("gf180mcu")
    assert "here" in mod.diff_trees(before, mod.digest_tree(root))["modified"]


def test_added_and_removed_files_are_differences(tmp_path):
    root = _volume(tmp_path)
    before = mod.digest_tree(root)
    (root / "new.txt").write_text("x")
    (root / "gf180mcu/versions/abc123/gf180mcuD/libs.ref/c.tlef").unlink()
    d = mod.diff_trees(before, mod.digest_tree(root))
    assert d["added"] == ["new.txt"] and len(d["removed"]) == 1


# --------------------------------------------------------------- globbing

def test_star_does_not_cross_a_path_separator():
    """fnmatch's `*` does, which would turn a glob naming one directory's files
    into one covering the whole subtree — the opposite of a scope declaration."""
    r = mod.glob_to_re("sky130/versions/*/sky130A/x.tlef")
    assert r.match("sky130/versions/abc/sky130A/x.tlef")
    assert not r.match("sky130/versions/a/b/sky130A/x.tlef")
    assert mod.glob_to_re("a/**/z").match("a/b/c/z")


# --------------------------------------------------------------- reconciling

def _delta(modified=(), added=(), removed=()):
    return {"modified": {m: ("old", "new") for m in modified},
            "added": list(added), "removed": list(removed)}


def _mods(tmp_path, **over):
    return mod.load_manifest(_manifest(tmp_path, **over))


P = "sky130/versions/abc123/sky130A/libs.ref/lib/techlef/{}.tlef"


def test_a_declared_modification_reconciles(tmp_path):
    problems, per = mod.reconcile(
        _delta([P.format("a"), P.format("b")]), _mods(tmp_path))
    assert problems == []
    assert len(per["e1"]["modified"]) == 2


def test_an_undeclared_modification_fails(tmp_path):
    """THE load-bearing property. A patch step nobody declared changes files no
    entry covers, and the build stops naming them."""
    problems, _ = mod.reconcile(
        _delta([P.format("a"), P.format("b"),
                "gf180mcu/versions/abc123/gf180mcuD/libs.ref/c.tlef"]),
        _mods(tmp_path))
    assert any("UNDECLARED" in p and "gf180mcuD" in p for p in problems)


def test_a_declaration_that_stopped_applying_fails(tmp_path):
    """The other direction. A step that silently stops matching satisfies every
    post-condition it no longer reaches; the count is what notices."""
    problems, _ = mod.reconcile(_delta([P.format("a")]), _mods(tmp_path))
    assert any("COUNT" in p and "measured 1" in p for p in problems)


def test_a_declaration_whose_blast_radius_grew_fails(tmp_path):
    problems, _ = mod.reconcile(
        _delta([P.format("a"), P.format("b"), P.format("c")]), _mods(tmp_path))
    assert any("COUNT" in p and "measured 3" in p for p in problems)


def test_two_entries_claiming_one_path_fail(tmp_path):
    """Declarations must partition, or one file satisfies two counts."""
    p = tmp_path / "m2.json"
    e = {"id": "e1", "issue": "x", "what": "w",
         "paths": ["sky130/**"], "expect_modified": 1}
    p.write_text(json.dumps({"modifications": [e, dict(e, id="e2")]}))
    problems, _ = mod.reconcile(_delta([P.format("a")]), mod.load_manifest(p))
    assert any("AMBIGUOUS" in x for x in problems)


# --------------------------------------------------------------- provenance

def test_the_79_path_assertion_is_re_run_at_the_end(tmp_path):
    """The original runs BEFORE the local steps, so it cannot see a step that
    re-points the symlink afterwards."""
    good = tmp_path / "good"
    (tmp_path / "versions" / "deadbeef").mkdir(parents=True)
    good.symlink_to(tmp_path / "versions" / "deadbeef")
    assert mod.check_symlinks([str(good)], "deadbeef") == []
    bad = mod.check_symlinks([str(good)], "cafef00d")
    assert bad and "PROVENANCE" in bad[0]


# --------------------------------------------------------------- cli

def test_cli_baseline_then_declared_verify_passes(tmp_path):
    root = _volume(tmp_path)
    base = tmp_path / "b.sha256"
    assert _run("baseline", "--root", str(root), "--out", str(base)).returncode == 0

    for n in ("a", "b"):
        f = root / f"sky130/versions/abc123/sky130A/libs.ref/lib/techlef/{n}.tlef"
        f.write_text("RECT -0.8 -0.8 0.8 0.8 ;\n")

    rec = tmp_path / "rec.json"
    r = _run("verify", "--root", str(root), "--baseline", str(base),
             "--manifest", str(_manifest(tmp_path)),
             "--upstream-sha", "abc123", "--record", str(rec))
    assert r.returncode == 0, r.stderr
    got = json.loads(rec.read_text())
    assert got["upstream"]["open_pdks"] == "abc123"
    assert got["files_modified_locally"] == 2
    m = got["local_modifications"][0]
    assert len(m["modified"]) == 2
    assert m["modified"][0]["upstream_sha256"] != m["modified"][0]["shipped_sha256"]


def test_cli_undeclared_verify_fails_and_writes_no_record(tmp_path):
    root = _volume(tmp_path)
    base = tmp_path / "b.sha256"
    _run("baseline", "--root", str(root), "--out", str(base))
    for n in ("a", "b"):
        (root / f"sky130/versions/abc123/sky130A/libs.ref/lib/techlef/{n}.tlef"
         ).write_text("grown\n")
    (root / "gf180mcu/versions/abc123/gf180mcuD/libs.ref/c.tlef").write_text("swept in\n")

    rec = tmp_path / "rec.json"
    r = _run("verify", "--root", str(root), "--baseline", str(base),
             "--manifest", str(_manifest(tmp_path)),
             "--upstream-sha", "abc123", "--record", str(rec))
    assert r.returncode == 1
    assert "UNDECLARED" in r.stderr and "c.tlef" in r.stderr
    assert not rec.exists(), (
        "a record was written for a volume that failed reconciliation, which "
        "is exactly the false statement this step exists to prevent")


def test_cli_refuses_an_empty_upstream_sha(tmp_path):
    """vibeic-eda#60: an ARG before the first FROM expands to the empty string
    inside a stage that does not redeclare it, and a check against "" passes by
    comparing nothing."""
    root = _volume(tmp_path)
    base = tmp_path / "b.sha256"
    _run("baseline", "--root", str(root), "--out", str(base))
    r = _run("verify", "--root", str(root), "--baseline", str(base),
             "--manifest", str(_manifest(tmp_path)), "--upstream-sha", "",
             "--record", str(tmp_path / "r.json"))
    assert r.returncode == 1 and "empty" in r.stderr


def test_cli_refuses_a_missing_baseline(tmp_path):
    """No baseline means nothing knows what arrived, and every diff is empty —
    which would look identical to "nothing was changed"."""
    r = _run("verify", "--root", str(_volume(tmp_path)),
             "--baseline", str(tmp_path / "absent"),
             "--manifest", str(_manifest(tmp_path)),
             "--upstream-sha", "abc123", "--record", str(tmp_path / "r.json"))
    assert r.returncode == 1 and "baseline" in r.stderr


# --------------------------------------------------------------- the wiring
#
# The program is only as good as where it is called from. These pin the
# Dockerfile, because a correct checker invoked in the wrong order checks
# nothing.

def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _line_of(pattern: str) -> int:
    src = _dockerfile().splitlines()
    for i, ln in enumerate(src):
        if re.search(pattern, ln):
            return i
    raise AssertionError(f"Dockerfile has no line matching {pattern!r}")


def test_the_baseline_is_taken_before_the_first_local_pdk_step():
    """A baseline taken after the patch records the patched bytes as
    "delivered", and the diff is empty — a green check that checked nothing."""
    assert _line_of(r"pdk_local_mods\.py baseline") < _line_of(
        r"pdk_via_min_width_patch\.py --layers met5")


def test_the_79_assertion_still_runs_and_still_redeclares_its_ARG():
    src = _dockerfile()
    assert re.search(r"^ARG OPEN_PDKS_VOLUME_CONTENTS_SHA$", src, re.M), (
        "the in-stage ARG redeclaration is gone; the assertion would compare "
        "against the empty string and pass (vibeic-eda#60)")
    assert 'test -n "${OPEN_PDKS_VOLUME_CONTENTS_SHA}"' in src
    assert _line_of(r"readlink -f \"/foss/pdks/\$fam\"") < _line_of(
        r"pdk_local_mods\.py baseline")


def test_verify_is_the_last_RUN_in_the_file():
    """Every step above it is inside the measured window. A step added below it
    is not, which is why this position is asserted rather than assumed."""
    src = _dockerfile().splitlines()
    last = max(i for i, ln in enumerate(src) if ln.startswith("RUN "))
    tail = "\n".join(src[last:last + 12])
    assert "pdk_local_mods.py verify" in tail, (
        "something now runs after the PDK reconciliation, so 'every local "
        f"modification' no longer means every one:\n{tail}")


def test_verify_is_fed_the_asserted_sha_and_re_checks_the_symlinks():
    src = _dockerfile()
    assert '--upstream-sha "${OPEN_PDKS_VOLUME_CONTENTS_SHA}"' in src
    for fam in ("sky130A", "gf180mcuD"):
        assert f"--pdk-symlink /foss/pdks/{fam}" in src


def test_the_record_is_written_outside_the_tree_it_describes():
    src = _dockerfile()
    rec = re.search(r"--record (\S+)", src).group(1)
    root = re.search(r"pdk_local_mods\.py verify[\s\S]{0,200}?--root (\S+)",
                     src).group(1)
    assert not rec.startswith(root.rstrip("/") + "/"), (
        f"{rec} is inside {root}: a record written into the tree it describes "
        f"is a modification made after the check that must see every one")


def test_the_image_ends_as_the_runtime_user():
    """The verify step needs root to write into /foss/pdks. Leaving it there
    would ship an image whose user is not the one every earlier step was
    verified against."""
    src = [ln for ln in _dockerfile().splitlines()
           if ln.startswith("USER ")]
    assert src[-1] == "USER 1000", f"the image would ship as {src[-1]!r}"


# --------------------------------------------------------------- the manifest

def test_the_manifest_declares_the_768_patch_and_is_loadable():
    mods = mod.load_manifest(MANIFEST)
    ids = [m["id"] for m in mods]
    assert "met5-via-patch-min-width" in ids, ids
    e = next(m for m in mods if m["id"] == "met5-via-patch-min-width")
    assert e["expect_modified"] == 12, (
        "the 768 step writes 12 tech LEFs (6 HD + 6 HVL); MEASURED against the "
        "pinned base image")


def test_the_manifest_does_not_cover_the_gf180_sites():
    """#768 is deliberately met5-only: the same shape exists on gf180's
    Metal2/Metal3/Metal5, on far denser layers whose DRC consequences have NOT
    been measured. A later sweep that reached them must fail as an undeclared
    modification rather than ride along on this entry."""
    mods = mod.load_manifest(MANIFEST)
    gf = ("gf180mcu/versions/b344c97e/gf180mcuD/libs.ref/"
          "gf180mcu_fd_sc_mcu7t5v0/techlef/gf180mcu_fd_sc_mcu7t5v0__nom.tlef")
    assert not any(r.match(gf) for m in mods for r in m["_res"]), (
        "a gf180 tech LEF is covered by a declaration, so sweeping the "
        "unmeasured sites in would pass")


def test_the_manifest_paths_match_what_the_768_step_actually_writes():
    """The declaration and the step have to be about the same files. Derived
    from the Dockerfile's own HD/HVL paths rather than restated."""
    src = _dockerfile()
    mods = mod.load_manifest(MANIFEST)
    ver = "b344c97e"
    for lib in re.findall(r"/foss/pdks/sky130A/libs\.ref/(\S+)/techlef", src):
        rel = f"sky130/versions/{ver}/sky130A/libs.ref/{lib}/techlef/x.tlef"
        assert any(r.match(rel) for m in mods for r in m["_res"]), (
            f"the build patches {lib} but no manifest entry covers it")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
