"""Load a trip file, normalise it into days, and resolve every location to coordinates."""

import json
from pathlib import Path

CACHE_FILE = Path(__file__).parent / "cache" / "geocode_cache.json"


class Geocoder:
    """Nominatim (OpenStreetMap) lookups with a persistent on-disk cache."""

    def __init__(self):
        self.cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
        self._geocode = None
        self._dirty = False

    def lookup(self, place):
        if place not in self.cache:
            if self._geocode is None:
                from geopy.extra.rate_limiter import RateLimiter
                from geopy.geocoders import Nominatim

                self._geocode = RateLimiter(Nominatim(user_agent="map-trip-visualizer").geocode, min_delay_seconds=1)
            result = self._geocode(place)
            if result is None:
                raise SystemExit(f"Could not find a location for {place!r}; add lat/lon manually.")
            self.cache[place] = [result.latitude, result.longitude]
            self._dirty = True
            print(f"Geocoded {place!r} -> {self.cache[place]}")
        return tuple(self.cache[place])

    def save(self):
        if self._dirty:
            CACHE_FILE.parent.mkdir(exist_ok=True)
            CACHE_FILE.write_text(json.dumps(self.cache, indent=2), encoding="utf-8")


def _resolve(spec, geocoder, fallback_name=None):
    """Turn {"lat","lon"}, {"place"}, or a bare place string into a (lat, lon) tuple."""
    if isinstance(spec, str):
        return geocoder.lookup(spec)
    if "lat" in spec and "lon" in spec:
        return float(spec["lat"]), float(spec["lon"])
    place = spec.get("place") or fallback_name
    if not place:
        raise SystemExit(f"Location {spec!r} needs lat/lon or a place name.")
    return geocoder.lookup(place)


def _days_from_flat_stops(trip):
    """Support the original format: a flat "stops" list, grouped into days by date."""
    days = []
    for stop in trip["stops"]:
        date = stop.get("date")
        if not days or days[-1]["date"] != date:
            days.append({"date": date, "stops": []})
        days[-1]["stops"].append(stop)
    return days


EXTRA_TYPES = ("planned", "optional", "backup", "idea")


def _resolve_extras(extras, geocoder, where):
    """Extras are places shown on the map without being routed through."""
    for extra in extras:
        if "name" not in extra:
            raise SystemExit(f"An extra on {where} is missing a \"name\".")
        kind = extra.setdefault("type", "optional")
        if kind not in EXTRA_TYPES:
            raise SystemExit(f"Extra {extra['name']!r} on {where} has type {kind!r}; use one of {', '.join(EXTRA_TYPES)}.")
        extra["_point"] = _resolve(extra, geocoder, fallback_name=extra["name"])


def load_trip(path):
    """Return the trip with trip["days"][i]["stops"][j] carrying "_point" and "_station" tuples."""
    trip = json.loads(Path(path).read_text(encoding="utf-8"))
    if "days" not in trip:
        if not trip.get("stops"):
            raise SystemExit("Trip file needs a \"days\" list (or a flat \"stops\" list).")
        trip["days"] = _days_from_flat_stops(trip)

    geocoder = Geocoder()
    try:
        for n, day in enumerate(trip["days"], start=1):
            if not day.get("stops"):
                raise SystemExit(f"Day {n} has no stops.")
            for stop in day["stops"]:
                if "name" not in stop:
                    raise SystemExit(f"A stop on day {n} is missing a \"name\".")
                stop["_point"] = _resolve(stop, geocoder, fallback_name=stop["name"])
                stop["_station"] = _resolve(stop["station"], geocoder) if stop.get("station") else None
            _resolve_extras(day.get("extras", []), geocoder, f"day {n}")
        _resolve_extras(trip.get("extras", []), geocoder, "the trip")
    finally:
        geocoder.save()
    return trip
