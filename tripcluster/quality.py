"""Photo quality, borrowed from Apple.

macOS Photos scores every image it holds — composition, focus, framing,
lighting, noise — and stores the results in its own database. Those scores are
the judgement a photo wall wants, already computed, so nothing here trains or
infers anything. ``tools/apple_scores.py`` exports them; this module matches
them to Immich assets and uses them.

Pure functions over plain dicts. No network, no database, in keeping with
everything else below ``immich.py``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

# Higher is better for these.
MERITS = (
    "ZOVERALLAESTHETICSCORE",
    "ZPLEASANTCOMPOSITIONSCORE",
    "ZSHARPLYFOCUSEDSUBJECTSCORE",
    "ZWELLFRAMEDSUBJECTSCORE",
)
# Higher is worse.
PENALTIES = ("ZLOWLIGHT", "ZNOISESCORE", "ZFAILURESCORE")

# A filename may repeat: iPhones recycle IMG_1234 every few thousand shots, and
# over an eight-year library 17,358 photos carry only 15,244 distinct names.
# Matching on name alone put the wrong score on one wall photo in five, so the
# timestamp decides between candidates.
MATCH_TOLERANCE_SECONDS = 3600


class Scores:
    """Apple's scores, looked up by (filename, timestamp)."""

    def __init__(self, rows: list[dict]):
        self._by_name: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            name = r.get("name")
            if name:
                self._by_name[name].append(r)

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_name.values())

    @classmethod
    def load(cls, path: str | Path) -> "Scores":
        p = Path(path)
        if not p.exists():
            return cls([])
        data = json.loads(p.read_text())
        # Tolerate the old filename-keyed dict, which cannot disambiguate.
        if isinstance(data, dict):
            data = [dict(v, name=k) for k, v in data.items()]
        return cls(data)

    def for_asset(self, filename: str, ts_utc: int | None) -> dict | None:
        cands = self._by_name.get(filename or "")
        if not cands:
            return None
        if ts_utc is None:
            return cands[0] if len(cands) == 1 else None
        best = min(cands, key=lambda r: abs((r.get("ts_utc") or 0) - ts_utc))
        if abs((best.get("ts_utc") or 0) - ts_utc) > MATCH_TOLERANCE_SECONDS:
            return None
        return best

    def overall(self, filename: str, ts_utc: int | None) -> float | None:
        s = self.for_asset(filename, ts_utc)
        if not s:
            return None
        return s.get("ZOVERALLAESTHETICSCORE")


def composite(scores: dict) -> float:
    """One number from several, for ranking within a burst.

    ZOVERALLAESTHETICSCORE is Apple's own composite and carries most of the
    weight. The extras break ties between frames of the same scene, which is
    exactly where the overall score tends to agree with itself: of four shots
    of the same canyon, the one in focus and level is the keeper.
    """
    overall = scores.get("ZOVERALLAESTHETICSCORE")
    if overall is None:
        return 0.0
    extra = sum(scores.get(k, 0.0) or 0.0 for k in MERITS[1:])
    minus = sum(scores.get(k, 0.0) or 0.0 for k in PENALTIES)
    return overall + 0.1 * extra - 0.1 * minus


def group_bursts(items: list[dict], within_seconds: int) -> list[list[dict]]:
    """Split time-sorted items into runs shot close together.

    Not a burst in the camera sense — this catches the ordinary habit of taking
    four photos of the same view in half a minute. 162 of 651 wall photos were
    within 60 seconds of the one before, which is why the wall felt repetitive.

    Each item needs ``ts_utc``. Order is preserved.
    """
    if not items:
        return []
    ordered = sorted(items, key=lambda i: i.get("ts_utc") or 0)
    groups = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        gap = (cur.get("ts_utc") or 0) - (prev.get("ts_utc") or 0)
        if gap <= within_seconds:
            groups[-1].append(cur)
        else:
            groups.append([cur])
    return groups


def pick_best(group: list[dict], scores: Scores) -> dict:
    """The keeper from one burst.

    Falls back to the first frame when nothing in the group has a score —
    arbitrary, but stable, and better than dropping the whole group.
    """
    best, best_score = None, None
    for item in group:
        s = scores.for_asset(item.get("name", ""), item.get("ts_utc"))
        if s is None:
            continue
        val = composite(s)
        if best_score is None or val > best_score:
            best, best_score = item, val
    return best if best is not None else group[0]
