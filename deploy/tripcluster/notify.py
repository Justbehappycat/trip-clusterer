#!/usr/bin/env python3
"""Post a one-line message to a webhook. Used to report a failed weekly run.

Written in Python rather than curl because the image already has Python and
adding curl for one HTTP POST is not worth a layer.

Never fails loudly: a broken notifier must not turn a recoverable clustering
failure into a crash loop. Problems go to stderr and the exit status stays 0.

    NOTIFY_URL=https://ntfy.sh/your-topic python notify.py "message"

NOTIFY_FORMAT selects the body shape:

    text    (default) the message as a plain body — ntfy, Gotify
    slack   {"text": ...}
    discord {"content": ...}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    url = os.environ.get("NOTIFY_URL", "").strip()
    if not url:
        return 0                      # notifications are opt-in

    message = " ".join(sys.argv[1:]).strip() or "trip clusterer: no message"
    fmt = os.environ.get("NOTIFY_FORMAT", "text").strip().lower()

    if fmt == "slack":
        body, ctype = json.dumps({"text": message}).encode(), "application/json"
    elif fmt == "discord":
        body, ctype = json.dumps({"content": message}).encode(), "application/json"
    else:
        body, ctype = message.encode(), "text/plain; charset=utf-8"

    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status >= 400:
                print(f"notify: {url} returned {resp.status}", file=sys.stderr)
    except (urllib.error.URLError, OSError) as e:
        # Deliberately not fatal. The run already failed; failing to say so
        # should not also take the container down.
        print(f"notify: could not reach the webhook: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
