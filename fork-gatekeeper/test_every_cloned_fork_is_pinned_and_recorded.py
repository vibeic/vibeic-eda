"""A fork the build CLONES must reach the release record and the image's provenance.

vibeic-eda#104 finding 2. `asap7sc7p5t_28` is a fork we own, whose entire history
we force-replaced, and whose CDL is the golden netlist every ASAP7 LVS run is
compared against. Its ref was pinned to a commit in bbebc21 — and it still did
not appear in `RELEASED.json`'s `pins`, so it stayed outside `pins_fingerprint`;
and it still wrote no `/vibeic/provenance/*.json`, so `check_image_provenance`
could not ask whether the image contains the commit the pin names. Both gates
reported a confident PASS over it, because both were counting a set it was not
in.

TWO CAUSES, ONE SHAPE — a set derived by a rule that quietly omits members.

  1. THE PIN SET IS PAIRED BY NAME-MANGLING. `pinned_refs` and
     `ref_arg_names` take the repo name, upper-case it, and look for an ARG of
     that name (then a `_`-flattened retry). `asap7sc7p5t_28` -> `ASAP7SC7P5T_28`
     never matches `ASAP7SC_REF`, so three forks were simply absent from every
     count. `discover_forks.parse_dockerfile_pins` pairs the same files
     STRUCTURALLY — by `--branch ${X_REF}`, `checkout ${X_REF}` and
     `git -C <dir> checkout ${X_REF}` — and gets all three right, and its tests
     have asserted so since vibeic-eda#54. Two readers of one fact, disagreeing,
     with the more accurate one not being the one the release used.

  2. THE PROVENANCE GUARD NEVER STATED ITS DENOMINATOR.
     `test_image_provenance_covers_every_tool` requires EMITTED == COLLECTED ==
     PRIMARY, which is exactly right for a record that exists and exactly silent
     about a stage that writes none: absent from all three sets, the equality
     holds and the fork is never mentioned. `check_image_provenance` then printed
     `19/19 tools ... are at the pinned commit` — true, and read by everyone as
     "every fork we ship". Measured on 307c4d9, three stages in the ROOT
     Dockerfile clone a vibeic fork at a pinned SHA and emit no record at all:
     `nangate45-src`, `asap7-lvs-src`, `align-src`. The guard is now required to
     divide into the forks the build CLONES and to NAME the ones no record speaks
     for, so an absent record shows up as an absent record rather than as a
     smaller fraction that still reads as 100%.

Everything below is derived from the Dockerfiles, so none of it has to be edited
when a fork is added or retired. A clone without a pin, or a PRIMARY key naming
an ARG that pins nothing, fails by NAME.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import check_pins_current as CPC          # noqa: E402
import daily_release as DR                # noqa: E402

#: `github.com/vibeic/<repo>.git` — a clone of one of our forks.
FORK_URL = re.compile(r"github\.com/vibeic/([A-Za-z0-9_.-]+?)\.git")
#: `> /vibeic/provenance/<name>.json`, i.e. a record being WRITTEN. The `COPY`
#: sites name the same path and are not emitters.
EMIT = re.compile(r">+\s*/vibeic/provenance/([A-Za-z0-9_.-]+)\.json")
#: `FROM <base> [AS <name>]` — the stage boundary.
STAGE = re.compile(r"^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", re.M | re.I)


def _uncommented(p: Path) -> str:
    """A URL inside a `#` comment documents history; it clones nothing."""
    return "\n".join("" if ln.lstrip().startswith("#") else ln
                     for ln in p.read_text(encoding="utf-8", errors="replace").split("\n"))


def _dockerfiles() -> list[Path]:
    return [ROOT / "Dockerfile"] + sorted(ROOT.glob("tools/*/Dockerfile"))


def _cloned_forks() -> dict[str, str]:
    """repo -> the Dockerfile that clones it, over every Dockerfile."""
    out: dict[str, str] = {}
    for p in _dockerfiles():
        if not p.is_file():
            continue
        for repo in FORK_URL.findall(_uncommented(p)):
            out.setdefault(repo, p.relative_to(ROOT).as_posix())
    return out


def test_the_measurement_is_not_vacuous():
    """A parser matching nothing would make every assertion below pass."""
    assert len(_cloned_forks()) >= 20, sorted(_cloned_forks())
    assert len(CPC.pinned_refs(ROOT)) >= 20, sorted(CPC.pinned_refs(ROOT))


def test_every_cloned_fork_carries_a_pin_the_release_record_can_see():
    """`pinned_refs` is what writes `RELEASED.json`'s `pins`, and `pins` is what
    `pins_fingerprint` is computed over. A fork missing here is a fork two images
    can differ in while carrying an identical release record."""
    cloned, pinned = _cloned_forks(), CPC.pinned_refs(ROOT)
    missing = sorted(set(cloned) - set(pinned))
    assert not missing, (
        "these forks are cloned by the build and do not appear in the pin set, "
        "so they are outside `pins_fingerprint` and outside every count that "
        "reads it:\n"
        + "\n".join(f"  {r}  (cloned in {cloned[r]})" for r in missing))


def test_every_cloned_fork_resolves_to_the_arg_that_actually_pins_it():
    """The release reads the ARG NAME through a second, separate pairing, and a
    fork missing from THAT one is dropped from `refs_now` — so the artefact tag
    the release compares against is composed from a smaller set than the one it
    just checked."""
    cloned, args_of = _cloned_forks(), DR.ref_arg_names(ROOT)
    missing = sorted(set(cloned) - set(args_of))
    assert not missing, (
        "these forks are cloned by the build and `ref_arg_names` cannot say "
        "which ARG pins them:\n"
        + "\n".join(f"  {r}  (cloned in {cloned[r]})" for r in missing))


def test_the_two_pairings_agree_with_the_structural_parser():
    """One fact, one reader. `discover_forks.parse_dockerfile_pins` pairs by the
    clone's SYNTAX and its tests pin the three-clones-in-one-RUN case; the two
    functions above must not re-derive the same pairing by a different rule."""
    import discover_forks as DF
    struct: dict[str, dict] = {}
    for p in _dockerfiles():
        if not p.is_file():
            continue
        for meta in DF.parse_dockerfile_pins(_uncommented(p)).values():
            if meta.get("pinned_via") is None:      # a real clone, not an ARG-only claim
                struct.setdefault(meta["repo"], meta)
    pinned, args_of = CPC.pinned_refs(ROOT), DR.ref_arg_names(ROOT)
    bad = []
    for repo, meta in sorted(struct.items()):
        if pinned.get(repo) != meta["ref"]:
            bad.append(f"{repo}: pinned_refs says {pinned.get(repo)!r}, "
                       f"the clone says {meta['ref']!r}")
        want_arg = meta["arg"][:-len("_REF")]
        if args_of.get(repo) != want_arg:
            bad.append(f"{repo}: ref_arg_names says {args_of.get(repo)!r}, "
                       f"the clone reads {want_arg!r}")
    assert not bad, "\n".join(bad)


def test_the_provenance_guard_states_the_set_it_cannot_speak_for():
    """`19/19` was true and meant "19 of the records that exist".

    A fork with no `/vibeic/provenance/*.json` is not a FAIL in that checker — it
    is absent from BOTH sides of the fraction, which is how `asap7sc7p5t_28`
    could be unpinned, unrecorded and unmentioned while the guard printed a
    confident PASS over the image that shipped its golden CDL. The denominator
    that matters is the forks the build CLONES, and the ones no record speaks for
    have to be NAMED, not silently excluded."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "check_image_provenance", ROOT / "tools" / "check_image_provenance.py")
    M = importlib.util.module_from_spec(spec)
    sys.modules["check_image_provenance"] = M
    spec.loader.exec_module(M)

    assert hasattr(M, "pinned_forks") and hasattr(M, "covered_forks"), (
        "the guard cannot state its own coverage: it reports N/N over the "
        "records it happens to have and says nothing about the forks it has "
        "no record for")

    # The denominator is the same set the release record is built from — one
    # reader, so the two cannot drift.
    assert set(M.pinned_forks()) == set(CPC.pinned_refs(ROOT))

    # And a record must be able to speak for more than the artefact it names,
    # because a stage may clone several forks into one artefact.
    single = M.covered_forks({"repo": "https://github.com/vibeic/yosys.git"})
    assert single == {"yosys"}, single
    multi = M.covered_forks({"repo": "https://github.com/vibeic/asap7sc7p5t_28.git",
                             "also": {"asap7_pdk_r1p7": "b" * 40}})
    assert multi == {"asap7sc7p5t_28", "asap7_pdk_r1p7"}, multi


def test_the_asap7_golden_cdl_is_one_the_guard_can_now_be_asked_about():
    """Stated as the rule, not the name: every fork whose pin lives in the ROOT
    Dockerfile and whose content is copied into the runtime must be reachable by
    the guard, i.e. some record's PRIMARY key must be an ARG that pins it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "check_image_provenance", ROOT / "tools" / "check_image_provenance.py")
    M = importlib.util.module_from_spec(spec)
    sys.modules["check_image_provenance"] = M
    spec.loader.exec_module(M)

    args_pinning_a_fork = {site["arg"] for site in CPC.pin_sites(ROOT).values()}
    unknown = sorted(k for k in M.PRIMARY.values() if k not in args_pinning_a_fork)
    assert not unknown, (
        "these PRIMARY keys name an ARG that pins no cloned fork, so the guard "
        f"would compare the image against a value nothing produces: {unknown}")

    # And every ARG the map names must exist in the file the map says it is in.
    ARG = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)\s*=\s*(\S+)", re.M)
    bad = []
    for tool, key in sorted(M.PRIMARY.items()):
        f = M.pin_file(tool)
        val = dict(ARG.findall(f.read_text(encoding="utf-8", errors="replace"))
                   ).get(key, "").split("#")[0].strip()
        if not re.fullmatch(r"[0-9a-f]{40}", val):
            bad.append(f"{tool}: ARG {key} in {f.name} is {val!r}, not a commit")
    assert not bad, bad
