# Map Trip Visualizer

Turns a day-by-day trip itinerary (a JSON file) into a single interactive HTML map you can open in any
browser or share as a link.

**Live maps** (once GitHub Pages is enabled for this repository):
- [Iceland — Golden Circle, South Coast & Snæfellsnes](https://purusharthasingh.github.io/map_trip_visualizer/trip%20maps/iceland_map.html)
- [Sample — London to Rome](https://purusharthasingh.github.io/map_trip_visualizer/trip%20maps/sample_map.html)

## What the map shows

- **Real routes**: car and bus legs follow the actual roads, trains follow the rail tracks, flights are
  drawn as curved arcs.
- **One colour per day**, with an itinerary panel: switch days on and off, click a day to zoom to it and
  bring it to the front, see distance and driving time.
- **Highlights, overnight stays and stops**, plus extra places marked as planned, optional or
  bad-weather backups, and unscheduled ideas.
- **Points of interest along the drive**, found automatically (castles, waterfalls, viewpoints...).
- **Pop-ups with photos** (freely licensed, from Wikimedia Commons) and links to each place's Wikipedia
  article and official website.
- **Readable at every zoom**: icons nudge aside instead of overlapping, routes driven both ways show as two
  side-by-side lines when zoomed in, and name banners hide when they'd collide.
- **Works on phones**: the itinerary becomes a pull-up sheet at the bottom of the screen.
- **One self-contained file**: photos are embedded, so the HTML works on its own (it needs internet only
  for the map tiles and the map library).

## Setup

Requires Python 3.10 or newer (built with 3.14).

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt     # Windows
.venv/bin/python -m pip install -r requirements.txt             # macOS / Linux
```

## Usage

```
.venv\Scripts\python.exe map_trip.py "trip json\iceland.json" --open
```

The map is written to `trip maps/<trip file name>_map.html`. Options:

| Option | Effect |
|---|---|
| `-o path.html` | Save the map somewhere else |
| `--open` | Open it in your browser when done |
| `--basemap topo` / `opentopomap` | Start on the topographic or terrain map (default: streets) |
| `--no-pois` | Skip the automatic points of interest along the drive |
| `--poi-radius 8` | How far off the road to look for points of interest, in km (default 5) |
| `--no-photos` | Leave photos out of the pop-ups |
| `--animate` | Animated "flowing" route lines |

The first build of a new trip looks things up online and can take a few minutes. Everything is cached
(`cache/` and `images/<trip>/`), so rebuilds take seconds.

## Trip file format

See [`trip json/sample.json`](trip%20json/sample.json) and [`trip json/iceland.json`](trip%20json/iceland.json).

```json
{
  "name": "Trip title",
  "days": [
    {
      "date": "2026-10-05",
      "title": "Golden Circle",
      "color": "#4a3aa7",
      "stops": [
        {"name": "Þingvellir", "lat": 64.2559, "lon": -21.1293, "transport": "car", "highlight": true,
         "notes": "Rift valley."},
        {"name": "Reykjavik", "place": "Reykjavik, Iceland", "transport": "car", "overnight": true}
      ],
      "extras": [
        {"name": "Brúarfoss", "lat": 64.2642, "lon": -20.5159, "type": "optional"}
      ]
    }
  ],
  "extras": [{"name": "Esja", "lat": 64.245, "lon": -21.6569, "type": "idea"}]
}
```

**Stops** are the places you travel to, in order; each day continues from the previous day's last stop.

| Field | Meaning |
|---|---|
| `name` | Required |
| `lat` + `lon`, or `place` | Location. A `place` is looked up by name; coordinates are more reliable |
| `transport` | How you got here: `car`, `bus`, `train`, `flight`, `boat` (other values draw a straight line) |
| `highlight` / `overnight` | Shown as a ★ with a name banner / as a bed |
| `notes` | Shown in the pop-up |
| `station` | For train legs: the station's `lat`/`lon` or `place`, so the route starts on the tracks |
| `url` | A website link (overrides the one found automatically) |
| `photo` | A Wikimedia Commons file name to use, or `false` for no photo |
| `pois` | `false` to skip points of interest on the way to this stop |

**Extras** are shown on the map but not routed through. They take `name`, a location, `type`
(`planned`, `optional`, `backup`, or `idea` for the trip-level list), `notes`, `url` and `photo`.
A day's `color` is optional; days get a colour-blind-friendly palette by default.

## Data sources

All free and keyless:

- Map tiles: [Esri](https://www.esri.com/) (streets, topographic) and [OpenTopoMap](https://opentopomap.org)
- Place lookup: [Nominatim](https://nominatim.openstreetmap.org) (OpenStreetMap)
- Road routing: [OSRM](https://project-osrm.org); rail routing: [BRouter](https://brouter.de)
- Points of interest: [Overpass API](https://overpass-api.de) (OpenStreetMap), ranked using
  [Wikidata](https://www.wikidata.org)
- Photos and links: [Wikidata](https://www.wikidata.org), [Wikipedia](https://en.wikipedia.org) and
  [Wikimedia Commons](https://commons.wikimedia.org). Each photo is credited in its pop-up.

These are shared public services, so requests are paced and results cached. Wikimedia asks scripts to
identify a contact: if you run this yourself, set `WIKIMEDIA_CONTACT` in [`photos.py`](photos.py) to
your own GitHub profile or email.

## Project layout

```
map_trip.py      command-line entry point
trip_io.py       loads the trip file and looks up place names
routing.py       road, rail and flight routes
pois.py          points of interest along the drive
photos.py        photos and links for each place
render.py        builds the map and its itinerary panel
trip json/       trip files
trip maps/       generated maps
images/<trip>/   downloaded photos and their credits
cache/           cached lookups and routes
```

`trip itinerary/` is for private notes (bookings, payment details) and is ignored by git. Keep personal
details out of the trip JSON too: its notes appear in the published map.
