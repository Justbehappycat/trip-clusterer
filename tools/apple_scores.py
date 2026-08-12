#!/usr/bin/env python3
"""Read Apple's own aesthetic scores out of the macOS Photos library.

Photos runs ML over every image and stores ~30 scores per asset in its SQLite
database — overall, pleasant_composition, sharply_focused_subject,
well_framed_subject, low_light, noise and more. They drive Memories and
Featured Photos and are invisible in the UI. They are also exactly the
judgement a photo wall wants, already computed, at no CPU cost.

Runs on the Mac, not the NAS: the library only exists here. Read-only — the
database is copied to a temporary directory first, because Photos keeps it
open with a write-ahead log and reading it in place risks confusing Photos.

    python3 tools/apple_scores.py                 # coverage summary only
    python3 tools/apple_scores.py --export scores.json

Needs Full Disk Access for whatever runs it (Terminal, or Claude Code).
Revoke it afterwards; nothing else here needs it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile

LIBRARY = os.path.expanduser("~/Pictures/Photos Library.photoslibrary")

# Core Data prefixes every column with Z. The per-dimension scores live on
# ZCOMPUTEDASSETATTRIBUTES; the composite "overall" and "curation" scores are
# on ZASSET itself in current Photos versions, so both tables are searched.
WANTED = [
    "ZOVERALLAESTHETICSCORE", "ZCURATIONSCORE",
    "ZPLEASANTCOMPOSITIONSCORE", "ZSHARPLYFOCUSEDSUBJECTSCORE",
    "ZWELLFRAMEDSUBJECTSCORE", "ZWELLTIMEDSHOTSCORE",
    "ZWELLCHOSENSUBJECTSCORE", "ZINTERESTINGSUBJECTSCORE",
    "ZPLEASANTLIGHTINGSCORE", "ZHARMONIOUSCOLORSCORE",
    "ZLOWLIGHT", "ZNOISESCORE", "ZFAILURESCORE",
    "ZINTRUSIVEOBJECTPRESENCESCORE",
]

# Higher is worse for these — they are penalties, not merits.
PENALTIES = {"ZLOWLIGHT", "ZNOISESCORE", "ZFAILURESCORE",
             "ZINTRUSIVEOBJECTPRESENCESCORE"}


def open_copy(path: str) -> sqlite3.Connection:
    tmp = tempfile.mkdtemp(prefix="photoscores-")
    src = os.path.join(path, "database", "Photos.sqlite")
    for suffix in ("", "-wal", "-shm"):
        try:
            shutil.copy(src + suffix, os.path.join(tmp, "Photos.sqlite" + suffix))
        except FileNotFoundError:
            pass
        except PermissionError:
            raise SystemExit(
                "Permission denied reading the Photos library.\n"
                "Grant Full Disk Access to whatever is running this, in\n"
                "System Settings > Privacy & Security > Full Disk Access."
            )
    return sqlite3.connect(f"file:{os.path.join(tmp, 'Photos.sqlite')}?mode=ro", uri=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", default=LIBRARY)
    ap.add_argument("--export", metavar="FILE",
                    help="write {filename: {score: value}} as JSON")
    args = ap.parse_args()

    if not os.path.isdir(args.library):
        raise SystemExit(f"no Photos library at {args.library}")

    db = open_copy(args.library)

    def columns(table: str) -> set:
        return {r[1].upper() for r in db.execute(f"PRAGMA table_info({table})")}

    attr_cols = columns("ZCOMPUTEDASSETATTRIBUTES")
    asset_cols = columns("ZASSET")
    if not attr_cols:
        raise SystemExit("no ZCOMPUTEDASSETATTRIBUTES table — unexpected Photos version")

    # Which table each wanted score lives on. Photos moves them between
    # releases, so this is discovered every run rather than assumed.
    where = {}
    for w in WANTED:
        if w in attr_cols:
            where[w] = "c"
        elif w in asset_cols:
            where[w] = "a"
    print(f"found {len(where)} of {len(WANTED)} scores")
    for w, t in where.items():
        print(f"  {w:34} on {'ZCOMPUTEDASSETATTRIBUTES' if t == 'c' else 'ZASSET'}")
    missing = [w for w in WANTED if w not in where]
    if missing:
        print("  not in this version:", ", ".join(missing))
    if not where:
        return 1

    total = db.execute("SELECT COUNT(*) FROM ZASSET").fetchone()[0]
    print(f"\nassets in library: {total}")

    # ZASSET.ZFILENAME is Photos' internal name — a UUID for anything synced
    # from iCloud. The name the camera gave it (IMG_1234.HEIC), which is what
    # Immich stores, is on ZADDITIONALASSETATTRIBUTES.
    sel = ", ".join(f"{where[w]}.{w}" for w in where)
    q = (f"SELECT COALESCE(x.ZORIGINALFILENAME, a.ZFILENAME), a.ZDATECREATED, {sel} "
         f"FROM ZASSET a "
         f"LEFT JOIN ZCOMPUTEDASSETATTRIBUTES c ON c.Z_PK = a.ZCOMPUTEDATTRIBUTES "
         f"LEFT JOIN ZADDITIONALASSETATTRIBUTES x ON x.ZASSET = a.Z_PK")
    names = list(where)
    rows = list(db.execute(q))

    print("\ncoverage and distribution (p50 / p90):")
    values = {n: [] for n in names}
    for r in rows:
        for n, v in zip(names, r[2:]):
            if v is not None:
                values[n].append(v)
    for n in names:
        vs = sorted(values[n])
        if not vs:
            print(f"  {n:34} no values")
            continue
        p50 = vs[len(vs) // 2]
        p90 = vs[min(len(vs) - 1, int(len(vs) * 0.9))]
        flag = "  (penalty: lower is better)" if n in PENALTIES else ""
        print(f"  {n:34} {100*len(vs)/total:5.1f}% scored   "
              f"{p50:7.3f} / {p90:7.3f}{flag}")

    if args.export:
        # A *list*, not a filename-keyed dict. iPhones recycle IMG_1234 every
        # few thousand shots, so an eight-year library has many collisions —
        # 17,358 rows collapse to 15,244 names, and matching on name alone put
        # the wrong score on one wall photo in five. Consumers match on name
        # plus timestamp.
        out = []
        for r in rows:
            name = r[0]
            if not name:
                continue
            vals = {n: v for n, v in zip(names, r[2:]) if v is not None}
            if not vals:
                continue
            # Apple's epoch is 2001-01-01; Immich uses Unix.
            vals["name"] = name
            vals["ts_utc"] = int(r[1] + 978307200) if r[1] is not None else None
            out.append(vals)
        with open(args.export, "w") as fh:
            json.dump(out, fh)
        names_seen = len({o["name"] for o in out})
        print(f"\nwrote {len(out)} scored assets ({names_seen} distinct filenames) "
              f"to {args.export}")
        print("Filenames, timestamps and scores only — no coordinates, faces "
              "or thumbnails.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
