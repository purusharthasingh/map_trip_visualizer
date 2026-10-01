"""Find a freely licensed photo and useful links (Wikipedia, official website) for each place.

Each place is matched to a Wikidata item, tried in this order:
1. A known Wikidata ID (points of interest found on the way already carry one).
2. An English Wikipedia article located near the place whose title matches the place's name.
3. A Wikidata search by name, accepted only if the item's coordinates are close by.
No confident match means no photo or links: a wrong one is worse than none.

From the matched item: its image (P18, from Wikimedia Commons), its English Wikipedia article and its
official website (P856). Photos are downloaded once into images/<trip>/; everything found, including
photo credits, is recorded in images/<trip>/photos.json and reused on later runs.
"""

import difflib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from routing import USER_AGENT, haversine_km

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
THUMB_WIDTH = 500  # px; a standard Wikimedia thumbnail width, sharp in a ~300 px pop-up on high-density screens
NEARBY_KM = 3.0  # how far from the place a matching article or item may be
EXACT_NAME_NEARBY_KM = 15.0  # ...or, for an exact name match, this far
COMMONS_NEARBY_KM = 5.0  # how far from the place a geotagged Commons photo may have been taken
# Minimum name similarity (0-1). High on purpose: 'Laugardalslaug' (a pool) vs 'Laugardalsvöllur'
# (a stadium) scores 0.80 and must not match, while 'Þjóðveldisbærinn Stöng' (0.857) should.
NAME_MATCH = 0.85

# Wikimedia asks automated clients to identify themselves with a way to reach the operator, and heavily
# throttles requests that don't (about one per 30 s). Sent only to Wikipedia, Wikidata and Commons.
WIKIMEDIA_CONTACT = "https://github.com/purusharthasingh"
PHOTO_USER_AGENT = (
    f"{USER_AGENT} (personal trip-map script; {WIKIMEDIA_CONTACT or 'no contact given'}) python-urllib"
)
MIN_INTERVAL_S = 0.5  # polite pacing between requests
_last_request = [0.0]


def _fetch(url):
    """GET with polite pacing; on 429/503 wait as long as the server asks, then retry."""
    for attempt in range(5):
        wait = _last_request[0] + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": PHOTO_USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == 4:
                raise
            retry_after = exc.headers.get("Retry-After", "")
            time.sleep(min(int(retry_after) if retry_after.isdigit() else 10 * (attempt + 1), 90))


def _get(api, **params):
    params.setdefault("format", "json")
    return json.loads(_fetch(f"{api}?{urllib.parse.urlencode(params)}"))


# --- name matching -----------------------------------------------------------------------------------
def _fold(text):
    """Lower-case ASCII form for comparing names: 'Þingvellir' -> 'thingvellir'."""
    text = text.lower().translate(str.maketrans({"þ": "th", "ð": "d", "æ": "ae", "ø": "o", "ß": "ss"}))
    text = unicodedata.normalize("NFKD", text)
    return re.sub(r"[^a-z0-9 ]+", " ", "".join(c for c in text if not unicodedata.combining(c))).strip()


GENERIC_WORDS = {
    "the", "island", "beach", "lighthouse", "waterfall", "falls", "church", "airport", "international",
    "national", "park", "lava", "field", "crater", "river", "hot", "lagoon", "geothermal", "spa", "concert",
    "hall", "canyon", "glacier", "lake", "mountain", "museum", "town", "village", "bay", "valley",
}


LODGING_WORDS = {"hotel", "hostel", "guesthouse", "apartment", "airbnb"}


def name_variants(name):
    """Names worth matching on. Anything after a comma is location ('Fox Hostel, Hrífunes'); '&' joins
    separate places and '(...)' holds an alternative name, so each of those is tried on its own."""
    variants = []
    for part in name.split(",")[0].split("&"):
        variants.append(re.sub(r"\(.*?\)", "", part).strip())
        variants += [inner.strip() for inner in re.findall(r"\((.*?)\)", part)]
    # A name made only of generic words ('Hotel, Le Marais' -> 'Hotel') identifies nothing.
    return [v for v in variants if v and not set(_fold(v).split()) <= GENERIC_WORDS | LODGING_WORDS]


def _similarity(a, b):
    a, b = _fold(a), _fold(re.sub(r"\(.*?\)", "", b))  # Wikipedia's '(...)' is disambiguation
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    # One name containing the other counts as a match only when the extra words are generic, e.g.
    # 'Keflavík Airport' / 'Keflavík International Airport'. 'Árbær Open Air Museum' is not 'Árbær'.
    if (ta <= tb or tb <= ta) and (ta ^ tb) <= GENERIC_WORDS:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def name_similarity(name, title):
    return max((_similarity(v, title) for v in name_variants(name)), default=0.0)


def _strip_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", _fold(text)).strip("-")[:60] or "photo"


def _claim_value(entity, prop):
    for claim in entity.get("claims", {}).get(prop, []):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if value:
            return value
    return None


class PlaceInfoFinder:
    """Photo and links for places, cached in images/<trip>/photos.json."""

    def __init__(self, image_dir):
        self.dir = Path(image_dir)
        self.index_file = self.dir / "photos.json"
        self.index = json.loads(self.index_file.read_text(encoding="utf-8")) if self.index_file.exists() else {}
        self._dirty = False

    def save(self):
        if self._dirty:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.index_file.write_text(json.dumps(self.index, indent=2, ensure_ascii=False), encoding="utf-8")

    def info(self, name, point, wikidata=None):
        """{'matched', 'wikipedia', 'website', 'photo'} for a place, or None when nothing matched.

        'photo' is None or {'path', 'width', 'height', 'author', 'license', 'source', ...}.
        """
        key = f"{name}|{point[0]:.4f},{point[1]:.4f}"
        if key not in self.index:
            try:
                self.index[key] = self._find(name, point, wikidata)
            except Exception as exc:  # network trouble: try again next run rather than caching "nothing"
                print(f"Warning: photo/link lookup failed for {name}: {exc}")
                return None
            self._dirty = True
        record = self.index[key]
        if not record:
            return None
        photo = record.get("photo")
        if photo and (self.dir / photo["file"]).exists():
            photo = dict(photo, path=self.dir / photo["file"])
        else:
            photo = None
        return dict(record, photo=photo)

    # --- lookup -----------------------------------------------------------------------------------
    def commons_photo(self, name, filename):
        """A specific Commons file chosen by hand (a stop's "photo" field), cached like the rest."""
        key = f"commons:{filename}"
        if key not in self.index:
            try:
                self.index[key] = self._download(name, filename.removeprefix("File:"), filename)
            except Exception as exc:
                print(f"Warning: couldn't fetch photo {filename!r} for {name}: {exc}")
                return None
            self._dirty = True
        photo = self.index[key]
        return dict(photo, path=self.dir / photo["file"]) if (self.dir / photo["file"]).exists() else None

    def _find(self, name, point, wikidata):
        record = self._find_item(name, point, wikidata)
        if not (record and record["photo"]):
            # Last resort: a Commons file whose title names the place.
            image = self._commons_search(name, point)
            if image:
                record = record or {"matched": image, "wikidata": None, "wikipedia": None, "website": None}
                record["photo"] = self._download(name, image, image)
        return record

    def _commons_search(self, name, point):
        """A photo taken near the place whose file name contains the place's distinctive words."""
        data = _get(COMMONS_API, action="query", list="geosearch", gscoord=f"{point[0]}|{point[1]}",
                    gsradius=int(COMMONS_NEARBY_KM * 1000), gsnamespace=6, gslimit=500)
        files = sorted(data.get("query", {}).get("geosearch", []), key=lambda f: f["dist"])
        for variant in name_variants(name):
            words = set(_fold(variant).split()) - GENERIC_WORDS  # 'Harpa Concert Hall' -> {'harpa'}
            if not words:
                continue
            for f in files:
                title = f["title"].removeprefix("File:")
                folded = set(_fold(Path(title).stem).split())
                if words <= folded and Path(title).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") \
                        and not folded & {"map", "logo", "locator", "flag", "sign"}:
                    return title
        return None

    def _find_item(self, name, point, wikidata):
        # Cheapest sources first; later ones only run if nothing earlier matched.
        sources = [
            lambda: [(wikidata.split(";")[0].strip(), name)] if wikidata else [],
            lambda: self._nearby_articles(name, point),
            lambda: self._wikidata_search(name, point),
        ]
        for source in sources:
            candidates = source()
            if not candidates:
                continue
            entities = self._entities([qid for qid, _ in candidates])
            # Links come from the best match; the photo from the best match that has one.
            qid, matched = candidates[0]
            best = entities.get(qid, {})
            record = {
                "matched": matched,
                "wikidata": qid,
                "wikipedia": self._wikipedia_url(best),
                "website": _claim_value(best, "P856"),
                "photo": None,
            }
            for cqid, cmatched in candidates:
                image = _claim_value(entities.get(cqid, {}), "P18")
                if image:
                    record["photo"] = self._download(name, image, cmatched)
                    break
            if not record["photo"] and record["wikipedia"]:
                # No Wikidata image: fall back to the article's own lead photo (free licences only).
                image = self._article_image(record["wikipedia"])
                if image:
                    try:
                        record["photo"] = self._download(name, image, matched)
                    except (KeyError, StopIteration):  # file is local to Wikipedia, not on Commons
                        pass
            return record
        return None

    def _nearby_articles(self, name, point):
        # One request: articles near the point, each with its Wikidata ID.
        data = _get(WIKIPEDIA_API, action="query", generator="geosearch", ggscoord=f"{point[0]}|{point[1]}",
                    ggsradius=int(NEARBY_KM * 1000), ggslimit=50, prop="pageprops", ppprop="wikibase_item")
        pages = [
            p for p in data.get("query", {}).get("pages", {}).values()
            if p.get("pageprops", {}).get("wikibase_item") and name_similarity(name, p["title"]) >= NAME_MATCH
        ]
        pages.sort(key=lambda p: -name_similarity(name, p["title"]))
        return [(p["pageprops"]["wikibase_item"], p["title"]) for p in pages]

    def _wikidata_search(self, name, point):
        ids = []
        for variant in name_variants(name)[:3]:
            found = _get(WIKIDATA_API, action="wbsearchentities", search=variant, language="en", limit=7)
            ids += [hit["id"] for hit in found.get("search", []) if hit["id"] not in ids]
        if not ids:
            return []
        entities = _get(WIKIDATA_API, action="wbgetentities", ids="|".join(ids[:50]), props="claims|labels",
                        languages="en|is")["entities"]
        results = []
        for qid in ids:
            entity = entities.get(qid, {})
            labels = entity.get("labels", {})
            label = (labels.get("en") or labels.get("is") or {}).get("value", "")
            coord = _claim_value(entity, "P625")
            similarity = name_similarity(name, label)
            if not coord or similarity < NAME_MATCH:
                continue
            # Large features (lava fields, valleys) are often recorded far from where you stop to see
            # them, so an exact name may be further away than a merely similar one.
            limit = EXACT_NAME_NEARBY_KM if similarity >= 0.99 else NEARBY_KM
            if haversine_km(point, (coord["latitude"], coord["longitude"])) <= limit:
                results.append((qid, label))
        results.sort(key=lambda r: -name_similarity(name, r[1]))
        return results

    def _entities(self, qids):
        data = _get(WIKIDATA_API, action="wbgetentities", ids="|".join(qids[:50]), props="claims|sitelinks",
                    sitefilter="enwiki")
        return data.get("entities", {})

    def _article_image(self, wikipedia_url):
        title = urllib.parse.unquote(wikipedia_url.rsplit("/", 1)[-1]).replace("_", " ")
        data = _get(WIKIPEDIA_API, action="query", titles=title, prop="pageimages", piprop="name", pilicense="free")
        page = next(iter(data.get("query", {}).get("pages", {}).values()), {})
        return page.get("pageimage")

    @staticmethod
    def _wikipedia_url(entity):
        title = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
        return f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}" if title else None

    def _download(self, name, filename, matched):
        data = _get(COMMONS_API, action="query", titles=f"File:{filename}", prop="imageinfo",
                    iiprop="url|extmetadata", iiurlwidth=THUMB_WIDTH)
        info = next(iter(data["query"]["pages"].values()))["imageinfo"][0]
        meta = info.get("extmetadata", {})
        thumb = info.get("thumburl") or info["url"]
        ext = Path(urllib.parse.urlparse(thumb).path).suffix.lower() or ".jpg"
        out = f"{_slug(name)}{ext}"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / out).write_bytes(_fetch(thumb))
        return {
            "file": out,
            "matched": matched,
            "commons_file": filename,
            "width": info.get("thumbwidth") or info.get("width"),
            "height": info.get("thumbheight") or info.get("height"),
            "author": _strip_html(meta.get("Artist", {}).get("value")) or "Unknown author",
            "license": _strip_html(meta.get("LicenseShortName", {}).get("value")) or "see source",
            "source": info.get("descriptionurl"),
        }
