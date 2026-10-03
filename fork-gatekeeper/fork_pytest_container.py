"""Run declared fork pytest checks in the released EDA dependency environment.

BLOCKING: a missing receipt, an empty selection, or any skipped/failed case
cannot authorize a merge. The candidate checkout supplies the code; the image
supplies its runtime dependencies. The existing semantic gate runs INSIDE the
container, where its direct-child PID and descendant census remain meaningful.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent


def _case_id(case) -> str:
    filename, classname = case.get("file"), case.get("classname", "")
    if not filename:
        return f"{classname}::{case.get('name')}"
    module = filename.removesuffix(".py").replace("/", ".")
    classes = (classname[len(module) + 1:].replace(".", "::")
               if classname.startswith(module + ".") else
               "" if classname == module else classname)
    return "::".join(x for x in (filename, classes, case.get("name")) if x)


def grade_receipts(rc: int, semantic: dict, junit: Path) -> tuple[int, dict]:
    """Require the semantic receipt and every individual JUnit case to agree."""
    if not isinstance(semantic, dict):
        semantic = {}
    record = dict(semantic)
    try:
        cases = list(ET.parse(junit).getroot().iter("testcase"))
        ids = [_case_id(c) for c in cases]
        skipped = [ids[i] for i, c in enumerate(cases) if c.find("skipped") is not None]
        red = [ids[i] for i, c in enumerate(cases)
               if c.find("failure") is not None or c.find("error") is not None]
        progress = semantic["progress"]
        complete = (
            semantic["mechanism"] == "validated_semantic_progress_lease"
            and semantic["outcome"] == "natural"
            and semantic["exit_code"] == rc
            and semantic["process_rc"] == rc
            and semantic["junit_cases"] == len(cases) > 0
            and progress["selected"] == progress["finished"] == len(cases)
            and progress["stage"] == "finished"
            and progress["exitstatus"] == rc
            and progress["protocol_error"] is None
            and semantic["junit_red"] == len(red)
            and semantic["process_tree"]["final_empty"] is True
            and len(set(ids)) == len(ids)
        )
    except (OSError, ET.ParseError, KeyError, TypeError, ValueError):
        complete, ids, skipped, red = False, [], [], []
    record.update(case_ids=ids, skipped_case_ids=skipped, failed_case_ids=red)
    if not complete or rc not in (0, 1):
        record.update(outcome=(semantic.get("outcome") if semantic.get("outcome") not in (None, "natural")
                               else "protocol_incomplete"),
                      reason="missing or inconsistent container pytest receipts")
        return 2, record
    if rc or skipped or red:
        record["reason"] = f"container pytest rejected: failed={red}, skipped={skipped}, rc={rc}"
        return 1, record
    record["reason"] = f"{len(ids)} container pytest cases passed; zero skips"
    return 0, record


def _source_identity(wt: Path) -> dict:
    def git(*args):
        return subprocess.check_output(["git", "-C", str(wt), *args], text=True).strip()
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("candidate checkout has tracked modifications")
    return {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")}


def _snapshot(wt: Path, head: str, evidence: Path) -> tuple[Path, str]:
    """Only tracked bytes at the attested commit may enter the Python path."""
    archive = evidence / "source.tar"
    subprocess.run(["git", "-C", str(wt), "archive", "--format=tar", "--output", str(archive), head], check=True)
    source = evidence / "candidate"
    source.mkdir()
    subprocess.run(["tar", "-xf", str(archive), "-C", str(source)], check=True)
    return source, hashlib.sha256(archive.read_bytes()).hexdigest()


def _inside(request_path: Path) -> None:
    request = json.loads(request_path.read_text())
    for name in request["required_commands"]:
        if not shutil.which(name):
            raise RuntimeError(f"required command missing: {name}")
    for name in request["required_paths"]:
        if not Path(name).exists():
            raise RuntimeError(f"required path missing: {name}")
    # The image startup may prepend its installed packages. Both this probe and
    # every nested `python -m ...` must resolve the candidate first instead.
    os.environ["PYTHONPATH"] = "/candidate:/gatekeeper"
    sys.path.insert(0, "/candidate")
    for name in request["source_modules"]:
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.origin or not Path(spec.origin).resolve().is_relative_to("/candidate"):
            raise RuntimeError(f"source module does not resolve inside candidate: {name}")
    Path("/evidence/started.json").write_text(json.dumps({
        "source_modules": {name: importlib.util.find_spec(name).origin
                           for name in request["source_modules"]},
        "pythonpath": os.environ["PYTHONPATH"],
    }))
    os.execv(sys.executable, [
        sys.executable, "/gatekeeper/semantic_pytest_gate.py",
        "--junit", "/evidence/junit.xml", "--json", "/evidence/semantic.json",
        "--cwd", "/candidate", "--stall-grace", str(request["stall_grace"]),
        "--poll", str(request["poll"]), "--", *request["cmd"],
    ])


def _run_until_started(argv: list[str], evidence: Path, grace: float):
    """Lease Docker startup only; the inner semantic gate owns test runtime."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + grace
    try:
        while not (evidence / "started.json").is_file():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, grace)
            try:
                out, err = proc.communicate(timeout=min(1.0, remaining))
                return subprocess.CompletedProcess(argv, proc.returncode, out, err)
            except subprocess.TimeoutExpired:
                pass
        out, err = proc.communicate()
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def run_container_pytest(cmd: list[str], wt: Path, config: dict,
                         stall_grace: float, poll: float) -> tuple[int, str, str, dict]:
    """Return the same rc/stdout/stderr/semantic tuple as the native runner.

    Evidence is durable and isolated per invocation. Docker's immutable local
    image ID, candidate commit/tree, exact argv, CID, semantic record and JUnit
    are retained together; no pull/build or shared container is used.
    """
    state = Path(os.environ.get("GK_STATE_DIR", str(Path.home() / ".cache/eda-fork-gatekeeper")))
    root = state / "fork-pytest"
    root.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix=f"{wt.name}-", dir=root)).resolve()
    records = evidence / "records"
    records.mkdir()
    record = {"outcome": "launch_error", "elapsed_seconds": 0.0,
              "mechanism": "validated_semantic_progress_lease", "evidence": str(evidence)}
    out = err = ""
    cid = ""
    rc = 2
    try:
        if not isinstance(config, dict) or config.get("image") != "released":
            raise ValueError("container configuration requires image=released")
        env = config.get("environment", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError("container environment must be a string mapping")
        for field in ("required_commands", "required_paths", "source_modules"):
            value = config.get(field, [])
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ValueError(f"container {field} must be a list of strings")
        version = json.loads((HERE.parent / "RELEASED.json").read_text())["version"]
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError("invalid released image version")
        image = f"ghcr.io/vibeic/vibeic-eda:{version}"
        image_id = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True, timeout=30).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("local image identity was not resolved")
        source = _source_identity(wt)
        snapshot, archive_sha256 = _snapshot(wt, source["head"], evidence)
        request = {"cmd": cmd, "stall_grace": stall_grace, "poll": poll,
                   **{k: config.get(k, []) for k in ("required_commands", "required_paths", "source_modules")}}
        (records / "request.json").write_text(json.dumps(request, indent=2) + "\n")
        record.update(image=image, image_id=image_id, source=source,
                      source_directory=str(wt.resolve()), source_archive_sha256=archive_sha256,
                      command=cmd, container_environment=env)
        argv = ["docker", "run", "--init", "--network", "none", "--name", f"fork-pytest-{uuid.uuid4().hex}",
                "--cidfile", str(evidence / "cid"),
                "--mount", f"type=bind,src={snapshot},dst=/candidate,readonly",
                "--mount", f"type=bind,src={HERE},dst=/gatekeeper,readonly",
                "--mount", f"type=bind,src={records},dst=/evidence",
                "--workdir", "/candidate"]
        env = {**env, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
               "PYTHONPATH": "/candidate:/gatekeeper", "MPLCONFIGDIR": "/tmp/matplotlib"}
        for key, value in env.items():
            argv += ["--env", f"{key}={value}"]
        argv += [image_id, "--skip", "python3", "/gatekeeper/fork_pytest_container.py",
                 "--inside", "/evidence/request.json"]
        # The inner semantic driver is the only progress supervisor. Docker
        # CPU/output activity cannot override its validated lifecycle lease.
        proc = _run_until_started(argv, records, stall_grace)
        out, err = proc.stdout, proc.stderr
        (evidence / "stdout.log").write_text(out)
        (evidence / "stderr.log").write_text(err)
        semantic = json.loads((records / "semantic.json").read_text())
        rc, graded = grade_receipts(proc.returncode, semantic, records / "junit.xml")
        record.update(graded)
        record["junit"] = str(records / "junit.xml")
        if _source_identity(wt) != source:
            rc = 2
            record.update(outcome="source_changed", reason="candidate identity changed during the check")
        if rc:
            err += "\n" + record["reason"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        rc = 2
        err += f"\nCONTAINER_PYTEST_NO_RECORD: {type(exc).__name__}: {exc}"
        record.update(outcome="startup_stalled" if isinstance(exc, subprocess.TimeoutExpired)
                      else "container_incomplete", reason=err.strip())
    finally:
        try:
            cid = (evidence / "cid").read_text().strip()
        except OSError:
            pass
        if re.fullmatch(r"[0-9a-f]{64}", cid):
            record["container_id"] = cid
            try:
                inspection = subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, timeout=30)
                (evidence / "container.json").write_text(inspection.stdout)
                cleanup = subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True, timeout=30)
                record["container_cleanup_rc"] = cleanup.returncode
                if cleanup.returncode:
                    raise RuntimeError(cleanup.stderr)
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                rc = 2
                record.update(outcome="container_cleanup_incomplete", reason=str(exc))
        record["exit_code"] = rc
        (evidence / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    return rc, out, err, record


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--inside":
        raise SystemExit("invoke through daily_merge.run_post_merge_checks")
    _inside(Path(sys.argv[2]))
