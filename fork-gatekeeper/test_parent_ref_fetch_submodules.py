"""Parent refs and isolated gitlink merges use only the graph they require."""
import json
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import daily_0530 as D
import daily_merge as M


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(repo, message):
    git(repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", message, "--allow-empty")


@pytest.fixture
def parent_with_unpublished_gitlink(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    dependency = tmp_path / "dependency"
    git(tmp_path, "init", "-q", "-b", "main", str(dependency))
    commit(dependency, "published dependency")
    published = tmp_path / "published.git"
    git(tmp_path, "clone", "-q", "--bare", str(dependency), str(published))
    commit(dependency, "unpublished experimental dependency")
    unpublished = git(dependency, "rev-parse", "HEAD")

    parent = tmp_path / "parent"
    git(tmp_path, "init", "-q", "-b", "main", str(parent))
    git(parent, "submodule", "add", "-q", str(published), "dep")
    commit(parent, "parent with published dependency")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--recurse-submodules", str(parent), str(clone))
    git(clone, "remote", "add", "upstream", str(parent))
    git(clone, "fetch", "--no-recurse-submodules", "-q", "upstream")
    git(clone, "config", "fetch.recurseSubmodules", "on-demand")

    git(parent, "checkout", "-qb", "experimental")
    git(parent, "update-index", "--cacheinfo", "160000", unpublished, "dep")
    commit(parent, "experimental branch references an unpublished gitlink")
    git(parent, "checkout", "-q", "main")
    return parent, clone


@pytest.mark.parametrize("moved_main", [False, True])
@pytest.mark.parametrize("entry", ["upstream", "origin", "daily_merge"])
def test_parent_comparisons_ignore_unpublished_experimental_submodule(
        parent_with_unpublished_gitlink, monkeypatch, entry, moved_main):
    parent, clone = parent_with_unpublished_gitlink
    if moved_main:
        commit(parent, "new published parent commit")
    published_tip = git(parent, "rev-parse", "main")
    before_dependency = git(clone / "dep", "rev-parse", "HEAD")

    if entry == "upstream":
        rep = {}
        D.step1_upstream(("git", "-C", str(clone)), "main", rep)
        expected = "merged 1 upstream commit(s)" if moved_main else "already current"
        assert rep["upstream"] == expected, rep
        assert rep["upstream_evidence"]["tip_seen"] == published_tip
    elif entry == "origin":
        branches = D._fork_branches(("git", "-C", str(clone)), "main")
        assert branches == ["origin/experimental"], branches
        assert git(clone, "rev-parse", "origin/main") == published_tip
    else:
        monkeypatch.setattr(M, "FORKS_ROOT", clone.parent)
        result = M.merge_one("clone", "main", dry=True, checks=[])
        # This fixture's origin and upstream share the published parent, so
        # refreshing origin also carries the new main commit already.
        assert result["state"] == "ALREADY_CURRENT", result
        assert result["upstream_default"]["remote_tip"] == published_tip

    assert git(clone / "dep", "rev-parse", "HEAD") == before_dependency


@pytest.mark.parametrize("diagnostic", [
    "fatal: remote error: upload-pack: not our ref deadbeef",
    "error: cannot lock ref refs/remotes/upstream/work",
    "remote: error: permission denied",
])
def test_fetch_diagnosis_survives_progress_output(diagnostic):
    cp = subprocess.CompletedProcess(
        ["git", "fetch"], 1, "",
        "remote: Enumerating objects: 450, done.\n"
        "Receiving objects: 100% (450/450), done.\n" + diagnostic + "\n"
    )
    assert D._first_error_line(cp) == diagnostic


# Exercise actual registry declarations without copying any production design
# or depending on the host's shared forks. All histories below are synthesized.
_REGISTRY = json.loads(
    pathlib.Path(__file__).with_name("FORKS.json").read_text())
_REGISTERED_CHILDREN = [
    {key: row[key] for key in ("tool", "branch", "upstream")}
    for row in _REGISTRY["forks"]
    if all(row.get(key) for key in ("tool", "branch", "upstream"))
]


def _shared_snapshot(repo):
    """Object/ref refreshes are allowed; shared worktree/index/config edits are not."""
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "worktrees": git(repo, "worktree", "list", "--porcelain"),
        "status": git(repo, "status", "--porcelain=v1", "-z"),
        "index": (repo / ".git" / "index").read_bytes(),
        "config": (repo / ".git" / "config").read_bytes(),
        "files": {
            str(path.relative_to(repo)): path.read_bytes()
            for path in repo.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(repo).parts
        },
    }


@pytest.fixture
def gitlink_fleet(tmp_path, monkeypatch):
    """A -> B -> C child graph; parent sides independently select B and C.

    The parent shared checkout deliberately has no initialized submodule. The
    registered sibling has all commit objects unless a negative case asks for
    a real incomplete clone. Parent and child working copies carry local edits.
    No fixture remote uses a network transport; .gitmodules URLs are identities
    only and GIT_ALLOW_PROTOCOL prevents accidentally contacting them.
    """
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setattr(M, "GIT_PROGRESS_POLL_S", 0.01)
    monkeypatch.setattr(M, "POST_MERGE_POLL_S", 0.01)

    def make(*, child_entry=None, graph="linear", defect="", url_kind="upstream",
             update_module_url=False, module_shape=""):
        child_entry = dict(child_entry or {
            "tool": "child-source", "branch": "main",
            "upstream": "neutral-upstream/child-source",
        })
        forks = tmp_path / "forks"
        forks.mkdir()
        seed = tmp_path / "child-seed"
        git(tmp_path, "init", "-q", "-b", "main", str(seed))
        (seed / "data.txt").write_text("base\n")
        git(seed, "add", "data.txt")
        commit(seed, "child common base")
        base = git(seed, "rev-parse", "HEAD")
        git(seed, "branch", "base")
        (seed / "data.txt").write_text("child intermediate\n")
        git(seed, "add", "data.txt")
        commit(seed, "child upstream step")
        incoming = git(seed, "rev-parse", "HEAD")
        git(seed, "branch", "incoming")
        if graph == "divergent":
            git(seed, "checkout", "-q", "--detach", base)
        (seed / "data.txt").write_text("child descendant\n")
        git(seed, "add", "data.txt")
        commit(seed, "child fork step")
        ours = git(seed, "rev-parse", "HEAD")
        if graph == "reverse-linear":
            incoming, ours = ours, incoming
            git(seed, "branch", "-f", "incoming", incoming)
        git(seed, "branch", "fork-tip", ours)

        child = forks / child_entry["tool"]
        source_branch = "incoming" if defect == "missing-object" else "fork-tip"
        clone_args = ["clone", "-q", "--no-local", "--branch", source_branch]
        if defect == "missing-object":
            clone_args.append("--single-branch")
        git(tmp_path, *clone_args, str(seed), str(child))
        if defect != "missing-object":
            for oid in (base, incoming, ours):
                git(child, "cat-file", "-e", f"{oid}^{{commit}}")
        else:
            absent = subprocess.run(
                ["git", "-C", str(child), "cat-file", "-e", f"{ours}^{{commit}}"],
                capture_output=True, text=True)
            assert absent.returncode != 0, "missing-object fixture has the object"
        git(child, "config", "fixture.preserve", "child-config")
        (child / "data.txt").write_text("uncommitted child owner edit\n")
        (child / "owner-note").write_text("untracked child owner note\n")

        parent_seed = tmp_path / "parent-seed"
        git(tmp_path, "init", "-q", "-b", "main", str(parent_seed))
        module_path = "deps/engine"
        declared_path = "../escape" if defect == "unsafe-module-path" else module_path
        slug = (child_entry["upstream"] if url_kind == "upstream"
                else f"{_REGISTRY['org']}/{child_entry['tool']}")
        if defect == "unknown-url":
            slug = "unregistered/unknown-child"
        section = "module/engine" if module_shape else "engine"
        module_url = (f"../../{slug}.git" if module_shape
                      else f"https://github.com/{slug}.git")
        (parent_seed / ".gitmodules").write_text(
            f'[submodule "{section}"]\n'
            f'\tpath = {declared_path}\n\turl = {module_url}\n')
        (parent_seed / "shared.txt").write_text("base\n")
        git(parent_seed, "add", ".gitmodules", "shared.txt")
        # Fixture construction only: the program under test must leave gitlink
        # selection to native Git, never synthesize a resolution with this.
        git(parent_seed, "update-index", "--add", "--cacheinfo",
            "160000", base, module_path)
        commit(parent_seed, "parent common base")
        parent_base = git(parent_seed, "rev-parse", "HEAD")
        origin, upstream = tmp_path / "origin.git", tmp_path / "upstream.git"
        git(tmp_path, "clone", "-q", "--bare", str(parent_seed), str(origin))
        git(tmp_path, "clone", "-q", "--bare", str(parent_seed), str(upstream))

        git(parent_seed, "update-index", "--cacheinfo", "160000", ours, module_path)
        if module_shape:
            child_url = f"https://github.com/{_REGISTRY['org']}/{child_entry['tool']}.git"
            duplicate_url = (f"https://github.com/{_REGISTRY['org']}/other-child-source.git"
                             if module_shape == "ambiguous-duplicate-relative"
                             else child_url)
            (parent_seed / ".gitmodules").write_text(
                '[submodule "module/engine"]\n'
                f'\tpath = {module_path}\n\turl = {child_url}\n'
                f'[submodule "{module_path}"]\n\turl = {duplicate_url}\n')
            git(parent_seed, "add", ".gitmodules")
        if update_module_url:
            git(parent_seed, "config", "--file", ".gitmodules", "submodule.engine.url",
                f"https://github.com/{_REGISTRY['org']}/{child_entry['tool']}.git")
            git(parent_seed, "add", ".gitmodules")
        (parent_seed / "fork-only.txt").write_text("fork addition\n")
        git(parent_seed, "add", "fork-only.txt")
        if defect == "mixed-conflict":
            (parent_seed / "shared.txt").write_text("fork content\n")
            git(parent_seed, "add", "shared.txt")
        commit(parent_seed, "parent fork selects child fork tip")
        parent_ours = git(parent_seed, "rev-parse", "HEAD")
        git(parent_seed, "push", "-q", str(origin), "HEAD:refs/heads/main")

        git(parent_seed, "checkout", "-q", "--detach", parent_base)
        git(parent_seed, "update-index", "--cacheinfo", "160000", incoming, module_path)
        (parent_seed / "upstream-only.txt").write_text("upstream addition\n")
        git(parent_seed, "add", "upstream-only.txt")
        if defect == "mixed-conflict":
            (parent_seed / "shared.txt").write_text("upstream content\n")
            git(parent_seed, "add", "shared.txt")
        commit(parent_seed, "parent upstream selects child intermediate tip")
        parent_incoming = git(parent_seed, "rev-parse", "HEAD")
        git(parent_seed, "push", "-q", str(upstream), "HEAD:refs/heads/main")

        parent = forks / "parent-source"
        git(tmp_path, "clone", "-q", str(origin), str(parent))
        git(parent, "remote", "add", "upstream", str(upstream))
        git(parent, "config", "fixture.preserve", "parent-config")
        (parent / "shared.txt").write_text("uncommitted parent owner edit\n")
        (parent / "owner-note").write_text("untracked parent owner note\n")
        registry_child = dict(child_entry)
        if defect == "unsafe-registered-tool":
            registry_child["tool"] = "../child-seed"
        if defect == "symlink-source":
            outside = tmp_path / "outside-child"
            child.rename(outside)
            child.symlink_to(outside, target_is_directory=True)
        registry = {"org": _REGISTRY["org"], "forks": [
            {"tool": "parent-source", "branch": "main",
             "upstream": "neutral-upstream/parent-source"},
            *([] if defect == "unregistered" else [registry_child]),
        ]}
        if defect == "ambiguous-mapping":
            registry["forks"].append({**registry_child, "tool": "other-child-source"})
        if module_shape == "ambiguous-duplicate-relative":
            registry["forks"].append({"tool": "other-child-source", "branch": "main",
                                      "upstream": "neutral-upstream/other-child-source"})
        monkeypatch.setattr(M, "FORKS_ROOT", forks)
        monkeypatch.setattr(M, "_forks_json", lambda path=None: registry)
        return SimpleNamespace(
            parent=parent, child=child, origin=origin, upstream=upstream,
            parent_base=parent_base, parent_ours=parent_ours,
            parent_incoming=parent_incoming, module_path=module_path,
            base=base, incoming=incoming, ours=ours,
            selected=incoming if graph == "reverse-linear" else ours,
            selected_content="child descendant\n",
            postcheck_record=tmp_path / "child-postcheck.json",
            before_parent=_shared_snapshot(parent),
            before_child=_shared_snapshot(child),
        )

    return make


def _run_gitlink_merge(fleet, monkeypatch, *, checks=None, fail_clone=False,
                       race=False, stall_retry=False, final_checkout_failure=None):
    """Trace native Git; inject only an I/O failure or a competing ref update."""
    real_sh = M._sh
    trace = SimpleNamespace(commands=[], merges=[], pushes=[], clone_failures=[],
                            final_checkouts=[])

    def observed(cmd, *args, **kwargs):
        cmd = list(map(str, cmd))
        trace.commands.append(cmd)
        if cmd[:2] == ["git", "clone"] and "--local" in cmd and fail_clone:
            trace.clone_failures.append(cmd)
            return 128, "", "fatal: fixture local clone I/O failure"
        if cmd[:2] == ["git", "push"]:
            trace.pushes.append(cmd)
            if race:
                git(fleet.origin, "update-ref", "refs/heads/main",
                    fleet.parent_base, fleet.parent_ours)
        is_merge = cmd[0] == "git" and "merge" in cmd and "--abort" not in cmd
        is_final_checkout = (cmd[0] == "git" and "checkout" in cmd
                             and len(trace.merges) == 2
                             and trace.merges[-1]["rc"] == 0)
        cwd = kwargs.get("cwd", args[0] if args else None)
        if is_merge:
            head_before = git(cwd, "rev-parse", "HEAD")
        if is_merge and stall_retry and len(trace.merges) == 1:
            answer = (M.watchdog.RC_STALLED, "", "fixture retry watchdog stall")
        elif is_final_checkout and final_checkout_failure is not None:
            answer = (final_checkout_failure, "", "fixture final child checkout failure")
        else:
            answer = real_sh(cmd, *args, **kwargs)
        if is_merge:
            trace.merges.append({
                "command": cmd, "rc": answer[0],
                "head_before": head_before,
                "unmerged": git(cwd, "ls-files", "-u"),
            })
        if is_final_checkout:
            trace.final_checkouts.append({
                "command": cmd, "cwd": str(cwd), "rc": answer[0],
                "child_head": git(cwd, "rev-parse", "HEAD"),
            })
        return answer

    monkeypatch.setattr(M, "_sh", observed)
    result = M.merge_one("parent-source", "main", checks=checks or [],
                         expected_pin=fleet.parent_ours)
    assert _shared_snapshot(fleet.parent) == fleet.before_parent
    assert _shared_snapshot(fleet.child) == fleet.before_child
    assert not any("update-index" in cmd for cmd in trace.commands)
    # Registered sibling objects are the entire repair input. The parent fetch
    # stays non-recursive, and no child remote/submodule-update fetch is allowed.
    for cmd in trace.commands:
        if "fetch" in cmd:
            assert "--no-recurse-submodules" in cmd, cmd
            assert str(fleet.parent) in cmd, cmd
        if cmd[:2] == ["git", "clone"]:
            assert "--local" in cmd and "--no-recurse-submodules" in cmd, cmd
            assert str(fleet.child) in cmd, cmd
        assert "submodule" not in cmd, cmd
    assert trace.merges, result
    first = trace.merges[0]
    assert first["rc"] != 0, first
    for stage, oid in ((1, fleet.base), (2, fleet.ours), (3, fleet.incoming)):
        assert f"160000 {oid} {stage}\t{fleet.module_path}" in first["unmerged"]
    assert len(trace.merges) <= 2, trace.merges
    for merge in trace.merges:
        assert fleet.parent_incoming in merge["command"], merge
        assert merge["head_before"] == fleet.parent_ours, merge
    return result, trace


def _assert_gitlink_published(fleet, result, trace):
    # This assertion is deliberately first: BASE fails on an existing observable
    # CONFLICT_NEEDS_HUMAN result, not on a missing new receipt/helper attribute.
    assert result["state"] == "MERGED", result
    published = git(fleet.origin, "rev-parse", "refs/heads/main")
    assert published != fleet.parent_ours
    for parent in (fleet.parent_ours, fleet.parent_incoming):
        git(fleet.origin, "merge-base", "--is-ancestor", parent, published)
    assert git(fleet.origin, "ls-tree", published, fleet.module_path) == (
        f"160000 commit {fleet.selected}\t{fleet.module_path}")
    assert git(fleet.origin, "show", f"{published}:fork-only.txt") == "fork addition"
    assert git(fleet.origin, "show", f"{published}:upstream-only.txt") == "upstream addition"
    assert [merge["rc"] for merge in trace.merges] == [1, 0]
    assert len(trace.pushes) == 1
    assert (f"--force-with-lease=refs/heads/main:{fleet.parent_ours}"
            in trace.pushes[0])


@pytest.mark.parametrize("url_kind", ["upstream", "fork"])
def test_daily_merge_retries_native_git_with_registered_local_child_graph(
        gitlink_fleet, monkeypatch, url_kind):
    fleet = gitlink_fleet(url_kind=url_kind)
    result, trace = _run_gitlink_merge(fleet, monkeypatch)
    _assert_gitlink_published(fleet, result, trace)


def test_daily_merge_registered_fork_url_change_keeps_same_child_identity(
        gitlink_fleet, monkeypatch):
    fleet = gitlink_fleet(update_module_url=True)
    result, trace = _run_gitlink_merge(fleet, monkeypatch)
    _assert_gitlink_published(fleet, result, trace)


@pytest.mark.parametrize("module_shape", [
    "duplicate-relative", "ambiguous-duplicate-relative",
])
def test_daily_merge_relative_urls_and_duplicate_path_declarations(
        gitlink_fleet, monkeypatch, module_shape):
    fleet = gitlink_fleet(module_shape=module_shape)
    result, trace = _run_gitlink_merge(fleet, monkeypatch)
    if module_shape == "duplicate-relative":
        _assert_gitlink_published(fleet, result, trace)
    else:
        assert result["state"] == "CONFLICT_NEEDS_HUMAN", result
        assert len(trace.merges) == 1
        assert trace.pushes == []
        assert git(fleet.origin, "rev-parse", "refs/heads/main") == fleet.parent_ours


@pytest.mark.parametrize("child_entry", _REGISTERED_CHILDREN,
                         ids=lambda row: row["tool"])
def test_daily_merge_registered_child_mapping_corpus(
        gitlink_fleet, monkeypatch, child_entry):
    fleet = gitlink_fleet(child_entry=child_entry)
    result, trace = _run_gitlink_merge(fleet, monkeypatch)
    _assert_gitlink_published(fleet, result, trace)


@pytest.mark.parametrize("graph,defect", [
    ("divergent", ""),
    ("linear", "mixed-conflict"),
    ("linear", "missing-object"),
    ("linear", "unregistered"),
    ("linear", "unknown-url"),
    ("linear", "ambiguous-mapping"),
    ("linear", "unsafe-module-path"),
    ("linear", "unsafe-registered-tool"),
    ("linear", "symlink-source"),
])
def test_daily_merge_unresolved_or_untrusted_child_graph_never_publishes(
        gitlink_fleet, monkeypatch, graph, defect):
    fleet = gitlink_fleet(graph=graph, defect=defect)
    result, trace = _run_gitlink_merge(fleet, monkeypatch)
    assert result["state"] == "CONFLICT_NEEDS_HUMAN", result
    assert git(fleet.origin, "rev-parse", "refs/heads/main") == fleet.parent_ours
    assert trace.pushes == []
    if graph == "divergent":
        assert len(trace.merges) == 2, trace.merges
        assert trace.merges[1]["rc"] != 0
    else:
        assert len(trace.merges) == 1, trace.merges
    if defect == "mixed-conflict":
        assert "shared.txt" in trace.merges[0]["unmerged"]


def test_daily_merge_child_clone_preparation_failure_never_publishes(
        gitlink_fleet, monkeypatch):
    fleet = gitlink_fleet()
    result, trace = _run_gitlink_merge(fleet, monkeypatch, fail_clone=True)
    assert result["state"] == "CONFLICT_NEEDS_HUMAN", result
    assert trace.clone_failures, trace.commands
    assert len(trace.merges) == 1
    assert trace.pushes == []
    assert git(fleet.origin, "rev-parse", "refs/heads/main") == fleet.parent_ours


def test_daily_merge_gitlink_retry_still_runs_blocking_post_merge_checks(
        gitlink_fleet, monkeypatch):
    fleet = gitlink_fleet()
    checks = [{"name": "candidate-control", "path": "shared.txt",
               "cmd": [sys.executable, "-c", "raise SystemExit(7)"]}]
    result, trace = _run_gitlink_merge(fleet, monkeypatch, checks=checks)
    assert result["state"] == "POST_MERGE_CHECK_FAILED", result
    assert [merge["rc"] for merge in trace.merges] == [1, 0]
    assert result["checks"][0]["rc"] == 7
    assert trace.pushes == []
    assert git(fleet.origin, "rev-parse", "refs/heads/main") == fleet.parent_ours


def test_daily_merge_gitlink_retry_still_rejects_concurrent_remote_rewind(
        gitlink_fleet, monkeypatch):
    fleet = gitlink_fleet()
    result, trace = _run_gitlink_merge(fleet, monkeypatch, race=True)
    assert result["state"] == "PUSH_CAS_REJECTED", result
    assert [merge["rc"] for merge in trace.merges] == [1, 0]
    assert len(trace.pushes) == 1
    assert (f"--force-with-lease=refs/heads/main:{fleet.parent_ours}"
            in trace.pushes[0])
    assert git(fleet.origin, "rev-parse", "refs/heads/main") == fleet.parent_base


def test_daily_merge_gitlink_retry_watchdog_stall_is_not_a_conflict_verdict(
        gitlink_fleet, monkeypatch):
    fleet = gitlink_fleet()
    result, trace = _run_gitlink_merge(fleet, monkeypatch, stall_retry=True)
    assert result["state"] == "MERGE_STALLED", result
    assert [merge["rc"] for merge in trace.merges] == [1, M.watchdog.RC_STALLED]
    assert result["submodule_graph_retry"]["initial_merge"]["conflicts"] == [
        fleet.module_path]
    assert trace.pushes == []
    assert git(fleet.origin, "rev-parse", "refs/heads/main") == fleet.parent_ours


def _materialized_child_check(fleet):
    """A real postcheck reads the candidate checkout, not a recovery receipt."""
    script = (
        "import json, pathlib, subprocess; "
        f"child = pathlib.Path({fleet.module_path!r}); "
        "actual = subprocess.check_output(['git', '-C', str(child), 'rev-parse', "
        "'HEAD'], text=True).strip(); "
        "selected = subprocess.check_output(['git', 'rev-parse', "
        f"{'HEAD:' + fleet.module_path!r}], text=True).strip(); "
        "content = (child / 'data.txt').read_text(); "
        f"pathlib.Path({str(fleet.postcheck_record)!r}).write_text(json.dumps("
        "{'head': actual, 'selected': selected, 'content': content})); "
        f"assert actual == selected == {fleet.selected!r}, (actual, selected); "
        f"assert content == {fleet.selected_content!r}, content"
    )
    return [{"name": "materialized-child", "path": f"{fleet.module_path}/data.txt",
             "cmd": [sys.executable, "-c", script]}]


@pytest.mark.parametrize("graph", ["linear", "reverse-linear"])
def test_daily_merge_postcheck_reads_native_selected_child_head_and_content(
        gitlink_fleet, monkeypatch, graph):
    fleet = gitlink_fleet(graph=graph)
    result, trace = _run_gitlink_merge(
        fleet, monkeypatch, checks=_materialized_child_check(fleet))
    _assert_gitlink_published(fleet, result, trace)
    assert result["checks"][0]["rc"] == 0
    assert json.loads(fleet.postcheck_record.read_text()) == {
        "head": fleet.selected, "selected": fleet.selected,
        "content": fleet.selected_content,
    }
    if graph == "reverse-linear":
        assert trace.final_checkouts, trace.commands
        assert trace.final_checkouts[-1]["child_head"] == fleet.incoming
        assert trace.final_checkouts[-1]["rc"] == 0


@pytest.mark.parametrize("checkout_rc", [128, M.watchdog.RC_STALLED],
                         ids=["failed", "stalled"])
def test_daily_merge_final_child_checkout_failure_blocks_checks_and_publication(
        gitlink_fleet, monkeypatch, checkout_rc):
    fleet = gitlink_fleet(graph="reverse-linear")
    result, trace = _run_gitlink_merge(
        fleet, monkeypatch, checks=_materialized_child_check(fleet),
        final_checkout_failure=checkout_rc)
    expected_state = ("MERGE_STALLED" if checkout_rc == M.watchdog.RC_STALLED
                      else "CONFLICT_NEEDS_HUMAN")
    assert result["state"] == expected_state, result
    assert [merge["rc"] for merge in trace.merges] == [1, 0]
    assert trace.final_checkouts[-1]["rc"] == checkout_rc
    assert trace.final_checkouts[-1]["child_head"] == fleet.ours
    assert result["submodule_graph_retry"]["outcome"] == "materialization_failed"
    assert result["checks"] == []
    assert not fleet.postcheck_record.exists()
    assert trace.pushes == []
    assert git(fleet.origin, "rev-parse", "refs/heads/main") == fleet.parent_ours
