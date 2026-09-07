#!/usr/bin/env python3
"""The entrypoint must publish /tmp/passwd by rename, never write it in place.

WHAT WAS MEASURED (2026-09-07, image 0.3.51, vibeic-eda#179)
============================================================
The base image's ``/dockerstartup/scripts/generate_container_user.sh`` -- sourced
by the ENTRYPOINT before anything else -- created the nss_wrapper files with::

    install -m 0644 /etc/passwd /tmp/passwd

``install`` over an existing destination is not a write, it is a REPLACEMENT::

    unlinkat(AT_FDCWD, "/tmp/passwd", 0)                     = 0
    openat(AT_FDCWD, "/tmp/passwd", O_WRONLY|O_CREAT|O_EXCL) = 4

so the path does not exist between those two calls.  Measured inside the shipped
image, four shells sourcing the script the way ``ui_startup.sh`` does while one
reader polled the file:

    base script:  4639 reads, 252 saw NO FILE, 80 saw a SHORT file,
                  109 of the sourcings hit
                  "[ERROR] Failed to create nss_wrapper passwd/group files."
    this script:  4500 reads, 0, 0, 0

That error line is not cosmetic: the script ``exit 1``s there, and it is sourced
by the entrypoint under ``set -e``, so a colliding write KILLS THE CONTAINER AT
START.  The second writer loses on its own ``O_EXCL`` create -- ``install:
cannot create regular file '/tmp/passwd': File exists`` -- which is a writer
racing a writer, not a reader racing a writer.

A SECOND DEFECT WAS FOUND IN THE SAME FOUR LINES
------------------------------------------------
The duplicate guard was ``grep -q "^designer:"``, but this image's own
/etc/passwd already ships ``designer:x:1000:0::/headless:/bin/bash``, which the
copy carries into /tmp/passwd.  So for any ``--user`` uid other than 1000 the
guard matched a line the script had not written and the entry for the RUNNING
uid was never appended.  Measured on 0.3.51 through the entrypoint with
``--user 4242:4242``: ``getent passwd 4242`` returned nothing and ``id -un``
answered ``id: cannot find name for user ID 4242`` -- while /tmp/group DID get
``designers:x:4242:designer``, because /etc/group has no ``designers`` line to
collide with.  The guard now keys on what the script is about to write,
``designer:x:<uid>:``, which still matches at uid 1000 and so leaves the files
the fleet's own identity produces byte-identical.

WHAT THIS TEST ASSERTS
----------------------
Both directions, against the SHIPPED BYTES of tools/generate_container_user.sh:
the real publisher is extracted from the file and driven under concurrent load,
and the base's ``install``-and-append mechanism is driven through the same
harness as the mutation.  A run in which the mutation is NOT caught fails the
test rather than passing quietly: a red direction that is not red proves nothing
about the green one.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "generate_container_user.sh"
DOCKERFILE = ROOT / "Dockerfile"

#: Where the base image keeps the file this one replaces.
INSTALLED_PATH = "/dockerstartup/scripts/generate_container_user.sh"

#: The mechanism the base image used, restored verbatim as the mutation.
BASE_MECHANISM = """
_mutant_publish_nss_file() {
    local _source=$1 _destination=$2 _key=$3 _line=$4
    install -m 0644 "$_source" "$_destination" || return 1
    if ! grep -q -- "$_key" "$_destination"; then
        printf '%s\\n' "$_line" >> "$_destination"
    fi
}
"""


def _extract(name: str) -> str:
    """Return the shipped definition of ``name``, so the test drives real bytes.

    A re-typed copy of the function would agree with the file by coincidence,
    which is the failure mode this repository keeps meeting; the slice is
    asserted to be a plausible function body so a rename cannot silently empty
    it.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    opener = f"\n{name}() {{\n"
    assert opener in text, f"{SCRIPT} defines no {name}()"
    start = text.index(opener) + 1
    end = text.index("\n}\n", start) + len("\n}\n")
    body = text[start:end]
    assert body.count("\n") > 5, f"{name}() slice looks truncated: {body!r}"
    return body


def _uncommented(text: str) -> str:
    """The script with comment lines removed.

    Every claim below is about what the script DOES.  This file's own header
    quotes the defect it fixes, ``install -m 0644`` included, and a guard that
    flips when only prose changes is not a guard.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# --------------------------------------------------------------------------
# the property, both directions, under load
# --------------------------------------------------------------------------

#: Writers publish; one reader polls.  A reader may only ever see a COMPLETE
#: file: the source's size, or the source's size plus the appended line.  Any
#: other size, and any absence, is a partial state escaping to a reader.
DRIVER = r"""
set -u
SOURCE=$1; DEST=$2; PUBLISH=$3; SECONDS_=$4; WRITERS=$5; KEY=$6; LINE=$7

cp "$SOURCE" "$DEST"
SIZE=$(wc -c < "$SOURCE")
GROWN=$(( SIZE + ${#LINE} + 1 ))

for _ in $(seq "$WRITERS"); do
  ( end=$((SECONDS+SECONDS_))
    while [ $SECONDS -lt $end ]; do
      "$PUBLISH" "$SOURCE" "$DEST" "$KEY" "$LINE" 2>>"$DEST.writer-errors" || \
        echo "PUBLISH-FAILED" >> "$DEST.writer-errors"
    done ) &
done

reads=0; partial=0; absent=0
end=$((SECONDS+SECONDS_))
while [ $SECONDS -lt $end ]; do
  reads=$((reads+1))
  if [ ! -f "$DEST" ]; then absent=$((absent+1)); continue; fi
  b=$(wc -c < "$DEST" 2>/dev/null || echo -1)
  if [ "$b" != "$SIZE" ] && [ "$b" != "$GROWN" ]; then partial=$((partial+1)); fi
done
wait
echo "reads=$reads absent=$absent partial=$partial"
echo "writer_errors=$(wc -l < "$DEST.writer-errors" 2>/dev/null || echo 0)"
echo "final_key_lines=$(grep -c -- "$KEY" "$DEST" 2>/dev/null || true)"
echo "final_bytes=$(wc -c < "$DEST" 2>/dev/null || echo -1)"
"""

KEY = "^designer:x:4242:"
LINE = "designer:x:4242:4242:Default Application User:/headless:/bin/bash"


def _run_load(tmp_path: Path, mechanism: str, publisher: str,
              *, seconds: int = 3, writers: int = 4) -> dict:
    """Drive ``publisher`` under ``writers`` concurrent publishers and one reader.

    The source is deliberately large: the window a reader can catch is the copy
    itself, so a small file would make the difference between the two mechanisms
    a matter of luck rather than of measurement.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source"
    with source.open("wb") as fh:
        row = b"user%05d:x:%d:%d::/home/u:/bin/sh\n"
        for i in range(120_000):
            fh.write(row % (i, 3000 + i, 3000 + i))
    program = tmp_path / "driver.sh"
    program.write_text(mechanism + "\n" + DRIVER, encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(program), str(source), str(tmp_path / "passwd"),
         publisher, str(seconds), str(writers), KEY, LINE],
        capture_output=True, text=True, timeout=seconds * 20 + 120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = dict(
        part.split("=", 1) for part in proc.stdout.split()
        if "=" in part
    )
    return {k: int(v) for k, v in out.items()}


def test_the_base_mechanism_lets_a_reader_see_a_partial_or_absent_file(tmp_path):
    """RED. `install` unlinks the destination and copies into a fresh inode."""
    seen = _run_load(tmp_path, BASE_MECHANISM, "_mutant_publish_nss_file")
    assert seen["reads"] > 100, f"the reader barely ran: {seen}"
    assert seen["absent"] + seen["partial"] > 0, (
        "the mutation was not caught, so this run says nothing about the fix: "
        f"{seen}. `install -m 0644` unlinks the destination and copies into a "
        "new inode; if no reader saw that, the harness stopped exercising it."
    )


def test_the_shipped_publisher_never_exposes_a_partial_or_absent_file(tmp_path):
    """GREEN. The same load against the real function in tools/."""
    seen = _run_load(tmp_path, _extract("_vibeic_publish_nss_file"),
                     "_vibeic_publish_nss_file")
    assert seen["reads"] > 100, f"the reader barely ran: {seen}"
    assert seen["absent"] == 0, f"a reader found no file at all: {seen}"
    assert seen["partial"] == 0, f"a reader saw a partial file: {seen}"


def test_concurrent_publishers_do_not_fail_each_other(tmp_path):
    """The writer-versus-writer half: `install`'s O_EXCL create loses, ours does not.

    That loss is what reached the entrypoint as
    "[ERROR] Failed to create nss_wrapper passwd/group files." and `exit 1`.
    """
    mutant = _run_load(tmp_path / "a", BASE_MECHANISM, "_mutant_publish_nss_file")
    ours = _run_load(tmp_path / "b", _extract("_vibeic_publish_nss_file"),
                     "_vibeic_publish_nss_file")
    assert mutant["writer_errors"] > 0, (
        f"the mutation did not collide, so the green side proves nothing: {mutant}"
    )
    assert ours["writer_errors"] == 0, f"a publisher failed: {ours}"


def test_publishing_is_idempotent_under_concurrency(tmp_path):
    """However many publishers run, the entry appears exactly once."""
    seen = _run_load(tmp_path, _extract("_vibeic_publish_nss_file"),
                     "_vibeic_publish_nss_file", seconds=2, writers=6)
    assert seen["final_key_lines"] == 1, (
        f"the entry was duplicated or lost: {seen}"
    )


# --------------------------------------------------------------------------
# the guard keys on the line the script writes, not on the name `designer`
# --------------------------------------------------------------------------

def _publish_once(tmp_path: Path, source_text: str, uid: int) -> str:
    program = tmp_path / "once.sh"
    program.write_text(
        _extract("_vibeic_publish_nss_file")
        + f'\n_vibeic_publish_nss_file "$1" "$2" "^designer:x:{uid}:" '
        f'"designer:x:{uid}:{uid}:Default Application User:/headless:/bin/bash"\n',
        encoding="utf-8",
    )
    source = tmp_path / "src"
    source.write_text(source_text, encoding="utf-8")
    dest = tmp_path / "passwd"
    proc = subprocess.run(["bash", str(program), str(source), str(dest)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return dest.read_text(encoding="utf-8")


#: What this image's /etc/passwd actually ends with (measured on 0.3.51).
SHIPPED_TAIL = "nova:x:111:118::/var/lib/nova:/bin/bash\ndesigner:x:1000:0::/headless:/bin/bash\n"


def test_uid_1000_is_left_byte_identical_to_what_the_base_produced(tmp_path):
    """No regression for the identity the fleet runs.

    The base's `^designer:` guard matched the shipped line and appended nothing.
    A uid-keyed guard must reach the same answer here, or the fix has changed
    the file every container on the fleet gets.
    """
    out = _publish_once(tmp_path, SHIPPED_TAIL, 1000)
    assert out == SHIPPED_TAIL


def test_a_uid_the_image_does_not_ship_gets_a_resolvable_entry(tmp_path):
    """The defect: at uid 4242 the base appended nothing and `id -un` failed."""
    out = _publish_once(tmp_path, SHIPPED_TAIL, 4242)
    assert out.startswith(SHIPPED_TAIL)
    assert "designer:x:4242:4242:Default Application User:/headless:/bin/bash" in out
    assert out.count("designer:x:4242:") == 1


def test_a_source_without_a_trailing_newline_does_not_swallow_the_entry(tmp_path):
    """`>>` onto an unterminated last line would have joined the two."""
    out = _publish_once(tmp_path, "root:x:0:0::/root:/bin/sh", 4242)
    lines = out.splitlines()
    assert lines[0] == "root:x:0:0::/root:/bin/sh"
    assert lines[1].startswith("designer:x:4242:")


def test_an_unreadable_source_fails_loudly_and_leaves_the_destination_alone(tmp_path):
    """MEASURED while writing this fix: the publisher reported success on ENOENT.

    The build of the temp file ran under ``set -e`` inside a subshell used as the
    condition of an ``if``, and bash suppresses errexit for everything inside an
    ``if`` condition -- subshells included -- so a failing ``cat`` produced an
    empty temp file that was then renamed over a good /tmp/passwd, and the
    function returned 0.  A publisher that cannot read its source must say so and
    leave the previous complete file in place.
    """
    program = tmp_path / "fail.sh"
    program.write_text(
        _extract("_vibeic_publish_nss_file")
        + '\n_vibeic_publish_nss_file "$1" "$2" "^x:" "x:1"\n',
        encoding="utf-8",
    )
    dest = tmp_path / "passwd"
    dest.write_text("previous:complete:content\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(program), str(tmp_path / "does-not-exist"), str(dest)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0, "an unreadable source was reported as published"
    assert dest.read_text(encoding="utf-8") == "previous:complete:content\n"
    leftovers = sorted(p.name for p in tmp_path.iterdir()
                       if p.name.startswith("passwd."))
    assert leftovers == [], f"a half-built temp file was left behind: {leftovers}"


# --------------------------------------------------------------------------
# the readiness sentinel
# --------------------------------------------------------------------------

def test_the_sentinel_is_published_by_rename_and_honours_its_variable(tmp_path):
    sentinel = tmp_path / "ready"
    program = tmp_path / "ready.sh"
    program.write_text(
        f'VIBEIC_USER_READY="{sentinel}"\n'
        + _extract("_vibeic_publish_ready")
        + '\n_vibeic_publish_ready "USER_ID=4242"\n',
        encoding="utf-8",
    )
    proc = subprocess.run(["bash", str(program)], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert sentinel.read_text(encoding="utf-8") == "ready\nUSER_ID=4242\n"
    assert oct(sentinel.stat().st_mode & 0o777) == "0o644"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("ready.")
                 and p.name != "ready.sh"]
    assert leftovers == [], f"the temp file was not renamed away: {leftovers}"


# --------------------------------------------------------------------------
# what the script ASKS FOR, driven end to end with a shimmed `id`
# --------------------------------------------------------------------------
#
# The tests above drive the publisher; these drive the CALL SITES.  Restoring the
# base's `^designer:` guard changes neither the publisher nor the Dockerfile, so
# without this the second defect could come back through a green suite -- which
# is exactly what a trial mutation showed before these were written.

def _drive(tmp_path: Path, uid: int, gid: int) -> list[tuple[str, list[str]]]:
    """Source the real script with `id` shimmed and the publishers recording.

    Only the two publishers are replaced, so every other line -- the uid branch,
    the HOME check, the guard expressions, the ordering -- is the shipped one.
    Nothing touches /tmp: the stubs record and return.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    binder = tmp_path / "bin"
    binder.mkdir(exist_ok=True)
    shim = binder / "id"
    shim.write_text(
        "#!/bin/sh\n"
        f'case "$1" in -u) echo {uid} ;; -g) echo {gid} ;; '
        '*) exec /usr/bin/id "$@" ;; esac\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    text = SCRIPT.read_text(encoding="utf-8")
    for name in ("_vibeic_publish_nss_file", "_vibeic_publish_ready"):
        opener = f"\n{name}() {{\n"
        start = text.index(opener) + 1
        end = text.index("\n}\n", start) + len("\n}\n")
        text = text[:start] + (
            f'{name}() {{\n'
            f'    printf "CALL<{name}>\\n" >> "$VIBEIC_TEST_RECORD"\n'
            f'    printf "ARG<%s>\\n" "$@" >> "$VIBEIC_TEST_RECORD"\n'
            "    return 0\n}\n"
        ) + text[end:]

    program = tmp_path / "driven.sh"
    program.write_text(text, encoding="utf-8")
    record = tmp_path / "record"
    env = dict(os.environ,
               PATH=f"{binder}:{os.environ['PATH']}",
               HOME="/headless",
               IIC_OSIC_TOOLS_QUIET="1",
               VIBEIC_TEST_RECORD=str(record),
               VIBEIC_USER_READY=str(tmp_path / "ready"))
    proc = subprocess.run(["bash", "-c", f'source "{program}"'], env=env,
                          capture_output=True, text=True, timeout=60)
    assert record.exists(), (
        "the script reached neither publisher, so it announced nothing and "
        f"wrote nothing. stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    calls: list[tuple[str, list[str]]] = []
    for chunk in record.read_text(encoding="utf-8").split("CALL<")[1:]:
        name, _, rest = chunk.partition(">\n")
        calls.append((name, re.findall(r"ARG<(.*?)>\n", rest, re.S)))
    return calls


def test_the_passwd_guard_is_keyed_on_the_running_uid(tmp_path):
    """`^designer:` matched a line /etc/passwd already ships. `^designer:x:<uid>:` does not."""
    calls = _drive(tmp_path, 4242, 4242)
    passwd = [args for name, args in calls
              if name == "_vibeic_publish_nss_file" and args[0] == "/etc/passwd"]
    assert len(passwd) == 1, calls
    source, destination, key, line = passwd[0]
    assert destination == "/tmp/passwd"
    assert key == "^designer:x:4242:", (
        "the guard does not name the uid the container is running as, so a "
        f"pre-existing `designer` line suppresses the entry: {key!r}"
    )
    assert line.startswith("designer:x:4242:4242:")
    assert line.endswith(":/headless:/bin/bash")


def test_the_group_guard_is_keyed_on_the_running_gid(tmp_path):
    calls = _drive(tmp_path, 4242, 777)
    group = [args for name, args in calls
             if name == "_vibeic_publish_nss_file" and args[0] == "/etc/group"]
    assert len(group) == 1, calls
    _, destination, key, line = group[0]
    assert destination == "/tmp/group"
    assert key == "^designers:x:777:", key
    assert line == "designers:x:777:designer"


def test_at_uid_1000_the_guard_still_matches_the_line_the_image_ships(tmp_path):
    """The no-regression half, at the call site rather than in the publisher."""
    calls = _drive(tmp_path, 1000, 1000)
    passwd = [args for name, args in calls
              if name == "_vibeic_publish_nss_file" and args[0] == "/etc/passwd"]
    assert re.match(r"^\^designer:x:1000:", passwd[0][2]), passwd
    assert re.search(passwd[0][2], SHIPPED_TAIL, re.M), (
        "the guard no longer matches the designer line this image's /etc/passwd "
        "ships, so uid 1000 would get a second entry it never had"
    )


def test_root_publishes_the_sentinel_and_no_nss_files(tmp_path):
    """A waiter must not hang on a container that legitimately does no setup."""
    calls = _drive(tmp_path, 0, 0)
    assert [name for name, _ in calls] == ["_vibeic_publish_ready"], calls
    assert "USER_ID=0" in calls[0][1][0]


def test_the_sentinel_is_written_after_the_files_it_announces():
    """A sentinel that lands first announces a readiness that does not exist."""
    text = _uncommented(SCRIPT.read_text(encoding="utf-8"))
    passwd_call = text.index('_vibeic_publish_nss_file /etc/passwd')
    group_call = text.index('_vibeic_publish_nss_file /etc/group')
    ready_call = text.index('_vibeic_publish_ready \\')
    assert passwd_call < ready_call and group_call < ready_call


# --------------------------------------------------------------------------
# the file the image ships, and the wiring that ships it
# --------------------------------------------------------------------------

def test_the_script_is_valid_bash():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_no_in_place_write_of_the_nss_files_returns():
    """The regression this file exists to catch, stated as a rule not a spelling.

    Any write whose destination is the live path -- `install`, `cp`, `>`, `tee`
    -- reintroduces the window.  Only a rename may name /tmp/passwd or
    /tmp/group as its target.
    """
    text = _uncommented(SCRIPT.read_text(encoding="utf-8"))
    forbidden = re.compile(
        r"\b(install|cp|tee)\b[^\n]*\$?\{?(NSS_WRAPPER_PASSWD|NSS_WRAPPER_GROUP|_destination)\b"
    )
    assert not forbidden.search(text), (
        "the nss_wrapper files are written in place again: "
        f"{forbidden.search(text).group(0)!r}"
    )
    assert re.search(r'>\s*"\$_temp"', text), "nothing writes the temp file"
    assert 'mv -f -- "$_temp" "$_destination"' in text, (
        "the passwd/group publisher no longer renames into place"
    )
    assert 'mv -f -- "$_temp" "$VIBEIC_USER_READY"' in text, (
        "the sentinel publisher no longer renames into place"
    )


def test_the_dockerfile_installs_it_over_the_base_images_copy():
    """A script in tools/ that the image never installs fixes nothing."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^COPY[^\n]*tools/generate_container_user\.sh", text, re.M), (
        "no COPY brings tools/generate_container_user.sh into the image"
    )
    assert INSTALLED_PATH in text, (
        f"nothing installs it over the base image's {INSTALLED_PATH}"
    )
    assert re.search(r"install -m 0755[^\n\\]*generate_container_user\.sh", text), (
        "the installed copy must keep the base file's 0755 mode"
    )


def test_the_dockerfile_asserts_the_installed_copy_is_ours():
    """Copying is not installing, and installing is not verifying.

    The compose greps the file it just wrote, so a COPY that lands the wrong
    bytes -- or a base image that starts shipping the file somewhere else --
    fails the build instead of shipping the defect back.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'grep -q \'mv -f -- "$_temp" "$_destination"\'' in text
    assert "still writes /tmp/passwd in place" in text


def test_the_copy_costs_no_layer_the_budget_does_not_have():
    """It rides an existing COPY; a second COPY is a layer the root build lacks."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    copies = re.findall(r"^COPY[^\n]*generate_container_user\.sh[^\n]*$",
                        text, re.M)
    assert len(copies) == 1, f"expected exactly one COPY, found {copies}"
    assert "refresh_versions_manifest.sh" in copies[0], (
        "the script no longer rides the existing COPY, so it costs a layer: "
        f"{copies[0]}"
    )


# --------------------------------------------------------------------------
# the installed path, in the image itself
# --------------------------------------------------------------------------

def _image() -> str | None:
    """An image to grade, or None. Never a default: a guess would grade nothing."""
    return os.environ.get("VIBEIC_EDA_TEST_IMAGE")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_the_running_image_publishes_the_sentinel_and_survives_concurrency():
    """End-to-end, against the ENTRYPOINT's own copy of the script.

    Skipped without VIBEIC_EDA_TEST_IMAGE because the image is 31 GB and is not
    present on a CI runner; on a fleet host it is the only check that grades the
    file the entrypoint actually sources.
    """
    image = _image()
    if not image:
        pytest.skip("set VIBEIC_EDA_TEST_IMAGE=<image> to grade a built image")
    probe = r"""
set -u
: > /tmp/werr
for i in 1 2 3 4; do
  ( end=$((SECONDS+4))
    while [ $SECONDS -lt $end ]; do
      ( set -e; source /dockerstartup/scripts/generate_container_user.sh ) >>/tmp/werr 2>&1
    done ) &
done
end=$((SECONDS+4)); absent=0
while [ $SECONDS -lt $end ]; do [ -f /tmp/passwd ] || absent=$((absent+1)); done
wait
echo "absent=$absent"
echo "errors=$(grep -c 'Failed to create nss_wrapper' /tmp/werr || true)"
echo "sentinel=$([ -f /tmp/.vibeic-user-ready ] && echo yes || echo no)"
echo "uid_lines=$(grep -c "^designer:x:$(id -u):" /tmp/passwd || true)"
"""
    proc = subprocess.run(
        ["docker", "run", "--rm", "-i", "--memory", "512m", "--memory-swap",
         "512m", "-e", "IIC_OSIC_TOOLS_QUIET=1", "--user",
         f"{os.getuid()}:{os.getgid()}", image, "--skip", "bash", "-s"],
        input=probe, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    seen = dict(part.split("=", 1) for part in proc.stdout.split() if "=" in part)
    assert seen.get("absent") == "0", proc.stdout
    assert seen.get("errors") == "0", proc.stdout
    assert seen.get("sentinel") == "yes", proc.stdout
    assert seen.get("uid_lines") == "1", proc.stdout
