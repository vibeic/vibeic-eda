#!/usr/bin/env python3
"""Discover hash-pinned LLVM payloads for the supported OpenROAD Bazel layout.

No Starlark is executed and no LLVM payload is downloaded here.  Unknown
selection/override layouts fail closed instead of silently prefetching an old
compiler while Bazel downloads the real one outside the resumable transport.
"""

import argparse
import ast
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import re
import tarfile
import urllib.parse
from pathlib import Path

from release_download import download


REGISTRY = "https://bcr.bazel.build/modules/llvm/"
MAX_METADATA_BYTES = 16 * 1024 * 1024


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("missing or invalid SHA256")
    return value


def _sri(value):
    if not isinstance(value, str) or not value.startswith("sha256-"):
        raise ValueError("only SHA256 integrity is supported")
    return _sha(base64.b64decode(value[7:], validate=True).hex())


def _url(value, *, payload=False):
    if not isinstance(value, str):
        raise ValueError("missing URL")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port or parsed.query or parsed.fragment):
        raise ValueError("expected canonical HTTPS URL")
    if payload:
        if not re.fullmatch(
                r"/hermeticbuild/(?:hermetic-llvm|llvm-redist)/releases/download/[^/]+/[A-Za-z0-9][A-Za-z0-9._+-]*",
                parsed.path) or parsed.netloc != "github.com":
            raise ValueError("unsupported LLVM release URL")
    elif parsed.netloc != "bcr.bazel.build" and parsed.netloc != "github.com":
        raise ValueError("unsupported metadata origin")
    return value


def _fetch(url, digest, scratch):
    _url(url)
    _sha(digest)
    path = scratch / digest
    download(url, digest, path, attempts=3, max_time=240)
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("LLVM metadata exceeds size bound")
    return path.read_bytes()


def _keywords(call):
    if any(keyword.arg is None for keyword in call.keywords):
        raise ValueError("dynamic module keyword arguments are unsupported")
    if len({keyword.arg for keyword in call.keywords}) != len(call.keywords):
        raise ValueError("duplicate module keyword arguments are unsupported")
    return {keyword.arg: keyword.value for keyword in call.keywords}


def _literal(node):
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        raise ValueError("LLVM selection must be a literal string")
    return node.value


def _root_version(text):
    tree = ast.parse(text)
    versions = []
    top_calls = {id(node.value) for node in tree.body if isinstance(node, ast.Expr)}
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        name = call.func.id if isinstance(call.func, ast.Name) else ""
        if name == "include":
            raise ValueError("included MODULE files are unsupported")
        if name in {"bazel_dep", "single_version_override", "multiple_version_override",
                    "archive_override", "git_override", "local_path_override"}:
            kwargs = _keywords(call)
            module = kwargs.get("name" if name == "bazel_dep" else "module_name")
            module_name = _literal(module)
            if module_name != "llvm":
                continue
            if name != "bazel_dep" or id(call) not in top_calls:
                raise ValueError("LLVM module overrides/dynamic declarations are unsupported")
            if "repo_name" in kwargs and _literal(kwargs["repo_name"]) != "llvm":
                raise ValueError("LLVM repo_name aliases are unsupported")
            versions.append(_literal(kwargs.get("version")))
        if name == "use_extension":
            label = _literal(call.args[0] if call.args else _keywords(call).get("extension_bzl_file"))
            if label.startswith("@llvm//") and label.rsplit(":", 1)[-1] in {
                    "llvm.bzl", "llvm_toolchain_minimal.bzl"}:
                raise ValueError("root LLVM source/version/index extension overrides are unsupported")
    if len(versions) != 1 or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]*", versions[0]):
        raise ValueError("expected exactly one literal LLVM module dependency")
    return versions[0]


def _member(bundle, name):
    matches = [member for member in bundle.getmembers() if member.name == name]
    if len(matches) != 1 or not matches[0].isfile() or matches[0].size > MAX_METADATA_BYTES:
        raise ValueError(f"missing, duplicate or unsafe LLVM archive member: {name}")
    return bundle.extractfile(matches[0]).read()


def _llvm_version(module):
    tree = ast.parse(module.decode())
    values = [node.value for node in ast.walk(tree)
              if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "LLVM_VERSION"
                      for target in node.targets)]
    if len(values) != 1:
        raise ValueError("ambiguous LLVM_VERSION assignment")
    version = _literal(values[0])
    tags = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "llvm" and node.func.attr == "version"]
    if len(tags) != 1:
        raise ValueError("unsupported LLVM version tag")
    value = _keywords(tags[0]).get("llvm_version")
    if not isinstance(value, ast.Name) or value.id != "LLVM_VERSION":
        raise ValueError("LLVM version tag does not use LLVM_VERSION")
    index_tags = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Attribute) or not isinstance(call.func.value, ast.Name):
            continue
        owner, name = call.func.value.id, call.func.attr
        if owner == "llvm" and name in {"from_path", "from_git", "from_archive"}:
            raise ValueError("module LLVM source overrides are unsupported")
        if owner == "llvm_toolchain_minimal" and name == "index":
            index_tags.append(_literal(_keywords(call).get("file")))
    if index_tags != ["//extensions:llvm_toolchain_minimal_index.json"]:
        raise ValueError("module LLVM prebuilt index selection is unsupported")
    return version


def _manifest(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("LLVM manifest must contain exactly two payloads")
    selected = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"url", "sha256"}:
            raise ValueError("LLVM manifest entries require only url and sha256")
        selected.append({"url": _url(item["url"], payload=True), "sha256": _sha(item["sha256"])})
    names = [Path(urllib.parse.urlsplit(item["url"]).path).name for item in selected]
    if len(set(names)) != 2 or len({item["sha256"] for item in selected}) != 2:
        raise ValueError("LLVM manifest payloads must be unique")
    return selected


def discover(source_dir, scratch_dir):
    """Return [{url, sha256}, ...] for LLVM source and Linux amd64 seed."""
    source_dir, scratch = Path(source_dir), Path(scratch_dir)
    version = _root_version((source_dir / "MODULE.bazel").read_text())
    lock = json.loads((source_dir / "MODULE.bazel.lock").read_text())
    hashes = lock["registryFileHashes"]
    registry = REGISTRY + version + "/"
    candidates = [url for url in hashes if re.fullmatch(
        r"https://[^/]+/modules/llvm/[^/]+/source\.json", url)]
    if candidates != [registry + "source.json"]:
        raise ValueError("ambiguous or overridden LLVM registry selection")
    scratch.mkdir(parents=True, exist_ok=True)
    descriptor = json.loads(_fetch(registry + "source.json", hashes[registry + "source.json"], scratch))
    module = _fetch(registry + "MODULE.bazel", hashes[registry + "MODULE.bazel"], scratch)
    archive_url = _url(descriptor["url"], payload=True)
    archive = _fetch(archive_url, _sri(descriptor["integrity"]), scratch)
    prefix = descriptor["strip_prefix"]
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9._+-]+", prefix):
        raise ValueError("unsafe LLVM archive prefix")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        archive_module = _member(bundle, prefix + "/MODULE.bazel")
        sources = json.loads(_member(bundle, prefix + "/llvm_versions.json"))
        index = json.loads(_member(bundle, prefix + "/extensions/llvm_toolchain_minimal_index.json"))
    patches = descriptor.get("patches", {})
    if patches:
        if set(patches) != {"module_dot_bazel_version.patch"} or descriptor.get("patch_strip") != 1:
            raise ValueError("unsupported LLVM registry patches")
        patch = _fetch(registry + "patches/module_dot_bazel_version.patch",
                       _sri(patches["module_dot_bazel_version.patch"]), scratch).decode()
        removed = [line[1:] for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")]
        added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
        if (removed != ['    version = "0.0.0",'] or added != [f'    version = "{version}",']
                or [line for line in patch.splitlines() if line.startswith("--- ")] != ["--- a/MODULE.bazel"]
                or [line for line in patch.splitlines() if line.startswith("+++ ")] != ["+++ b/MODULE.bazel"]):
            raise ValueError("registry patch changes more than the module version")
        for line in patch.splitlines():
            if not (line.startswith((" ", "--- ", "+++ ", "@@ "))
                    or line == "-" + removed[0] or line == "+" + added[0]
                    or (line and set(line) == {"="})):
                raise ValueError("unsupported registry patch syntax")
        archive_module = archive_module.replace((removed[0] + "\n").encode(),
                                                (added[0] + "\n").encode(), 1)
    if archive_module != module:
        raise ValueError("registry MODULE differs from verified patched archive")
    llvm_version = _llvm_version(module)
    release = index["latest_by_llvm_version"].get(llvm_version)
    if release is None:
        release = index["latest_by_llvm_version"].get(index.get("default_prebuilt_llvm_version"))
    if release is None:
        raise ValueError("LLVM has no indexed minimal prebuilt release")
    selected = [sources[llvm_version], index["releases"][release]["linux-amd64-musl"]]
    return _manifest([{"url": item["url"], "sha256": item["sha256"]} for item in selected])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", nargs="?")
    parser.add_argument("scratch_dir", nargs="?")
    parser.add_argument("--manifest", type=Path, help="use the prior hash-verified discovery stage's JSON")
    parser.add_argument("--distdir", type=Path, help="download both verified payloads for Bazel")
    args = parser.parse_args()
    if args.manifest:
        if args.source_dir or args.scratch_dir or not args.distdir:
            parser.error("--manifest requires --distdir and excludes source_dir/scratch_dir")
        if args.manifest.stat().st_size > 65536:
            raise ValueError("LLVM manifest exceeds size bound")
        selected = _manifest(json.loads(args.manifest.read_text()))
    else:
        if not args.source_dir or not args.scratch_dir:
            parser.error("source_dir and scratch_dir are required without --manifest")
        selected = discover(args.source_dir, args.scratch_dir)
    if args.distdir:
        basenames = [Path(urllib.parse.urlsplit(item["url"]).path).name for item in selected]
        if len(set(basenames)) != len(basenames):
            raise ValueError("LLVM distdir basename collision")
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(download, item["url"], item["sha256"], args.distdir / name)
                     for item, name in zip(selected, basenames)]
            for task in tasks:
                task.result()
    print(json.dumps(selected, indent=2))


if __name__ == "__main__":
    main()
