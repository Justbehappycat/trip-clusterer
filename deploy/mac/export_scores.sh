#!/bin/sh
# Export Apple's aesthetic scores and push them to the Synology.
#
# The Mac Mini is in the architecture for exactly one reason: the macOS Photos
# library is the only place these scores exist, and Apple has already computed
# them. Everything else runs on the NAS.
#
# Installed as a LaunchAgent — see com.tripcluster.scores.plist. Runs a few
# hours before the NAS clustering job, so the scores are fresh when the wall is
# rebuilt.
set -eu

REPO="${REPO:-$HOME/Trip-plan}"
OUT="${OUT:-$HOME/.cache/tripcluster/apple_scores.json}"
NAS="${NAS:-Luckypatcher@192.168.1.218}"
NAS_PATH="${NAS_PATH:-/volume1/docker/tripcluster/apple_scores.json}"

mkdir -p "$(dirname "$OUT")"

# System Python is fine: the script is stdlib only, deliberately, so this does
# not depend on the project venv existing or being rebuilt.
/usr/bin/python3 "$REPO/tools/apple_scores.py" --export "$OUT.tmp"

# Only replace the previous export once the new one is complete, so a failed
# run leaves the NAS with the last good file rather than a truncated one.
mv "$OUT.tmp" "$OUT"

# -O forces the legacy protocol: DSM has SFTP disabled by default and modern
# scp speaks SFTP, which fails with "subsystem request failed".
scp -O -q "$OUT" "$NAS:$NAS_PATH"

echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') exported $(wc -c < "$OUT" | tr -d ' ') bytes to $NAS"
