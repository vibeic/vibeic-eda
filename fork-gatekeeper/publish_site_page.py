#!/usr/bin/env python3
"""Commit and publish one certified generated page without sweeping site work.

The page renderer writes into the live ``vibeic.ai`` checkout.  A successful
05:30 round used to stop there, leaving generated changes uncommitted and the
GitHub main branch stale.  This publisher admits exactly one tracked page,
refuses every unrelated worktree/index change, publishes with an explicit
remote-tip lease, and verifies the branch readback.

The round uses the opt-in isolated mode to snapshot the generated allowlist
into a fresh origin clone. Shared source files, index and HEAD are never changed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


RC_OK, RC_REFUSED, RC_UNMEASURED = 0, 1, 2
OID = re.compile(r"[0-9a-fA-F]{40,64}")


def _run(root: Path, *args: str, timeout: int = 300) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True,
            text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or "timed out"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _names(root: Path, *args: str) -> Tuple[Optional[List[str]], Optional[str]]:
    rc, out, err = _run(root, *args)
    if rc != 0:
        return None, " ".join((err or out).split())[:220]
    return [line for line in out.splitlines() if line.strip()], None


MATRIX_BEGIN = "<!-- IC_PDK_MATRIX:BEGIN -->"
MATRIX_END = "<!-- IC_PDK_MATRIX:END -->"


def _matrix_change_is_generated(root: Path, page: str, *, before_ref: str,
                                after_ref: Optional[str]) -> Tuple[bool, str]:
    rc, before, err = _run(root, "show", f"{before_ref}:{page}")
    if rc != 0:
        return False, (f"could not read {before_ref}:{page}: "
                       f"{err.strip()[:160]}")
    if after_ref is None:
        try:
            after = (root / page).read_text(encoding="utf-8")
        except OSError as exc:
            return False, f"could not read generated {page}: {exc}"
    else:
        spec = f":{page}" if after_ref == "INDEX" else f"{after_ref}:{page}"
        rc, after, err = _run(root, "show", spec)
        if rc != 0:
            return False, f"could not read {spec}: {err.strip()[:160]}"

    def outside(value: str):
        if value.count(MATRIX_BEGIN) != 1 or value.count(MATRIX_END) != 1:
            return None
        start = value.index(MATRIX_BEGIN) + len(MATRIX_BEGIN)
        end = value.index(MATRIX_END)
        if end < start:
            return None
        return value[:start], value[end:]

    old_outside, new_outside = outside(before), outside(after)
    if old_outside is None or new_outside is None:
        return False, f"{page} does not contain exactly one ordered matrix marker pair"
    if old_outside != new_outside:
        return False, f"{page} changed outside its generated IC_PDK_MATRIX region"
    return True, "generated matrix region only"


def _unstage(root: Path, pages: Iterable[str]) -> None:
    names = list(pages)
    if names:
        _run(root, "reset", "--quiet", "HEAD", "--", *names)


def publish(root: Path, page: str, branch: str, message: str,
            matrix_pages: Iterable[str] = ()) -> Tuple[int, dict]:
    matrix_pages = tuple(dict.fromkeys(matrix_pages))
    allowed = {page, *matrix_pages}
    result = {
        "program": "publish_site_page",
        "schema_version": 1,
        "status": "running",
        "root": str(root),
        "page": page,
        "matrix_pages": list(matrix_pages),
        "branch": branch,
        "commit": None,
        "remote_tip_before": None,
        "remote_tip_after": None,
    }
    if not (root / ".git").exists():
        result.update(status="unmeasured", detail=f"{root} is not a Git checkout")
        return RC_UNMEASURED, result
    page_path = (root / page).resolve()
    try:
        page_path.relative_to(root.resolve())
    except ValueError:
        result.update(status="refused", detail="page is outside the site checkout")
        return RC_REFUSED, result
    if not page_path.is_file():
        result.update(status="unmeasured", detail=f"generated page {page} is missing")
        return RC_UNMEASURED, result
    for generated in sorted(allowed):
        rc, out, err = _run(
            root, "ls-files", "--error-unmatch", "--", generated)
        if rc != 0:
            result.update(status="refused",
                          detail=f"{generated} is not a tracked site file")
            return RC_REFUSED, result
    rc, out, err = _run(root, "symbolic-ref", "--short", "HEAD")
    if rc != 0 or out.strip() != branch:
        result.update(status="refused",
                      detail=f"site checkout is not on declared branch {branch}")
        return RC_REFUSED, result

    staged, why = _names(root, "diff", "--cached", "--name-only")
    if staged is None:
        result.update(status="unmeasured", detail=f"could not inspect site index: {why}")
        return RC_UNMEASURED, result
    if staged:
        result.update(status="refused",
                      detail=f"site index already contains changes: {', '.join(staged[:8])}")
        return RC_REFUSED, result
    tracked, why = _names(root, "diff", "--name-only")
    untracked, untracked_why = _names(
        root, "ls-files", "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        result.update(status="unmeasured",
                      detail=f"could not inspect site worktree: {why or untracked_why}")
        return RC_UNMEASURED, result
    other = sorted((set(tracked) | set(untracked)) - allowed)
    if other:
        result.update(status="refused",
                      detail=("unrelated site worktree changes are present: "
                              + ", ".join(other[:8])))
        return RC_REFUSED, result
    rc, out, err = _run(root, "fetch", "origin", branch, timeout=1800)
    if rc != 0:
        result.update(status="unmeasured",
                      detail=f"could not fetch site origin/{branch}: {err.strip()[:200]}")
        return RC_UNMEASURED, result
    rc, out, err = _run(root, "rev-parse", "--verify",
                        f"refs/remotes/origin/{branch}^{{commit}}")
    remote = out.strip().lower()
    if rc != 0 or not OID.fullmatch(remote):
        result.update(status="unmeasured",
                      detail=f"origin/{branch} did not resolve to one commit")
        return RC_UNMEASURED, result
    result["remote_tip_before"] = remote
    rc, out, err = _run(root, "rev-parse", "HEAD^{commit}")
    head = out.strip().lower()
    if rc != 0 or not OID.fullmatch(head):
        result.update(status="unmeasured", detail="site HEAD did not resolve")
        return RC_UNMEASURED, result
    if head != remote:
        rc, _, _ = _run(root, "merge-base", "--is-ancestor", remote, head)
        if rc != 0:
            result.update(status="refused",
                          detail=(f"site {branch} is behind or diverged from "
                                  f"origin/{branch}; no generated page was committed"))
            return RC_REFUSED, result
        backlog, why = _names(root, "diff", "--name-only", f"{remote}..{head}")
        if backlog is None:
            result.update(status="unmeasured",
                          detail=f"could not inspect unpushed site commits: {why}")
            return RC_UNMEASURED, result
        if set(backlog) - allowed:
            result.update(status="refused",
                          detail=("unpushed site commits contain files other than "
                                  f"the generated allowlist: {', '.join(backlog[:8])}"))
            return RC_REFUSED, result
        for matrix_page in matrix_pages:
            if matrix_page not in backlog:
                continue
            valid, detail = _matrix_change_is_generated(
                root, matrix_page, before_ref=remote, after_ref="HEAD")
            if not valid:
                result.update(
                    status="refused",
                    detail=("unpushed site commit is not generator-confined: "
                            + detail),
                )
                return RC_REFUSED, result

    changed_generated = sorted(set(tracked) & allowed)
    if changed_generated:
        rc, _, err = _run(root, "add", "--", *changed_generated)
        if rc != 0:
            result.update(status="unmeasured",
                          detail=(f"could not stage generated pages: "
                                  f"{err.strip()[:180]}"))
            return RC_UNMEASURED, result
        staged_now, why = _names(root, "diff", "--cached", "--name-only")
        if staged_now is None or set(staged_now) != set(changed_generated):
            _unstage(root, changed_generated)
            result.update(
                status="unmeasured",
                detail=("staged generated page set changed unexpectedly: "
                        + (why or ", ".join(staged_now or []))),
            )
            return RC_UNMEASURED, result
        for matrix_page in matrix_pages:
            if matrix_page not in changed_generated:
                continue
            valid, detail = _matrix_change_is_generated(
                root, matrix_page, before_ref="HEAD", after_ref="INDEX")
            if not valid:
                _unstage(root, changed_generated)
                result.update(status="refused", detail=detail)
                return RC_REFUSED, result
        rc, _, err = _run(
            root, "-c", "user.name=vibeic-fork-gatekeeper",
            "-c", "user.email=fork-gatekeeper@vibeic.ai",
            "commit", "-s", "-m", message, timeout=300,
        )
        if rc != 0:
            _unstage(root, changed_generated)
            result.update(status="unmeasured",
                          detail=f"could not commit generated pages: {err.strip()[:180]}")
            return RC_UNMEASURED, result

    rc, out, err = _run(root, "rev-parse", "HEAD^{commit}")
    candidate = out.strip().lower()
    if rc != 0 or not OID.fullmatch(candidate):
        result.update(status="unmeasured", detail="site candidate did not resolve")
        return RC_UNMEASURED, result
    result["commit"] = candidate
    if candidate != remote:
        rc, push_out, push_err = _run(
            root, "push", "--porcelain",
            f"--force-with-lease=refs/heads/{branch}:{remote}",
            "origin", f"HEAD:refs/heads/{branch}", timeout=1800,
        )
        if rc != 0:
            detail = " ".join((push_out + " " + push_err).split())
            result.update(status="unmeasured",
                          detail=f"site push was not confirmed: {detail[:220]}")
            return RC_UNMEASURED, result
    rc, out, err = _run(root, "ls-remote", "--refs", "origin",
                        f"refs/heads/{branch}")
    readback = [line.split()[0].lower() for line in out.splitlines()
                if line.strip() and line.split()]
    if rc != 0 or readback != [candidate]:
        result.update(status="unmeasured",
                      detail=(f"site push readback was {readback or 'missing'}, "
                              f"expected {candidate}"))
        return RC_UNMEASURED, result
    result.update(status="published", remote_tip_after=candidate,
                  detail=("generated page already published" if candidate == remote
                          else "generated page committed, pushed, and read back"))
    return RC_OK, result


def _generated_path(root: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or {"..", ".git"} & set(relative.parts):
        raise ValueError("generated path is outside the site file allowlist")
    path = root / relative
    path.resolve().relative_to(root.resolve())
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("generated paths must not traverse symlinks")
        current = current.parent
    if not path.is_file():
        raise OSError(f"generated page {name} is missing")
    return path


def _matrix_parts(value: str) -> Tuple[str, str, str]:
    if value.count(MATRIX_BEGIN) != 1 or value.count(MATRIX_END) != 1:
        raise ValueError("matrix requires exactly one ordered marker pair")
    start = value.index(MATRIX_BEGIN) + len(MATRIX_BEGIN)
    end = value.index(MATRIX_END)
    if end < start:
        raise ValueError("matrix requires exactly one ordered marker pair")
    return value[:start], value[start:end], value[end:]


def publish_isolated(root: Path, page: str, branch: str, message: str,
                     matrix_pages: Iterable[str] = ()) -> Tuple[int, dict]:
    """Publish generated snapshots on origin's tip, preserving shared work.

    Matrix pages transfer only their generated region; their remote surrounding
    text is retained. The existing clean-index, allowlist, ancestry, lease and
    readback gates still execute on the isolated checkout.
    """
    matrix_pages = tuple(dict.fromkeys(matrix_pages))
    result = {"program": "publish_site_page", "schema_version": 1,
              "status": "running", "root": str(root), "page": page,
              "matrix_pages": list(matrix_pages), "branch": branch,
              "isolated": True, "commit": None}

    def fail(rc, detail):
        result.update(status="refused" if rc == RC_REFUSED else "unmeasured",
                      detail=detail)
        return rc, result

    if not (root / ".git").exists():
        return fail(RC_UNMEASURED, "source is not a Git checkout")
    snapshots = {}
    try:
        for name in dict.fromkeys([page, *matrix_pages]):
            source = _generated_path(root, name)
            rc, _, _ = _run(root, "ls-files", "--error-unmatch", "--", name)
            if rc != 0:
                return fail(RC_REFUSED, f"{name} is not a tracked site file")
            snapshots[name] = source.read_bytes()
        for name in matrix_pages:
            valid, detail = _matrix_change_is_generated(
                root, name, before_ref="HEAD", after_ref=None)
            if not valid:
                return fail(RC_REFUSED, detail)
            _matrix_parts(snapshots[name].decode("utf-8"))
    except ValueError as exc:
        return fail(RC_REFUSED, str(exc))
    except OSError as exc:
        return fail(RC_UNMEASURED, str(exc))

    rc, out, _ = _run(root, "remote", "get-url", "origin")
    if rc != 0 or not out.strip():
        return fail(RC_UNMEASURED, "source origin could not be read")
    origin = out.strip()
    with tempfile.TemporaryDirectory(prefix="vibeic-site-publish-") as directory:
        isolated = Path(directory) / "site"
        rc, _, _ = _run(root, "clone", "--quiet", "--single-branch",
                         "--branch", branch, "--", origin, str(isolated),
                         timeout=1800)
        if rc != 0:
            return fail(RC_UNMEASURED, f"isolated origin clone failed (rc={rc})")
        try:
            for name, data in snapshots.items():
                if _generated_path(root, name).read_bytes() != data:
                    return fail(RC_REFUSED, f"generated source changed during isolation: {name}")
            for name, data in snapshots.items():
                target = _generated_path(isolated, name)
                if name in matrix_pages:
                    prefix, _, suffix = _matrix_parts(target.read_text(encoding="utf-8"))
                    _, region, _ = _matrix_parts(data.decode("utf-8"))
                    data = (prefix + region + suffix).encode("utf-8")
                target.write_bytes(data)
        except ValueError as exc:
            return fail(RC_REFUSED, str(exc))
        except OSError as exc:
            return fail(RC_UNMEASURED, str(exc))
        rc, published = publish(isolated, page, branch, message, matrix_pages)
        published.update(root=str(root), isolated=True)
        return rc, published


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site-root", type=Path, required=True)
    parser.add_argument("--page", default="eda-forks.html")
    parser.add_argument("--matrix-page", action="append", default=[])
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="dashboard: publish certified 05:30 fork state")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--isolated", action="store_true",
                        help="publish generated snapshots from a fresh origin clone")
    args = parser.parse_args(argv)
    publisher = publish_isolated if args.isolated else publish
    rc, result = publisher(
        args.site_root.resolve(), args.page, args.branch, args.message,
        args.matrix_page)
    if args.json:
        _atomic_json(args.json, result)
    print(f"publish_site_page: {result['status']} — {result.get('detail', '')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
