#!/usr/bin/env bash
# One serialization inode for every supported vibeic-eda publisher.
#
# Do not derive this from a checkout or GK_STATE_DIR: two clones (or a test
# state directory accidentally left in a production environment) must not be
# able to publish mutable tags concurrently.  The uid database is the same
# authority daily_release.py uses, so HOME/XDG overrides cannot fork the lock.

vibeic_release_lock_path() {
    local account_home
    account_home="$(/usr/bin/getent passwd "$(/usr/bin/id -u)" \
        | /usr/bin/awk -F: 'NR == 1 { print $6 }')"
    [ -n "${account_home}" ] || {
        echo "[UNMEASURED] cannot resolve release account home" >&2
        return 1
    }
    printf '%s\n' "${account_home}/.cache/eda-fork-gatekeeper/daily-release.lock"
}

vibeic_acquire_release_lock() {
    local lock_path="${1:-}" lock_dir metadata expected
    [ -n "${lock_path}" ] || lock_path="$(vibeic_release_lock_path)" || return 1
    lock_dir="$(/usr/bin/dirname -- "${lock_path}")"
    /usr/bin/mkdir -p -- "${lock_dir}" || return 1
    if [ -L "${lock_path}" ]; then
        echo "[UNMEASURED] release lock is a symlink: ${lock_path}" >&2
        return 1
    fi

    # FD 8 remains open for this shell's lifetime and is inherited by commands
    # it launches. The kernel releases the lock on every exit/signal path.
    exec 8>>"${lock_path}" || return 1
    metadata="$(/usr/bin/stat -Lc '%F:%h:%u' "/proc/$$/fd/8" 2>/dev/null)" \
        || return 1
    expected="regular file:1:$(/usr/bin/id -u)"
    if [ "${metadata}" != "${expected}" ]; then
        echo "[UNMEASURED] release lock is not a private regular file: " \
             "${lock_path} (${metadata})" >&2
        return 1
    fi
    if ! /usr/bin/flock -n 8; then
        echo "[BLOCKED] another release owns ${lock_path}; no publication " \
             "was attempted" >&2
        return 1
    fi
    export VIBEIC_RELEASE_LOCK_PATH="${lock_path}"
}
