"""vibeic-eda#194: the :latest repair must produce the shape its verifier reads,
and must be reachable when the state it repairs has made the suite red.

MEASURED 2026-09-07, recovering 0.3.50. The 13:20 round published the image and
lost the race to push its record (`origin/main is not an ancestor of HEAD`), so
:latest was correctly left at 0.3.49 — the record is Git authority and :latest
must never lead it. Then:

    :latest != recorded version
      -> test_release_record_pins_ahead (3 tests) FAIL
      -> semantic pytest RED -> run_tick NOT CHECKED -> daily_release never runs
      -> the noop arm that repairs :latest never runs -> :latest != recorded version

and the repair, invoked directly, could not verify its own work:

    :latest   index    4074074cb0d8dd99…  -> manifests: [5e2b472f8c6be44e…]
    :0.3.50   manifest 5e2b472f8c6be44e…
    repair -> False   repair_attempted: true   category: latest_manifest_mismatch

`docker buildx imagetools create --tag <target> <source>` wraps a single image
manifest in an OCI INDEX. An index digest never equals the manifest digest it
wraps, and the verifier compares identities.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_release as R  # noqa: E402


VERSION = "0.3.50"
MANIFEST = "5e2b472f8c6be44e7e366423c50ec1d3b169bae4b10cfbca212b0e4c1bef447d"
INDEX = "4074074cb0d8dd994eba1e6f0393ad012cb369ff37d08c82de8529f73feaf7c0"
STALE = "89a8fd7295208ee6d06e216ade9edc6161d26db52099e9f22ceb77a2d76e3f49"


def _probe(latest_identity):
    """A registry whose version tag is MANIFEST and whose latest is whatever
    the last publish left there."""
    state = {"latest": latest_identity}

    def probe(ref):
        ident = MANIFEST if ref.endswith(f":{VERSION}") else state["latest"]
        return R.PresenceMeasurement(
            state=R.Presence.PRESENT if ident else R.Presence.ABSENT,
            subject=ref, source="test", detail="", identity=ident)
    return probe, state


def test_the_repair_that_wraps_the_manifest_in_an_index_cannot_verify_itself():
    """THE REGRESSION, as an executable statement of what went wrong: a publish
    that produces an index leaves the repair reporting mismatch about its own
    successful write."""
    probe, state = _probe(STALE)

    def publish_as_index(source, target):
        state["latest"] = INDEX          # what `imagetools create` produces
        return 0, "", ""

    ok, ev = R.repair_latest_publication(
        VERSION, probe=probe, publish=publish_as_index,
        expected_identity=MANIFEST)
    assert ok is False
    assert ev["repair_attempted"] is True
    assert ev["category"] == "latest_manifest_mismatch"


def test_the_repair_verifies_when_the_publish_preserves_the_manifest():
    """THE FIXED DIRECTION: republishing the same manifest under :latest is
    what the release path does, and the verifier can then be believed."""
    probe, state = _probe(STALE)

    def publish_as_manifest(source, target):
        state["latest"] = MANIFEST
        return 0, "", ""

    ok, ev = R.repair_latest_publication(
        VERSION, probe=probe, publish=publish_as_manifest,
        expected_identity=MANIFEST)
    assert ok is True and ev["verified"] is True
    assert ev["manifests"]["latest"]["identity"] == MANIFEST


def test_the_default_publisher_is_pull_tag_push_not_imagetools_create():
    """The default publisher is the one that ran in production, so its SHAPE is
    the property under test: `imagetools create` is exactly the call that
    produced an index. Asserted on the argv, because the media type is decided
    by which docker subcommand is invoked."""
    probe, _ = _probe(STALE)
    calls = []

    def fake_sh(argv, **kw):
        calls.append(list(argv))
        return 0, "", ""            # _sh returns (rc, stdout, stderr)

    real_sh = R._sh
    R._sh = fake_sh
    try:
        R.repair_latest_publication(VERSION, probe=probe,
                                    expected_identity=MANIFEST)
    finally:
        R._sh = real_sh

    flat = [" ".join(c) for c in calls]
    assert not any("imagetools" in c for c in flat), (
        "the default publisher still uses `buildx imagetools create`, which "
        f"wraps a manifest in an index: {flat}")
    assert any(c[:2] == ["docker", "pull"] for c in calls), flat
    assert any(c[:2] == ["docker", "tag"] for c in calls), flat
    assert any(c[:2] == ["docker", "push"] for c in calls), flat
    # the tag must be created FROM the version tag, TO :latest
    tag = next(c for c in calls if c[:2] == ["docker", "tag"])
    assert tag[2].endswith(f":{VERSION}") and tag[3].endswith(":latest"), tag


def test_a_failed_pull_is_reported_and_nothing_is_pushed():
    """A repair that cannot fetch the bytes must not go on to tag and push
    whatever a stale local cache happens to hold under that name."""
    probe, _ = _probe(STALE)
    calls = []

    def fake_sh(argv, **kw):
        calls.append(list(argv))
        rc = 1 if argv[:2] == ["docker", "pull"] else 0
        return rc, "", "no such manifest"

    real_sh = R._sh
    R._sh = fake_sh
    try:
        ok, ev = R.repair_latest_publication(VERSION, probe=probe,
                                             expected_identity=MANIFEST)
    finally:
        R._sh = real_sh
    assert ok is False
    assert not any(c[:2] == ["docker", "push"] for c in calls), calls
    assert ev["repair"]["exit_code"] == 1


# --------------------------------------------------------------------------
# The CLI arm: the repair must be reachable without the round that cannot run
# --------------------------------------------------------------------------

def test_the_repair_has_a_cli_arm():
    out = subprocess.run([sys.executable, str(HERE / "daily_release.py"), "--help"],
                         capture_output=True, text=True)
    assert "--repair-latest" in out.stdout, (
        "the only caller of repair_latest_publication is daily_release's noop "
        "arm, which the red suite caused by the very state it repairs gates")


def _tree(tmp_path, version, recorded=None):
    root = tmp_path / "eda"
    (root / "fork-gatekeeper").mkdir(parents=True)
    (root / "VERSION").write_text(version + "\n")
    (root / "RELEASED.json").write_text(json.dumps({
        "version": recorded if recorded is not None else version,
        "manifest_identity": MANIFEST, "pins": {}, "pins_fingerprint": "x"}) + "\n")
    return root


def _run(root, *args):
    return subprocess.run(
        [sys.executable, str(HERE / "daily_release.py"),
         "--eda-root", str(root), *args],
        capture_output=True, text=True)


def test_the_cli_refuses_a_version_the_record_does_not_authorise(tmp_path):
    """:latest may only be pointed at the version this tree's record names —
    otherwise the repair becomes a way to publish any tag as latest."""
    root = _tree(tmp_path, "0.3.50", recorded="0.3.50")
    r = _run(root, "--repair-latest", "0.3.49")
    assert r.returncode != 0
    assert "REFUSED" in r.stderr and "0.3.50" in r.stderr


def test_the_cli_refuses_when_the_release_authority_declines(tmp_path, monkeypatch):
    """authorize_latest_repair is the same gate the release path uses; a stale
    checkout must not repair :latest after origin has moved on."""
    root = _tree(tmp_path, "0.3.50")
    script = HERE / "daily_release.py"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys, json\n"
        f"sys.path.insert(0, {str(HERE)!r})\n"
        "import daily_release as R\n"
        "R.authorize_latest_repair = lambda *a, **k: (False, {'summary': 'origin moved on'})\n"
        "R.repair_latest_publication = lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not repair'))\n"
        f"sys.argv = ['daily_release.py', '--eda-root', {str(root)!r}, '--repair-latest', '0.3.50']\n"
        "raise SystemExit(R.main())\n")
    r = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True)
    assert r.returncode != 0
    assert "origin moved on" in r.stderr
