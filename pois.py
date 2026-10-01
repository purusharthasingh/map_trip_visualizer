"""Find notable points of interest along a route using OpenStreetMap's Overpass API.

Only features with a Wikidata link are considered, and they are ranked by how many
Wikipedia language editions cover them, so famous places beat obscure ones.
Results are cached per leg in cache/poi_cache.json.
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from routing import USER_AGENT, haversine_km

CACHE_FILE = Path(__file__).parent / "cache" / "poi_cache.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
CHUNK_KM = 60  # route is split into chunks; each gets a small bounding box instead of one huge one
MIN_SITELINKS = 3  # a place needs articles in at least this many Wikipedia languages to count as notable

# (OSM key, value) -> (label, icon, priority). Higher priority wins when thinning out crowded areas.
CATEGORIES = {
    ("historic", "castle"): ("Castle", "🏰", 5),
    ("natural", "waterfall"): ("Waterfall", "💧", 5),
    ("tourism", "viewpoint"): ("Viewpoint", "👁", 4),
    ("historic", "archaeological_site"): ("Archaeological site", "🏺", 4),
    ("historic", "ruins"): ("Ruins", "🏚", 3),
    ("natural", "peak"): ("Peak", "⛰", 3),
    ("tourism", "museum"): ("Museum", "🏛", 2),
    ("tourism", "attraction"): ("Attraction", "✦", 2),
    ("historic", "monument"): ("Monument", "🗿", 1),
}
# Things occasionally tagged as attractions that nobody wants suggested (e.g. power stations).
EXCLUDED_KEYS = ("power", "industrial", "military", "man_made")


def _category(tags):
    if any(key in tags for key in EXCLUDED_KEYS):
        return None
    for (key, value), info in CATEGORIES.items():
        if tags.get(key) == value:
            return info
    return None


def _distance_to_path_km(point, path):
    """Approximate shortest distance from a point to a polyline (equirectangular projection)."""
    lat0 = math.radians(point[0])
    kx, ky = 111.32 * math.cos(lat0), 110.57

    def xy(p):
        return (p[1] - point[1]) * kx, (p[0] - point[0]) * ky

    best = float("inf")
    for a, b in zip(path, path[1:]):
        (x1, y1), (x2, y2) = xy(a), xy(b)
        dx, dy = x2 - x1, y2 - y1
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / seg2))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


def _chunk_bboxes(coords, margin_km):
    """Split the route into ~CHUNK_KM pieces and return a padded (s, w, n, e) box for each."""
    boxes, chunk, chunk_km = [], [coords[0]], 0.0
    for a, b in zip(coords, coords[1:]):
        chunk.append(b)
        chunk_km += haversine_km(a, b)
        if chunk_km >= CHUNK_KM:
            boxes.append(chunk)
            chunk, chunk_km = [b], 0.0
    if len(chunk) > 1 or not boxes:
        boxes.append(chunk)

    result = []
    for pts in boxes:
        lat_pad = margin_km / 110.57
        lon_pad = margin_km / (111.32 * math.cos(math.radians(pts[0][0])))
        result.append((
            min(p[0] for p in pts) - lat_pad, min(p[1] for p in pts) - lon_pad,
            max(p[0] for p in pts) + lat_pad, max(p[1] for p in pts) + lon_pad,
        ))
    return result


def _overpass_query(bboxes):
    selectors = [f'nwr["{k}"="{v}"]["wikidata"]' for k, v in CATEGORIES]
    parts = [f"{sel}({s:.4f},{w:.4f},{n:.4f},{e:.4f});" for s, w, n, e in bboxes for sel in selectors]
    return "[out:json][timeout:120];(" + "".join(parts) + ");out center tags;"


def _post_overpass(query, retries=2):
    body = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(OVERPASS_URL, data=body, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=150) as response:
                return json.load(response)["elements"]
        except urllib.error.HTTPError as exc:
            # 429 = rate limited, 504 = server busy; both are worth a short wait and retry.
            if exc.code not in (429, 504) or attempt == retries:
                raise
            time.sleep(10 * (attempt + 1))


def _wikipedia_url(tags):
    value = tags.get("wikipedia")
    if not value or ":" not in value:
        return None
    lang, title = value.split(":", 1)
    return f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"


class PoiFinder:
    def __init__(self, radius_km=5.0, min_spacing_km=12.0, stop_clearance_km=3.0):
        self.radius_km = radius_km
        self.min_spacing_km = min_spacing_km
        self.stop_clearance_km = stop_clearance_km
        self._picked, self._picked_names = [], set()
        self.cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
        self._dirty = False

    def save(self):
        if self._dirty:
            CACHE_FILE.parent.mkdir(exist_ok=True)
            CACHE_FILE.write_text(json.dumps(self.cache), encoding="utf-8")

    def _candidates(self, coords):
        """Every notable feature within radius of the route (cached; network on first run)."""
        a, b = coords[0], coords[-1]
        key = f"v3|{a[0]:.5f},{a[1]:.5f}|{b[0]:.5f},{b[1]:.5f}|{len(coords)}|{self.radius_km}"
        if key not in self.cache:
            elements = _post_overpass(_overpass_query(_chunk_bboxes(coords, self.radius_km)))
            found, seen = [], set()
            for el in elements:
                tags = el.get("tags", {})
                name = tags.get("name:en") or tags.get("name")
                category = _category(tags)
                center = el.get("center") or el
                osm_id = f"{el['type']}/{el['id']}"
                if not name or not category or "lat" not in center or osm_id in seen:
                    continue
                seen.add(osm_id)
                point = (center["lat"], center["lon"])
                off_route = _distance_to_path_km(point, coords)
                if off_route <= self.radius_km:
                    found.append({
                        "id": osm_id, "wikidata": tags["wikidata"], "name": name, "lat": point[0], "lon": point[1],
                        "category": category[0], "icon": category[1], "priority": category[2],
                        "off_route_km": round(off_route, 1), "wikipedia": _wikipedia_url(tags),
                    })
            _add_wikidata_fame(found)
            self.cache[key] = found
            self._dirty = True
        return self.cache[key]

    def along(self, coords, avoid_points=(), max_count=None):
        """Pick the most notable, well-spread points of interest along a route.

        Places already picked for earlier legs in this run are neither repeated nor crowded.
        """
        candidates = [
            c for c in self._candidates(coords)
            if c["sitelinks"] >= MIN_SITELINKS
            and c["name"] not in self._picked_names
            and all(haversine_km((c["lat"], c["lon"]), p) > self.stop_clearance_km for p in avoid_points)
        ]
        # Fame first (Wikipedia language editions), category as a small bonus, then closeness to the road.
        candidates.sort(key=lambda c: (-(c["sitelinks"] + c["priority"]), c["off_route_km"]))
        per_category_cap = math.ceil(max_count / 2) if max_count else None
        chosen, per_category = [], {}
        for c in candidates:
            point = (c["lat"], c["lon"])
            if per_category_cap and per_category.get(c["category"], 0) >= per_category_cap:
                continue
            if all(haversine_km(point, (o["lat"], o["lon"])) >= self.min_spacing_km for o in chosen + self._picked):
                chosen.append(c)
                per_category[c["category"]] = per_category.get(c["category"], 0) + 1
                if max_count and len(chosen) >= max_count:
                    break
        self._picked += chosen
        self._picked_names.update(c["name"] for c in chosen)

        def position_on_route(c):
            return min(range(len(coords)), key=lambda i: haversine_km(coords[i], (c["lat"], c["lon"])))

        return sorted(chosen, key=position_on_route)


def _add_wikidata_fame(pois):
    """Annotate each POI with its Wikipedia sitelink count (a good notability signal) and an English link."""
    ids = sorted({p["wikidata"].split(";")[0].strip() for p in pois})
    entities = {}
    for i in range(0, len(ids), 50):  # API limit per request
        params = urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(ids[i:i + 50]), "props": "sitelinks", "format": "json",
        })
        request = urllib.request.Request(f"{WIKIDATA_API}?{params}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            entities.update(json.load(response).get("entities", {}))

    for p in pois:
        sitelinks = entities.get(p["wikidata"].split(";")[0].strip(), {}).get("sitelinks", {})
        p["sitelinks"] = sum(1 for site in sitelinks if site.endswith("wiki") and site != "commonswiki")
        if "enwiki" in sitelinks:
            # Article titles are specific ("Roman Theatre of Orange") where OSM names can be generic.
            p["name"] = sitelinks["enwiki"]["title"]
            p["wikipedia"] = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(p['name'].replace(' ', '_'))}"
