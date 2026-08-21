"""Regression boundaries for the certified EDA-forks page.

The page is a certificate for one immutable repository revision, one normalized
gap record, and one released image identity.  These tests deliberately mutate
each side of those bindings and require refusal before the last-good page moves.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

import build_page as PAGE
import capture_release_ll as LL
import test_fork_github_convergence_ll as GAP_FIX
import test_page_publication_current_round as CERT_FIX


HERE = Path(__file__).resolve().parent
REPO = HERE.parent
REAL_IMAGE_REF = PAGE._image_ref
HOSTILE_GIT_ENV = {
    "GIT_DIR": "/tmp/not-the-certified-repository.git",
    "GIT_WORK_TREE": "/tmp/not-the-certified-worktree",
    "GIT_INDEX_FILE": "/tmp/not-the-certified-index",
    "GIT_OBJECT_DIRECTORY": "/tmp/not-the-certified-objects",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/tmp/not-the-certified-alternates",
    "GIT_REPLACE_REF_BASE": "refs/not-the-certified-replacements/",
    "GIT_NAMESPACE": "not-the-certified-namespace",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": "/tmp/not-the-certified-hooks",
}


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run test setup through the certificate's fixed, sanitized Git authority."""
    result = subprocess.run(
        [*LL._GIT_PREFIX, *args],
        cwd=cwd,
        env=LL._git_environment(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _clean_checkout(tmp_path: Path, monkeypatch) -> Path:
    """Clone immutable HEAD so this regression file cannot dirty its own subject."""
    checkout = tmp_path / "certified-repository"
    _git("clone", "--quiet", "--no-hardlinks", str(REPO), str(checkout))
    source_head = _git("-C", str(REPO), "rev-parse", "HEAD").stdout.strip()
    clone_head = _git("-C", str(checkout), "rev-parse", "HEAD").stdout.strip()
    assert clone_head == source_head
    assert _git(
        "-C", str(checkout), "status", "--porcelain=v1",
        "--untracked-files=all",
    ).stdout == ""
    monkeypatch.setattr(PAGE, "HERE", checkout / "fork-gatekeeper")
    return checkout


def _strict_paths(tmp_path: Path, checkout: Path, *, gap: dict | None = None):
    fixture_dir = tmp_path / "evidence"
    fixture_dir.mkdir()
    paths = CERT_FIX._strict_fixture(fixture_dir, gap=gap)
    _bind_forks_path(paths, checkout / "fork-gatekeeper" / "FORKS.json")
    return paths


def _bind_forks_path(paths: dict, forks_path: Path) -> None:
    """Keep retained AI registry provenance bound to the supplied fleet file."""
    paths["forks"] = forks_path
    ll = json.loads(paths["ll"].read_text(encoding="utf-8"))
    ll["ai_authorization"]["registry"]["source"] = str(forks_path)
    paths["ll"].write_text(json.dumps(ll), encoding="utf-8")
    paths["ll_record"] = ll


@pytest.mark.parametrize(
    "dirty_kind,relative,prefix",
    [
        pytest.param(
            "unstaged", "fork-gatekeeper/build_page.py", " M ",
            id="unstaged-build-page",
        ),
        pytest.param(
            "staged", "fork-gatekeeper/capture_release_ll.py", "M  ",
            id="staged-capture-code",
        ),
        pytest.param(
            "untracked-input", "fork-gatekeeper/FORKS.certified.json", "?? ",
            id="untracked-publication-input",
        ),
    ],
)
def test_certified_build_refuses_repo_changes_relative_to_its_immutable_head(
        tmp_path, monkeypatch, dirty_kind, relative, prefix):
    checkout = _clean_checkout(tmp_path, monkeypatch)
    paths = _strict_paths(tmp_path, checkout)
    CERT_FIX._page_state(tmp_path, monkeypatch)
    changed = checkout / relative

    if dirty_kind == "untracked-input":
        changed.write_text(
            paths["forks"].read_text(encoding="utf-8"), encoding="utf-8")
        _bind_forks_path(paths, changed)
    else:
        with changed.open("a", encoding="utf-8") as stream:
            stream.write("\n# certified-boundary dirty fixture\n")
        if dirty_kind == "staged":
            _git("-C", str(checkout), "add", "--", relative)

    # Pin the setup to locale-independent porcelain rather than matching Git's
    # human prose.  The production read must use the same fixed/sanitized Git
    # authority even after the caller supplies a hostile ambient repository.
    status = _git(
        "-C", str(checkout), "status", "--porcelain=v1",
        "--untracked-files=all", "--", relative,
    ).stdout
    assert status == f"{prefix}{relative}\n"
    for name, value in HOSTILE_GIT_ENV.items():
        monkeypatch.setenv(name, value)

    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")
    with pytest.raises(PAGE.PublicationEvidenceError):
        evidence = CERT_FIX._load_strict(paths)
        PAGE.build(out, publication=evidence)

    assert out.read_text(encoding="utf-8") == "LAST GOOD"


def _normalized_special_gap(kind: str) -> dict:
    gap = copy.deepcopy(GAP_FIX._gap_record())
    row = gap["rows"][0]
    if kind == "watch-only":
        row.update(
            integrated=False,
            pin=None,
            sync_lag=None,
            sync_lag_at_merge=None,
            release_lag=None,
            image_behind=None,
            ours_unshipped=None,
            ours_unshipped_substantive=None,
            tip_state=None,
            tip_note=None,
        )
        row.pop("merge_ref_seen")
        gap["q2_forks_not_built_from_ours"] = [row["tool"]]
    elif kind == "contents-assertion":
        asserted = "d" * 64
        row.update(
            kind="contents_assertion",
            asserted_contents=asserted,
            pin=None,
            sync_lag=None,
            sync_lag_at_merge=None,
            release_lag=None,
            image_behind=None,
            ours_unshipped=None,
            ours_unshipped_substantive=None,
            tip_state=None,
            tip_note=None,
        )
        row.pop("merge_ref_seen")
        gap["assertions"] = [{"tool": row["tool"], "contents": asserted}]
    else:  # pragma: no cover - parametrization owns the closed set
        raise AssertionError(kind)
    return gap


def _special_row_fixture(tmp_path: Path, monkeypatch, kind: str):
    checkout = _clean_checkout(tmp_path, monkeypatch)
    paths = _strict_paths(
        tmp_path, checkout, gap=_normalized_special_gap(kind))
    evidence = CERT_FIX._load_strict(paths)
    state = CERT_FIX._page_state(tmp_path, monkeypatch)
    gap_row = evidence.gap["rows"][0]
    ledger_path = state / "ledger" / f"{gap_row['tool']}.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    pin = (gap_row["asserted_contents"]
           if kind == "contents-assertion" else gap_row["pin"])
    ledger.update(
        integrated=gap_row["integrated"],
        ahead=gap_row["ahead"],
        pin_kind=(gap_row["kind"]
                  if kind == "contents-assertion" else None),
        pinned_ref=pin,
        pinned_ref_full=pin,
        behind_commits=gap_row["image_behind"],
        sync_lag=gap_row["sync_lag"],
        release_lag=gap_row["release_lag"],
        ours_unshipped=gap_row["ours_unshipped"],
        ours_unshipped_substantive=gap_row[
            "ours_unshipped_substantive"],
    )
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    # Prove the fixture itself is certifiable before changing exactly one field.
    PAGE.build(tmp_path / "control.html", publication=evidence)
    return evidence, ledger_path


HEALTH_MUTATIONS = [
    pytest.param("behind_commits", 77, id="behind-77"),
    pytest.param("sync_lag", 99, id="sync-99"),
    pytest.param("release_lag", 88, id="release-88"),
    pytest.param("ours_unshipped", 66, id="ours-unshipped-66"),
    pytest.param(
        "ours_unshipped_substantive", 66, id="ours-substantive-66"),
    pytest.param("ahead", 77, id="ahead-77"),
    pytest.param("integrated", "flip", id="integrated"),
    pytest.param("pin_kind", "wrong-kind", id="pin-kind"),
    pytest.param("pinned_ref_full", "f" * 40, id="pin-identity"),
]


@pytest.mark.parametrize("kind", ["watch-only", "contents-assertion"])
@pytest.mark.parametrize("field,replacement", HEALTH_MUTATIONS)
def test_nonintegrated_health_fields_cannot_diverge_from_normalized_gap_row(
        tmp_path, monkeypatch, kind, field, replacement):
    evidence, ledger_path = _special_row_fixture(tmp_path, monkeypatch, kind)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if replacement == "flip":
        replacement = not ledger[field]
    elif replacement == "wrong-kind":
        replacement = ("pin" if ledger[field] == "contents_assertion"
                       else "contents_assertion")
    ledger[field] = replacement
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")

    match = (re.escape(field)
             if kind == "contents-assertion"
             or field not in {"pin_kind", "pinned_ref_full"}
             else "watch-only row carries a pin identity")
    with pytest.raises(PAGE.PublicationEvidenceError, match=match):
        PAGE.build(out, publication=evidence)

    assert out.read_text(encoding="utf-8") == "LAST GOOD"


# #80 — THE INVARIANT IS UNCHANGED AND THE MECHANISM IS NOT. A certified page
# must never count an image it cannot bind to the release. It used to satisfy
# that by refusing to count at all, which is why the page read "37 · ?" from the
# moment the certified path started working. It now satisfies it by BINDING:
# re-measure the registry tag the way the release did, compare with the identity
# the LL recorded, and count `image@sha256:…`. Both scenarios below are the
# reasons that binding can fail, and both must still end at `NTOOLS = null`.
CERTIFIED_MANIFEST = {"schemaVersion": 2, "layers": [{"size": 1}]}
CERTIFIED_IDENTITY = hashlib.sha256(
    json.dumps(CERTIFIED_MANIFEST, sort_keys=True, separators=(",", ":"),
               ensure_ascii=False).encode("utf-8")).hexdigest()
CERTIFIED_DIGEST = "sha256:" + "c" * 64


@pytest.mark.parametrize("scenario", ["released-absent", "identity-mismatch"])
def test_certified_inventory_never_uses_an_unbound_local_image(
        tmp_path, monkeypatch, scenario):
    checkout = _clean_checkout(tmp_path, monkeypatch)
    paths = _strict_paths(tmp_path, checkout)
    evidence = CERT_FIX._load_strict(paths)
    CERT_FIX._page_state(tmp_path, monkeypatch)
    monkeypatch.setattr(PAGE, "_image_ref", REAL_IMAGE_REF)
    monkeypatch.delenv("GK_INVENTORY_IMAGE", raising=False)

    version = evidence.ll["release"]["target"]
    image = PAGE.release_program.RELEASE_IMAGE
    pinned = f"{image}@{CERTIFIED_DIGEST}"
    if scenario == "released-absent":
        # The registry answer IS the recorded image, so the binding succeeds and
        # the only thing left to fail is the local presence of those bits.
        evidence.ll["release"]["remote_publication"]["manifests"]["version"][
            "identity"] = CERTIFIED_IDENTITY

    real_run = subprocess.run

    def fake_docker(argv, *args, **kwargs):
        if argv and argv[0] == "/usr/bin/git":
            return real_run(argv, *args, **kwargs)
        if argv[:3] == ["docker", "manifest", "inspect"]:
            assert f"{image}:{version}" in argv, argv
            body = json.dumps([{"Descriptor": {"digest": CERTIFIED_DIGEST},
                                "OCIManifest": CERTIFIED_MANIFEST}])
            return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")
        if argv[:3] == ["docker", "image", "inspect"]:
            absent = scenario == "released-absent"
            return subprocess.CompletedProcess(
                argv, 1 if absent else 0, stdout="[]",
                stderr="fixture: image absent" if absent else "")
        raise AssertionError(f"unexpected command {argv}")

    measured_refs: list[str] = []

    def fake_collect(ref: str):
        measured_refs.append(ref)
        return {"a": [{"dir": f"local-tool-{index}"} for index in range(59)]}

    monkeypatch.setattr(PAGE.subprocess, "run", fake_docker)
    monkeypatch.setattr(PAGE.inventory, "collect", fake_collect)
    out = tmp_path / "eda-forks.html"
    out.write_text("LAST GOOD", encoding="utf-8")

    try:
        PAGE.build(out, publication=evidence)
    except PAGE.PublicationEvidenceError:
        assert out.read_text(encoding="utf-8") == "LAST GOOD"
    else:
        html = out.read_text(encoding="utf-8")
        assert "const NTOOLS = null;" in html
        assert "const NTOOLS = 59;" not in html

    # Never a tag, and never the ambient `:latest` the old refusal was written
    # about — a certified count is taken from the digest or not at all.
    assert all(ref == pinned for ref in measured_refs), measured_refs
    assert not any(":latest" in ref for ref in measured_refs), measured_refs
