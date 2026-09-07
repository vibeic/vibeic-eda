#!/bin/bash
# SPDX-FileCopyrightText: 2022-2026 Harald Pretl and Georg Zachl
# Johannes Kepler University, Department for Integrated Circuits
# SPDX-License-Identifier: Apache-2.0
#
# Set current user in nss_wrapper.
#
# vibeic's copy of the base image's /dockerstartup/scripts/generate_container_user.sh,
# sourced by /dockerstartup/scripts/ui_startup.sh (the image ENTRYPOINT) before
# anything else runs.  A HARDENING of that script, not a rewrite: every message,
# every exported variable, every library-path fallback and the root short-circuit
# are unchanged.  Three defects are fixed, each MEASURED on 0.3.51 (vibeic-eda#179).
#
# 1. THE DESTINATION WAS ABSENT, NOT MERELY HALF-WRITTEN.
#    `install -m 0644 /etc/passwd /tmp/passwd` over an existing destination is
#    (strace, coreutils 9.x):
#        unlinkat(AT_FDCWD, "/tmp/passwd", 0)                        = 0
#        openat(AT_FDCWD, "/tmp/passwd", O_WRONLY|O_CREAT|O_EXCL)    = 4
#    so between those two calls the path does not exist at all.  Measured with
#    four concurrent writers and one reader over a shared directory: 356 of 4000
#    reads (8.9%) found no file.  The same shape with a temp file and a rename:
#    0 of 3894.  Worse, two concurrent writers make the second `install` fail
#    EEXIST on its own O_EXCL create, which is exactly the base script's
#    "[ERROR] Failed to create nss_wrapper passwd/group files." path -- and that
#    path `exit 1`s the ENTRYPOINT, killing the container at start.
#    FIX: build the file under a fresh mktemp name in the same directory and
#    rename(2) it into place.  A reader sees the old complete inode or the new
#    complete inode, never a gap, and two writers cannot collide.
#
# 2. THE DUPLICATE GUARD MATCHED A LINE IT DID NOT WRITE.
#    The guard was `grep -q "^designer:"`, but this image's /etc/passwd ALREADY
#    ships `designer:x:1000:0::/headless:/bin/bash`, which the copy carries into
#    /tmp/passwd.  So for any `--user` uid other than 1000 the guard matched that
#    pre-existing line and the entry for the RUNNING uid was never appended.
#    Measured, 0.3.51, `--user 4242:4242` through the entrypoint:
#        getent passwd 4242  -> (nothing)
#        id -un              -> id: cannot find name for user ID 4242
#    while /tmp/group DID get `designers:x:4242:designer`, because /etc/group has
#    no `designers` line to collide with.  The two halves disagreed.
#    FIX: guard on the KEY THIS SCRIPT IS ABOUT TO WRITE -- `designer:x:<uid>:`
#    and `designers:x:<gid>:` -- which is what "guard against duplicate entries"
#    meant.  At uid 1000 the shipped line still matches, so /tmp/passwd is
#    byte-identical to what the base produced for the identity the fleet runs.
#
# 3. NOTHING SIGNALLED THAT THE SETUP HAD FINISHED.
#    FIX: /tmp/.vibeic-user-ready, published by rename AFTER both files are in
#    place, so a harness can wait for it:
#        docker exec <c> sh -c 'until [ -f /tmp/.vibeic-user-ready ]; do sleep 0.1; done'
#    It is written on the root path too -- a waiter must not hang forever on a
#    container that legitimately does no nss_wrapper setup -- and it is never
#    removed at the start of a re-run: with an atomic publisher readiness never
#    regresses, so a waiter must never see it disappear.
#
# MEASURED AND WORTH KNOWING BEFORE READING /tmp/passwd INTO A THEORY: the image
# config carries no LD_PRELOAD and no NSS_WRAPPER_* ENV, so `docker exec` starts
# WITHOUT nss_wrapper and never reads these files at all.  Only the entrypoint's
# own descendants do.  The files here matter to the container's CMD, not to an
# exec that lands beside it.

USER_ID=$(id -u)
GROUP_ID=$(id -g)
[ -z "${IIC_OSIC_TOOLS_QUIET}" ] && echo "[INFO] USER_ID: $USER_ID, GROUP_ID: $GROUP_ID"

# Where readiness is announced.  Overridable so a test can drive the real script
# without writing into the caller's /tmp; the default is what the image ships.
VIBEIC_USER_READY="${VIBEIC_USER_READY:-/tmp/.vibeic-user-ready}"

# Publish $1 (plus $4, unless a line matching $3 is already in $1) at $2, by
# rename.  Returns non-zero without touching $2 if any step fails: a half-built
# temp file is removed rather than published, because a destination that still
# holds the previous complete file is strictly better than one that holds a
# partial new one.
_vibeic_publish_nss_file() {
    local _source=$1 _destination=$2 _key=$3 _line=$4
    local _temp _rc=0

    _temp=$(mktemp "${_destination}.vibeic-XXXXXX") || return 1

    # A SUBSHELL, not a brace group, and explicit `|| exit` rather than `set -e`.
    # In a brace group the `exit` would end the shell that SOURCED this script --
    # the entrypoint, i.e. the container.  And `set -e` would be INERT here:
    # bash suppresses errexit for everything inside the condition of an `if`,
    # subshells included, so an unreadable /etc/passwd was reported as a
    # successful publish until this was measured.
    if ! (
        cat -- "$_source" || exit 1
        if ! grep -q -- "$_key" "$_source"; then
            # /etc/passwd and /etc/group end in a newline; a source that does not
            # would otherwise have its last line joined to the appended one.
            if [ -s "$_source" ] && [ -n "$(tail -c 1 -- "$_source")" ]; then
                printf '\n' || exit 1
            fi
            printf '%s\n' "$_line" || exit 1
        fi
        exit 0
    ) > "$_temp"; then
        _rc=1
    fi

    if [ "$_rc" -eq 0 ]; then
        chmod 0644 -- "$_temp" || _rc=1
    fi
    if [ "$_rc" -eq 0 ]; then
        # rename(2), same directory: atomic for every reader, and it replaces a
        # pre-existing symlink instead of following it -- the property the base
        # script reached for `install` to get.
        mv -f -- "$_temp" "$_destination" || _rc=1
    fi
    if [ "$_rc" -ne 0 ]; then
        rm -f -- "$_temp"
    fi
    return "$_rc"
}

# Announce readiness by the same rename, so a waiter never reads a half-written
# sentinel either.
_vibeic_publish_ready() {
    local _temp _rc=0
    _temp=$(mktemp "${VIBEIC_USER_READY}.XXXXXX") || return 1
    printf 'ready\n%s\n' "$*" > "$_temp" || _rc=1
    if [ "$_rc" -eq 0 ]; then
        chmod 0644 -- "$_temp" || _rc=1
    fi
    if [ "$_rc" -eq 0 ]; then
        mv -f -- "$_temp" "$VIBEIC_USER_READY" || _rc=1
    fi
    if [ "$_rc" -ne 0 ]; then
        rm -f -- "$_temp"
    fi
    return "$_rc"
}

if [[ "$USER_ID" != "0" ]]; then
    if [ -z "${HOME}" ]; then
        echo "[ERROR] HOME is not set; cannot generate nss_wrapper passwd entry."
        return 1 2>/dev/null || exit 1
    fi

    NSS_WRAPPER_PASSWD=/tmp/passwd
    NSS_WRAPPER_GROUP=/tmp/group

    # 4th field of /etc/group is a comma-separated list of *usernames*, not UIDs.
    if ! _vibeic_publish_nss_file /etc/passwd "$NSS_WRAPPER_PASSWD" \
            "^designer:x:${USER_ID}:" \
            "designer:x:${USER_ID}:${GROUP_ID}:Default Application User:${HOME}:/bin/bash" \
        || ! _vibeic_publish_nss_file /etc/group "$NSS_WRAPPER_GROUP" \
            "^designers:x:${GROUP_ID}:" \
            "designers:x:${GROUP_ID}:designer"; then
        echo "[ERROR] Failed to create nss_wrapper passwd/group files."
        unset -f _vibeic_publish_nss_file _vibeic_publish_ready
        return 1 2>/dev/null || exit 1
    fi

    export NSS_WRAPPER_PASSWD
    export NSS_WRAPPER_GROUP

    if [ -r /usr/lib/libnss_wrapper.so ]; then
        NSS_WRAPPER_LIB=/usr/lib/libnss_wrapper.so
    elif [ -r /usr/lib64/libnss_wrapper.so ]; then
        NSS_WRAPPER_LIB=/usr/lib64/libnss_wrapper.so
    elif [ -r /usr/lib/x86_64-linux-gnu/libnss_wrapper.so ]; then
        NSS_WRAPPER_LIB=/usr/lib/x86_64-linux-gnu/libnss_wrapper.so
    elif [ -r /usr/lib/aarch64-linux-gnu/libnss_wrapper.so ]; then
        NSS_WRAPPER_LIB=/usr/lib/aarch64-linux-gnu/libnss_wrapper.so
    else
        echo "[ERROR] No libnss_wrapper.so installed!"
        unset -f _vibeic_publish_nss_file _vibeic_publish_ready
        return 1 2>/dev/null || exit 1
    fi

    # Prepend to LD_PRELOAD; preserve any value already set by the caller.
    export LD_PRELOAD="${NSS_WRAPPER_LIB}${LD_PRELOAD:+ $LD_PRELOAD}"
    export USER=designer

    if ! _vibeic_publish_ready \
            "USER_ID=${USER_ID}
GROUP_ID=${GROUP_ID}
NSS_WRAPPER_PASSWD=${NSS_WRAPPER_PASSWD}
NSS_WRAPPER_GROUP=${NSS_WRAPPER_GROUP}"; then
        echo "[ERROR] Failed to publish the container user readiness sentinel."
        unset -f _vibeic_publish_nss_file _vibeic_publish_ready
        return 1 2>/dev/null || exit 1
    fi
else
    # Root does no nss_wrapper setup, and says so rather than leaving a waiter
    # to time out against a container that is in fact ready.
    if ! _vibeic_publish_ready "USER_ID=0
NSS_WRAPPER=unused"; then
        echo "[ERROR] Failed to publish the container user readiness sentinel."
        unset -f _vibeic_publish_nss_file _vibeic_publish_ready
        return 1 2>/dev/null || exit 1
    fi
fi

unset -f _vibeic_publish_nss_file _vibeic_publish_ready
