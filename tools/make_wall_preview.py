#!/usr/bin/env python3
"""Preview the Phase 6 wall: adaptive clock, location in a corner.

Kiosk cannot do this — it offers show/hide and format for the clock, no
position. Placing the time where the photo is calm needs our own renderer, so
this generates a self-contained page to look at before committing to that.

Each photo is analysed once: a 3x3 grid of gradient magnitude, which is a
decent proxy for "is there detail here". The clock goes in the calmest cell,
the caption in the calmest cell far enough away not to crowd it.

    python3 tools/make_wall_preview.py -n 6 -o wall.html

Photos are embedded as data URIs, so the page is self-contained and needs no
API key — which also means it is a preview, not the real thing. The real one
proxies images at request time.
"""

from __future__ import annotations

import argparse
import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                          # noqa: E402
from PIL import Image                                       # noqa: E402

from tripcluster.config import load_config                  # noqa: E402
from tripcluster.immich import ImmichClient, ImmichError    # noqa: E402

# Row/column to CSS. Centre is never used for either element.
CELLS = {
    (0, 0): ("top", "left"), (0, 1): ("top", "center"), (0, 2): ("top", "right"),
    (1, 0): ("middle", "left"), (1, 2): ("middle", "right"),
    (2, 0): ("bottom", "left"), (2, 1): ("bottom", "center"),
    (2, 2): ("bottom", "right"),
}


def grid_detail(img: Image.Image) -> dict:
    """Mean gradient magnitude per 3x3 cell. Low means calm."""
    g = np.asarray(img.convert("L").resize((180, 120)), dtype=float)
    gy, gx = np.gradient(g)
    mag = np.hypot(gx, gy)
    h, w = mag.shape
    out = {}
    for i, (r0, r1) in enumerate(((0, h // 3), (h // 3, 2 * h // 3), (2 * h // 3, h))):
        for j, (c0, c1) in enumerate(((0, w // 3), (w // 3, 2 * w // 3), (2 * w // 3, w))):
            out[(i, j)] = float(mag[r0:r1, c0:c1].mean())
    return out


def place(detail: dict) -> tuple[tuple, tuple]:
    """Pick a cell for the clock and one for the caption.

    The caption is kept at least two grid steps from the clock, so the two
    never crowd each other on a photo whose calm region is one corner.
    """
    ranked = [c for c, _ in sorted(detail.items(), key=lambda kv: kv[1]) if c in CELLS]
    clock = ranked[0]
    for cand in ranked[1:]:
        if abs(cand[0] - clock[0]) + abs(cand[1] - clock[1]) >= 2:
            return clock, cand
    return clock, ranked[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-n", "--count", type=int, default=6)
    ap.add_argument("-o", "--out", default="wall.html")
    ap.add_argument("--album", default="Views")
    args = ap.parse_args()

    cfg = load_config(args.config)
    try:
        client = ImmichClient(cfg.api)
    except ImmichError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2

    album = next((a for a in client._request("GET", "/api/albums")
                  if a.get("albumName") == args.album), None)
    if not album:
        print(f"ERROR no album named {args.album!r}", file=sys.stderr)
        return 1

    r = client._request("POST", cfg.api.search_assets, json={
        "albumIds": [album["id"]], "size": args.count * 3, "page": 1,
        "withExif": True})
    items = ((r.get("assets") or {}).get("items") or [])[: args.count]

    slides = []
    for it in items:
        resp = client.session.get(
            client.base + f"/api/assets/{it['id']}/thumbnail?size=preview",
            timeout=60)
        if resp.status_code != 200:
            continue
        img = Image.open(io.BytesIO(resp.content))
        clock_cell, cap_cell = place(grid_detail(img))

        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=72, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()

        ex = it.get("exifInfo") or {}
        where = ex.get("city") or ex.get("state") or ""
        when = (ex.get("dateTimeOriginal") or "")[:10]
        slides.append({
            "src": f"data:image/jpeg;base64,{b64}",
            "clock": CELLS[clock_cell], "cap": CELLS[cap_cell],
            "where": where or "—", "when": when,
            "region": ex.get("state") or ex.get("country") or "",
        })

    if not slides:
        print("ERROR no thumbnails fetched", file=sys.stderr)
        return 1

    import json
    Path(args.out).write_text(TEMPLATE.replace("__SLIDES__", json.dumps(slides)))
    print(f"wrote {args.out}: {len(slides)} slides")
    for s in slides:
        print(f"  clock {s['clock'][0]:6}/{s['clock'][1]:6}   "
              f"caption {s['cap'][0]:6}/{s['cap'][1]:6}   {s['where']}")
    return 0


TEMPLATE = r"""<!doctype html>
<meta charset="utf-8">
<title>Photo wall preview</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{height:100%;background:#000;overflow:hidden;
    font:400 16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    color:#fff;-webkit-font-smoothing:antialiased}
  #stage{position:fixed;inset:0}
  img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
    opacity:0;transition:opacity 1.4s ease}
  img.on{opacity:1}
  /* A gentle scrim only where text sits, rather than over the whole photo —
     a full overlay is what makes most photo frames look flat. */
  .overlay{position:absolute;inset:0;pointer-events:none;display:grid;
    grid-template-rows:1fr 1fr 1fr;grid-template-columns:1fr 1fr 1fr;
    padding:4vmin}
  .slot{display:flex}
  .slot[data-v=top]{align-items:flex-start}
  .slot[data-v=middle]{align-items:center}
  .slot[data-v=bottom]{align-items:flex-end}
  .slot[data-h=left]{justify-content:flex-start}
  .slot[data-h=center]{justify-content:center}
  .slot[data-h=right]{justify-content:flex-end}

  #clock{font-size:9vmin;font-weight:200;letter-spacing:-.02em;
    text-shadow:0 2px 24px rgba(0,0,0,.55);line-height:1}
  #clock small{display:block;font-size:2.2vmin;font-weight:400;opacity:.85;
    letter-spacing:.12em;text-transform:uppercase;margin-top:1.2vmin}
  #caption{font-size:2.4vmin;letter-spacing:.02em;white-space:nowrap;
    text-shadow:0 2px 18px rgba(0,0,0,.6);opacity:.92}
  #caption b{font-weight:600}
  #caption span{opacity:.7;margin-left:1.2vmin}
  /* Preview-only. Sits outside the 3x3 grid's padding so it cannot collide
     with the clock or caption wherever they land. */
  #hint{position:fixed;left:50%;bottom:.6vmin;transform:translateX(-50%);
    font-size:1.4vmin;opacity:.28;letter-spacing:.04em}
</style>
<div id="stage"></div>
<div class="overlay" id="ov"></div>
<div id="hint">preview — clock and caption move to the calmest part of each photo</div>
<script>
const SLIDES = __SLIDES__;
const stage = document.getElementById("stage"), ov = document.getElementById("ov");

// Nine slots matching the 3x3 analysis grid.
const V = ["top","middle","bottom"], H = ["left","center","right"];
const slots = {};
for (const v of V) for (const h of H) {
  const d = document.createElement("div");
  d.className = "slot"; d.dataset.v = v; d.dataset.h = h;
  ov.appendChild(d); slots[v + "/" + h] = d;
}
const clock = document.createElement("div"); clock.id = "clock";
const caption = document.createElement("div"); caption.id = "caption";

function tick() {
  const d = new Date();
  const hh = d.getHours() % 12 || 12, mm = String(d.getMinutes()).padStart(2,"0");
  clock.innerHTML = `${hh}:${mm}<small>${d.toLocaleDateString(undefined,
    {weekday:"long", month:"long", day:"numeric"})}</small>`;
}
setInterval(tick, 1000); tick();

let i = 0, img = null;
function show() {
  const s = SLIDES[i % SLIDES.length];
  const next = new Image();
  next.src = s.src;
  next.onload = () => {
    stage.appendChild(next);
    requestAnimationFrame(() => next.classList.add("on"));
    if (img) { const old = img; old.classList.remove("on");
               setTimeout(() => old.remove(), 1600); }
    img = next;
    slots[s.clock[0] + "/" + s.clock[1]].appendChild(clock);
    slots[s.cap[0] + "/" + s.cap[1]].appendChild(caption);
    caption.innerHTML = `<b>${s.where}</b>` +
      (s.region ? `<span>${s.region}</span>` : "") +
      `<span>${s.when}</span>`;
  };
  i++;
}
show();
setInterval(show, 6000);
</script>
"""


if __name__ == "__main__":
    raise SystemExit(main())
