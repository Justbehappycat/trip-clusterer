#!/usr/bin/env python3
"""Render trips as a standalone map page — Phase 6 prototype.

Reads assets, runs the normal clustering pipeline, and writes one
self-contained HTML file: no tile server, no CDN, no network at render time.
State outlines are baked in as inline SVG from a GeoJSON fetched once and
cached, so the page works on a kiosk with the WAN down.

    python3 tools/make_map.py --fixture fixtures/synthetic.json -o map.html

Pin thumbnails are deliberately *not* wired to Immich yet: that needs the
thumbnail endpoint, which is unverified until --check-api runs against a live
instance. Pins show a placeholder tile and the photo count.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripcluster.cluster import build_trips          # noqa: E402
from tripcluster.config import load_config           # noqa: E402
from tripcluster.geocode import label_stops          # noqa: E402
from tripcluster.model import Asset                  # noqa: E402
from tripcluster.naming import heuristic_name        # noqa: E402

W, H = 1600, 1000
PAD = 60

# Trip colours, walked in order. Distinguishable in both themes.
COLOURS = ["#e8833a", "#3aa0e8", "#5ec27a", "#c95cc9", "#e0c341", "#e05c6b"]


def project(lat: float, lon: float, b: dict) -> tuple[float, float]:
    """Equirectangular with a cos(lat) correction at the map's mid-latitude.

    Not equal-area, but over one country it is visually indistinguishable from
    a conic and needs no projection library.
    """
    k = math.cos(math.radians((b["lat0"] + b["lat1"]) / 2))
    x = (lon - b["lon0"]) * k
    y = b["lat1"] - lat
    return (
        PAD + x / b["spanx"] * (W - 2 * PAD),
        PAD + y / b["spany"] * (H - 2 * PAD),
    )


def rings(geom: dict) -> list[list]:
    if geom["type"] == "Polygon":
        return geom["coordinates"]
    if geom["type"] == "MultiPolygon":
        return [r for poly in geom["coordinates"] for r in poly]
    return []


def build_bounds(stops: list[tuple[float, float]]) -> dict:
    """Frame the map on the stops, with margin, clamped to the lower 48."""
    lats = [s[0] for s in stops]
    lons = [s[1] for s in stops]
    lat0, lat1 = min(lats) - 4, max(lats) + 4
    lon0, lon1 = min(lons) - 5, max(lons) + 5
    lat0, lat1 = max(lat0, 23.0), min(lat1, 51.5)
    lon0, lon1 = max(lon0, -126.0), min(lon1, -66.0)
    k = math.cos(math.radians((lat0 + lat1) / 2))
    b = {"lat0": lat0, "lat1": lat1, "lon0": lon0, "lon1": lon1}
    b["spanx"] = (lon1 - lon0) * k
    b["spany"] = lat1 - lat0
    # Preserve aspect: widen whichever axis is under-filled.
    target = (W - 2 * PAD) / (H - 2 * PAD)
    if b["spanx"] / b["spany"] < target:
        b["spanx"] = b["spany"] * target
    else:
        b["spany"] = b["spanx"] / target
    return b


def load_assets(path: Path) -> list[Asset]:
    raw = json.loads(path.read_text())
    assets = [Asset(**r) for r in raw]
    assets.sort(key=lambda a: a.ts_utc)
    return assets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--fixture", required=True, help="JSON asset dump")
    ap.add_argument("--geojson", default="mapdata/us-states.json")
    ap.add_argument("-o", "--out", default="map.html")
    args = ap.parse_args()

    cfg = load_config(args.config)
    assets = load_assets(Path(args.fixture))
    trips = build_trips(assets, (cfg.home_lat, cfg.home_lon), cfg.thresholds)
    for t in trips:
        label_stops(t.stops, cfg.geocoder)
        t.heuristic_name = heuristic_name(t, cfg.album_prefix)

    if not trips:
        print("no trips found", file=sys.stderr)
        return 1

    pts = [(s.lat, s.lon) for t in trips for s in t.stops]
    pts.append((cfg.home_lat, cfg.home_lon))
    b = build_bounds(pts)

    geo = json.loads(Path(args.geojson).read_text())
    paths = []
    for feat in geo["features"]:
        for ring in rings(feat["geometry"]):
            pts_ = [project(la, lo, b) for lo, la in ring]
            if len(pts_) < 3:
                continue
            d = "M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts_) + "Z"
            paths.append(d)

    trips_js = []
    for i, t in enumerate(trips):
        trips_js.append({
            "name": t.display_name,
            "kind": t.kind,
            "days": round(t.duration_days, 1),
            "photos": t.photo_count,
            "colour": COLOURS[i % len(COLOURS)],
            "legs": [leg.kind for leg in t.legs],
            "stops": [
                {
                    "label": s.label,
                    "n": s.photo_count,
                    "xy": [round(v, 1) for v in project(s.lat, s.lon, b)],
                }
                for s in t.stops
            ],
        })

    hx, hy = project(cfg.home_lat, cfg.home_lon, b)
    html = TEMPLATE.replace("__W__", str(W)).replace("__H__", str(H))
    html = html.replace("__PATHS__", json.dumps(paths))
    html = html.replace("__TRIPS__", json.dumps(trips_js))
    html = html.replace("__HOME__", json.dumps([round(hx, 1), round(hy, 1)]))
    Path(args.out).write_text(html)

    print(f"wrote {args.out}: {len(trips)} trips, "
          f"{sum(len(t.stops) for t in trips)} stops, {len(paths)} outline rings")
    return 0


TEMPLATE = r"""<!doctype html>
<meta charset="utf-8">
<title>Trip map</title>
<style>
  :root { --bg:#0e1726; --ink:#eaf0f8; --muted:#8fa3bf; --land:#1b2942; --edge:#2c4066; }
  * { box-sizing:border-box; }
  html,body { margin:0; height:100%; background:var(--bg); color:var(--ink);
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    -webkit-user-select:none; user-select:none; overflow:hidden; }
  #wrap { position:relative; width:100vw; height:100vh; }
  /* touch-action:none is what stops Edge from eating the drag as a page
     scroll or its own pinch-zoom. Without it the kiosk pans the document
     instead of the map. */
  svg { position:absolute; inset:0; width:100%; height:100%;
    touch-action:none; cursor:grab; }
  svg.drag { cursor:grabbing; }
  .state { fill:var(--land); stroke:var(--edge); stroke-width:1; vector-effect:non-scaling-stroke; }
  .route { fill:none; stroke-width:3; stroke-linecap:round; stroke-linejoin:round; opacity:.85;
    vector-effect:non-scaling-stroke; }
  .route.dim { opacity:.12; }
  .flight { stroke-dasharray:2 9; stroke-linecap:round; }
  .pin { cursor:pointer; }
  .pin .card { fill:#fff; stroke:#fff; stroke-width:3; rx:10; }
  .pin .thumb { rx:7; }
  .pin .cnt { fill:#fff; font-weight:700; font-size:15px; paint-order:stroke;
    stroke:rgba(0,0,0,.55); stroke-width:3px; }
  .pin.dim { opacity:.18; }
  .home { fill:none; stroke:var(--muted); stroke-width:2; stroke-dasharray:4 4; }
  /* There is no safe corner. Bottom-left clears Seattle but lands on the
     Southwest; every position hides some stop at some window aspect. So the
     panel collapses instead, and never swallows a gesture aimed at the map:
     the container is transparent to pointers and only its controls take them. */
  #panel { position:absolute; left:24px; bottom:24px; max-width:330px;
    background:rgba(14,23,38,.86); border:1px solid var(--edge); border-radius:14px;
    backdrop-filter:blur(8px); pointer-events:none; overflow:hidden; }
  /* Only the controls themselves take pointers, not the container or the
     body's padding — otherwise the panel's empty space steals taps aimed at
     pins underneath it. */
  #panelbody { pointer-events:none; }
  #panelhead, .trip, .warn { pointer-events:auto; }
  #panelbody { padding:0 18px 16px; }
  #panel.min #panelbody { display:none; }
  #panelhead { display:flex; align-items:center; gap:10px; padding:14px 18px;
    cursor:pointer; min-height:44px; }
  #chev { margin-left:auto; color:var(--muted); transition:transform .15s; }
  #panel.min #chev { transform:rotate(180deg); }
  h1 { margin:0 0 2px; font-size:17px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:12px; }
  .trip { display:flex; gap:10px; align-items:baseline; padding:8px 10px; margin:0 -6px;
    border-radius:9px; cursor:pointer; }
  .trip:hover, .trip.on { background:rgba(255,255,255,.07); }
  .dot { width:10px; height:10px; border-radius:50%; flex:none; transform:translateY(1px); }
  .tn { font-weight:600; }
  .tm { color:var(--muted); font-size:12.5px; }
  .warn { margin-top:12px; padding-top:11px; border-top:1px solid var(--edge);
    color:#f0b866; font-size:12.5px; }
  #hint { position:absolute; right:24px; top:22px; color:var(--muted); font-size:12.5px;
    display:flex; align-items:center; gap:12px; }
  /* 44px min touch target — a 12px-tall text button is unusable with a finger. */
  #reset { min-height:44px; min-width:72px; padding:0 16px; font:inherit; cursor:pointer;
    color:var(--ink); background:rgba(14,23,38,.86); border:1px solid var(--edge);
    border-radius:10px; touch-action:manipulation; }
  #reset:active { background:rgba(255,255,255,.1); }
</style>
<div id="wrap">
  <!-- "meet", not "slice": slice fills the screen but crops the long axis,
       which silently hides stops on any aspect ratio that isn't 1.6:1 — and
       the kiosk monitor's resolution and orientation aren't known yet. meet
       letterboxes instead, guaranteeing every stop is visible at reset. -->
  <svg viewBox="0 0 __W__ __H__" preserveAspectRatio="xMidYMid meet" id="map"></svg>
  <div id="panel">
    <div id="panelhead"><h1>Trip map</h1><span id="chev">▾</span></div>
    <div id="panelbody">
      <div class="sub">Phase 6 prototype — synthetic data</div>
      <div id="list"></div>
      <div class="warn" id="warn"></div>
    </div>
  </div>
  <div id="hint">Drag to pan · pinch to zoom · tap a pin
    <button id="reset">Reset</button></div>
</div>
<script>
const PATHS = __PATHS__, TRIPS = __TRIPS__, HOME = __HOME__;
const NS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("map");
const el = (n, a={}) => { const e = document.createElementNS(NS, n);
  for (const k in a) e.setAttribute(k, a[k]); return e; };

for (const d of PATHS) svg.appendChild(el("path", {d, class:"state"}));

// Home marker — not a stop, just context.
svg.appendChild(el("circle", {cx:HOME[0], cy:HOME[1], r:9, class:"home"}));

const pinEls = [], routeEls = [];
TRIPS.forEach((t, ti) => {
  // One polyline segment per leg so flights can dash independently.
  t.stops.forEach((s, i) => {
    if (i === 0) return;
    const a = t.stops[i-1].xy, b = s.xy;
    const kind = t.legs[i-1] || "";
    const p = el("path", {
      d:`M${a[0]},${a[1]}L${b[0]},${b[1]}`,
      class:"route" + (kind === "flight" ? " flight" : ""),
      stroke:t.colour,
    });
    p.dataset.trip = ti; svg.appendChild(p); routeEls.push(p);
  });

  t.stops.forEach(s => {
    const [x, y] = s.xy, w = 74, h = 74;
    // Outer group is anchored on the stop; the inner group carries the
    // artwork and gets counter-scaled on zoom so pins keep a constant
    // on-screen size. Artwork is drawn relative to the anchor at (0,0),
    // which is also the tip of the tail.
    const g = el("g", {class:"pin", transform:`translate(${x},${y})`});
    g.dataset.trip = ti;
    const inner = el("g", {class:"pin-art"});
    const top = -h - 14;
    inner.appendChild(el("rect", {x:-w/2, y:top, width:w, height:h, class:"card", rx:10}));
    // Placeholder for the Immich thumbnail — see module docstring.
    inner.appendChild(el("rect", {x:-w/2+5, y:top+5, width:w-10, height:h-10,
      class:"thumb", rx:7, fill:t.colour, "fill-opacity":.55}));
    inner.appendChild(el("path", {d:`M-9,-14L0,0L9,-14Z`, fill:"#fff"}));
    const c = el("text", {x:-w/2+10, y:top+h-11, class:"cnt"}); c.textContent = s.n;
    inner.appendChild(c);
    const ttl = el("title"); ttl.textContent = `${s.label} — ${s.n} photos`;
    inner.appendChild(ttl);
    g.appendChild(inner);
    svg.appendChild(g); pinEls.push(g);
  });
});

const list = document.getElementById("list");
TRIPS.forEach((t, ti) => {
  const row = document.createElement("div");
  row.className = "trip"; row.dataset.trip = ti;
  row.innerHTML = `<span class="dot" style="background:${t.colour}"></span>
    <span><span class="tn">${t.name}</span><br>
    <span class="tm">${t.kind.replace("_"," ")} · ${t.stops.length} stops ·
    ${t.photos} photos · ${t.days}d</span></span>`;
  row.onclick = e => { e.stopPropagation(); focus(ti === sel ? null : ti); };
  list.appendChild(row);
});

// Surface the known misclassification rather than quietly drawing it.
const susp = TRIPS.filter(t => t.kind === "road_trip" && t.stops.length === 2);
document.getElementById("warn").textContent = susp.length
  ? `⚠ ${susp.length} two-stop “road trip”: no intermediate photos, so the `
    + `flight/drive call rests on implied speed alone.`
  : "";

let sel = null;
function focus(ti) {
  sel = ti;
  const dim = n => n.classList.toggle("dim", ti !== null && +n.dataset.trip !== ti);
  pinEls.forEach(dim); routeEls.forEach(dim);
  document.querySelectorAll(".trip").forEach(r =>
    r.classList.toggle("on", ti !== null && +r.dataset.trip === ti));
}
/* ---- pan / pinch-zoom ------------------------------------------------
   Pointer Events rather than touch events: one code path covers the
   touchscreen, a mouse, and the trackpad used while developing. The map is
   moved by rewriting the viewBox, so nothing re-renders — only the browser's
   transform changes, which is what keeps it smooth on an N100. */

const W = __W__, H = __H__;
const MIN_W = W * 0.15;          // deepest zoom ~6.7x
const view = {x:0, y:0, w:W, h:H};
const pointers = new Map();
let pinchStart = null, moved = 0, downAt = null;

/* Must match preserveAspectRatio on the <svg>. "meet" fits the whole viewBox
   inside the viewport, so the effective scale is the smaller of the two
   ratios. Get this wrong and pan drifts away from the finger. */
function fit() {
  const r = svg.getBoundingClientRect();
  const k = Math.min(r.width / view.w, r.height / view.h);
  return {r, k};
}
function toSvg(cx, cy) {
  const {r, k} = fit();
  return {
    x: view.x + (cx - r.left - (r.width - view.w * k) / 2) / k,
    y: view.y + (cy - r.top  - (r.height - view.h * k) / 2) / k,
  };
}
function clamp() {
  view.w = Math.min(Math.max(view.w, MIN_W), W);
  view.h = view.w * (H / W);
  // Keep at least a third of the frame over the map on every side.
  const mx = view.w / 3, my = view.h / 3;
  view.x = Math.min(Math.max(view.x, -mx), W - view.w + mx);
  view.y = Math.min(Math.max(view.y, -my), H - view.h + my);
}
function apply() {
  clamp();
  svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
  // Counter-scale pins so they stay a constant size on screen.
  const s = view.w / W;
  for (const g of pinEls) {
    const art = g.firstElementChild;
    if (art) art.setAttribute("transform", `scale(${s})`);
  }
}
function zoomAt(cx, cy, factor) {
  const before = toSvg(cx, cy);
  view.w *= factor;
  clamp();
  const after = toSvg(cx, cy);
  view.x += before.x - after.x;
  view.y += before.y - after.y;
  apply();
}

svg.addEventListener("pointerdown", e => {
  // Register first, capture second. Capture is an optimisation — it keeps a
  // drag alive if the finger leaves the element — and it throws NotFoundError
  // for any pointer id the browser doesn't consider active. Letting that
  // escape would abandon the gesture entirely.
  pointers.set(e.pointerId, {x:e.clientX, y:e.clientY});
  try { svg.setPointerCapture(e.pointerId); } catch (_) {}
  if (pointers.size === 1) { moved = 0; downAt = {x:e.clientX, y:e.clientY}; }
  if (pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    pinchStart = {d:Math.hypot(a.x-b.x, a.y-b.y), w:view.w};
  }
  svg.classList.add("drag");
});

svg.addEventListener("pointermove", e => {
  const p = pointers.get(e.pointerId);
  if (!p) return;
  const dx = e.clientX - p.x, dy = e.clientY - p.y;
  p.x = e.clientX; p.y = e.clientY;

  if (pointers.size === 2 && pinchStart) {
    const [a, b] = [...pointers.values()];
    const d = Math.hypot(a.x-b.x, a.y-b.y);
    if (d > 0) {
      const mid = {x:(a.x+b.x)/2, y:(a.y+b.y)/2};
      const target = pinchStart.w * (pinchStart.d / d);
      zoomAt(mid.x, mid.y, target / view.w);
    }
    moved = 99;                      // a pinch is never a tap
    return;
  }
  const {k} = fit();
  view.x -= dx / k; view.y -= dy / k;
  moved += Math.hypot(dx, dy);
  apply();
});

function endPointer(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinchStart = null;
  if (pointers.size === 0) {
    svg.classList.remove("drag");
    // A drag that barely moved is a tap. 8px of slop, because a finger on a
    // touchscreen always slides a little.
    if (moved < 8 && downAt) {
      const el_ = document.elementFromPoint(downAt.x, downAt.y);
      const g = el_ && el_.closest(".pin, .route");
      focus(g ? +g.dataset.trip : null);
    }
    downAt = null;
  }
}
svg.addEventListener("pointerup", endPointer);
svg.addEventListener("pointercancel", endPointer);

svg.addEventListener("wheel", e => {
  e.preventDefault();
  zoomAt(e.clientX, e.clientY, e.deltaY > 0 ? 1.15 : 1/1.15);
}, {passive:false});

// Double-tap / double-click returns to the full frame.
svg.addEventListener("dblclick", () => {
  view.x = 0; view.y = 0; view.w = W; view.h = H; apply();
});
document.getElementById("reset").onclick = () => {
  view.x = 0; view.y = 0; view.w = W; view.h = H; apply(); focus(null);
};
document.getElementById("panelhead").onclick = () =>
  document.getElementById("panel").classList.toggle("min");

apply();
</script>
"""


if __name__ == "__main__":
    raise SystemExit(main())
