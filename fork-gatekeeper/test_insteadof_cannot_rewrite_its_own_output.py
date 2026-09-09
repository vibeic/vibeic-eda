"""Two `-c` options this closure passes can fight each other.

MEASURED 2026-09-09, and it cost a round. Materialising a vendored submodule
sets BOTH

    -c submodule.<name>.url=<child clone origin>
    -c url.<child clone origin>.insteadOf=<.gitmodules url>

and `insteadOf` matches by PREFIX. When the `.gitmodules` URL is a prefix of
the child origin -- the ordinary `.git` suffix case -- the rule matches the
URL this function itself just set, replaces that prefix with the whole child
origin, and yields the suffix twice.

`vibeic/yosys` vendors `sv-elab`, and the two disagree by exactly that suffix:

    .gitmodules           https://github.com/vibeic/sv-elab
    child clone origin    https://github.com/vibeic/sv-elab.git

so materialisation cloned `https://github.com/vibeic/sv-elab.git.git`:

    remote: Repository not found.

The closure went UNMEASURED, `run_tick` returned 6, `build_page` was skipped,
and the public page missed a day.

Both directions are pinned here. The rule must be dropped when it would
corrupt the URL the closure set, it must still be emitted for a genuine
mirror, and the per-submodule `url` set -- which answers a different failure
-- must survive either way.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import vendored_dependency_closure as C  # noqa: E402

_SOURCE = (_HERE / "vendored_dependency_closure.py").read_text(encoding="utf-8")


def _collides(child_url: str, edge_url: str) -> bool:
    """The predicate the module uses, spelled the way the module spells it."""
    return bool(edge_url) and child_url.strip().startswith(edge_url)


def test_the_exact_pair_that_lost_the_round_is_collides():
    """THE REGRESSION. These two literal URLs are the ones that were measured."""
    assert _collides("https://github.com/vibeic/sv-elab.git",
                          "https://github.com/vibeic/sv-elab")


@pytest.mark.parametrize("child,edge", [
    ("https://github.com/vibeic/sv-elab.git", "https://github.com/vibeic/sv-elab"),
    ("https://github.com/vibeic/x/", "https://github.com/vibeic/x"),
    ("https://github.com/vibeic/x", "https://github.com/vibeic/x"),
])
def test_a_suffix_extended_origin_is_recognised(child, edge):
    assert _collides(child, edge)


@pytest.mark.parametrize("child,edge", [
    # A real local mirror: needs its rule, and cannot self-apply.
    ("/home/reyerchu/vibe-ic-forks/sv-elab", "https://github.com/vibeic/sv-elab"),
    # A different host.
    ("https://mirror.internal/vibeic/sv-elab", "https://github.com/vibeic/sv-elab"),
    # A different repo on the same host.
    ("https://github.com/vibeic/yosys", "https://github.com/vibeic/sv-elab"),
])
def test_a_genuine_mirror_still_gets_its_rule(child, edge):
    """THE BIDIRECTIONAL CONTROL. Narrowing this into 'never emit a rule'
    would silently break hermetic and local-mirror materialisation, which is
    the reason the rule exists."""
    assert not _collides(child, edge)


def test_the_guard_gates_only_the_insteadof():
    """The per-submodule `url` set answers a DIFFERENT failure — a worktree
    inheriting a local path from the shared .git/config, which cost three
    rounds on 2026-09-05. Gating it on this guard would trade one
    round-losing bug for the other."""
    guarded = _SOURCE.split("self_applying = ", 1)[1]
    insteadof = guarded.index(".insteadOf=")
    submodule_url = guarded.index("-c\", f\"submodule.{sub_name}.url=")
    between = guarded[insteadof:submodule_url]
    assert "if not self_applying:" in guarded[:insteadof], (
        "the insteadOf is not gated by the guard")
    assert "self_applying" not in between, (
        "the per-submodule url set must not be gated by this guard")


def test_the_guard_is_actually_wired_into_the_materialise_args():
    """A predicate nothing consults is not a fix."""
    assert re.search(r"if not self_applying:\s*\n\s*materialise_args\.extend",
                     _SOURCE), "the guard does not gate the emitted -c argument"


def test_git_itself_doubles_the_suffix(tmp_path):
    """THE FALSIFICATION, against real git, hermetically.

    Without this the tests above only pin a string predicate. This shows the
    predicate names a REAL git behaviour and that dropping the rule is what
    fixes it. No network and no ambient credentials: an earlier draft shelled
    out to github.com and, with the environment stripped, git asked for a
    username instead of reaching the remote — the assertion then measured the
    harness, not the bug.
    """
    real = tmp_path / "r.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(real)], check=True)

    edge = f"file://{tmp_path}/r"          # what .gitmodules would name
    child = f"{edge}.git"                  # what the child clone's origin says
    assert _collides(child, edge), "the fixture is not the measured shape"

    env = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}

    # Production resolves the CHILD url -- that is what `submodule.<name>.url`
    # is set to -- while the rule's pattern is the EDGE url. The edge url is a
    # prefix of it, so the rule rewrites the closure's own setting.
    with_rule = subprocess.run(
        ["git", "-c", f"url.{child}.insteadOf={edge}", "ls-remote", child],
        capture_output=True, text=True, timeout=120, env=env)
    assert with_rule.returncode != 0, "the doubled URL unexpectedly resolved"
    assert "r.git.git" in with_rule.stderr, with_rule.stderr

    # THE CONTROL: the same remote, the same git, no rule. This is what the
    # guard leaves behind, and it must work.
    without_rule = subprocess.run(
        ["git", "ls-remote", child],
        capture_output=True, text=True, timeout=120, env=env)
    assert without_rule.returncode == 0, without_rule.stderr


def test_a_genuine_mirror_rule_still_resolves(tmp_path):
    """The other direction, against real git: when the replacement does NOT
    start with the pattern, the rule fires exactly once and reaches the
    mirror. A guard that suppressed every rule would break this."""
    mirror = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(mirror)], check=True)

    edge = "https://github.com/vibeic/sv-elab"
    child = f"file://{mirror}"
    assert not _collides(child, edge)

    env = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    r = subprocess.run(
        ["git", "-c", f"url.{child}.insteadOf={edge}", "ls-remote", edge],
        capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------
# The tests above pin git's behaviour and the predicate. Neither FAILS on the
# unfixed closure, because neither runs it. This one does: it drives the real
# materialisation through the shipped fixture, shaped exactly as production
# was on the round that was lost.
# --------------------------------------------------------------------------

import json  # noqa: E402
import test_vendored_dependency_closure as H  # noqa: E402


def _suffix_shaped_fixture(tmp_path):
    """The production shape: the `.gitmodules` URL is the child clone's origin
    MINUS the `.git` suffix.

    This is not contrived. `vibeic/yosys` names `https://github.com/vibeic/
    sv-elab` in `.gitmodules` while the fork clone's origin is that URL plus
    `.git`, and GitHub serves both. Edge discovery matches on the
    `vibeic/<repo>` shape, so the URLs here are the real ones and a local
    `insteadOf` on the host clone -- the same device the shipped fixture uses
    -- makes them resolve offline.
    """
    forks = tmp_path / "forks"
    forks.mkdir()

    nested_work = tmp_path / "nested-work"
    H._init_work(nested_work, "main")
    old = H._commit(nested_work, "value.txt", "old\n", "old nested value")
    new = H._commit(nested_work, "value.txt", "new\n", "new nested value")
    nested_bare = tmp_path / "Nested.git"
    H._bare_from(nested_work, nested_bare, "main")
    subprocess.run(["git", "clone", "-q", str(nested_bare),
                    str(forks / "Nested")], check=True)

    # THE SHAPE UNDER TEST: origin carries `.git`, `.gitmodules` does not.
    child_origin = "https://github.com/vibeic/Nested.git"
    edge_url = "https://github.com/vibeic/Nested"
    assert _collides(child_origin, edge_url), "the fixture is not the measured shape"
    H._git(forks / "Nested", "remote", "set-url", "origin", child_origin)
    # ...and make that name resolve offline, for this clone and for the host.
    for repo in (forks / "Nested",):
        H._git(repo, "config", f"url.{nested_bare.as_uri()}.insteadOf", child_origin)

    host_work = tmp_path / "host-work"
    H._init_work(host_work, "master")
    (host_work / ".gitmodules").write_text(
        '[submodule "nested"]\n'
        '  path = vendor/nested\n'
        f'  url = {edge_url}\n', encoding="utf-8")
    (host_work / "check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    H._git(host_work, "add", ".gitmodules", "check.py")
    H._git(host_work, "update-index", "--add", "--cacheinfo",
           f"160000,{old},vendor/nested")
    H._git(host_work, "commit", "-q", "-m", "pin old nested value")
    host_old = H._git(host_work, "rev-parse", "HEAD")
    host_bare = tmp_path / "Host.git"
    H._bare_from(host_work, host_bare, "master")
    subprocess.run(["git", "clone", "-q", str(host_bare),
                    str(forks / "Host")], check=True)
    # The host clone resolves BOTH spellings to the local bare, so the only
    # thing that can fail here is a URL neither spelling produces -- which is
    # exactly what the doubled suffix is.
    H._git(forks / "Host", "config",
           f"url.{nested_bare.as_uri()}.insteadOf", child_origin)
    H._git(forks / "Host", "config", "--add",
           f"url.{nested_bare.as_uri()}.insteadOf", edge_url)

    eda = tmp_path / "eda"
    (eda / "tools" / "host").mkdir(parents=True)
    (eda / "tools" / "host" / "Dockerfile").write_text(
        f"ARG HOST_REF={host_old} # pinned; branch master\n"
        "ARG HOST_REPO=https://github.com/vibeic/Host.git\n"
        "RUN git clone \"${HOST_REPO}\" /host \\\n"
        " && cd /host && git checkout ${HOST_REF} \\\n"
        " && git submodule update --init --recursive\n", encoding="utf-8")
    registry = {"forks": [
        {"tool": "Host", "branch": "master", "upstream": "up/Host",
         "post_merge_check": [{"name": "candidate-check", "path": "check.py",
                               "cmd": ["python3", "check.py"]}]},
        {"tool": "Nested", "branch": "main", "upstream": "up/Nested"},
    ]}
    forks_json = eda / "fork-gatekeeper" / "FORKS.json"
    forks_json.parent.mkdir(parents=True)
    forks_json.write_text(json.dumps(registry), encoding="utf-8")
    return eda, forks, forks_json, host_bare, host_old, new


def test_a_suffix_shaped_submodule_materialises(tmp_path, monkeypatch):
    """The suffix-shaped graph materialises end to end through the real closure.

    HONEST LABEL: this test passes on the UNFIXED program too, and that is a
    property of offline testing, not a weakness anyone should paper over.

    Reaching a remote offline requires an `insteadOf` in the host clone's own
    config, and git resolves a URL with the LONGEST matching pattern. Any rule
    specific enough to redirect the fixture's remote is longer than the rule
    the closure adds, so it wins, and the defect cannot appear. In production
    no such rule exists — the submodule names plain github.com — so the
    closure's own rule is the only match and the doubling is what git sees.

    So this is a POSITIVE regression test: the shape that lost the round must
    keep materialising. `test_git_itself_doubles_the_suffix` is the one that
    demonstrates the defect against real git, and the two wiring tests are the
    ones that fail on the unfixed source.
    """
    eda, forks, registry, host_bare, host_old, new = _suffix_shaped_fixture(tmp_path)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:https")

    rc, record = C.run(eda, forks, registry, dry_run=False,
                       max_depth=3, max_passes=5)

    update = record["passes"][0]["updates"][0]
    assert update["state"] != "UNMEASURED", update.get("detail")
    assert ".git.git" not in str(update.get("detail") or "")
    assert rc == C.RC_OK and record["status"] == "complete"
    assert update["state"] == "UPDATED" and update["from"] == host_old
    remote_tip = H._git(host_bare, "rev-parse", "refs/heads/master")
    assert H._git(host_bare, "ls-tree", remote_tip,
                  "vendor/nested").split()[2] == new
