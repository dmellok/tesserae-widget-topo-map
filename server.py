"""Topographic map widget.

Draws a real topographic sheet for any place: contour lines traced out of a
digital elevation model, with water, railways and a road hierarchy laid over
them, a spot height on the local summit, and labelled index contours.

Data comes from three keyless public services:
  * AWS Terrain Tiles - "terrarium" PNGs, an elevation grid encoded in RGB
  * OpenFreeMap       - OpenMapTiles-schema vector tiles (.pbf), maxzoom 14
  * Nominatim         - place-name geocoding (only when a place name is given)

Neither tile format is one the standard library reads, and rather than take a
dependency this module carries both readers: a small protobuf reader for the
subset of the wire format MVT uses, and a PNG reader for the one colour type
terrarium ever emits. Contours are traced with marching squares, stitched into
polylines, simplified, and returned as relative SVG path data, so the client
only has to paint.

Terrain does not change and OpenStreetMap geometry for a fixed frame changes
rarely, so every tile and every finished trace is cached on disk; a warm render
costs no network at all.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent import futures
from typing import Any

TILE_HOST = "https://tiles.openfreemap.org"
TILEJSON = f"{TILE_HOST}/planet"
DEM_HOST = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "tesserae-topo-map/0.1 (+https://github.com/dmellok/tesserae)"

MAX_ZOOM = 14
MAX_TILES = 25
# Terrarium serves to z15, but its best global source (SRTM, 1 arc-second) is
# already fully resolved around z13; past that the tiles are upsampled and the
# extra pixels only cost time.
MAX_DEM_ZOOM = 14
MAX_DEM_TILES = 16
TILE_WORKERS = 8  # concurrent fetches; a cold frame must beat the render budget

COORD = 1000  # client-side coordinate space is COORD x COORD
# Bumped whenever tracing, bucketing or the payload shape changes. Sheets are
# cached against the frame, which cannot notice that the code that drew them
# has moved on; without this a rule change is invisible on every warm cell.
CACHE_VERSION = 8
# Marching-squares grid; the SQUARE of this is the cell count, and that count
# drives both the bilinear sampling and the trace. At a wide span the DEM is
# already being subsampled 3:1 here, so the cells bought above ~180 cost real
# seconds on a cold frame without showing up in the contours.
GRID_MAX = 180
GRID_MIN = 64
RELIEF_MAX = 80  # relief grid actually shipped to the client
BUILD_TTL_S = 7 * 86400
GEO_TTL_S = 30 * 86400
NODATA = -32768.0

# Contour intervals worth printing on a map, in metres.
NICE_INTERVALS = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000)
INDEX_EVERY = 5  # every nth contour is drawn heavy and labelled
MAX_LEVELS = 90  # hard stop; a huge range at a fine interval is unreadable
MAX_LABELS = 7

# Which overlays each theme wants. Colour lives on the client; this is only
# about how much geometry has to travel.
THEME_LAYERS = {
    "mono": {"parks": False, "rail": True, "relief": False},
    "survey": {"parks": True, "rail": True, "relief": False},
    "usgs": {"parks": True, "rail": True, "relief": False},
    "blueprint": {"parks": False, "rail": True, "relief": False},
    "relief": {"parks": False, "rail": False, "relief": True},
    "spectra": {"parks": True, "rail": True, "relief": False},
    "bwry": {"parks": False, "rail": False, "relief": False},
}

CUSTOM_LAYER_OPTIONS = {
    "parks": "show_parks",
    "rail": "show_rail",
    "relief": "show_relief",
}


def _truthy(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def _layers_for(theme: str, options: dict[str, Any]) -> dict[str, bool]:
    """Which geometry has to travel for this theme."""
    if theme == "custom":
        return {
            key: _truthy(options.get(opt), key != "relief")
            for key, opt in CUSTOM_LAYER_OPTIONS.items()
        }
    return THEME_LAYERS.get(theme, THEME_LAYERS["mono"])


# OpenMapTiles transportation classes -> the stroke buckets the client paints.
# A topo sheet generalises harder than a street poster: everything below a
# tertiary road collapses into one thin "minor" class.
ROAD_BUCKET = {
    "motorway": "motorway",
    "trunk": "motorway",
    "primary": "major",
    "secondary": "major",
    "tertiary": "mid",
    "busway": "mid",
    "minor": "minor",
    "track": "track",
    "path": "track",
}

# OpenMapTiles waterway classes. Mountain country has a mapped stream in
# every gully, so they are split off from named rivers and drawn as a hairline;
# ditches and drains are field drainage and never belong on a topo sheet.
WATER_BUCKET = {
    "river": "river",
    "canal": "river",
    "stream": "stream",
}

EPS = {
    "water": 1.6,
    "river": 1.2,
    "stream": 1.4,
    "green": 2.0,
    "rail": 1.2,
    "motorway": 0.8,
    "major": 0.9,
    "mid": 1.1,
    "minor": 1.3,
    "track": 1.6,
    "contour": 0.55,  # contours carry the sheet; simplify them gently
    "contour_index": 0.45,
}
MINSPAN = {"green": 3.0, "water": 2.0}

WANT_LAYERS = {"water", "waterway", "park", "landcover", "landuse", "transportation"}


# --------------------------------------------------------------------------
# protobuf / MVT reader
# --------------------------------------------------------------------------


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _zig(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _fields(buf: bytes, start: int = 0, end: int | None = None):
    end = len(buf) if end is None else end
    i = start
    while i < end:
        key, i = _varint(buf, i)
        fnum, wtype = key >> 3, key & 7
        if wtype == 0:
            val, i = _varint(buf, i)
        elif wtype == 1:
            val, i = (i, i + 8), i + 8
        elif wtype == 2:
            ln, i = _varint(buf, i)
            val, i = (i, i + ln), i + ln
        elif wtype == 5:
            val, i = (i, i + 4), i + 4
        else:  # unknown wire type - stop rather than misread the rest
            return
        yield fnum, wtype, val


def _packed(buf: bytes, span: tuple[int, int]) -> list[int]:
    out: list[int] = []
    i, end = span
    while i < end:
        v, i = _varint(buf, i)
        out.append(v)
    return out


def _value(buf: bytes, span: tuple[int, int]) -> Any:
    for fnum, _wt, val in _fields(buf, *span):
        if fnum == 1:
            return buf[val[0] : val[1]].decode("utf-8", "replace")
        if fnum == 2:
            return struct.unpack("<f", buf[val[0] : val[1]])[0]
        if fnum == 3:
            return struct.unpack("<d", buf[val[0] : val[1]])[0]
        if fnum in (4, 5):
            return val
        if fnum == 6:
            return _zig(val)
        if fnum == 7:
            return bool(val)
    return None


def _rings(cmds: list[int]) -> list[list[tuple[int, int]]]:
    rings: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    x = y = i = 0
    n = len(cmds)
    while i < n:
        ci = cmds[i]
        i += 1
        cmd, count = ci & 0x7, ci >> 3
        if cmd == 1:  # MoveTo
            for _ in range(count):
                if i + 1 >= n:
                    break
                x += _zig(cmds[i])
                y += _zig(cmds[i + 1])
                i += 2
                if cur:
                    rings.append(cur)
                cur = [(x, y)]
        elif cmd == 2:  # LineTo
            for _ in range(count):
                if i + 1 >= n:
                    break
                x += _zig(cmds[i])
                y += _zig(cmds[i + 1])
                i += 2
                cur.append((x, y))
        elif cmd == 7:  # ClosePath
            if cur:
                cur.append(cur[0])
                rings.append(cur)
                cur = []
        else:
            break
    if cur:
        rings.append(cur)
    return rings


def decode_tile(raw: bytes, want: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """Raw .pbf -> {layer_name: {"extent": int, "features": [...]}}."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    layers: dict[str, dict[str, Any]] = {}
    for fnum, _wt, val in _fields(raw):
        if fnum != 3:
            continue
        name = ""
        extent = 4096
        keys: list[str] = []
        values: list[Any] = []
        fspans: list[tuple[int, int]] = []
        for lf, _lw, lv in _fields(raw, *val):
            if lf == 1:
                name = raw[lv[0] : lv[1]].decode("utf-8", "replace")
                if want is not None and name not in want:
                    break  # skip this layer entirely
            elif lf == 2:
                fspans.append(lv)
            elif lf == 3:
                keys.append(raw[lv[0] : lv[1]].decode("utf-8", "replace"))
            elif lf == 4:
                values.append(_value(raw, lv))
            elif lf == 5:
                extent = lv
        if want is not None and name not in want:
            continue
        feats = []
        for fspan in fspans:
            ftype = 0
            tags: list[int] = []
            geom: list[int] = []
            for ff, _fw, fv in _fields(raw, *fspan):
                if ff == 2:
                    tags = _packed(raw, fv)
                elif ff == 3:
                    ftype = fv
                elif ff == 4:
                    geom = _packed(raw, fv)
            props = {}
            for j in range(0, len(tags) - 1, 2):
                ki, vi = tags[j], tags[j + 1]
                if ki < len(keys) and vi < len(values):
                    props[keys[ki]] = values[vi]
            feats.append({"type": ftype, "props": props, "geom": _rings(geom)})
        layers[name] = {"extent": extent, "features": feats}
    return layers


# --------------------------------------------------------------------------
# PNG reader (terrarium tiles only: 8-bit truecolour, no interlace)
# --------------------------------------------------------------------------


def decode_png_rgb(raw: bytes) -> tuple[int, int, bytes] | None:
    """8-bit RGB PNG -> (w, h, pixel bytes). None if it is any other shape.

    Terrarium only ever serves colour type 2 at depth 8, so the reader is
    deliberately narrow: anything else is a sign the endpoint changed, and
    guessing at it would silently produce wrong elevations.
    """
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    i = 8
    idat = bytearray()
    w = h = 0
    while i + 8 <= len(raw):
        ln = int.from_bytes(raw[i : i + 4], "big")
        typ = raw[i + 4 : i + 8]
        body = raw[i + 8 : i + 8 + ln]
        i += 12 + ln  # length + type + data + crc
        if typ == b"IHDR":
            if len(body) < 13:
                return None
            w = int.from_bytes(body[0:4], "big")
            h = int.from_bytes(body[4:8], "big")
            if body[8] != 8 or body[9] != 2 or body[12] != 0:
                return None  # depth / colour type / interlace not what we read
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    if not w or not h or not idat:
        return None
    try:
        data = zlib.decompress(bytes(idat))
    except zlib.error:
        return None

    stride, bpp = w * 3, 3
    if len(data) < h * (stride + 1):
        return None
    out = bytearray(h * stride)
    pos = 0
    for row in range(h):
        ftype = data[pos]
        pos += 1
        line = bytearray(data[pos : pos + stride])
        pos += stride
        o = row * stride
        p = o - stride
        if ftype == 1:  # Sub
            for k in range(bpp, stride):
                line[k] = (line[k] + line[k - bpp]) & 255
        elif ftype == 2:  # Up
            for k in range(stride):
                line[k] = (line[k] + out[p + k]) & 255
        elif ftype == 3:  # Average
            for k in range(stride):
                a = line[k - bpp] if k >= bpp else 0
                line[k] = (line[k] + ((a + out[p + k]) >> 1)) & 255
        elif ftype == 4:  # Paeth
            for k in range(stride):
                a = line[k - bpp] if k >= bpp else 0
                b = out[p + k]
                c = out[p + k - bpp] if k >= bpp else 0
                est = a + b - c
                pa, pb, pc = abs(est - a), abs(est - b), abs(est - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[k] = (line[k] + pred) & 255
        elif ftype != 0:
            return None
        out[o : o + stride] = line
    return w, h, bytes(out)


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------


def _merc(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2**z
    r = math.radians(max(-85.05, min(85.05, lat)))
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n
    return x, y


def _rdp(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 - i0 < 2:
            continue
        ax, ay = pts[i0]
        bx, by = pts[i1]
        dx, dy = bx - ax, by - ay
        d2 = dx * dx + dy * dy
        best, bi = eps, -1
        for i in range(i0 + 1, i1):
            px, py = pts[i]
            if d2 == 0:
                dist = math.hypot(px - ax, py - ay)
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / d2
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                dist = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if dist > best:
                best, bi = dist, i
        if bi >= 0:
            keep[bi] = True
            stack.append((i0, bi))
            stack.append((bi, i1))
    return [p for p, k in zip(pts, keep) if k]


def _path(rings, eps: float, close: bool, minspan: float) -> str:
    """Simplified, quantised, relative SVG path data."""
    out = []
    for ring in rings:
        if len(ring) < 2:
            continue
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        if max(xs) < -20 or min(xs) > COORD + 20:
            continue
        if max(ys) < -20 or min(ys) > COORD + 20:
            continue
        if minspan and (max(xs) - min(xs)) < minspan and (max(ys) - min(ys)) < minspan:
            continue
        q: list[tuple[int, int]] = []
        last = None
        for x, y in _rdp(ring, eps):
            xy = (int(round(x)), int(round(y)))
            if xy != last:
                q.append(xy)
                last = xy
        if len(q) < 2:
            continue
        px, py = q[0]
        segs = []
        for x, y in q[1:]:
            segs.append(f"{x - px} {y - py}")
            px, py = x, y
        # Absolute moveto, relative linetos: lowercase "m" would be relative to
        # the previous subpath's end point and every ring after the first would
        # drift. The deltas are where the size saving is anyway.
        out.append(f"M{q[0][0]} {q[0][1]}l" + "l".join(segs) + ("z" if close else ""))
    return "".join(out)


# --------------------------------------------------------------------------
# marching squares
# --------------------------------------------------------------------------

# Which pair of edges each of the 16 corner patterns joins. Corners are read
# TL, TR, BR, BL into the index; edges are T(op), R(ight), B(ottom), L(eft).
# 5 and 10 are the ambiguous saddles and are resolved against the cell centre.
MS_CASES = {
    1: (("L", "B"),),
    2: (("B", "R"),),
    3: (("L", "R"),),
    4: (("T", "R"),),
    6: (("T", "B"),),
    7: (("L", "T"),),
    8: (("L", "T"),),
    9: (("T", "B"),),
    11: (("T", "R"),),
    12: (("L", "R"),),
    13: (("B", "R"),),
    14: (("L", "B"),),
}
SADDLE_HIGH = (("L", "T"), ("B", "R"))
SADDLE_LOW = (("L", "B"), ("T", "R"))


def _trace_all(grid, interval, kfirst, klast, sx, sy) -> dict[int, list]:
    """Every contour level at once -> {level index: loose segments}.

    Tracing one level at a time means re-walking the whole grid per level, and
    at a couple of hundred squares a side that is the single most expensive
    thing this module does. Instead each cell is visited once and only the
    levels that fall between its own lowest and highest corner are considered;
    in real terrain that is one or two out of twenty-odd.
    """
    rows = len(grid)
    cols = len(grid[0])
    out: dict[int, list] = {}
    for j in range(rows - 1):
        r0 = grid[j]
        r1 = grid[j + 1]
        for i in range(cols - 1):
            a, b = r0[i], r0[i + 1]
            c, d = r1[i + 1], r1[i]
            lo = a if a < b else b
            if c < lo:
                lo = c
            if d < lo:
                lo = d
            hi = a if a > b else b
            if c > hi:
                hi = c
            if d > hi:
                hi = d
            k0 = int(math.floor(lo / interval))
            k1 = int(math.ceil(hi / interval))
            if k0 < kfirst:
                k0 = kfirst
            if k1 > klast:
                k1 = klast
            for k in range(k0, k1 + 1):
                _cell(out, k, k * interval, a, b, c, d, i, j, sx, sy)
    return out


def _cell(out, k, level, a, b, c, d, i, j, sx, sy) -> None:
    """Marching squares for one cell at one level."""
    idx = 0
    if a >= level:
        idx |= 8
    if b >= level:
        idx |= 4
    if c >= level:
        idx |= 2
    if d >= level:
        idx |= 1
    if idx == 0 or idx == 15:
        return
    if idx == 5 or idx == 10:
        centre = (a + b + c + d) / 4.0
        if idx == 5:
            pairs = SADDLE_HIGH if centre >= level else SADDLE_LOW
        else:
            pairs = SADDLE_LOW if centre >= level else SADDLE_HIGH
    else:
        pairs = MS_CASES[idx]

    # Edge crossings, interpolated. Every shared edge is computed from the
    # same two corner values in both cells, so neighbouring polylines meet at
    # bit-identical points and stitch cleanly. Only the edges this pattern
    # actually uses get computed.
    pts = {}
    for pair in pairs:
        for name in pair:
            if name in pts:
                continue
            if name == "T":
                t = (level - a) / (b - a) if b != a else 0.5
                pts[name] = ((i + t) * sx, j * sy)
            elif name == "B":
                t = (level - d) / (c - d) if c != d else 0.5
                pts[name] = ((i + t) * sx, (j + 1) * sy)
            elif name == "L":
                t = (level - a) / (d - a) if d != a else 0.5
                pts[name] = (i * sx, (j + t) * sy)
            else:
                t = (level - b) / (c - b) if c != b else 0.5
                pts[name] = ((i + 1) * sx, (j + t) * sy)
    segs = out.setdefault(k, [])
    for e0, e1 in pairs:
        segs.append((pts[e0], pts[e1]))


def _stitch(segs: list[tuple[tuple, tuple]]) -> list[list]:
    """Loose segments -> as few polylines as possible.

    Contour lines arrive as thousands of unordered two-point pieces. Painting
    them that way costs an SVG subpath each and makes dashing meaningless, so
    they get walked into runs first.
    """
    if not segs:
        return []
    adj: dict[tuple, list[tuple[tuple, int]]] = {}
    for n, (p, q) in enumerate(segs):
        adj.setdefault(p, []).append((q, n))
        adj.setdefault(q, []).append((p, n))
    used = [False] * len(segs)
    lines: list[list] = []
    for n, (p, q) in enumerate(segs):
        if used[n]:
            continue
        used[n] = True
        line = [p, q]
        for forward in (True, False):
            while True:
                cur = line[-1]
                step = None
                for other, k in adj.get(cur, ()):
                    if not used[k]:
                        step = (other, k)
                        break
                if step is None:
                    break
                used[step[1]] = True
                line.append(step[0])
            if forward:
                line.reverse()  # walk the other way from the original start
        lines.append(line)
    return lines


def _nice_interval(span: float, requested: float) -> float:
    """A contour interval that gives a readable number of lines."""
    if requested > 0:
        return requested
    if span <= 0:
        return 1.0
    # The smallest interval that stays under the cap wins, so a sheet carries
    # as many lines as it can still be read at. Printed sheets sit around
    # 20-30 contours; below about 10 the terrain stops having a shape.
    for step in NICE_INTERVALS:
        if span / step <= 26:
            return float(step)
    return float(NICE_INTERVALS[-1])


# --------------------------------------------------------------------------
# network + cache
# --------------------------------------------------------------------------


def _cache_dir(ctx: dict[str, Any], *parts: str) -> str | None:
    base = ctx.get("data_dir") if ctx else None
    if not base:
        return None
    p = os.path.join(base, *parts)
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        return None
    return p


def _get_bytes(url: str, timeout: float = 20.0) -> tuple[bytes | None, str | None]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), None
    except urllib.error.HTTPError as err:
        if err.code in (403, 404):
            return b"", None  # tile genuinely absent (ocean, out of range)
        return None, f"tile server returned {err.code}"
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, "could not reach the tile server"


def _json_cache(path: str | None, ttl: float) -> Any:
    if not path or not os.path.exists(path):
        return None
    try:
        if time.time() - os.path.getmtime(path) > ttl:
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _json_store(path: str | None, obj: Any) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
    except OSError:
        pass


def _tile_build(ctx: dict[str, Any]) -> tuple[str | None, str | None]:
    """Current OpenFreeMap planet build id, from the TileJSON."""
    meta = _cache_dir(ctx, "meta")
    path = os.path.join(meta, "tilejson.json") if meta else None
    cached = _json_cache(path, BUILD_TTL_S)
    if cached and cached.get("build"):
        return cached["build"], None
    raw, err = _get_bytes(TILEJSON, timeout=15.0)
    if not raw:
        return None, err or "tile server sent an empty index"
    try:
        doc = json.loads(raw.decode("utf-8"))
        tmpl = (doc.get("tiles") or [""])[0]
    except (ValueError, IndexError, AttributeError):
        return None, "tile server sent an unreadable index"
    parts = [p for p in tmpl.split("/") if p]
    build = None
    for i, p in enumerate(parts):
        if p == "planet" and i + 1 < len(parts):
            build = parts[i + 1]
            break
    if not build:
        return None, "tile server sent an unexpected index"
    _json_store(path, {"build": build})
    _prune(ctx, build)
    return build, None


def _prune(ctx: dict[str, Any], build: str) -> None:
    """Drop vector tiles cached under superseded planet builds, and old sheets.

    DEM tiles are never pruned: terrain does not get rebuilt, and they are the
    expensive half of a cold frame.
    """
    root = _cache_dir(ctx, "tiles")
    if root:
        try:
            names = os.listdir(root)
        except OSError:
            names = []
        for name in names:
            if name == build:
                continue
            stale = os.path.join(root, name)
            if not os.path.isdir(stale):
                continue
            try:
                for entry in os.listdir(stale):
                    os.remove(os.path.join(stale, entry))
                os.rmdir(stale)
            except OSError:
                pass  # best effort; a locked file just gets retried next time

    sheets = _cache_dir(ctx, "sheets")
    if not sheets:
        return
    cutoff = time.time() - GEO_TTL_S
    try:
        for entry in os.listdir(sheets):
            p = os.path.join(sheets, entry)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def _geocode(place: str, ctx: dict[str, Any]) -> tuple[dict | None, str | None]:
    meta = _cache_dir(ctx, "geo")
    safe = "".join(c if c.isalnum() else "_" for c in place.lower())[:80]
    path = os.path.join(meta, f"{safe}.en.json") if meta else None
    cached = _json_cache(path, GEO_TTL_S)
    if cached:
        return cached, None
    # accept-language=en keeps the name predictable; Nominatim otherwise
    # answers in the local script.
    qs = urllib.parse.urlencode(
        {
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 1,
            "accept-language": "en",
            "q": place,
        }
    )
    raw, err = _get_bytes(f"{NOMINATIM}?{qs}", timeout=15.0)
    if not raw:
        return None, err or "could not reach the geocoder"
    try:
        hits = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None, "the geocoder sent an unreadable reply"
    if not hits:
        return None, f"no place found for “{place}”"
    h = hits[0]
    addr = h.get("address") or {}
    out = {
        "lat": float(h["lat"]),
        "lon": float(h["lon"]),
        "name": (h.get("name") or place),
        "country": addr.get("country") or "",
    }
    _json_store(path, out)
    return out, None


def _fetch_many(jobs, ctx, subdir: str, suffix: str):
    """Fetch a list of (url, cache_key) concurrently, caching each forever.

    A cold frame is up to 25 vector tiles plus 16 DEM tiles; in series that
    overruns Tesserae's page-render budget on the first paint.
    """
    cdir = _cache_dir(ctx, *subdir.split("/"))

    def one(job):
        url, key = job
        path = os.path.join(cdir, f"{key}{suffix}") if cdir else None
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    return fh.read(), None
            except OSError:
                pass
        raw, err = _get_bytes(url)
        if raw is None:
            return None, err
        if path:
            try:
                with open(path, "wb") as fh:
                    fh.write(raw)
            except OSError:
                pass
        return raw, None

    if not jobs:
        return [], None
    workers = min(TILE_WORKERS, len(jobs))
    with futures.ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, jobs))
    for raw, err in results:
        if raw is None:
            return None, err
    return [raw for raw, _ in results], None


# --------------------------------------------------------------------------
# elevation
# --------------------------------------------------------------------------


def _dem_frame(lat: float, span_m: float) -> tuple[int, float]:
    """Pick the DEM zoom, and report how many DEM pixels the frame spans.

    The highest zoom whose tile count fits the budget wins: more pixels across
    the frame means smoother contours, up to the point the source runs out of
    real detail.
    """
    for z in range(MAX_DEM_ZOOM, 0, -1):
        res = 156543.03392 * math.cos(math.radians(lat)) / (2**z)
        if res <= 0:
            continue
        px = span_m / res
        tiles = (math.ceil(px / 256) + 1) ** 2
        if tiles <= MAX_DEM_TILES:
            return z, px
    return 1, span_m / (156543.03392 * math.cos(math.radians(lat)) / 2)


def _dem_grid(lat, lon, span_m, ctx) -> tuple[Any, str | None]:
    """Sample the DEM into a square grid covering the frame.

    Returns (grid, None) with grid[row][col] in metres, north-up, or an error.
    """
    z, frame_px = _dem_frame(lat, span_m)
    cx, cy = _merc(lat, lon, z)
    half = (frame_px / 2) / 256.0  # frame half-width, in tile units
    x0, y0 = cx - half, cy - half
    x1, y1 = cx + half, cy + half

    tx0, tx1 = math.floor(x0), math.floor(x1)
    ty0, ty1 = math.floor(y0), math.floor(y1)
    coords = [
        (tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)
    ]
    if len(coords) > MAX_DEM_TILES:
        return None, "That area is too large to contour. Reduce the span."

    jobs = [(f"{DEM_HOST}/{z}/{tx}/{ty}.png", f"{z}_{tx}_{ty}") for tx, ty in coords]
    raws, err = _fetch_many(jobs, ctx, "dem", ".png")
    if err:
        return None, "could not reach the elevation server"

    tiles: dict[tuple[int, int], bytes] = {}
    for (tx, ty), raw in zip(coords, raws):
        if not raw:
            continue
        got = decode_png_rgb(raw)
        if got is None:
            return None, "the elevation server sent an unreadable tile"
        _w, _h, px = got
        tiles[(tx, ty)] = px

    if not tiles:
        return None, "No elevation data for that location."

    def at(gx: int, gy: int) -> float:
        """Elevation at a global DEM pixel; NODATA outside the fetched tiles."""
        tx, lx = divmod(gx, 256)
        ty, ly = divmod(gy, 256)
        px = tiles.get((tx, ty))
        if px is None:
            return NODATA
        o = (ly * 256 + lx) * 3
        return (px[o] * 256 + px[o + 1] + px[o + 2] / 256.0) - 32768.0

    grid_n = max(GRID_MIN, min(GRID_MAX, int(round(frame_px))))
    rows: list[list[float]] = []
    span_x = (x1 - x0) * 256.0
    px_x0 = x0 * 256.0
    px_y0 = y0 * 256.0
    for j in range(grid_n):
        fy = px_y0 + (j + 0.5) / grid_n * span_x - 0.5
        jy = math.floor(fy)
        wy = fy - jy
        row: list[float] = []
        for i in range(grid_n):
            fx = px_x0 + (i + 0.5) / grid_n * span_x - 0.5
            ix = math.floor(fx)
            wx = fx - ix
            # Bilinear: the grid is usually finer than the DEM, and nearest
            # neighbour would terrace every contour along pixel boundaries.
            p00 = at(ix, jy)
            p10 = at(ix + 1, jy)
            p01 = at(ix, jy + 1)
            p11 = at(ix + 1, jy + 1)
            if NODATA in (p00, p10, p01, p11):
                row.append(NODATA)
                continue
            top = p00 + (p10 - p00) * wx
            bot = p01 + (p11 - p01) * wx
            row.append(top + (bot - top) * wy)
        rows.append(row)

    # A frame straddling the edge of coverage would otherwise trace a contour
    # along the NODATA boundary; flood the holes with the nearest real value.
    real = [v for row in rows for v in row if v != NODATA]
    if not real:
        return None, "No elevation data for that location."
    fill = sum(real) / len(real)
    for row in rows:
        for i, v in enumerate(row):
            if v == NODATA:
                row[i] = fill
    return rows, None


# --------------------------------------------------------------------------
# contours
# --------------------------------------------------------------------------


def _contours(grid, interval: float, index_every: int):
    """Trace every level crossing the grid. Returns (minor, index, levels)."""
    flat = [v for row in grid for v in row]
    lo, hi = min(flat), max(flat)
    first = math.floor(lo / interval) + 1
    last = math.ceil(hi / interval) - 1
    if last < first:
        return [], [], []
    if last - first + 1 > MAX_LEVELS:
        last = first + MAX_LEVELS - 1

    n = len(grid)
    sx = COORD / (n - 1)
    sy = COORD / (n - 1)
    traced = _trace_all(grid, interval, first, last, sx, sy)
    minor: list[list] = []
    index: list[list] = []
    levels: list[float] = []
    for k in range(first, last + 1):
        level = k * interval
        lines = _stitch(traced.get(k) or [])
        if not lines:
            continue
        levels.append(level)
        # An index contour is one at a round multiple of the interval, counted
        # from zero, so the heavy lines land on the same heights any two sheets
        # of the same area would agree on.
        if index_every > 0 and k % index_every == 0:
            index.append((level, lines))
        else:
            minor.extend(lines)
    return minor, index, levels


def _label_points(index_lines, interval: float, unit_scale: float):
    """Spot a few index contours with their height, the way a sheet does."""
    out = []
    by_level: dict[float, list] = {}
    for level, lines in index_lines:
        for line in lines:
            if len(line) >= 12:
                by_level.setdefault(level, []).append(line)
    for lines in by_level.values():
        lines.sort(reverse=True, key=len)

    # Round-robin across heights, longest line of each first. Taking the
    # globally longest lines instead would label one contour four times and
    # tell the reader nothing about the range.
    candidates = []
    for rank in range(4):
        for level in sorted(by_level):
            lines = by_level[level]
            if rank < len(lines):
                candidates.append((level, lines[rank]))

    for level, line in candidates:
        if len(out) >= MAX_LABELS:
            break
        # Several positions are tried along the line rather than one. A single
        # fixed point means a contour whose 40% mark happens to fall outside
        # the printable band goes unlabelled entirely, which on concentric
        # terrain like a volcano leaves almost the whole sheet bare.
        for frac in (0.4, 0.6, 0.25, 0.75, 0.5, 0.15, 0.85):
            i = max(1, min(len(line) - 2, int(len(line) * frac)))
            x, y = line[i]
            # The sheet is sliced to fill the cell, so how much of the viewBox
            # survives depends entirely on aspect: a wide poster keeps roughly
            # the middle half of the height, a tall one the middle
            # three-quarters of the width. A label clipped in two is worse
            # than a label not placed, so they are confined to the band that
            # survives either extreme.
            # A steeply rotated number occupies far more vertical room than
            # its font size suggests, and the name band eats the foot of the
            # cell, so the lower bound is tighter than the upper.
            if x < 180 or x > COORD - 180 or y < 300 or y > COORD - 340:
                continue
            if any(math.hypot(x - o["x"], y - o["y"]) < 190 for o in out):
                continue
            ax, ay = line[i - 1]
            bx, by = line[i + 1]
            angle = math.degrees(math.atan2(by - ay, bx - ax))
            if angle > 90:
                angle -= 180
            elif angle < -90:
                angle += 180
            # Levels are traced in metres; the printed height follows whatever
            # unit the sheet is drawn in.
            shown = level * unit_scale
            text = f"{int(round(shown))}" if interval >= 1 else f"{shown:.1f}"
            out.append(
                {
                    "x": round(x, 1),
                    "y": round(y, 1),
                    "angle": round(angle, 1),
                    "text": text,
                }
            )
            break
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def _num(options: dict, name: str, default: float) -> float:
    v = options.get(name, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _coord_pair(lat: Any, lon: Any) -> tuple[float | None, float | None]:
    """Both coordinates, or neither. A half-filled pair is not a location."""
    if lat in (None, "") or lon in (None, ""):
        return None, None
    try:
        flat, flon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flon <= 180.0):
        return None, None
    return flat, flon


def _parse_lat_lon(text: str) -> tuple[float | None, float | None]:
    """Accept a literal ``"-37.65,145.09"`` the way the composer does."""
    parts = text.replace(";", ",").split(",")
    if len(parts) != 2:
        return None, None
    return _coord_pair(parts[0].strip(), parts[1].strip())


def _fmt_coord(lat: float, lon: float) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.4f}° {ns} / {abs(lon):.4f}° {ew}"


def _vector_paths(lat, lon, span_m, layers, ctx):
    """Water, parks, rail and roads for the frame, as SVG path data."""
    build, err = _tile_build(ctx)
    if err:
        return {}, 0, err

    z = MAX_ZOOM
    while z > 1:
        n = 2**z
        m_per_tile = 40075016.686 * math.cos(math.radians(lat)) / n
        if m_per_tile <= 0:
            break
        if math.ceil(span_m / m_per_tile) + 1 <= int(math.sqrt(MAX_TILES)):
            break
        z -= 1

    n = 2**z
    m_per_tile = 40075016.686 * math.cos(math.radians(lat)) / n
    if m_per_tile <= 0:
        return {}, 0, "That latitude is too close to the pole to map."
    cx, cy = _merc(lat, lon, z)
    half = (span_m / 2) / m_per_tile
    x0, x1, y0, y1 = cx - half, cx + half, cy - half, cy + half
    coords = [
        (z, tx, ty)
        for tx in range(math.floor(x0), math.floor(x1) + 1)
        for ty in range(math.floor(y0), math.floor(y1) + 1)
    ]
    if len(coords) > MAX_TILES:
        return {}, 0, "That area is too large to draw. Reduce the span."

    jobs = [
        (f"{TILE_HOST}/planet/{build}/{tz}/{tx}/{ty}.pbf", f"{tz}_{tx}_{ty}")
        for tz, tx, ty in coords
    ]
    raws, err = _fetch_many(jobs, ctx, f"tiles/{build}", ".pbf")
    if err:
        return {}, 0, err

    sx = COORD / (x1 - x0)
    sy = COORD / (y1 - y0)
    buckets: dict[str, list] = {}
    for (_z, tx, ty), raw in zip(coords, raws):
        if not raw:
            continue
        for lname, layer in decode_tile(raw, WANT_LAYERS).items():
            e = layer["extent"]
            for feat in layer["features"]:
                cls = feat["props"].get("class")
                key = None
                if lname == "water" and cls != "swimming_pool":
                    key = "water"
                elif lname == "waterway":
                    key = WATER_BUCKET.get(cls or "")
                elif lname == "park" or (
                    lname == "landcover" and cls in ("grass", "wood", "forest")
                ):
                    key = "green" if layers["parks"] else None
                elif lname == "transportation":
                    if cls == "rail":
                        key = "rail" if layers["rail"] else None
                    elif layers["roads"]:
                        key = ROAD_BUCKET.get(cls or "")
                if not key:
                    continue
                rings = [
                    [
                        (((tx + px / e) - x0) * sx, ((ty + py / e) - y0) * sy)
                        for px, py in ring
                    ]
                    for ring in feat["geom"]
                ]
                buckets.setdefault(key, []).extend(rings)

    closed = ("water", "green")
    paths = {}
    for key, rings in buckets.items():
        d = _path(rings, EPS.get(key, 1.2), key in closed, MINSPAN.get(key, 0.0))
        if d:
            paths[key] = d
    return paths, len(coords), None


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    ctx = ctx or {}
    theme = str(options.get("theme") or "mono")
    layers = _layers_for(theme, options)
    # Roads are the one overlay that can beat the contours outright: at city
    # spans the street grid is denser than the terrain. Every theme gets the
    # switch, not just the custom one.
    layers["roads"] = _truthy(options.get("show_roads"), True)
    span_m = max(500.0, min(80000.0, _num(options, "span_m", 8000)))
    requested = max(0.0, _num(options, "interval_m", 0))
    index_every = int(max(0, _num(options, "index_every", INDEX_EVERY)))
    feet = _truthy(options.get("feet"), False)

    # --- where -------------------------------------------------------------
    # A ``location_search`` cell option is promoted by Tesserae's composer into
    # flat ``latitude`` / ``longitude`` / ``label`` before fetch() runs, so the
    # resolved coordinates are the normal path. The rest is for callers that
    # bypass the composer (Studio renders, MCP-authored cells).
    lat, lon = _coord_pair(options.get("latitude"), options.get("longitude"))
    name = str(options.get("label") or "").strip()
    # When the composer resolved the location it also resolved the country,
    # and the geocode branch below never runs to find it.
    country = str(options.get("country") or "").strip()

    if lat is None:
        loc = options.get("location") or options.get("place")
        if isinstance(loc, dict):
            lat, lon = _coord_pair(loc.get("latitude"), loc.get("longitude"))
            name = name or str(loc.get("name") or "").strip()
        elif isinstance(loc, str) and loc.strip():
            lat, lon = _parse_lat_lon(loc)
            if lat is None:
                hit, err = _geocode(loc.strip(), ctx)
                if err:
                    return {"error": err}
                lat, lon = hit["lat"], hit["lon"]
                name = name or hit["name"]
                country = hit["country"]

    if lat is None:
        return {"error": "Pick a location for this cell."}
    if not name:
        name = _fmt_coord(lat, lon)

    # --- already traced this exact sheet? ----------------------------------
    # Tracing is the expensive half and the answer depends only on the frame,
    # never on colour, so the finished path data is what gets cached.
    sig = json.dumps(
        {
            "v": CACHE_VERSION,
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "s": round(span_m, 1),
            "i": requested,
            "x": index_every,
            "f": feet,
            "l": sorted(k for k, v in layers.items() if v),
        },
        sort_keys=True,
    )
    sdir = _cache_dir(ctx, "sheets")
    spath = (
        os.path.join(sdir, hashlib.sha1(sig.encode()).hexdigest()[:20] + ".json")
        if sdir
        else None
    )
    cached = _json_cache(spath, GEO_TTL_S)
    if cached:
        return _result(cached, name, lat, lon, span_m, theme, country, options)

    grid, err = _dem_grid(lat, lon, span_m, ctx)
    if err:
        return {"error": err}

    flat = [v for row in grid for v in row]
    lo, hi = min(flat), max(flat)
    # Contours are always traced in metres; feet only changes what gets
    # printed, so the interval is converted rather than the elevations.
    unit_scale = 3.28084 if feet else 1.0
    interval = _nice_interval((hi - lo) * unit_scale, requested) / unit_scale
    if interval <= 0:
        interval = 1.0

    minor, index, levels = _contours(grid, interval, index_every)
    if not levels:
        return {
            "error": "The ground here is too flat to contour. "
            "Widen the span or set a finer interval."
        }

    # Highest point in frame, for the spot height. A maximum sitting on the
    # frame edge is almost always the shoulder of a summit that is actually
    # outside it, so it gets no mark: a triangulation symbol half off the
    # sheet claims a peak that is not there.
    n = len(grid)
    peak_j = peak_i = 0
    for j, row in enumerate(grid):
        for i, v in enumerate(row):
            if v == hi:
                peak_j, peak_i = j, i
                break
        else:
            continue
        break
    step = COORD / (n - 1)
    edge = n * 0.05
    on_edge = (
        peak_i < edge or peak_i > n - edge or peak_j < edge or peak_j > n - edge
    )

    payload = {
        "minor": _path(minor, EPS["contour"], False, 0.0),
        "index": _path(
            [ln for _lv, lines in index for ln in lines],
            EPS["contour_index"],
            False,
            0.0,
        ),
        "labels": _label_points(index, interval * unit_scale, unit_scale),
        "interval": round(interval * unit_scale, 2),
        "min": round(lo * unit_scale),
        "max": round(hi * unit_scale),
        "lines": len(levels),
        "peak": None if on_edge else {
            "x": round(peak_i * step, 1),
            "y": round(peak_j * step, 1),
            "elev": round(hi * unit_scale),
        },
        "unit": "ft" if feet else "m",
    }
    if layers["relief"]:
        payload["relief"] = _relief(grid, lo, hi)

    vec, tiles, verr = _vector_paths(lat, lon, span_m, layers, ctx)
    if verr:
        # A topo sheet is still a topo sheet without its roads; only the
        # contours are load-bearing, so a vector-tile outage degrades rather
        # than fails.
        payload["overlay_error"] = verr
    payload["paths"] = vec
    payload["tiles"] = tiles

    _json_store(spath, payload)
    return _result(payload, name, lat, lon, span_m, theme, country, options)


def _relief(grid, lo: float, hi: float) -> dict[str, Any]:
    """A coarse normalised height field for the client to shade from."""
    n = len(grid)
    m = min(RELIEF_MAX, n)
    rng = (hi - lo) or 1.0
    cells = []
    for j in range(m):
        sj = min(n - 1, int(j * n / m))
        row = grid[sj]
        for i in range(m):
            si = min(n - 1, int(i * n / m))
            cells.append(int(round((row[si] - lo) / rng * 255)))
    return {"w": m, "h": m, "cells": cells}


def _result(payload, name, lat, lon, span_m, theme, country, options):
    """Shape the widget payload. Shared so a cache hit and a fresh trace are
    indistinguishable to the client."""
    sub = _fmt_coord(lat, lon)
    if country and _truthy(options.get("show_country"), True):
        sub = f"{country.upper()}  ·  {sub}"
    unit = payload.get("unit", "m")
    return {
        "label": name.upper(),
        "sub": sub,
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "span_m": int(span_m),
        "theme": theme,
        "size": COORD,
        "contours": {"minor": payload["minor"], "index": payload["index"]},
        "labels": payload["labels"],
        "paths": payload.get("paths") or {},
        "relief": payload.get("relief"),
        "peak": payload["peak"],
        "interval": payload["interval"],
        "min_elev": payload["min"],
        "max_elev": payload["max"],
        "lines": payload["lines"],
        "unit": unit,
        "legend": f"CONTOUR INTERVAL {payload['interval']:g} {unit.upper()}",
        "tiles": payload.get("tiles", 0),
        "overlay_error": payload.get("overlay_error"),
        "attribution": "© Terrain Tiles · OpenFreeMap · OpenStreetMap",
    }
