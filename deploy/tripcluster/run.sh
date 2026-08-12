#!/bin/sh
# Cluster trips, then rebuild the wall's Views album. Loop forever.
#
# A plain loop rather than cron: there is one job, it has no dependencies, and
# a sleeping shell is easier to reason about than a cron daemon inside a
# container that nobody will ever attach to.
set -u

INTERVAL="${RUN_INTERVAL_SECONDS:-604800}"      # 7 days
CONFIG="${CONFIG_PATH:-/config/config.yaml}"

# The key arrives as a file so it never sits in the compose file or in `docker
# inspect`. Immich rejects a key with a trailing newline, so strip one.
if [ -n "${IMMICH_API_KEY_FILE:-}" ] && [ -r "${IMMICH_API_KEY_FILE}" ]; then
    IMMICH_API_KEY="$(tr -d '\r\n' < "${IMMICH_API_KEY_FILE}")"
    export IMMICH_API_KEY
fi

if [ -z "${IMMICH_API_KEY:-}" ]; then
    echo "FATAL no API key: set IMMICH_API_KEY_FILE to a readable file" >&2
    exit 2
fi

run_once() {
    echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') clustering trips"
    # --apply is deliberate here. The dry run is the default everywhere else,
    # but a scheduled job that only ever prints a plan is pointless. Album
    # names you have edited by hand are protected by custom_name in trips.db.
    if ! python cluster_trips.py --config "$CONFIG" --apply; then
        echo "!!! clustering failed; leaving the Views album alone" >&2
        return 1
    fi

    echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') rebuilding Views album"
    # Only reached when clustering succeeded: rebuilding Views from a
    # half-updated trips.db would drop photos off the wall.
    python tools/build_views_album.py --config "$CONFIG" --apply || {
        echo "!!! Views rebuild failed" >&2
        return 1
    }
}

while true; do
    run_once || echo "=== run failed; retrying at the next interval" >&2
    echo "=== sleeping ${INTERVAL}s"
    sleep "$INTERVAL"
done
