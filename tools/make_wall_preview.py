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


def corner_brightness(img: Image.Image) -> dict:
    """Mean luminance per 3x3 cell, 0-1. Bright cells need heavier treatment.

    Legibility over a photo is a brightness problem before it is a placement
    problem: white text on a sunlit sky needs a scrim, the same text on dark
    asphalt needs none. Measuring it lets the treatment be applied only where
    it is earned, instead of dimming every photo just in case.
    """
    g = np.asarray(img.convert("L").resize((180, 120)), dtype=float) / 255.0
    h, w = g.shape
    out = {}
    for i, (r0, r1) in enumerate(((0, h // 3), (h // 3, 2 * h // 3), (2 * h // 3, h))):
        for j, (c0, c1) in enumerate(((0, w // 3), (w // 3, 2 * w // 3), (2 * w // 3, w))):
            out[(i, j)] = float(g[r0:r1, c0:c1].mean())
    return out


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
        detail, bright = grid_detail(img), corner_brightness(img)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=72, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()

        ex = it.get("exifInfo") or {}
        where = ex.get("city") or ex.get("state") or ""
        when = (ex.get("dateTimeOriginal") or "")[:10]
        # Brightness at the two fixed anchors each layout uses.
        slides.append({
            "src": f"data:image/jpeg;base64,{b64}",
            "where": where or "—", "when": when,
            "region": ex.get("state") or ex.get("country") or "",
            "bright": {
                "bottomLeft": round(bright[(2, 0)], 3),
                "bottomRight": round(bright[(2, 2)], 3),
                "topCenter": round(bright[(0, 1)], 3),
                "bottomCenter": round(bright[(2, 1)], 3),
            },
        })

    if not slides:
        print("ERROR no thumbnails fetched", file=sys.stderr)
        return 1

    import json
    Path(args.out).write_text(TEMPLATE.replace("__SLIDES__", json.dumps(slides)))
    print(f"wrote {args.out}: {len(slides)} slides")
    for s in slides:
        b = s["bright"]
        print(f"  {s['where']:18} brightness  bl={b['bottomLeft']:.2f} "
              f"br={b['bottomRight']:.2f} tc={b['topCenter']:.2f}")
    return 0


TEMPLATE = r"""<!doctype html>
<meta charset="utf-8">
<title>Photo wall — layouts</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{height:100%;background:#000;overflow:hidden;color:#fff;
    font:400 16px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Display",
      "Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
  #stage{position:fixed;inset:0}
  img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
    opacity:0;transition:opacity 1.6s ease}
  img.on{opacity:1}

  /* Legibility without dimming the photo.
     Two shadows, not one: a tight dark shadow gives the glyph an edge against
     a bright sky, a wide soft one lifts it off busy detail. A single large
     blur does neither job well. Off-white rather than #fff — pure white on a
     bright sky blooms and reads as glare. */
  .lit{color:#f7f7f5;
    text-shadow:0 1px 2px rgba(0,0,0,.45), 0 2px 28px rgba(0,0,0,.35)}
  /* Earned, not automatic: only applied when the anchor is measurably bright,
     so a photo with a dark foreground is never scrimmed for no reason. */
  .bright .lit{text-shadow:0 1px 3px rgba(0,0,0,.75), 0 2px 34px rgba(0,0,0,.6)}
  .scrim{position:absolute;inset:auto 0 0 0;height:38%;opacity:0;
    transition:opacity .8s ease;pointer-events:none;
    background:linear-gradient(to top,rgba(0,0,0,.42),transparent)}
  .bright .scrim{opacity:1}

  .layer{position:absolute;inset:0;pointer-events:none;padding:5vmin;
    opacity:0;transition:opacity .35s ease}
  .layer.on{opacity:1}

  /* --- A: hub. Big thin time bottom-left, place opposite. -------------- */
  #a{display:flex;align-items:flex-end;justify-content:space-between}
  #a .time{font-size:13vmin;font-weight:150;letter-spacing:-.035em;line-height:.86;
    font-variant-numeric:tabular-nums}
  #a .sub{font-size:2vmin;font-weight:500;letter-spacing:.18em;
    text-transform:uppercase;opacity:.8;margin-top:1.6vmin}
  #a .place{text-align:right;font-size:1.7vmin;font-weight:500;
    letter-spacing:.16em;text-transform:uppercase;opacity:.78;line-height:1.8}
  #a .place em{display:block;font-style:normal;font-size:1.4vmin;opacity:.6;
    letter-spacing:.2em}

  /* --- B: editorial. Hairline rule, place tucked beneath the time. ----- */
  #b{display:flex;align-items:flex-end}
  #b .stack{display:flex;flex-direction:column;gap:1.4vmin}
  #b .rule{width:9vmin;height:1px;background:currentColor;opacity:.5}
  #b .time{font-size:11vmin;font-weight:250;letter-spacing:-.03em;line-height:.9;
    font-variant-numeric:tabular-nums}
  #b .meta{display:flex;gap:1.6vmin;align-items:baseline;font-size:1.7vmin;
    letter-spacing:.14em;text-transform:uppercase;opacity:.82}
  #b .meta b{font-weight:600}
  #b .meta span{opacity:.62}

  /* --- C: lock screen. Time high and centred, place low and centred. --- */
  #c{display:flex;flex-direction:column;align-items:center;
    justify-content:space-between}
  #c .time{font-size:16vmin;font-weight:200;letter-spacing:-.045em;line-height:1;
    font-variant-numeric:tabular-nums;margin-top:2vmin}
  #c .sub{font-size:2.1vmin;font-weight:500;letter-spacing:.16em;
    text-transform:uppercase;opacity:.82;text-align:center;margin-top:.6vmin}
  #c .place{font-size:1.7vmin;font-weight:500;letter-spacing:.18em;
    text-transform:uppercase;opacity:.75;text-align:center}

  #picker{position:fixed;top:2vmin;right:2vmin;display:flex;gap:.6vmin;
    font-size:1.5vmin;z-index:9}
  #picker button{font:inherit;padding:.9vmin 1.8vmin;border-radius:99px;
    border:1px solid rgba(255,255,255,.22);background:rgba(0,0,0,.35);
    color:#fff;cursor:pointer;backdrop-filter:blur(8px)}
  #picker button.on{background:#fff;color:#000;border-color:#fff}
  #note{position:fixed;left:2vmin;top:2vmin;font-size:1.4vmin;opacity:.5;z-index:9}
</style>
<div id="stage"></div>
<div class="scrim" id="scrim"></div>

<div class="layer" id="a">
  <div><div class="time lit"></div><div class="sub lit"></div></div>
  <div class="place lit"></div>
</div>

<div class="layer" id="b">
  <div class="stack">
    <div class="rule"></div>
    <div class="time lit"></div>
    <div class="meta lit"></div>
  </div>
</div>

<div class="layer" id="c">
  <div><div class="time lit"></div><div class="sub lit"></div></div>
  <div class="place lit"></div>
</div>

<div id="picker">
  <button data-l="a" class="on">Hub</button>
  <button data-l="b">Editorial</button>
  <button data-l="c">Lock screen</button>
</div>
<div id="note">1/2/3 to switch · space for next photo</div>

<script>
const SLIDES = __SLIDES__;
const stage = document.getElementById("stage");
const scrim = document.getElementById("scrim");
let layout = "a", i = 0, img = null, cur = SLIDES[0];

// Which anchor each layout puts text over — that is the one whose brightness
// decides whether the heavier treatment is needed.
const ANCHOR = {a: "bottomLeft", b: "bottomLeft", c: "topCenter"};
const BRIGHT = 0.55;   // above this, a photo needs help

function time() {
  const d = new Date(), h = d.getHours() % 12 || 12;
  return `${h}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function dateLine() {
  return new Date().toLocaleDateString(undefined,
    {weekday: "long", month: "long", day: "numeric"});
}

function paint() {
  const t = time(), dl = dateLine();
  const where = cur.where, region = cur.region, when = cur.when;

  document.querySelector("#a .time").textContent = t;
  document.querySelector("#a .sub").textContent = dl;
  document.querySelector("#a .place").innerHTML =
    `${where}<em>${region} · ${when}</em>`;

  document.querySelector("#b .time").textContent = t;
  document.querySelector("#b .meta").innerHTML =
    `<b>${where}</b><span>${region}</span><span>${when}</span>`;

  document.querySelector("#c .time").textContent = t;
  document.querySelector("#c .sub").textContent = dl;
  document.querySelector("#c .place").textContent =
    `${where}${region ? " · " + region : ""}`;

  const lum = (cur.bright || {})[ANCHOR[layout]] ?? 0;
  document.body.classList.toggle("bright", lum > BRIGHT);
}
setInterval(paint, 1000);

function setLayout(l) {
  layout = l;
  for (const el of document.querySelectorAll(".layer"))
    el.classList.toggle("on", el.id === l);
  for (const b of document.querySelectorAll("#picker button"))
    b.classList.toggle("on", b.dataset.l === l);
  paint();
}
document.getElementById("picker").onclick = e => {
  if (e.target.dataset.l) setLayout(e.target.dataset.l);
};
addEventListener("keydown", e => {
  if (e.key === "1") setLayout("a");
  if (e.key === "2") setLayout("b");
  if (e.key === "3") setLayout("c");
  if (e.key === " ") { e.preventDefault(); show(); }
});

function show() {
  cur = SLIDES[i % SLIDES.length]; i++;
  const next = new Image();
  next.src = cur.src;
  next.onload = () => {
    stage.appendChild(next);
    requestAnimationFrame(() => next.classList.add("on"));
    if (img) { const old = img; old.classList.remove("on");
               setTimeout(() => old.remove(), 1800); }
    img = next;
    paint();
  };
}
setLayout("a");
show();
setInterval(show, 7000);
</script>
"""


if __name__ == "__main__":
    raise SystemExit(main())
