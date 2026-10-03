"""The provenance guard must be asked about every tool the image records.

vibeic-eda#117 finding 5. `tools/check_image_provenance.py` exists to answer one
question the smoke tests cannot: does the built image contain the COMMITS the
pins name, or did a moved tag / a dropped COPY quietly ship somebody else's
binary? It answers that question ONLY for the tools listed in its `PRIMARY`
map, and nothing kept that map in step with the Dockerfiles.

Two instances were MEASURED on 3187811, both silent:

  * The stage-8 verification toolchain — cocotb, cocotb-coverage, pyuvm, sby —
    is cloned by SHA in the `tb-src` stage and pip/make-installed into the
    runtime. It wrote no provenance record at all, so `sby` in the image could
    have been built from any commit and the guard had nothing to compare.
  * `fastercap` DID write a record and the collector DID gather it, so the
    image carries `/vibeic/provenance/fastercap.json` — and `PRIMARY` never
    named it, so the file was carried and never read.

Those are the two halves of the same failure: a record with no reader, and a
tool with no record. Both look identical from outside — `check_image_provenance`
prints a confident PASS either way, because it reports on the tools it was told
about and cannot report on the ones it was not.

WHAT THIS PINS. Four independent facts about the same set, derived from the
Dockerfiles rather than restated:

  EMITTED    every `> /vibeic/provenance/<tool>.json` any Dockerfile writes
  CLONED     every source `tb-src` clones by SHA into /tb and the runtime installs
  COLLECTED  every record the `provenance` collector stage gathers
  PRIMARY    every record the checker actually opens

They must be the same set. Add a tool to one and not the others and this fails
by NAME, which is the property that was missing: each of the four is edited in a
different file, by a different person, for a different reason.

Deliberately derived with patterns that know nothing about the checker's own —
the checker's regexes are what was blind, so reusing them would reproduce the
blindness. The one thing imported from it is the map under test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CHECKER = ROOT / "tools" / "check_image_provenance.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_image_provenance", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_image_provenance"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()

SHA = re.compile(r"^[0-9a-f]{40}$")
#: `> /vibeic/provenance/<tool>.json`, i.e. a record being WRITTEN. The `COPY`
#: sites name the same path and must not be counted as emitters.
EMIT = re.compile(r">+\s*/vibeic/provenance/([A-Za-z0-9_.-]+)\.json")
#: `git clone <url> /tb/<tool>` — the stage-8 sources, which have no
#: `tools/<tool>/Dockerfile` of their own and are installed straight from source.
CLONE_TB = re.compile(
    r"git\s+clone\s+(?:--\S+\s+)*(\S+)\s+/tb/([A-Za-z0-9_.-]+)")
COLLECT = re.compile(
    r"^\s*COPY\s+--from=\S+\s+/vibeic/provenance/([A-Za-z0-9_.-]+)\.json", re.M)
ARG_ANY = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)\s*=\s*(\S+)", re.M)


def _uncommented(p: Path) -> str:
    """A path inside a `#` comment documents history; it emits nothing."""
    return "\n".join("" if ln.lstrip().startswith("#") else ln
                     for ln in p.read_text(encoding="utf-8", errors="replace").split("\n"))


def _dockerfiles() -> list[Path]:
    return [ROOT / "Dockerfile"] + sorted(ROOT.glob("tools/*/Dockerfile"))


def _emitted() -> set[str]:
    out: set[str] = set()
    for p in _dockerfiles():
        if p.is_file():
            out |= set(EMIT.findall(_uncommented(p)))
    return out


def _cloned_into_tb() -> dict[str, str]:
    """tool -> the clone URL, for the sources installed from /opt/vibeic-forks."""
    return {tool: url for url, tool in CLONE_TB.findall(_uncommented(ROOT / "Dockerfile"))}


def _collected() -> set[str]:
    """The records the `provenance` scratch stage gathers into one layer."""
    text = _uncommented(ROOT / "Dockerfile")
    start = re.search(r"^\s*FROM\s+\S+\s+AS\s+provenance\s*$", text, re.M | re.I)
    assert start, ("no `FROM ... AS provenance` collector stage in the root "
                   "Dockerfile — this test is measuring nothing")
    rest = text[start.end():]
    end = re.search(r"^\s*FROM\s", rest, re.M | re.I)
    return set(COLLECT.findall(rest[:end.start()] if end else rest))


def test_the_measurement_is_not_vacuous():
    """A parser that silently matches nothing would make every set equal.

    Floors, not counts: this must not have to be edited when a tool is added or
    retired, or it becomes the thing people edit to make the suite green.
    """
    assert len(_emitted()) >= 10, _emitted()
    assert len(_collected()) >= 10, _collected()
    assert len(_cloned_into_tb()) >= 4, _cloned_into_tb()
    assert len(M.PRIMARY) >= 10, sorted(M.PRIMARY)


def test_every_record_written_is_gathered_into_the_image():
    emitted, collected = _emitted(), _collected()
    assert emitted == collected, (
        "the records written and the records the collector gathers disagree.\n"
        f"  written but never gathered (never reaches the image): {sorted(emitted - collected)}\n"
        f"  gathered but never written (the COPY will fail): {sorted(collected - emitted)}")


def test_the_checker_reads_every_record_the_image_carries():
    collected, primary = _collected(), set(M.PRIMARY)
    assert collected == primary, (
        "check_image_provenance.PRIMARY and the image's provenance directory "
        "disagree, so the guard is silent about a tool it could check.\n"
        f"  in the image, never read by the checker: {sorted(collected - primary)}\n"
        f"  read by the checker, never in the image: {sorted(primary - collected)}")


def test_every_source_cloned_by_sha_records_where_it_came_from():
    """The stage-8 half of finding 5, stated as the rule rather than the four names.

    A source cloned at a pinned SHA and installed into the runtime is exactly
    the case provenance exists for: the pin is the only claim about what shipped,
    and a pin is a line in a file, not a fact about the image.
    """
    cloned, primary = set(_cloned_into_tb()), set(M.PRIMARY)
    assert cloned <= primary, (
        "these are cloned by SHA and installed into the image, and the "
        f"provenance guard cannot see them: {sorted(cloned - primary)}")


def test_a_record_names_the_same_repo_the_clone_used():
    """The record must not be able to drift away from the clone beside it.

    Both are written from the same `ARG *_REPO`, so this fails the moment one of
    the two is edited alone — a record naming `vibeic/sby` while the clone was
    repointed elsewhere is a provenance file that certifies the wrong thing.
    """
    text = _uncommented(ROOT / "Dockerfile")
    args = dict(ARG_ANY.findall(text))

    def expand(v: str) -> str:
        for _ in range(4):
            m = re.search(r"\$\{([A-Za-z0-9_]+)\}|\$([A-Za-z0-9_]+)", v)
            if not m:
                break
            v = v[:m.start()] + args.get(m.group(1) or m.group(2), "") + v[m.end():]
        return v

    records = {}
    for m in re.finditer(r'"tool":"([A-Za-z0-9_.-]+)","repo":"%s","ref":"%s"', text):
        # The repo is the first shell word the printf is given after the format.
        v = re.search(r'"(\$\{?[A-Za-z0-9_]+\}?)"', text[m.end():m.end() + 400])
        if v:
            records[m.group(1)] = v.group(1)
    bad = []
    for tool, url in _cloned_into_tb().items():
        got = records.get(tool)
        assert got, f"{tool}: cloned into /tb but no provenance record is written for it"
        if expand(got) != expand(url):
            bad.append(f"{tool}: record says {expand(got)!r}, clone used {expand(url)!r}")
    assert not bad, bad


def test_every_mapped_arg_exists_and_is_a_commit():
    """The map names an ARG; if that ARG is renamed the checker compares to ''.

    `want` would then be the empty string and every image would look wrong — or,
    worse for a tool whose record is also absent, the two nothings would agree.
    """
    bad = []
    for tool, key in sorted(M.PRIMARY.items()):
        f = M.pin_file(tool)
        if not f.is_file():
            bad.append(f"{tool}: {f} does not exist, so its pin cannot be read")
            continue
        val = dict(ARG_ANY.findall(f.read_text(encoding="utf-8", errors="replace"))
                   ).get(key, "").split("#")[0].strip()
        if not SHA.match(val):
            bad.append(f"{tool}: ARG {key} in {f.name} is {val!r}, not a commit")
    assert not bad, bad


def test_the_collected_layer_reaches_the_final_image():
    text = _uncommented(ROOT / "Dockerfile")
    assert re.search(r"^\s*COPY\s+--from=provenance\s+/vibeic/provenance\s+/vibeic/provenance",
                     text, re.M), (
        "nothing copies the collector stage into the final image, so every "
        "record is written, gathered, and thrown away")


def _image_reader(monkeypatch, records):
    """Exercise main's verdict using image reads, including absent records."""
    def run(*cmd):
        if cmd[:3] == ("docker", "image", "inspect"):
            return 0, "{}"
        if cmd[:2] == ("docker", "run"):
            return 0, "test-container"
        if cmd[:2] == ("docker", "rm"):
            return 0, ""
        if cmd[:2] == ("docker", "exec"):
            name = Path(cmd[-1]).stem
            if name not in records:
                return 1, "No such file"
            return 0, json.dumps(records[name])
        raise AssertionError(cmd)
    monkeypatch.setattr(M, "run", run)
    monkeypatch.setattr(M, "_pending", lambda: {})


@pytest.fixture
def image_case(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("ARG UNIT_REF=" + "a" * 40 + "\n")
    monkeypatch.setattr(M, "PRIMARY", {"unit": "UNIT_REF"})
    monkeypatch.setattr(M, "pin_file", lambda _tool: dockerfile)
    monkeypatch.setattr(M, "pinned_forks", lambda: {"unit": "a" * 40,
                                                  "dependency": "b" * 40})
    records = {"unit": {"tool": "unit", "repo": "https://github.com/vibeic/unit.git",
                        "ref": "a" * 40,
                        "dependency_repo": "https://github.com/vibeic/dependency.git",
                        "dependency_ref": "b" * 40}}
    _image_reader(monkeypatch, records)
    return records


@pytest.mark.parametrize("shape", ["legacy", "also"])
def test_dependency_commit_mismatch_is_a_failed_verdict(image_case, shape, capsys):
    record = image_case["unit"]
    if shape == "legacy":
        record["dependency_ref"] = "c" * 40
    else:
        del record["dependency_repo"], record["dependency_ref"]
        record["also"] = {"dependency": "c" * 40}
    assert M.main(["test-image"]) == 1
    assert "dependency" in capsys.readouterr().err


@pytest.mark.parametrize("field,value", [
    ("dependency_repo", "https://github.com/somewhere/dependency.git"),
    ("dependency_repo", "https://github.com/vibeic/unpinned.git"),
    ("dependency_ref", ""),
    ("also", {"dependency": None}),
    ("also", []),
])
def test_invalid_dependency_never_certifies_the_image(image_case, field, value):
    image_case["unit"][field] = value
    assert M.main(["test-image"]) == 1


def test_unrecorded_pinned_dependency_is_a_failed_verdict(image_case, capsys):
    del image_case["unit"]["dependency_repo"], image_case["unit"]["dependency_ref"]
    assert M.main(["test-image"]) == 1
    assert "dependency" in capsys.readouterr().err


@pytest.mark.parametrize("shape", ["legacy", "also"])
def test_correct_dependency_is_verified(image_case, shape, capsys):
    record = image_case["unit"]
    if shape == "also":
        del record["dependency_repo"], record["dependency_ref"]
        record["also"] = {"dependency": "b" * 40}
    assert M.main(["test-image"]) == 0
    assert "2 of 2 pinned fork(s)" in capsys.readouterr().out


def test_unavailable_denominator_cannot_certify_the_image(image_case, monkeypatch):
    def unavailable():
        raise ValueError("pin inventory unreadable")
    monkeypatch.setattr(M, "pinned_forks", unavailable)
    assert M.main(["test-image"]) == 2


def test_actual_pin_inventory_requires_each_secondary_source(monkeypatch):
    """The real checkout supplies the pins and primary mapping under test."""
    pins = M.pinned_forks()
    records, primary_repos = {}, set()
    for tool, arg in M.PRIMARY.items():
        ref = dict(ARG_ANY.findall(M.pin_file(tool).read_text()))[arg]
        repo = next(repo for repo, pin in pins.items() if pin == ref)
        primary_repos.add(repo)
        records[tool] = {"tool": tool, "repo": f"https://github.com/vibeic/{repo}.git",
                         "ref": ref}
    secondary = {repo: ref for repo, ref in pins.items() if repo not in primary_repos}
    assert secondary, "the real multi-source artifacts vanished from this measurement"
    record = next(iter(records.values()))
    record["also"] = secondary.copy()
    _image_reader(monkeypatch, records)
    assert M.main(["test-image"]) == 0
    for repo in secondary:
        record["also"] = {key: value for key, value in secondary.items() if key != repo}
        assert M.main(["test-image"]) == 1, f"unrecorded {repo} was accepted"


def _measured_emitters():
    """RUN commands in the real recipes that measure Git for their records."""
    for path in _dockerfiles():
        for line in _uncommented(path).replace("\\\n", " ").splitlines():
            if line.startswith("RUN ") and EMIT.search(line) and "git -C " in line:
                yield EMIT.search(line).group(1), line[4:]


@pytest.mark.parametrize("tool,command", list(_measured_emitters()),
                         ids=lambda value: value if len(value) < 60 else "recipe")
def test_recipe_emits_actual_checkout_identity(tmp_path, tool, command):
    """Execute each real emitter with independent Git repos, not pin-shaped data.

    Only filesystem paths are relocated; the printf and Git measurements are
    the exact Dockerfile RUN. A wrong requested pin must stop before emission.
    """
    source_paths = sorted(set(re.findall(r"git -C (/[^\s)]+)", command)))
    assert source_paths, tool
    expected, measured, relocated = set(), {}, {}
    for index, source in enumerate(source_paths):
        checkout = tmp_path / f"source-{index}"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-q",
                        "--allow-empty", "-m", f"source-{index}"], check=True)
        origin = f"https://github.com/vibeic/source-{index}.git"
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", origin], check=True)
        ref = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
        expected.add((origin, ref))
        measured[source], relocated[source] = ref, str(checkout)
    variables = {name: measured[source] for name, source in re.findall(
        r"([a-z_]+)=\$\(git -C (\S+) rev-parse HEAD\)", command)}
    checks = re.findall(r'test "\$([a-z_]+)" = "\$\{([A-Z0-9_]+)\}"', command)
    env = os.environ.copy()
    env.update({arg: variables[name] for name, arg in checks})
    output = tmp_path / "provenance"
    command = command.replace("/vibeic/provenance", str(output))
    for source in sorted(relocated, key=len, reverse=True):
        command = command.replace("git -C " + source + " ",
                                  "git -C " + relocated[source] + " ")
    result = subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    record_path = output / f"{tool}.json"
    record = json.loads(record_path.read_text())
    assert {(repo, ref) for _, repo, ref in M.record_sources(record)} == expected
    assert record["tool"] == tool
    if checks:
        record_path.unlink()
        env[checks[0][1]] = "f" * 40
        result = subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True)
        assert result.returncode != 0, f"{tool} accepted a different checkout than requested"
        assert not record_path.exists(), f"{tool} attested a requested pin it did not check out"
