#!/bin/sh
# Cluster trips, then rebuild the wall's Views album. Loop forever.
#
# A plain loop rather than cron: there is one job, it has no dependencies, and
# a sleeping shell is easier to reason about than a cron daemon inside a
# container that nobody will ever attach to.
set -u

INTERVAL="${RUN_INTERVAL_SECONDS:-604800}"      # 7 days
CONFIG="${CONFIG_PATH:-/config/config.yaml}"
LOG=/tmp/last_run.log

# The key arrives as a file so it never sits in the compose file or in `docker
# inspect`. Immich rejects a key with a trailing newline, so strip one.
if [ -n "${IMMICH_API_KEY_FILE:-}" ] && [ -r "${IMMICH_API_KEY_FILE}" ]; then
    IMMICH_API_KEY="$(tr -d '\r\n' < "${IMMICH_API_KEY_FILE}")"
    export IMMICH_API_KEY
fi

if [ -z "${IMMICH_API_KEY:-}" ]; then
    echo "FATAL no API key: set IMMICH_API_KEY_FILE to a readable file" >&2
    python /usr/local/bin/notify.py "trip clusterer: no API key, not starting"
    exit 2
fi

notify() {
    python /usr/local/bin/notify.py "$@" || true
}

run_once() {
    echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') clustering trips"
    # --apply is deliberate here. The dry run is the default everywhere else,
    # but a scheduled job that only ever prints a plan is pointless. Album
    # names you have edited by hand are protected by custom_name in trips.db.
    # Redirect then cat, rather than piping to tee: in POSIX sh a pipeline
    # reports the *last* command's status, so `cmd | tee` would report tee's
    # success and every failure would look like a clean run.
    python cluster_trips.py --config "$CONFIG" --apply > "$LOG" 2>&1
    rc=$?
    cat "$LOG"
    [ "$rc" -ne 0 ] && return 1
    # Backstop: the CLI logs some problems and still exits 0.
    grep -qE '^(ERROR|FATAL)' "$LOG" && return 1

    echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') rebuilding Views album"
    # Only reached when clustering succeeded: rebuilding Views from a
    # half-updated trips.db would drop photos off the wall.
    python tools/build_views_album.py --config "$CONFIG" \
        --scores /state/apple_scores.json --apply >> "$LOG" 2>&1
    rc=$?
    tail -n 12 "$LOG"
    [ "$rc" -ne 0 ] && return 2
    return 0
}

# Report only on a change of state, so a webhook does not become a weekly
# heartbeat nobody reads. A run that fails every week notifies once.
LAST_STATE=ok

while true; do
    run_once
    rc=$?

    if [ "$rc" -eq 0 ]; then
        if [ "$LAST_STATE" != "ok" ]; then
            notify "trip clusterer: recovered, the weekly run succeeded"
        fi
        LAST_STATE=ok
    else
        [ "$rc" -eq 1 ] && stage="clustering" || stage="Views rebuild"
        echo "!!! $stage failed (rc=$rc)" >&2
        if [ "$LAST_STATE" = "ok" ]; then
            # Last few lines only — a webhook is not a log viewer.
            detail="$(tail -n 6 "$LOG" 2>/dev/null | tr '\n' ' ')"
            notify "trip clusterer: $stage FAILED on $(hostname). $detail"
        fi
        LAST_STATE=failed
    fi

    echo "=== sleeping ${INTERVAL}s"
    sleep "$INTERVAL"
done
