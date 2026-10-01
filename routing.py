"""Turn a leg between two points into a path that follows how you actually travelled.

- car / bus: road route from the public OSRM server
- train: rail route from the public BRouter server ("rail" profile), else a smooth curve
- flight: great-circle arc
- anything else: straight line
Successful network routes are cached in cache/route_cache.json.
"""

import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

CACHE_FILE = Path(__file__).parent / "cache" / "route_cache.json"
USER_AGENT = "map-trip-visualizer/1.0"
SIMPLIFY_TOLERANCE_DEG = 0.0003  # ~30 m; keeps the HTML small without visible loss at trip scale

ROAD_MODES = {"car", "bus"}


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def path_km(coords):
    return sum(haversine_km(a, b) for a, b in zip(coords, coords[1:]))


def simplify(points, tolerance=SIMPLIFY_TOLERANCE_DEG):
    """Ramer-Douglas-Peucker line simplification (planar, in degrees)."""
    n = len(points)
    if n < 3:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        (y1, x1), (y2, x2) = points[start], points[end]
        dx, dy = x2 - x1, y2 - y1
        seg_len = math.hypot(dx, dy)
        worst, worst_i = 0.0, None
        for i in range(start + 1, end):
            y, x = points[i]
            if seg_len == 0:
                d = math.hypot(x - x1, y - y1)
            else:
                d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / seg_len
            if d > worst:
                worst, worst_i = d, i
        if worst_i is not None and worst > tolerance:
            keep[worst_i] = True
            stack.extend(((start, worst_i), (worst_i, end)))
    return [p for p, k in zip(points, keep) if k]


def great_circle(a, b, steps=64):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    v1 = (math.cos(lat1) * math.cos(lon1), math.cos(lat1) * math.sin(lon1), math.sin(lat1))
    v2 = (math.cos(lat2) * math.cos(lon2), math.cos(lat2) * math.sin(lon2), math.sin(lat2))
    omega = math.acos(max(-1.0, min(1.0, sum(p * q for p, q in zip(v1, v2)))))
    if omega == 0:
        return [a, b]
    coords, prev_lon = [], None
    for i in range(steps + 1):
        t = i / steps
        s1, s2 = math.sin((1 - t) * omega) / math.sin(omega), math.sin(t * omega) / math.sin(omega)
        x, y, z = (s1 * p + s2 * q for p, q in zip(v1, v2))
        lat, lon = math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))
        if prev_lon is not None:  # unwrap so arcs crossing the antimeridian stay continuous
            lon += 360 * round((prev_lon - lon) / 360)
        coords.append((lat, lon))
        prev_lon = lon
    return coords


def smooth_curve(a, b, bend=0.12, steps=32):
    """Gentle quadratic Bezier between two points, used when no real rail geometry exists."""
    (lat1, lon1), (lat2, lon2) = a, b
    mid_lat, mid_lon = (lat1 + lat2) / 2, (lon1 + lon2) / 2
    ctrl = (mid_lat - (lon2 - lon1) * bend, mid_lon + (lat2 - lat1) * bend)
    return [
        (
            (1 - t) ** 2 * lat1 + 2 * (1 - t) * t * ctrl[0] + t**2 * lat2,
            (1 - t) ** 2 * lon1 + 2 * (1 - t) * t * ctrl[1] + t**2 * lon2,
        )
        for t in (i / steps for i in range(steps + 1))
    ]


def _get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def _osrm_driving(a, b):
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson"
    )
    data = _get_json(url)
    if data.get("code") != "Ok":
        raise RuntimeError(data.get("message") or data.get("code"))
    route = data["routes"][0]
    coords = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]
    return coords, route["distance"] / 1000, route["duration"] / 3600


def _brouter_rail(a, b):
    lonlats = urllib.parse.quote(f"{a[1]},{a[0]}|{b[1]},{b[0]}", safe=",")
    url = f"https://brouter.de/brouter?lonlats={lonlats}&profile=rail&alternativeidx=0&format=geojson"
    feature = _get_json(url)["features"][0]
    coords = [(c[1], c[0]) for c in feature["geometry"]["coordinates"]]
    return coords, float(feature["properties"]["track-length"]) / 1000, None


class Router:
    def __init__(self):
        self.cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
        self._dirty = False

    def save(self):
        if self._dirty:
            CACHE_FILE.parent.mkdir(exist_ok=True)
            CACHE_FILE.write_text(json.dumps(self.cache), encoding="utf-8")

    def _fetch(self, kind, fetcher, a, b):
        key = f"{kind}|{a[0]:.5f},{a[1]:.5f}|{b[0]:.5f},{b[1]:.5f}"
        if key not in self.cache:
            coords, km, hours = fetcher(a, b)
            coords = [(round(lat, 5), round(lon, 5)) for lat, lon in simplify(coords)]
            self.cache[key] = {"coords": coords, "km": km, "hours": hours}
            self._dirty = True
        entry = self.cache[key]
        return [tuple(c) for c in entry["coords"]], entry["km"], entry["hours"]

    def route(self, a, b, mode, label=""):
        """Return {"coords", "km", "hours", "source"} for a leg from a to b."""
        mode = (mode or "").lower()
        try:
            if mode in ROAD_MODES:
                coords, km, hours = self._fetch("road", _osrm_driving, a, b)
                return {"coords": coords, "km": km, "hours": hours, "source": "road"}
            if mode == "train":
                coords, km, hours = self._fetch("rail", _brouter_rail, a, b)
                return {"coords": coords, "km": km, "hours": hours, "source": "rail"}
        except Exception as exc:  # network down, service error, no route found...
            hint = " (tip: give stops a \"station\" so rail legs start on the tracks)" if mode == "train" else ""
            print(f"Warning: no {mode} route for {label}: {exc}. Drawing an approximate line{hint}.")

        if mode == "flight":
            coords = great_circle(a, b)
        elif mode == "train":
            coords = smooth_curve(a, b)
        else:
            coords = [a, b]
        return {"coords": coords, "km": path_km(coords), "hours": None, "source": "approx"}
