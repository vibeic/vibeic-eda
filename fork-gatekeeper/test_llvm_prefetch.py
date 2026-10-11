"""Offline descriptor-chain tests; these never download compiler payloads."""

import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools" / "openroad"
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location("llvm_prefetch", TOOLS / "llvm_prefetch.py")
prefetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prefetch)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sri(data):
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


class Fixture:
    def __init__(self, tmp_path, monkeypatch, llvm="23.1.2", version="0.8.24"):
        self.source = tmp_path / "source"
        self.source.mkdir()
        self.scratch = tmp_path / "metadata"
        self.version = version
        self.llvm = llvm
        self.registry = prefetch.REGISTRY + version + "/"
        self.prefix = "hermetic-llvm-" + version
        self.archive_url = "https://github.com/hermeticbuild/hermetic-llvm/releases/download/v" + version + "/hermetic-llvm-v" + version + ".tar.gz"
        self.module = ('module(\n    name = "llvm",\n    version = "0.0.0",\n)\n'
                       'LLVM_VERSION = "' + llvm + '"\n'
                       'llvm = use_extension("//extensions:llvm.bzl", "llvm")\n'
                       'llvm.version(llvm_version = LLVM_VERSION)\n'
                       'llvm_toolchain_minimal.index(file="//extensions:llvm_toolchain_minimal_index.json")\n')
        self.patch = ('--- a/MODULE.bazel\n+++ b/MODULE.bazel\n@@ -1,4 +1,4 @@\n'
                      ' module(\n     name = "llvm",\n-    version = "0.0.0",\n'
                      '+    version = "' + version + '",\n )\n')
        self.selected = [
            {"url": "https://github.com/hermeticbuild/llvm-redist/releases/download/llvmorg-" + llvm + "-r1/llvm-project-" + llvm + ".src.tar.zst", "sha256": "1" * 64},
            {"url": "https://github.com/hermeticbuild/hermetic-llvm/releases/download/llvm-" + llvm + "-1/llvm-toolchain-minimal-" + llvm + "-linux-amd64-musl.tar.zst", "sha256": "2" * 64},
        ]
        self.sources = {llvm: self.selected[0]}
        self.index = {"latest_by_llvm_version": {llvm: "seed"},
                      "default_prebuilt_llvm_version": llvm,
                      "releases": {"seed": {"linux-amd64-musl": self.selected[1]}}}
        self.root = 'bazel_dep(name = "llvm", version = "' + version + '", dev_dependency = True)\n'
        self.responses = {}
        self.calls = []
        self.extra_members = []
        self.extra_patches = {}
        self.rebuild()

        def fake_download(url, digest, dest, **kwargs):
            self.calls.append(url)
            data = self.responses[url]
            if sha(data) != digest:
                raise ValueError("SHA256 mismatch")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return dest

        monkeypatch.setattr(prefetch, "download", fake_download)

    def rebuild(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            members = [("MODULE.bazel", self.module.encode()),
                       ("llvm_versions.json", json.dumps(self.sources).encode()),
                       ("extensions/llvm_toolchain_minimal_index.json", json.dumps(self.index).encode())]
            for name, data in members + self.extra_members:
                info = tarfile.TarInfo(self.prefix + "/" + name)
                info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
        registry_module = self.module.replace('version = "0.0.0"', 'version = "' + self.version + '"', 1).encode()
        descriptor = {"url": self.archive_url, "integrity": sri(archive.getvalue()),
                      "strip_prefix": self.prefix, "patch_strip": 1,
                      "patches": {"module_dot_bazel_version.patch": sri(self.patch.encode()), **self.extra_patches}}
        self.responses = {self.archive_url: archive.getvalue(),
                          self.registry + "MODULE.bazel": registry_module,
                          self.registry + "source.json": json.dumps(descriptor).encode(),
                          self.registry + "patches/module_dot_bazel_version.patch": self.patch.encode()}
        hashes = {url: sha(data) for url, data in self.responses.items() if url.endswith(("source.json", "MODULE.bazel"))}
        # Historical resolution candidates are not the selected module.
        hashes[prefetch.REGISTRY + "0.8.11/MODULE.bazel"] = "a" * 64
        (self.source / "MODULE.bazel").write_text(self.root)
        (self.source / "MODULE.bazel.lock").write_text(json.dumps({"registryFileHashes": hashes}))

    def run(self):
        return prefetch.discover(self.source, self.scratch)


@pytest.mark.parametrize("llvm,version", [("22.1.8", "0.8.18"), ("23.1.2", "0.8.24")])
def test_discovers_both_payloads_and_never_downloads_them(tmp_path, monkeypatch, llvm, version):
    fixture = Fixture(tmp_path, monkeypatch, llvm, version)
    assert fixture.run() == fixture.selected
    assert not set(fixture.calls) & {item["url"] for item in fixture.selected}


@pytest.mark.parametrize("override", [
    'single_version_override(module_name="llvm", version="9.0")',
    'archive_override(module_name="llvm", urls=["https://example.org/x"])',
    'git_override(module_name="llvm", remote="https://example.org/x")',
    'local_path_override(module_name="llvm", path="/tmp/x")',
    'multiple_version_override(module_name="llvm", versions=["0.8.24"])',
    'x = use_extension("@llvm//extensions:llvm.bzl", "llvm")',
    'x = use_extension("@llvm//extensions:llvm_toolchain_minimal.bzl", "llvm_toolchain_minimal")',
    'x = use_extension(extension_bzl_file="@llvm//extensions:llvm.bzl", extension_name="llvm")',
    'include("//:more.MODULE.bazel")',
])
def test_refuses_unsupported_root_overrides_before_network(tmp_path, monkeypatch, override):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.root += override + "\n"
    fixture.rebuild()
    with pytest.raises(ValueError):
        fixture.run()
    assert not fixture.calls


def test_ignores_comments_and_unrelated_override(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.root += '# bazel_dep(name="llvm", version="bad")\n'
    fixture.root += 'single_version_override(module_name="bison", version="3.0")\n'
    fixture.root += 'osx = use_extension("@llvm//extensions:osx.bzl", "osx")\n'
    fixture.rebuild()
    assert fixture.run() == fixture.selected


def test_rejects_llvm_repo_alias(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.root = fixture.root.replace('dev_dependency = True', 'repo_name = "renamed_llvm"')
    fixture.rebuild()
    with pytest.raises(ValueError, match="repo_name"):
        fixture.run()
    assert not fixture.calls


@pytest.mark.parametrize("extra", [
    'llvm.from_archive(url="https://example.org/other.tar.gz")\n',
    'llvm_toolchain_minimal.index(file="//:custom.json")\n',
    'LLVM_VERSION = "another"\n',
])
def test_rejects_module_selection_changes(tmp_path, monkeypatch, extra):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.module += extra
    fixture.rebuild()
    with pytest.raises(ValueError):
        fixture.run()


@pytest.mark.parametrize("suffix", ["source.json", "MODULE.bazel", "patches/module_dot_bazel_version.patch"])
def test_rejects_metadata_tampering(tmp_path, monkeypatch, suffix):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.responses[fixture.registry + suffix] += b"tampered"
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        fixture.run()


def test_rejects_archive_tampering(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.responses[fixture.archive_url] += b"tampered"
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        fixture.run()


def test_ambiguous_lock_selection(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    path = fixture.source / "MODULE.bazel.lock"
    lock = json.loads(path.read_text())
    lock["registryFileHashes"][prefetch.REGISTRY + "0.8.25/source.json"] = "b" * 64
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="ambiguous"):
        fixture.run()
    assert not fixture.calls


@pytest.mark.parametrize("extra", ["    bazel_compatibility = [],", "GIT binary patch", "diff --git a/llvm_versions.json b/llvm_versions.json"])
def test_registry_patch_may_not_modify_descriptors(tmp_path, monkeypatch, extra):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.patch += "+" + extra + "\n" if extra.startswith(" ") else extra + "\n"
    fixture.rebuild()
    with pytest.raises(ValueError, match="patch"):
        fixture.run()


def test_rejects_unhandled_additional_patch(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.extra_patches = {"descriptor.patch": sri(b"x")}
    fixture.rebuild()
    with pytest.raises(ValueError, match="patches"):
        fixture.run()


def test_prebuilt_fallback_matches_extension(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.index["latest_by_llvm_version"] = {"22.1.8": "seed"}
    fixture.index["default_prebuilt_llvm_version"] = "22.1.8"
    fixture.rebuild()
    assert fixture.run() == fixture.selected


def test_missing_prebuilt_fallback_fails(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.index["latest_by_llvm_version"] = {}
    fixture.rebuild()
    with pytest.raises(ValueError, match="no indexed"):
        fixture.run()


def test_duplicate_archive_member_fails(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.extra_members = [("llvm_versions.json", b"{}")]
    fixture.rebuild()
    with pytest.raises(ValueError, match="duplicate"):
        fixture.run()


@pytest.mark.parametrize("value", ["http://github.com/hermeticbuild/llvm-redist/releases/download/v/a", "https://evil.invalid/data", "https://github.com/hermeticbuild/llvm-redist/releases/download/v/..", "https://github.com/hermeticbuild/llvm-redist/releases/download/v/x%2Fy", "https://github.com/hermeticbuild/llvm-redist/releases/download/v/x?jwt=old"])
def test_untrusted_or_unsafe_payload_url_fails(tmp_path, monkeypatch, value):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.selected[0]["url"] = value
    fixture.rebuild()
    with pytest.raises(ValueError):
        fixture.run()


def test_cli_json_only_does_not_fetch_payloads(tmp_path, monkeypatch, capsys):
    fixture = Fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["llvm_prefetch.py", str(fixture.source), str(fixture.scratch)])
    prefetch.main()
    assert json.loads(capsys.readouterr().out) == fixture.selected
    assert len(fixture.calls) == 4


def test_cli_distdir_waits_for_both_payloads(tmp_path, monkeypatch, capsys):
    fixture = Fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(prefetch, "discover", lambda *_: fixture.selected)
    outputs = []
    monkeypatch.setattr(prefetch, "download", lambda url, digest, dest: outputs.append((url, digest, dest)))
    dest = tmp_path / "distdir"
    monkeypatch.setattr(sys, "argv", ["llvm_prefetch.py", str(fixture.source), str(fixture.scratch), "--distdir", str(dest)])
    prefetch.main()
    assert len(outputs) == 2
    assert all(output[2].parent == dest for output in outputs)
    assert json.loads(capsys.readouterr().out) == fixture.selected


def test_cli_propagates_payload_failure(tmp_path, monkeypatch, capsys):
    fixture = Fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(prefetch, "discover", lambda *_: fixture.selected)
    def fail(*_):
        raise RuntimeError("payload failed")
    monkeypatch.setattr(prefetch, "download", fail)
    monkeypatch.setattr(sys, "argv", ["llvm_prefetch.py", str(fixture.source), str(fixture.scratch), "--distdir", str(tmp_path / "distdir")])
    with pytest.raises(RuntimeError, match="payload failed"):
        prefetch.main()
    assert capsys.readouterr().out == ""


def test_cli_manifest_fetches_without_source_or_metadata(tmp_path, monkeypatch, capsys):
    fixture = Fixture(tmp_path, monkeypatch)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(fixture.selected))
    outputs = []
    monkeypatch.setattr(prefetch, "download", lambda url, digest, dest: outputs.append(dest))
    monkeypatch.setattr(sys, "argv", ["llvm_prefetch.py", "--manifest", str(manifest), "--distdir", str(tmp_path / "distdir")])
    prefetch.main()
    assert len(outputs) == 2
    assert json.loads(capsys.readouterr().out) == fixture.selected
    assert not fixture.calls


@pytest.mark.parametrize("bad", [None, [], [{"url": "x", "sha256": "f" * 64}], [None, None], [{"url": "x"}, {"url": "y"}]])
def test_malformed_manifest_rejected(bad):
    with pytest.raises(ValueError):
        prefetch._manifest(bad)


def test_duplicate_manifest_rejected(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="unique"):
        prefetch._manifest([fixture.selected[0], fixture.selected[0]])


def test_manifest_rejects_extra_fields(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.selected[0]["command"] = "do not execute"
    with pytest.raises(ValueError, match="only url and sha256"):
        prefetch._manifest(fixture.selected)
