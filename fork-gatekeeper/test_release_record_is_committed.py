"""The publish half was durable and the record half was not. vibeic-eda#71.

`docker push` puts the image in a registry, where it stays. `write_released_
record` writes a file in the WORKING TREE, and `daily_release` never shells out
to git — `_sh(["git", ...])` appeared nowhere in the program. So a checkout, a
landing sequence, or anyone tidying a dirty tree discards the record.

OBSERVED: 0.2.57 was built, smoke-tested and pushed, while `VERSION` and
`RELEASED.json` on main both still said 0.2.56 — with a fingerprint that no
longer reproduced, so `check_release_recorded` reported "this pin set has never
been released" about a pin set that had. The registry and the repo disagreed
about what the current release is, and the file's own contract makes that
disagreement actionable: the next tick would cut another version.

MEASURED, running the function against throwaway repositories:

    record + an unrelated pin edit also dirty   committed: True
        committed files: RELEASED.json, VERSION
        still dirty:     Dockerfile              <- the pin edit stays
    nothing changed                             committed: False
    not a git repo                              committed: False, reported
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_spec = importlib.util.spec_from_file_location(
    "daily_release", _HERE / "daily_release.py")
D = importlib.util.module_from_spec(_spec)
sys.modules["daily_release"] = D
try:
    _spec.loader.exec_module(D)
except SystemExit:
    pass


def _repo(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    (d / "VERSION").write_text("0.2.56\n")
    (d / "RELEASED.json").write_text('{"version": "0.2.56"}\n')
    (d / "Dockerfile").write_text("FROM scratch\n")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=d, check=True,
                   capture_output=True)
    return d


def _advance(d, v="0.2.57"):
    (d / "VERSION").write_text(v + "\n")
    (d / "RELEASED.json").write_text('{"version": "%s"}\n' % v)


def _committed_files(d):
    return subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                          cwd=d, capture_output=True, text=True).stdout.split()


def test_the_record_survives_a_checkout(tmp_path):
    """The defect in one assertion: after the call the record is in a COMMIT,
    so `git checkout .` cannot take it."""
    d = _repo(tmp_path)
    _advance(d)
    ok, note = D.commit_release_record(d, "0.2.57")
    assert ok, note
    subprocess.run(["git", "checkout", "--", "."], cwd=d, check=True)
    assert (d / "VERSION").read_text().strip() == "0.2.57"


def test_only_the_two_record_files_are_committed(tmp_path):
    """LOAD-BEARING. A release run's tree also holds the pin edits it made, and
    those are a separate decision that goes through review. Sweeping them in
    would publish an unreviewed pin change under a release commit — and `git add
    -A` is forbidden in this org for exactly this reason."""
    d = _repo(tmp_path)
    _advance(d)
    (d / "Dockerfile").write_text("FROM scratch\nARG UNREVIEWED=1\n")
    ok, _note = D.commit_release_record(d, "0.2.57")
    assert ok
    assert sorted(_committed_files(d)) == ["RELEASED.json", "VERSION"]
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=d,
                           capture_output=True, text=True).stdout
    assert "Dockerfile" in dirty, "the unreviewed pin edit was swept in"


def test_nothing_to_commit_is_not_an_empty_commit(tmp_path):
    """A release that changed no record must not leave a commit behind claiming
    it did."""
    d = _repo(tmp_path)
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                            capture_output=True, text=True).stdout
    ok, note = D.commit_release_record(d, "0.2.56")
    assert ok is False and "nothing to commit" in note
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                           capture_output=True, text=True).stdout
    assert before == after


def test_a_failure_is_reported_and_never_raises(tmp_path):
    """A release that PUBLISHED successfully must not be reported as failed
    because a commit could not be made — the image is in the registry either
    way, and an exception here would lose that fact."""
    d = tmp_path / "not-a-repo"
    d.mkdir()
    (d / "VERSION").write_text("0.2.57\n")
    ok, note = D.commit_release_record(d, "0.2.57")
    assert ok is False and note


def test_it_does_not_push(tmp_path):
    """Whether the record reaches origin is the caller's decision. A push from
    inside the release path would also turn a network failure into a failed
    release."""
    src = (_HERE / "daily_release.py").read_text(encoding="utf-8")
    seg = src[src.index("def commit_release_record"):]
    seg = seg[:seg.index("\ndef ", 10)]
    assert '"push"' not in seg


def test_it_never_uses_add_dash_A():
    src = (_HERE / "daily_release.py").read_text(encoding="utf-8")
    seg = src[src.index("def commit_release_record"):]
    seg = seg[:seg.index("\ndef ", 10)]
    assert '"-A"' not in seg and '"--all"' not in seg


def test_the_publish_path_calls_it_only_when_something_was_pushed():
    """WIRING, and the condition that makes it honest: a LOCAL ONLY build has
    published nothing, so recording it as released would assert a release
    nobody can pull — the same class of false record, pointed the other way.

    ASSERTED STRUCTURALLY, not by distance. This read a 400-CHARACTER window
    after `write_released_record` and required both strings inside it, so
    inserting any statement between them broke a test about guarding — and the
    obvious repair is to widen the window, which weakens it toward "somewhere in
    the file". What matters is that the call sits INSIDE an `if pushed:` block,
    however far it sits from its neighbour.
    """
    import ast
    src = (_HERE / "daily_release.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def _calls(node, name):
        return any(isinstance(n, ast.Call) and (
                       (isinstance(n.func, ast.Name) and n.func.id == name)
                       or (isinstance(n.func, ast.Attribute) and n.func.attr == name))
                   for n in ast.walk(node))

    guarded = [n for n in ast.walk(tree)
               if isinstance(n, ast.If)
               and isinstance(n.test, ast.Name) and n.test.id == "pushed"
               and _calls(n, "commit_release_record")]
    assert guarded, (
        "commit_release_record is not inside an `if pushed:` block — a LOCAL "
        "ONLY build would be recorded as a release nobody can pull")
    # ...and nowhere else: one guarded call site, not a guarded one plus a loose one.
    total = sum(1 for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "commit_release_record")
    assert total == 1, f"expected exactly one call site, found {total}"
