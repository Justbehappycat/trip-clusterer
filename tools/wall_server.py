#!/usr/bin/env python3
"""The photo wall — Phase 6. Serves the display page and proxies the images.

Replaces immich-kiosk for the display layer. Kiosk offers no control over
where the clock sits, and the whole point here is a lock-screen layout: time
high and centred, place low and centred, sized for reading across a room.

Two things have to happen server-side.

The API key never reaches the browser, so images are proxied rather than
linked. And each photo's brightness is measured once and cached, because
legibility over a photo is a brightness problem — white text on a sunlit sky
needs a scrim, the same text on wet asphalt does not, and deciding that in the
browser would mean shipping every image twice.

Standard library only, apart from Pillow and numpy which are already present
for the analysis. No framework: this serves three routes to one screen.

    python3 tools/wall_server.py --port 4000
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                          # noqa: E402
from PIL import Image                                       # noqa: E402

from tripcluster.config import load_config                  # noqa: E402
from tripcluster.immich import ImmichClient, ImmichError    # noqa: E402

log = logging.getLogger("wall")

# The lock-screen layout puts the time top-centre and the place bottom-centre,
# so those are the only two regions whose brightness matters.
ANCHORS = {"topCenter": (0, 1), "bottomCenter": (2, 1)}
BRIGHT_THRESHOLD = 0.55


def analyse(data: bytes) -> dict:
    """How hard each anchor region is to read text over.

    Two measurements, because they fail differently. **Luminance** catches a
    sunlit sky, where white text washes out. **Detail** catches a region that
    is dark but cluttered — pier signage, foliage, a shop front — where text
    is lost in the noise rather than the glare. The first version measured
    only brightness and put the location straight over a wall of lettering.
    """
    img = Image.open(io.BytesIO(data))
    g = np.asarray(img.convert("L").resize((180, 120)), dtype=float) / 255.0
    gy, gx = np.gradient(g)
    mag = np.hypot(gx, gy)
    h, w = g.shape
    rows = ((0, h // 3), (h // 3, 2 * h // 3), (2 * h // 3, h))
    cols = ((0, w // 3), (w // 3, 2 * w // 3), (2 * w // 3, w))
    out = {}
    for name, (i, j) in ANCHORS.items():
        r0, r1 = rows[i]
        c0, c1 = cols[j]
        out[name] = round(float(g[r0:r1, c0:c1].mean()), 3)
        # Scaled so a typical busy region lands near 1.0, like the luminance
        # figure, and one threshold can serve both.
        out[name + "Detail"] = round(float(mag[r0:r1, c0:c1].mean()) * 12.0, 3)
    return out


class Cache:
    """Brightness per asset, on disk. Analysis is done once per photo, ever."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS brightness ("
            "  asset_id TEXT PRIMARY KEY, json TEXT NOT NULL, at INTEGER NOT NULL)"
        )
        self.db.commit()
        self.lock = threading.Lock()

    def get(self, asset_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT json FROM brightness WHERE asset_id=?", (asset_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, asset_id: str, value: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO brightness VALUES (?,?,?)",
                (asset_id, json.dumps(value), int(time.time())),
            )
            self.db.commit()


class Wall:
    """The album, refreshed periodically, with images proxied on demand."""

    def __init__(self, cfg, album_name: str, cache: Cache, refresh_seconds: int):
        self.cfg = cfg
        self.client = ImmichClient(cfg.api)
        self.album_name = album_name
        self.cache = cache
        self.refresh_seconds = refresh_seconds
        self.slides: list[dict] = []
        self.fetched_at = 0.0
        self.lock = threading.Lock()

    def _album_id(self) -> str | None:
        for a in self.client._request("GET", "/api/albums"):
            if a.get("albumName") == self.album_name:
                return a["id"]
        return None

    def refresh(self) -> None:
        album_id = self._album_id()
        if not album_id:
            log.error("no album named %r", self.album_name)
            return
        slides, page = [], 1
        while True:
            r = self.client._request("POST", self.cfg.api.search_assets, json={
                "albumIds": [album_id], "page": page, "size": 1000,
                "withExif": True})
            a = r.get("assets") or {}
            items = a.get("items") or []
            if not items:
                break
            for it in items:
                ex = it.get("exifInfo") or {}
                slides.append({
                    "id": it["id"],
                    "where": ex.get("city") or ex.get("state") or "",
                    "region": ex.get("state") or ex.get("country") or "",
                    "when": (ex.get("dateTimeOriginal") or "")[:10],
                })
            nxt = a.get("nextPage")
            if not nxt:
                break
            page = int(nxt)
        with self.lock:
            self.slides = slides
            self.fetched_at = time.time()
        log.info("album %s: %d slides", self.album_name, len(slides))

    def maybe_refresh(self) -> None:
        if time.time() - self.fetched_at > self.refresh_seconds:
            try:
                self.refresh()
            except ImmichError as e:
                # Keep serving the last good list. A wall that goes blank
                # because the NAS blinked is worse than a slightly stale one.
                log.warning("refresh failed, keeping %d cached slides: %s",
                            len(self.slides), e)

    def image(self, asset_id: str) -> tuple[bytes, str] | None:
        r = self.client.session.get(
            f"{self.client.base}/api/assets/{asset_id}/thumbnail?size=preview",
            timeout=60)
        if r.status_code != 200:
            return None
        data = r.content
        if self.cache.get(asset_id) is None:
            try:
                self.cache.put(asset_id, analyse(data))
            except Exception as e:                     # noqa: BLE001
                log.warning("could not analyse %s: %s", asset_id, e)
        return data, r.headers.get("Content-Type", "image/jpeg")

    def warm(self, pause: float = 0.4) -> None:
        """Analyse anything not yet cached, slowly, in the background.

        Without this the *first* showing of each photo has no brightness and
        so gets no scrim — precisely the moment a bright sky would make the
        clock unreadable. The pause keeps this from competing with Immich's
        own transcoding queue, which on this NAS is thousands of jobs deep.
        """
        while True:
            with self.lock:
                pending = [s["id"] for s in self.slides
                           if self.cache.get(s["id"]) is None]
            if pending:
                log.info("warming brightness for %d photos", len(pending))
            for asset_id in pending:
                try:
                    self.image(asset_id)          # image() caches as a side effect
                except Exception as e:            # noqa: BLE001
                    log.debug("warm %s: %s", asset_id, e)
                time.sleep(pause)
            time.sleep(60)

    def payload(self) -> dict:
        self.maybe_refresh()
        with self.lock:
            slides = list(self.slides)
        for s in slides:
            s["bright"] = self.cache.get(s["id"]) or {}
        return {"slides": slides, "threshold": BRIGHT_THRESHOLD}


def make_handler(wall: Wall, page: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code, body: bytes, ctype: str, cache: str = "no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if path == "/api/slides":
                try:
                    body = json.dumps(wall.payload()).encode()
                except ImmichError as e:
                    return self._send(503, str(e).encode(), "text/plain")
                return self._send(200, body, "application/json")
            if path.startswith("/img/"):
                got = wall.image(path[len("/img/"):])
                if not got:
                    return self._send(404, b"no such image", "text/plain")
                data, ctype = got
                # Immich asset ids are stable and the bytes never change, so
                # let the browser keep them: on a wall this is the difference
                # between a smooth cross-fade and a visible pop.
                return self._send(200, data, ctype, "public, max-age=86400")
            self._send(404, b"not found", "text/plain")

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--album", default="Views")
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--cache", default="wall.db")
    ap.add_argument("--refresh", type=int, default=900,
                    help="seconds between album re-reads")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    try:
        wall = Wall(cfg, args.album, Cache(Path(args.cache)), args.refresh)
    except ImmichError as e:
        log.error("%s", e)
        return 2

    try:
        wall.refresh()
    except ImmichError as e:
        log.warning("initial refresh failed: %s", e)

    threading.Thread(target=wall.warm, daemon=True).start()

    page = (Path(__file__).resolve().parent / "wall_page.html").read_text()
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(wall, page))
    log.info("wall on http://0.0.0.0:%d  album=%r", args.port, args.album)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
