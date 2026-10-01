"""Render a day-by-day trip itinerary as an interactive HTML map.

Usage:
    python map_trip.py "trip json/iceland.json" [--open] [--basemap topo] [--animate]

The map is written to "trip maps/<trip name>_map.html" unless -o is given.
See "trip json/sample.json" for the file format. Each stop needs a "name" and either
"lat"/"lon" or a "place" string (geocoded via OpenStreetMap and cached).
Optional stop fields: "transport" (how you got there: car, bus, train, flight,
boat), "highlight", "overnight", "notes", and "station" (lat/lon or place of
the railway station used for train legs to/from this stop), and "pois": false
to skip looking for points of interest on the way to this stop.

Pop-ups show a freely licensed photo of each place where one can be found (Wikimedia Commons),
downloaded once into images/<trip>/ and embedded in the map so it stays a single shareable file,
plus links to the place's Wikipedia article and official website. A stop or extra can set "url"
(its website) and "photo" (a Commons file name, or false for none) to override what's found.

Notable places near road legs (castles, viewpoints, waterfalls, ...) are found
automatically via OpenStreetMap's Overpass API and cached in cache/poi_cache.json.
"""

import argparse
import webbrowser
from pathlib import Path

from photos import PlaceInfoFinder
from pois import PoiFinder
from render import BASEMAPS, build_map
from routing import Router
from trip_io import load_trip

MAPS_DIR = Path(__file__).parent / "trip maps"
IMAGES_DIR = Path(__file__).parent / "images"


def main():
    parser = argparse.ArgumentParser(description="Render a day-by-day trip itinerary as an interactive map.")
    parser.add_argument("trip", help='trip JSON file, e.g. "trip json/iceland.json"')
    parser.add_argument("-o", "--output", help='output HTML file (default: "trip maps/<trip name>_map.html")')
    parser.add_argument("--animate", action="store_true", help="animate routes to show the direction of travel")
    parser.add_argument("--no-pois", action="store_true", help="skip points of interest along road legs")
    parser.add_argument("--no-photos", action="store_true", help="leave photos out of the pop-ups")
    parser.add_argument("--poi-radius", type=float, default=5.0, help="how far off the road to look, in km (default 5)")
    parser.add_argument(
        "--basemap", choices=sorted(BASEMAPS), default="streets",
        help="base map shown first (others stay available in the layer switcher)",
    )
    parser.add_argument("--open", action="store_true", help="open the map in your browser")
    args = parser.parse_args()

    trip = load_trip(args.trip)
    router = Router()
    poi_finder = None if args.no_pois else PoiFinder(radius_km=args.poi_radius)
    # Photos for each trip live in images/<trip file name>/, e.g. images/iceland/.
    photos = None if args.no_photos else PlaceInfoFinder(IMAGES_DIR / Path(args.trip).stem)
    try:
        m = build_map(trip, router, animate=args.animate, poi_finder=poi_finder, basemap=args.basemap, photos=photos)
    finally:
        router.save()
        if poi_finder:
            poi_finder.save()
        if photos:
            photos.save()

    output = Path(args.output or MAPS_DIR / f"{Path(args.trip).stem}_map.html").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(output))
    print(f"Saved map to {output}")
    if args.open:
        webbrowser.open(output.as_uri())


if __name__ == "__main__":
    main()
