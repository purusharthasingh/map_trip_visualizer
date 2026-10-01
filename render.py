"""Draw a routed, day-by-day itinerary on a web base map."""

import base64
import io
import json
import math
from html import escape

import folium
from PIL import Image
from folium.plugins import AntPath, PolyLineOffset, PolyLineTextPath

# Categorical palette in fixed order (validated for adjacent-pair colour-blind separation).
# Consecutive days sit next to each other on the map, so adjacency is what matters.
DAY_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

# Base maps offered in the layer switcher; the first one listed (or the one chosen) is shown by default.
# openstreetmap.org's own tile servers are deliberately not used: their usage policy blocks pages opened
# from local files (no Referer header), which shows up as "403 Access blocked" tiles.
BASEMAPS = {
    "streets": {
        "name": "Streets (Esri)",
        "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        "attr": "Tiles &copy; Esri &mdash; Source: Esri, HERE, Garmin, USGS, Intermap, INCREMENT P, NRCan, "
        "Esri Japan, METI, Esri China (Hong Kong), Esri Korea, Esri (Thailand), NGCC, "
        "&copy; OpenStreetMap contributors, and the GIS User Community",
        "max_zoom": 19,
    },
    "topo": {
        "name": "Topographic (Esri)",
        "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        "attr": "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, Intermap, USGS, FAO, NPS, NRCAN, GeoBase, "
        "Kadaster NL, Ordnance Survey, METI, and the GIS User Community",
        "max_zoom": 19,
    },
    "opentopomap": {
        "name": "Terrain (OpenTopoMap)",
        "tiles": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attr": 'Map data &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, '
        'SRTM | Style &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
        "subdomains": "abc",
        "max_zoom": 17,
    },
}

LINE_DASH = {"flight": "10 8", "boat": "2 8"}
APPROX_DASH = "6 8"
LABEL_MIN_ZOOM = 7
# Routes are drawn this many pixels to the right of their direction of travel (like traffic keeping
# right), so a road driven out and back shows as two side-by-side lines, one per direction.
ROUTE_OFFSET_PX = 3
# Zoomed out beyond the point where the scale bar reads this many km, the fixed-pixel route offset grows
# past the gap between road and coast, so routes are drawn centred on the road from there out.
MAX_SCALE_BAR_KM = 20
# Direction arrows turn into clutter when zoomed out, where the keep-right offset already shows direction.
ARROW_MIN_ZOOM = 8
POI_MODES = {"car", "bus"}
POI_MIN_LEG_KM = 20


def _day_label(day, n):
    date = f" · {escape(day['date'])}" if day.get("date") else ""
    return f"Day {n}{date}"


def _day_title(day):
    stops = day["stops"]
    return day.get("title") or (stops[0]["name"] if len(stops) == 1 else f"{stops[0]['name']} → {stops[-1]['name']}")


def _swatch(color):
    return (
        f'<span style="display:inline-block;width:15px;height:15px;border-radius:4px;'
        f'background:{color};margin-right:7px;vertical-align:-2px"></span>'
    )


# Map glyphs drawn as shapes on a 24 x 24 grid, each centred in the square. Text characters and emoji
# sit on a font's text baseline and render differently per platform, so they never centre reliably.
# {c} is the fill colour; {bg} the colour of cut-outs (the icon's white background).
GLYPHS = {
    "bed": '<path fill="{c}" d="M7 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm12-6h-8v7H3V5H1v15h2v-3h18v3h2v-9a4 4 0 0 0-4-4z"/>',
    "umbrella": (
        '<path fill="{c}" d="M2.5 12.5a9.5 9.5 0 0 1 19 0z"/>'
        '<path d="M12 12.5v6a2.25 2.25 0 0 1-4.5 0" fill="none" stroke="{c}" stroke-width="2.2" stroke-linecap="round"/>'
    ),
    "Castle": '<path fill="{c}" d="M3 21V8h3v3h2.5V8h2v3h3V8h2v3H18V8h3v13h-6.5v-4.5a2.5 2.5 0 0 0-5 0V21z"/>',
    "Waterfall": '<path fill="{c}" d="M12 2.5c-3 4.5-6 8-6 11.5a6 6 0 0 0 12 0c0-3.5-3-7-6-11.5z"/>',
    "Viewpoint": (
        '<path fill="{c}" d="M1.5 12S5.5 5.5 12 5.5 22.5 12 22.5 12 18.5 18.5 12 18.5 1.5 12 1.5 12z"/>'
        '<circle cx="12" cy="12" r="3.6" fill="{bg}"/><circle cx="12" cy="12" r="1.8" fill="{c}"/>'
    ),
    "Archaeological site": (
        '<path fill="{c}" d="M9 2.5h6v2l-1 1.2v1.4c2.6 1.1 4.3 3.6 4.3 6.6 0 3.6-2.6 6.6-6.3 7.8-3.7-1.2-6.3-4.2-6.3-7.8'
        ' 0-3 1.7-5.5 4.3-6.6V5.7l-1-1.2z"/>'
    ),
    "Ruins": '<path fill="{c}" d="M3 19h18v2.5H3zM5 18V8h3v10zm5.5 0V5h3v13zM16 18v-6.5h3V18z"/>',
    "Peak": '<path fill="{c}" d="M1.5 20 9 6l4.2 7.6 2.3-3.6L22.5 20z"/>',
    "Museum": '<path fill="{c}" d="M12 2 2 7v2h20V7zM4 10.5v7h3v-7zm6.5 0v7h3v-7zm6.5 0v7h3v-7zM2 19v3h20v-3z"/>',
    "Attraction": '<path fill="{c}" d="M12 2l2.6 7.4L22 12l-7.4 2.6L12 22l-2.6-7.4L2 12l7.4-2.6z"/>',
    "Monument": '<path fill="{c}" d="M10 19V7.5L12 2l2 5.5V19zM6.5 19.5h11V22h-11z"/>',
}


# Vertical nudges (grid units) for glyphs whose shape doesn't fill the grid evenly top to bottom.
GLYPH_SHIFT = {"Castle": -2.5, "Peak": -1, "Ruins": -1.25, "Waterfall": 0.75}


def _glyph_svg(name, size, color, bg="#fff"):
    body = GLYPHS[name].format(c=color, bg=bg)
    if name in GLYPH_SHIFT:
        body = f'<g transform="translate(0 {GLYPH_SHIFT[name]})">{body}</g>'
    return f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" style="display:block">{body}</svg>'


# Shared style for a glyph's container: centres whatever is inside it.
_CENTRED = "display:flex;align-items:center;justify-content:center;"


def _star_points(size):
    """A five-pointed star whose bounding box is centred in a size x size square.

    Drawn as a shape rather than the ★ character: fonts place that glyph on a text baseline,
    so it never sits quite in the middle of a circle.
    """
    outer, inner = size * 0.46, size * 0.46 * 0.42
    height = outer * (1 + math.cos(math.radians(36)))  # top point to the two lower points
    cx, cy = size / 2, (size - height) / 2 + outer  # star centre that centres the bounding box
    points = []
    for i in range(10):
        radius = outer if i % 2 == 0 else inner
        angle = math.radians(-90 + i * 36)
        points.append(f"{cx + radius * math.cos(angle):.2f},{cy + radius * math.sin(angle):.2f}")
    return " ".join(points)


def _star_svg(size, fill="#fff"):
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" style="display:block">'
        f'<polygon points="{_star_points(size)}" fill="{fill}" stroke="{fill}" stroke-width="{size / 24:.2f}" '
        'stroke-linejoin="round"/></svg>'
    )


def _highlight_icon(color, cls):
    return folium.DivIcon(
        class_name=f"empty {cls} kind-highlight",
        icon_size=(46, 46),
        icon_anchor=(23, 23),
        html=(
            f'<div style="width:40px;height:40px;border-radius:50%;background:{color};border:3px solid #fff;'
            "display:flex;align-items:center;justify-content:center;"
            f'box-shadow:0 1px 6px rgba(0,0,0,.45)">{_star_svg(24)}</div>'
        ),
    )


def _overnight_icon(color, cls):
    return folium.DivIcon(
        class_name=f"empty {cls} kind-overnight",
        icon_size=(38, 38),
        icon_anchor=(19, 19),
        html=(
            f'<div style="width:30px;height:30px;border-radius:8px;background:#fff;border:4px solid {color};{_CENTRED}'
            f'box-shadow:0 1px 5px rgba(0,0,0,.4)">{_glyph_svg("bed", 19, color)}</div>'
        ),
    )


def _stop_icon(color, cls):
    return folium.DivIcon(
        class_name=f"empty {cls} kind-stop",
        icon_size=(22, 22),
        icon_anchor=(11, 11),
        html=(
            f'<div style="width:14px;height:14px;border-radius:50%;background:#fff;border:4px solid {color};'
            'box-shadow:0 1px 3px rgba(0,0,0,.35)"></div>'
        ),
    )


def _place_key(point):
    """Round to ~10 m so repeat visits to the same place match."""
    return round(point[0], 4), round(point[1], 4)


def _stop_kind(stop):
    return "highlight" if stop.get("highlight") else "overnight" if stop.get("overnight") else "stop"


def _visit_count(visits):
    if all(stop.get("overnight") for _, _, stop in visits):
        return f"{len(visits)} nights"
    return f"{len(visits)} visits"


POPUP_PHOTO_WIDTH = 300  # px
POPUP_PHOTO_MAX_HEIGHT = 220  # px; tall photos are cropped to this


def _place_media(places, item, name, point, wikidata=None, wikipedia=None):
    """(photo HTML, links HTML) for a pop-up.

    `item` is the place's entry in the trip file, whose "photo" (a Commons file name, or false) and
    "url" fields take priority over anything looked up.
    """
    item = item or {}
    info = places.info(name, point, wikidata) if places else None
    if item.get("photo") is False:
        photo = None
    elif item.get("photo") and places:
        photo = places.commons_photo(name, item["photo"])
    else:
        photo = info and info["photo"]

    links = []
    wikipedia = (info and info.get("wikipedia")) or wikipedia
    website = item.get("url") or (info and info.get("website"))
    if website:
        links.append((website, "Official website"))
    if wikipedia:
        links.append((wikipedia, "Wikipedia"))
    links_html = (
        '<div style="margin-top:6px">'
        + " · ".join(f'<a href="{escape(url)}" target="_blank" rel="noopener">{label}</a>' for url, label in links)
        + "</div>"
    ) if links else ""
    return _photo_html(photo), links_html


PHOTO_EMBED_WIDTH = 450  # px: 1.5x the pop-up width, sharp enough on phones without doubling the file
PHOTO_WEBP_QUALITY = 60


def _compressed_photo(path):
    """The photo re-encoded small for embedding (the downloaded original is left as it is).

    Embedded photos are most of the map's file size; WebP at this size is roughly a third of the
    downloaded JPEGs, which matters when the page is opened on mobile data.
    """
    with Image.open(path) as image:
        image = image.convert("RGB")
        if image.width > PHOTO_EMBED_WIDTH:
            image = image.resize(
                (PHOTO_EMBED_WIDTH, round(image.height * PHOTO_EMBED_WIDTH / image.width)), Image.LANCZOS
            )
        out = io.BytesIO()
        image.save(out, "WEBP", quality=PHOTO_WEBP_QUALITY, method=6)
        return out.getvalue()


def _photo_html(record):
    """Photo plus credit line, embedded in the page so the map stays a single file."""
    if not record:
        return ""
    mime, data = "image/webp", base64.b64encode(_compressed_photo(record["path"])).decode()
    height = POPUP_PHOTO_MAX_HEIGHT
    if record.get("width") and record.get("height"):
        height = min(round(POPUP_PHOTO_WIDTH * record["height"] / record["width"]), POPUP_PHOTO_MAX_HEIGHT)
    return (
        f'<img src="data:{mime};base64,{data}" alt="{escape(record["matched"])}" width="{POPUP_PHOTO_WIDTH}" '
        f'height="{height}" style="display:block;object-fit:cover;border-radius:6px;margin:0 0 4px">'
        f'<div style="font-size:11px;line-height:1.3;color:#8a8a85;margin:0 0 6px">Photo: {escape(record["author"])}'
        f' · {escape(record["license"])} · <a href="{escape(record["source"] or "")}" target="_blank" '
        'rel="noopener">Wikimedia Commons</a></div>'
    )


def _visits_popup(visits):
    """Pop-up for a place on several days: one entry per visit with that day's notes."""
    all_nights = all(stop.get("overnight") for _, _, stop in visits)
    parts = [f"<b>{escape(visits[0][2]['name'])}</b> <small>· {_visit_count(visits)}</small>"]
    for n, day, stop in visits:
        entry = f"<small>{_day_label(day, n)}{' · overnight' if stop.get('overnight') and not all_nights else ''}</small>"
        if stop.get("notes"):
            entry += f"<br>{escape(stop['notes'])}"
        parts.append(entry)
    return '<hr style="margin:6px 0;border:0;border-top:1px solid #e4e3dc">'.join(parts)


def _draw_leg(group, leg, mode, color, tooltip, animate, cls):
    coords = leg["coords"]
    if mode in LINE_DASH:
        dash = LINE_DASH[mode]
    elif leg["source"] == "approx":
        dash = APPROX_DASH
    else:
        dash = None

    if animate:  # the animated line has no offset support, so it stays centred on the road
        folium.PolyLine(coords, color="#ffffff", weight=10, opacity=0.9, className=cls).add_to(group)
        AntPath(coords, color=color, pulse_color="#ffffff", weight=5, delay=1500, dash_array=[12, 24], tooltip=tooltip, className=cls).add_to(group)
        return
    # White casing keeps the coloured line readable over busy map tiles. It is narrow enough that
    # the opposite direction's line (offset to the other side) is never covered.
    PolyLineOffset(coords, offset=ROUTE_OFFSET_PX, color="#ffffff", weight=9, opacity=0.9, className=cls).add_to(group)
    line = PolyLineOffset(
        coords, offset=ROUTE_OFFSET_PX, color=color, weight=5, opacity=0.95, dash_array=dash,
        tooltip=tooltip, className=cls,
    ).add_to(group)
    PolyLineTextPath(
        line, "        ►        ", repeat=True, offset=7, attributes={"fill": color, "font-size": "21", "class": f"{cls} route-arrow"}
    ).add_to(group)


def _connector(group, a, b, color, cls):
    """Thin dotted link between a stop and the station its rail leg actually uses."""
    folium.PolyLine([a, b], color=color, weight=3, opacity=0.8, dash_array="2 7", className=cls).add_to(group)


def _format_leg(km, hours, mode):
    text = f"{km:,.0f} km"
    if mode:
        text += f" by {escape(mode)}"
    if hours:
        text += f" · {hours:.1f} h"
    return text


def _bounds(points):
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


def _poi_icon(poi, color, cls):
    return folium.DivIcon(
        class_name=f"empty {cls} kind-poi",
        icon_size=(36, 36),
        icon_anchor=(18, 18),
        html=(
            f'<div style="width:28px;height:28px;border-radius:50%;background:#fff;border:3px solid {color};{_CENTRED}'
            f'box-shadow:0 1px 3px rgba(0,0,0,.35)">{_glyph_svg(poi["category"], 18, color)}</div>'
        ),
    )


def _draw_pois(poi_group, pois, color, day_label, cls, photos=None):
    for poi in pois:
        photo, links = _place_media(
            photos, None, poi["name"], (poi["lat"], poi["lon"]), poi.get("wikidata"), poi.get("wikipedia")
        )
        popup = photo + (
            f"<small>{day_label} · on the way</small><br><b>{escape(poi['name'])}</b><br>"
            f"{poi['category']} · {poi['off_route_km']:.1f} km off the route"
        ) + links
        folium.Marker(
            (poi["lat"], poi["lon"]),
            icon=_poi_icon(poi, color, cls),
            popup=folium.Popup(popup, max_width=340),
            tooltip=f"{poi['icon']} {escape(poi['name'])}",
            z_index_offset=_z("poi"),
        ).add_to(poi_group)


# Stacking order when markers overlap, highest on top. The page script also uses it to decide which
# marker keeps its true position and which gets nudged aside (see declutterMarkers).
MARKER_PRIORITY = {
    "highlight": 7, "overnight": 6, "stop": 5, "backup": 4, "planned": 3, "poi": 2, "optional": 1, "idea": 0,
}


def _z(kind):
    # Leaflet stacks markers by screen position plus zIndexOffset; thousands keep the kind order dominant.
    return MARKER_PRIORITY[kind] * 1000


# Sidebar switches, in display order: (kind, label, mini icon). They double as the legend.
_INK = "#5f5f5a"
MARKER_TOGGLES = [
    ("highlight", "Highlights",
     f'<i style="background:{_INK};border-radius:50%;display:inline-flex;align-items:center;'
     f'justify-content:center">{_star_svg(12)}</i>'),
    ("overnight", "Overnights",
     f'<i style="border:2px solid {_INK};border-radius:5px;{_CENTRED}">{_glyph_svg("bed", 12, _INK)}</i>'),
    ("stop", "Stops", f'<i style="border:3px solid {_INK};border-radius:50%"></i>'),
    ("poi", "On the way",
     f'<i style="border:2px solid {_INK};border-radius:50%;{_CENTRED}">{_glyph_svg("Castle", 11, _INK)}</i>'),
    ("planned", "Also planned", f'<i style="background:{_INK};border-radius:50%;transform:scale(.8)"></i>'),
    ("optional", "Optional", f'<i style="border:2px dashed {_INK};border-radius:50%;transform:scale(.8)"></i>'),
    ("backup", "Backups",
     f'<i style="border:2px solid {_INK};border-radius:50%;{_CENTRED}">{_glyph_svg("umbrella", 11, _INK)}</i>'),
    ("idea", "Ideas", '<i style="border:2px dashed #8a8a85;border-radius:50%;transform:scale(.8)"></i>'),
    ("banners", "Name banners",
     f'<i style="border:1px solid #c9c8c0;border-radius:3px;font:700 10px/1 sans-serif;width:24px;{_CENTRED}">Aa</i>'),
]

EXTRA_LABELS = {
    "planned": "Also planned",
    "optional": "Optional",
    "backup": "Bad-weather backup",
    "idea": "Idea, not scheduled",
}
EXTRA_GLYPHS = {"planned": "●", "optional": "◌", "backup": "☂", "idea": "◌"}
IDEA_COLOR = "#8a8a85"


def _extra_icon(kind, color, cls):
    if kind == "planned":
        style, size, glyph = f"background:{color};border:3px solid #fff", 20, ""
    elif kind == "backup":
        style, size, glyph = f"background:#fff;border:3px solid {color}", 28, _glyph_svg("umbrella", 17, color)
    else:  # optional / idea: hollow dashed ring
        style, size, glyph = f"background:#fff;border:3px dashed {color}", 20, ""
    return folium.DivIcon(
        class_name=f"empty {cls} kind-{kind}",
        icon_size=(size + 6, size + 6),
        icon_anchor=((size + 6) // 2, (size + 6) // 2),
        html=(
            f'<div style="width:{size}px;height:{size}px;border-radius:50%;{style};{_CENTRED}'
            f'box-shadow:0 1px 3px rgba(0,0,0,.35)">{glyph}</div>'
        ),
    )


def _draw_extras(group, extras, color, context_label, cls, photos=None):
    for extra in extras:
        kind = extra["type"]
        photo, links = _place_media(photos, extra, extra["name"], extra["_point"])
        popup = photo + f"<small>{context_label} · {EXTRA_LABELS[kind]}</small><br><b>{escape(extra['name'])}</b>"
        if extra.get("notes"):
            popup += f"<br>{escape(extra['notes'])}"
        popup += links
        folium.Marker(
            extra["_point"],
            icon=_extra_icon(kind, color, cls),
            popup=folium.Popup(popup, max_width=340),
            tooltip=f"{escape(extra['name'])} <small>({EXTRA_LABELS[kind].lower()})</small>",
            z_index_offset=_z(kind),
        ).add_to(group)


def _extras_list(extras, summary):
    if not extras:
        return ""
    items = "".join(
        f'<li data-point=\'{json.dumps(list(e["_point"]))}\' title="{EXTRA_LABELS[e["type"]]}">'
        f'{EXTRA_GLYPHS[e["type"]]} {escape(e["name"])}</li>'
        for e in extras
    )
    return f'<details class="pois"><summary>{summary}</summary><ul>{items}</ul></details>'


def _highlights_line(day):
    """The day's highlights, listed in its panel row; each name zooms to the place."""
    items = [
        f'<span class="hl-item" data-point=\'{json.dumps(list(s["_point"]))}\'>{escape(s["name"])}</span>'
        for s in day["stops"]
        if s.get("highlight")
    ]
    return f'<span class="hl">★ {" · ".join(items)}</span>' if items else "<br>"


def _wants_pois(stop, mode, leg):
    """Only road legs long enough to pass through new country; short city hops are skipped."""
    return stop.get("pois", True) and mode in POI_MODES and leg["km"] >= POI_MIN_LEG_KM


def _poi_list(pois):
    if not pois:
        return ""
    items = "".join(
        f'<li data-point=\'{json.dumps([p["lat"], p["lon"]])}\'>{p["icon"]} {escape(p["name"])}</li>' for p in pois
    )
    return f'<details class="pois"><summary>{len(pois)} point{"s" if len(pois) != 1 else ""} of interest on the way</summary><ul>{items}</ul></details>'


def _scale_bar_km(metres):
    """What Leaflet's scale bar shows for a 100 px span: rounded down to 1, 2, 3 or 5 x 10^n."""
    pow10 = 10 ** math.floor(math.log10(metres))
    first = metres / pow10
    nice = 5 if first >= 5 else 3 if first >= 3 else 2 if first >= 2 else 1
    return nice * pow10 / 1000


def _min_zoom_for_scale(lat):
    """Lowest zoom level at which the scale bar reads MAX_SCALE_BAR_KM or less at this latitude."""
    for zoom in range(23):
        metres_per_100px = 100 * 156543.03392 * math.cos(math.radians(lat)) / 2**zoom
        if _scale_bar_km(metres_per_100px) <= MAX_SCALE_BAR_KM:
            return zoom
    return 22


def _add_basemaps(m, default):
    for key, spec in BASEMAPS.items():
        spec = dict(spec)
        folium.TileLayer(
            tiles=spec.pop("tiles"), attr=spec.pop("attr"), name=spec.pop("name"),
            overlay=False, control=True, show=(key == default), crossOrigin="anonymous", **spec,
        ).add_to(m)


def build_map(trip, router, animate=False, poi_finder=None, basemap="streets", photos=None):
    m = folium.Map(tiles=None, control_scale=True)
    _add_basemaps(m, basemap)
    days = trip["days"]
    prev = None
    all_points, rows, total_km = [], [], 0.0
    extras = trip.get("extras", [])
    # Places already on the itinerary (stops and extras) shouldn't be re-suggested as points of interest.
    known_points = [s["_point"] for day in days for s in day["stops"] + day.get("extras", [])]
    known_points += [e["_point"] for e in extras]
    poi_group = folium.FeatureGroup(name="Points of interest on the way", control=False)
    kinds = set()  # marker kinds present, so the sidebar only offers relevant switches
    # Places visited more than once (a home base, the airport at both ends) share one marker.
    visits_by_place = {}
    for n, day in enumerate(days, start=1):
        for stop in day["stops"]:
            visits_by_place.setdefault(_place_key(stop["_point"]), []).append((n, day, stop))

    for n, day in enumerate(days, start=1):
        color = day.get("color") or DAY_COLORS[(n - 1) % len(DAY_COLORS)]
        title = _day_title(day)
        cls = f"trip-el day-{n}"  # lets the panel fade other days when one is selected
        group = folium.FeatureGroup(name=f"{_day_label(day, n)} — {escape(title)}", control=False)
        day_points = [prev["_point"]] if prev else []
        day_km, day_hours = 0.0, 0.0
        day_pois = []

        for stop in day["stops"]:
            leg_text = ""
            if prev:
                mode = (stop.get("transport") or "").lower()
                use_stations = mode == "train"
                start = (use_stations and prev["_station"]) or prev["_point"]
                end = (use_stations and stop["_station"]) or stop["_point"]
                label = f"{prev['name']} → {stop['name']}"
                leg = router.route(start, end, mode, label=label)

                if start != prev["_point"]:
                    _connector(group, prev["_point"], start, color, cls)
                if end != stop["_point"]:
                    _connector(group, end, stop["_point"], color, cls)
                leg_text = _format_leg(leg["km"], leg["hours"], mode)
                _draw_leg(group, leg, mode, color, f"{escape(label)}: {leg_text}", animate, cls)

                day_km += leg["km"]
                day_hours += leg["hours"] or 0
                day_points.extend(leg["coords"])

                if poi_finder and _wants_pois(stop, mode, leg):
                    try:
                        max_count = max(2, min(10, round(leg["km"] / 30)))
                        day_pois += poi_finder.along(leg["coords"], avoid_points=known_points, max_count=max_count)
                    except Exception as exc:
                        print(f"Warning: couldn't look up points of interest for {label}: {exc}")

            popup_html = f"<small>{_day_label(day, n)}</small><br><b>{escape(stop['name'])}</b>"
            if stop.get("notes"):
                popup_html += f"<br>{escape(stop['notes'])}"
            if leg_text:
                popup_html += f"<br><small>Arrived: {leg_text}</small>"

            visits = visits_by_place[_place_key(stop["_point"])]
            if visits[0][2] is stop:  # later visits to the same place reuse this marker
                # The most important visit decides the look: highlight, then overnight, then plain stop.
                kind = max((_stop_kind(v) for _, _, v in visits), key=MARKER_PRIORITY.get)
                kinds.add(kind)
                shared = len(visits) > 1
                # A shared marker belongs to every visiting day, so selecting any of them keeps it visible.
                marker_cls = "trip-el " + " ".join(f"day-{k}" for k, _, _ in visits) if shared else cls
                tooltip = escape(stop["name"])
                body = _visits_popup(visits) if shared else popup_html
                # Any visit's "photo" / "url" fields apply to the shared marker.
                item = {k: val for _, _, visit in reversed(visits) for k, val in visit.items() if k in ("photo", "url")}
                photo, links = _place_media(photos, item, stop["name"], stop["_point"])
                popup = folium.Popup(photo + body + links, max_width=340)
                if shared:
                    tooltip += f" ({_visit_count(visits)})"
                if kind == "highlight":
                    tooltip = folium.Tooltip(
                        escape(stop["name"]), permanent=True, direction="right", offset=(22, 0),
                        class_name=f"key-label {marker_cls} kind-highlight",
                    )
                icon = {"highlight": _highlight_icon, "overnight": _overnight_icon, "stop": _stop_icon}[kind]
                folium.Marker(
                    stop["_point"], icon=icon(color, marker_cls), popup=popup, tooltip=tooltip,
                    z_index_offset=_z(kind),
                ).add_to(group)

            day_points.append(stop["_point"])
            prev = stop

        day_extras = day.get("extras", [])
        kinds.update(e["type"] for e in day_extras)
        if day_pois:
            kinds.add("poi")
        _draw_extras(group, day_extras, color, _day_label(day, n), cls, photos)
        day_points.extend(e["_point"] for e in day_extras)
        group.add_to(m)
        _draw_pois(poi_group, day_pois, color, _day_label(day, n), cls, photos)
        all_points.extend(day_points)
        total_km += day_km
        stats = f"{day_km:,.0f} km" + (f" · {day_hours:.1f} h on the road" if day_hours else "")
        rows.append(
            f'<div class="day-row" data-day="{n}" data-group="{group.get_name()}" data-bounds=\'{json.dumps(_bounds(day_points))}\'>'
            f'<label class="day-toggle" title="Show or hide this day"><input type="checkbox" data-day-toggle="{n}" checked></label>'
            f'<div class="day-body">{_swatch(color)}<b>{_day_label(day, n)}</b><br>'
            f'<span class="t">{escape(title)}</span>{_highlights_line(day)}<span class="s">{stats}</span></div></div>'
            + _extras_list(day_extras, f"{len(day_extras)} more planned, optional &amp; backup stops")
            + _poi_list(day_pois)
        )

    kinds.update(e["type"] for e in extras)
    if "highlight" in kinds:
        kinds.add("banners")
    if extras:
        ideas = folium.FeatureGroup(name="Ideas, not scheduled", control=False)
        _draw_extras(ideas, extras, IDEA_COLOR, "Not scheduled", "trip-el day-0", photos)
        ideas.add_to(m)
        rows.append(_extras_list(extras, f"{len(extras)} ideas, not scheduled"))
    if poi_finder:
        poi_group.add_to(m)
    # Below the zoom where the scale bar reads MAX_SCALE_BAR_KM, the side-by-side route offset would push
    # coastal routes into the sea, so routes are drawn centred instead (see updateOffsets in the page).
    # The scale bar reads largest nearest the equator, so the trip's lowest latitude sets that zoom.
    offset_min_zoom = _min_zoom_for_scale(min(abs(p[0]) for p in all_points))
    m.fit_bounds(_bounds(all_points), padding=(30, 30))
    folium.LayerControl(collapsed=True).add_to(m)
    _add_itinerary_panel(m, trip, days, rows, total_km, _bounds(all_points), kinds, offset_min_zoom)
    return m


def _toggles_html(kinds):
    items = "".join(
        f'<label><input type="checkbox" data-kind="{kind}" checked>{icon}{label}</label>'
        for kind, label, icon in MARKER_TOGGLES
        if kind in kinds
    )
    if not items:
        return ""
    header = (
        '<div class="toggles-title"><span>Show on map</span>'
        '<label class="toggle-all"><input type="checkbox" data-all checked>Show all</label></div>'
    )
    return f'<div class="toggles">{header}{items}</div>'


def _toggle_css():
    rules = [f".hide-{k} .kind-{k} {{display:none !important}}" for k, _, _ in MARKER_TOGGLES if k != "banners"]
    return "\n  ".join(rules + [".hide-banners .key-label {display:none !important}"])


def _add_itinerary_panel(m, trip, days, rows, total_km, trip_bounds, kinds, offset_min_zoom):
    dates = [d["date"] for d in days if d.get("date")]
    date_range = f"{escape(dates[0])} → {escape(dates[-1])} · " if dates else ""
    panel = f"""
<style>
  .leaflet-container {{font-size:15px}}
  .leaflet-popup-content {{line-height:1.45}}
  .leaflet-popup-content small {{font-size:13px;color:#5f5f5a}}
  .key-label {{font-weight:600}}
  .leaflet-control-layers {{font-size:14px;line-height:1.6}}
  .leaflet-control-attribution, .leaflet-control-scale-line {{font-size:11px}}
  .hide-key-labels .key-label {{display:none}}
  /* Arrows are hidden with visibility, not display: the arrow add-on measures the arrow glyph when it
     redraws a line, and a display:none glyph measures 0, which makes it crash (dividing by zero). */
  .hide-arrows .route-arrow {{visibility:hidden}}
  text.route-arrow.day-off {{display:inline !important;visibility:hidden}}
  #itinerary {{position:absolute;left:10px;bottom:48px;z-index:1000;width:330px;max-height:70vh;display:flex;
    flex-direction:column;background:#fff;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.3);
    font:15px/1.45 system-ui,sans-serif;color:#1f1f1f}}
  #itinerary .panel-head {{position:relative;display:flex;align-items:flex-start;gap:8px;padding:14px 14px 8px 16px;
    cursor:pointer;flex:none}}
  #itinerary .panel-title {{flex:1;min-width:0}}
  #itinerary .panel-toggle {{flex:none;width:30px;height:30px;border-radius:50%;background:#f1f0ea;display:flex;
    align-items:center;justify-content:center}}
  #itinerary .panel-toggle::before {{content:"";width:8px;height:8px;border:solid #1f1f1f;border-width:0 2px 2px 0;
    transform:translateY(-2px) rotate(45deg)}}
  #itinerary.collapsed .panel-toggle::before {{transform:translateY(2px) rotate(-135deg)}}
  #itinerary .panel-head:focus-visible {{outline:3px solid #2a78d6;outline-offset:-3px;border-radius:12px}}
  #itinerary .panel-body {{overflow:auto;padding:0 16px 14px;min-height:0}}
  #itinerary.collapsed .panel-body {{display:none}}
  #itinerary.collapsed .panel-head {{padding-bottom:12px}}
  #itinerary h2 {{margin:0 0 2px;font-size:20px}}
  #itinerary .sub {{color:#5f5f5a}}
  #itinerary .day-row {{display:grid;grid-template-columns:22px 1fr;column-gap:6px;padding:7px 9px 7px 6px;
    border-radius:6px;cursor:pointer}}
  #itinerary .day-toggle {{padding-top:3px;cursor:pointer}}
  #itinerary .day-toggle input, #itinerary .days-head input {{margin:0;width:16px;height:16px;accent-color:#2a78d6}}
  #itinerary .day-row.day-hidden .day-body {{opacity:.45}}
  #itinerary .days-head {{display:flex;justify-content:space-between;align-items:center;font-weight:600;
    padding:0 9px 5px 6px;margin-bottom:3px;border-bottom:1px solid #e4e3dc}}
  #itinerary .days-head label {{display:flex;align-items:center;gap:5px;font-weight:400;font-size:14px;cursor:pointer}}
  #itinerary .hl {{display:block;font-size:14px;margin:2px 0 1px;color:#5f5f5a}}
  #itinerary .hl-item {{color:#1f1f1f;cursor:pointer;text-decoration:underline dotted #8a8a85;text-underline-offset:3px}}
  #itinerary .hl-item:hover {{color:#2a78d6}}
  .day-off {{display:none !important}}
  #itinerary .day-row:hover, #itinerary .day-row.active {{background:#f1f0ea}}
  #itinerary .all {{display:block;width:100%;margin:2px 0 10px;padding:9px 12px;border:0;border-radius:8px;
    background:#1f1f1f;color:#fff;font:600 15px/1.2 system-ui,sans-serif;cursor:pointer;
    box-shadow:0 1px 3px rgba(0,0,0,.25)}}
  #itinerary .all:hover {{background:#3a3a37}}
  #itinerary .all:focus-visible {{outline:3px solid #2a78d6;outline-offset:2px}}
  #itinerary .t {{color:#1f1f1f}}
  #itinerary .s, #itinerary .key {{color:#5f5f5a;font-size:14px}}
  #itinerary .key {{border-top:1px solid #e4e3dc;margin-top:8px;padding-top:8px}}
  #itinerary .pois {{margin:-2px 9px 5px 34px;font-size:14px;color:#5f5f5a}}
  #itinerary .pois summary {{cursor:pointer}}
  #itinerary .pois ul {{margin:4px 0 0;padding:0;list-style:none}}
  #itinerary .pois li {{padding:3px 5px;border-radius:4px;cursor:pointer;color:#1f1f1f}}
  #itinerary .pois li:hover {{background:#f1f0ea}}
  .trip-el {{transition:opacity .2s}}
  #itinerary .toggles {{display:grid;grid-template-columns:1fr 1fr;gap:2px 8px;margin:4px 0 10px;
    padding:8px 9px 10px;border:1px solid #e4e3dc;border-radius:8px;font-size:14px}}
  #itinerary .toggles-title {{grid-column:1/-1;display:flex;justify-content:space-between;align-items:center;
    font-weight:600;padding-bottom:5px;margin-bottom:3px;border-bottom:1px solid #e4e3dc}}
  #itinerary .toggles .toggle-all {{font-weight:400;color:#1f1f1f}}
  #itinerary .toggles label {{display:flex;align-items:center;gap:5px;cursor:pointer;padding:2px 0;white-space:nowrap}}
  #itinerary .toggles input {{margin:0;width:16px;height:16px;accent-color:#2a78d6}}
  #itinerary .toggles i {{display:inline-block;width:18px;height:18px;box-sizing:border-box;flex:none;
    text-align:center;line-height:14px;font-style:normal}}
  {_toggle_css()}
  /* Phones: the panel becomes a bottom sheet, collapsed to its header until tapped. */
  @media (max-width:600px) {{
    .leaflet-control-attribution {{font-size:9px;line-height:1.25}}
    #itinerary {{z-index:1001;left:0;right:0;bottom:0;width:auto;max-height:75vh;border-radius:16px 16px 0 0;
      box-shadow:0 -2px 12px rgba(0,0,0,.25);padding-bottom:env(safe-area-inset-bottom,0px)}}
    #itinerary .panel-head {{padding:18px 14px 10px 16px}}
    #itinerary .panel-head::before {{content:"";position:absolute;top:7px;left:50%;width:40px;height:4px;
      margin-left:-20px;border-radius:2px;background:#c9c8c0}}
    #itinerary h2 {{font-size:17px}}
    #itinerary .sub {{font-size:13px}}
    #itinerary .toggles input, #itinerary .day-toggle input, #itinerary .days-head input {{width:20px;height:20px}}
    #itinerary .day-row {{grid-template-columns:26px 1fr}}
    #itinerary .toggles label {{padding:4px 0}}
    .leaflet-bottom {{bottom:calc(76px + env(safe-area-inset-bottom,0px))}}
    .leaflet-popup-content {{max-width:calc(100vw - 80px) !important}}
    .leaflet-popup-content img {{max-width:100%;height:auto}}
    .key-label {{font-size:13px}}
  }}
</style>
<div id="itinerary">
  <div class="panel-head" role="button" tabindex="0" aria-expanded="true" aria-controls="panel-body"
       title="Show or hide the itinerary">
    <div class="panel-title"><h2>{escape(trip.get("name", "My Trip"))}</h2>
      <div class="sub">{date_range}{len(days)} days · {total_km:,.0f} km</div></div>
    <span class="panel-toggle" aria-hidden="true"></span>
  </div>
  <div class="panel-body" id="panel-body">
  {_toggles_html(kinds)}
  <button type="button" class="all" data-bounds='{json.dumps(trip_bounds)}'>⤢&nbsp; Show whole trip</button>
  <div class="days-head"><span>Days</span>
    <label><input type="checkbox" data-days-all checked>All days</label></div>
  {"".join(rows)}
  <div class="key">─── road / rail &nbsp; ‑ ‑ flight &nbsp; ··· approximate route<br>
    Routes keep right of their direction of travel, so a road driven both ways shows two
    side-by-side lines (when zoomed in).</div>
  </div>
</div>"""
    # Folium emits this before the map is created, so defer until the page has loaded.
    script = f"""
document.addEventListener('DOMContentLoaded', function () {{
  var map = {m.get_name()};
  // The offset plugin shifts every tiny road wiggle, which folds into loops when zoomed out. Simplify
  // in screen pixels first (so it adapts to the zoom level), then redraw the lines already on the map.
  if (L.PolylineOffset) {{
    var offsetPoints = L.PolylineOffset.offsetPoints;
    L.PolylineOffset.offsetPoints = function (pts, offset) {{
      return offsetPoints.call(this, L.LineUtil.simplify(pts, Math.abs(offset) * 1.5), offset);
    }};
    map.eachLayer(function (layer) {{
      if (layer.options && layer.options.offset && layer.redraw) layer.redraw();
    }});
  }}
  // Permanent key-point labels collide at country scale; show them once zoomed in.
  function updateKeyLabels() {{
    map.getContainer().classList.toggle('hide-key-labels', map.getZoom() < {LABEL_MIN_ZOOM});
    map.getContainer().classList.toggle('hide-arrows', map.getZoom() < {ARROW_MIN_ZOOM});
  }}
  map.on('zoomend', updateKeyLabels);
  updateKeyLabels();
  // Hide any label that would overlap one already shown; the selected day's labels get first pick.
  var focusedDay = null;
  // Markers that overlap on screen are nudged aside, placed in priority order (the selected day first,
  // then by kind: highlights, overnights, stops, ... optional/ideas last). The first marker placed keeps
  // its true position; later ones move to the nearest free spot. Re-run after every zoom.
  // Screen angles (0 = right, 90 = down). Right is tried last: name banners sit to the right of icons.
  var NUDGE_ANGLES = [180, 225, 135, 270, 90, 315, 45, 0];
  // Water test for nudged markers: read the base-map tile colour under a screen point. Water is blue on
  // all three base maps: blue at least 20 above red and 3 above green. That second test separates pale
  // shallow water, e.g. (221,241,248), from pale glaciers, e.g. (229,255,255). Land and parks fail both.
  // Several points are sampled so thin rivers don't count.
  function tileAt(x, y) {{
    var origin = map.getContainer().getBoundingClientRect(), cx = origin.left + x, cy = origin.top + y;
    var found = null;
    map.eachLayer(function (layer) {{
      if (found || !(layer instanceof L.TileLayer) || !layer._tiles) return;
      Object.keys(layer._tiles).some(function (key) {{
        var img = layer._tiles[key].el, box = img.getBoundingClientRect();
        if (cx >= box.left && cx < box.right && cy >= box.top && cy < box.bottom) {{
          found = {{img: img, box: box, cx: cx, cy: cy}};
          return true;
        }}
      }});
    }});
    return found;
  }}
  function isWaterPixel(x, y) {{
    var t = tileAt(x, y);
    if (!t || !t.img.complete || !t.img.naturalWidth) return false;  // unknown counts as land
    try {{
      if (!t.img._pixels) {{
        var c = document.createElement('canvas');
        c.width = t.img.naturalWidth; c.height = t.img.naturalHeight;
        c.getContext('2d').drawImage(t.img, 0, 0);
        t.img._pixels = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      }}
      var px = Math.floor((t.cx - t.box.left) * t.img.naturalWidth / t.box.width);
      var py = Math.floor((t.cy - t.box.top) * t.img.naturalHeight / t.box.height);
      var i = (py * t.img.naturalWidth + px) * 4, d = t.img._pixels;
      return d[i + 2] - d[i] >= 20 && d[i + 2] - d[i + 1] >= 3;
    }} catch (e) {{
      return false;  // pixels unreadable (e.g. no CORS): never hide on a guess
    }}
  }}
  function onWater(x, y, r) {{
    var h = r / 2, pts = [[0, 0], [h, 0], [-h, 0], [0, h], [0, -h]];
    return pts.filter(function (q) {{ return isWaterPixel(x + q[0], y + q[1]); }}).length >= 3;
  }}
  function declutterMarkers() {{
    var placed = [], items = [];
    map.eachLayer(function (layer) {{
      if (!(layer instanceof L.Marker) || !layer._icon) return;
      var icon = layer._icon;
      if (layer._baseZ === undefined) {{
        layer._baseZ = layer.options.zIndexOffset || 0;
        icon.dataset.ml = parseFloat(icon.style.marginLeft) || 0;
        icon.dataset.mt = parseFloat(icon.style.marginTop) || 0;
        var tip = layer.getTooltip();
        if (tip) tip._baseOffset = L.point(tip.options.offset);
      }}
      var focused = focusedDay && icon.classList.contains('day-' + focusedDay);
      layer.setZIndexOffset(layer._baseZ + (focused ? 10000 : 0));
      if (icon.offsetParent === null) return;  // hidden by a sidebar switch
      items.push({{layer: layer, icon: icon, rank: layer._baseZ + (focused ? 10000 : 0)}});
    }});
    items.sort(function (a, b) {{ return b.rank - a.rank; }});
    items.forEach(function (it) {{
      var size = it.layer.options.icon.options.iconSize;
      var r = (size ? size[0] : 24) / 2 - 2;  // allow a sliver of overlap before nudging
      var p = map.latLngToContainerPoint(it.layer.getLatLng());
      var best = [0, 0], hide = false;
      function free(dx, dy) {{
        var x = p.x + dx, y = p.y + dy;
        return placed.every(function (q) {{
          if (q.r !== undefined) return Math.hypot(x - q.x, y - q.y) >= r + q.r;  // another icon
          // a name banner: distance from the icon's centre to the nearest point of the rectangle
          var nx = Math.max(q.left, Math.min(x, q.right)), ny = Math.max(q.top, Math.min(y, q.bottom));
          return Math.hypot(x - nx, y - ny) >= r;
        }});
      }}
      if (!free(0, 0)) {{
        hide = true;  // unless a free spot on land turns up
        search: for (var ring = 1; ring <= 4; ring++) {{
          for (var i = 0; i < NUDGE_ANGLES.length; i++) {{
            var a = NUDGE_ANGLES[i] * Math.PI / 180, d = ring * r, dx = Math.cos(a) * d, dy = Math.sin(a) * d;
            if (free(dx, dy) && !onWater(p.x + dx, p.y + dy, r)) {{ best = [dx, dy]; hide = false; break search; }}
          }}
        }}
      }}
      // Nothing free on land: hide the marker (and its banner) at this zoom; zooming in brings it back.
      it.icon.style.visibility = hide ? 'hidden' : '';
      var tipEl = it.layer.getTooltip() && it.layer.getTooltip().getElement();
      if (tipEl) tipEl.dataset.markerHidden = hide ? '1' : '';
      if (hide) return;
      it.icon.style.marginLeft = (+it.icon.dataset.ml + best[0]) + 'px';
      it.icon.style.marginTop = (+it.icon.dataset.mt + best[1]) + 'px';
      var tip = it.layer.getTooltip();
      if (tip && tip.options.permanent && tip._baseOffset) {{  // keep name banners beside their icon
        tip.options.offset = tip._baseOffset.add([best[0], best[1]]);
        if (it.layer.isTooltipOpen()) tip.update();
      }}
      placed.push({{x: p.x + best[0], y: p.y + best[1], r: r}});
      // A visible banner is an obstacle too, so lower-priority icons aren't nudged underneath it.
      var el = tip && tip.options.permanent && it.layer.isTooltipOpen() && tip.getElement();
      if (el && el.offsetParent !== null) {{
        var box = el.getBoundingClientRect(), origin = map.getContainer().getBoundingClientRect();
        placed.push({{left: box.left - origin.left, top: box.top - origin.top,
                      right: box.right - origin.left, bottom: box.bottom - origin.top}});
      }}
    }});
  }}
  // Day switches: an element is hidden when every day it belongs to is switched off, so a marker shared
  // by several days (e.g. a home base) stays while any of them is on. Re-applied before each declutter
  // because some layers (e.g. direction arrows) redraw their elements on zoom.
  var hiddenDays = {{}};
  function applyDayVisibility() {{
    map.getContainer().querySelectorAll('.trip-el').forEach(function (el) {{
      var days = (el.getAttribute('class') || '').match(/\\bday-\\d+\\b/g) || [];
      var off = days.length > 0 && days.every(function (d) {{ return hiddenDays[d.slice(4)]; }});
      el.classList.toggle('day-off', off);
    }});
  }}
  function declutter() {{ applyDayVisibility(); declutterMarkers(); declutterLabels(); }}
  function declutterLabels() {{
    var labels = Array.prototype.slice.call(map.getContainer().querySelectorAll('.key-label'));
    var first = labels.filter(function (l) {{ return focusedDay && l.classList.contains('day-' + focusedDay); }});
    var kept = [];
    first.concat(labels.filter(function (l) {{ return first.indexOf(l) < 0; }})).forEach(function (label) {{
      label.style.visibility = '';
      if (label.dataset.markerHidden) {{ label.style.visibility = 'hidden'; return; }}
      var r = label.getBoundingClientRect();
      if (!r.width) return;  // hidden at this zoom level
      var overlaps = kept.some(function (k) {{
        return r.left < k.right && r.right > k.left && r.top < k.bottom && r.bottom > k.top;
      }});
      if (overlaps) label.style.visibility = 'hidden'; else kept.push(r);
    }});
  }}
  // Leaflet repositions tooltips on zoom, so measure once it has finished.
  map.on('zoomend overlayadd overlayremove baselayerchange', function () {{ setTimeout(declutter, 0); }});
  // The water test needs the tiles, which arrive after the zoom ends (and again after a base-map switch).
  function watchTiles(layer) {{
    if (!(layer instanceof L.TileLayer) || layer._declutterHooked) return;
    layer._declutterHooked = true;
    layer.on('load', function () {{ setTimeout(declutter, 0); }});
  }}
  map.eachLayer(watchTiles);
  map.on('layeradd', function (e) {{ watchTiles(e.layer); }});
  setTimeout(declutter, 0);
  // The panel collapses to its header: tap the header (or press Enter) to toggle. On phones it starts
  // collapsed and collapses again after choosing a day or place, so the map gets the screen.
  var panel = document.getElementById('itinerary'), panelHead = panel.querySelector('.panel-head');
  function isPhone() {{ return window.innerWidth <= 600; }}
  function setCollapsed(collapsed) {{
    panel.classList.toggle('collapsed', collapsed);
    panelHead.setAttribute('aria-expanded', String(!collapsed));
  }}
  setCollapsed(isPhone());
  panelHead.addEventListener('click', function () {{ setCollapsed(!panel.classList.contains('collapsed')); }});
  panelHead.addEventListener('keydown', function (e) {{
    if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); panelHead.click(); }}
  }});
  // Keep zoomed areas clear of the panel: beside it on wide screens, above it on phones.
  function fitOptions() {{
    if (isPhone()) return {{paddingTopLeft: [20, 20], paddingBottomRight: [20, panel.offsetHeight + 20]}};
    if (panel.classList.contains('collapsed'))
      return {{paddingTopLeft: [30, 30], paddingBottomRight: [30, panel.offsetHeight + 60]}};
    return {{paddingTopLeft: [panel.offsetWidth + 30, 30], paddingBottomRight: [30, 30]}};
  }}
  // You can zoom out until the whole trip fits this screen. Past the 20 km scale, routes are drawn
  // centred on the road: the side-by-side offset would be wider than the gap between road and coast.
  var OFFSET_MIN_ZOOM = {offset_min_zoom};
  var tripBounds = L.latLngBounds({json.dumps(trip_bounds)});
  function updateMinZoom() {{
    var o = fitOptions();
    var fit = map.getBoundsZoom(tripBounds, false, L.point(o.paddingTopLeft).add(o.paddingBottomRight));
    map.setMinZoom(Math.min(OFFSET_MIN_ZOOM, Math.floor(fit)));
  }}
  map.eachLayer(function (layer) {{
    if (layer instanceof L.Polyline && typeof layer.options.offset === 'number') layer._routeOffset = layer.options.offset;
  }});
  function updateOffsets() {{
    var centred = map.getZoom() < OFFSET_MIN_ZOOM;
    map.eachLayer(function (layer) {{
      if (layer._routeOffset === undefined) return;  // route lines only (banners use _baseOffset)
      var want = centred ? 0 : layer._routeOffset;
      if (layer.options.offset !== want) {{ layer.options.offset = want; layer.redraw(); }}  // the plugin reads it on redraw
    }});
  }}
  map.on('zoomend', updateOffsets);
  window.addEventListener('resize', updateMinZoom);
  updateMinZoom();
  // Selecting a day brings it to the front and fades the others, since days often share roads.
  var focusStyle = document.createElement('style');
  document.head.appendChild(focusStyle);
  function focusDay(row) {{
    var day = row.dataset.day;
    focusedDay = day || null;
    focusStyle.textContent = day ? '.trip-el:not(.day-' + day + ') {{ opacity: .15 !important; }}' : '';
    if (day) window[row.dataset.group].eachLayer(function (layer) {{
      // The arrow add-on can throw while re-attaching to a line that was just redrawn; a line left
      // behind another day's is cosmetic, so it must not stop the rest of the click from running.
      try {{ if (layer.bringToFront) layer.bringToFront(); }} catch (e) {{}}
    }});
  }}
  document.querySelectorAll('#itinerary [data-bounds]').forEach(function (row) {{
    row.addEventListener('click', function () {{
      var box = row.querySelector('[data-day-toggle]');
      if (box && !box.checked) {{ box.checked = true; box.dispatchEvent(new Event('change')); }}  // focusing shows it
      document.querySelectorAll('#itinerary .active').forEach(function (r) {{ r.classList.remove('active'); }});
      row.classList.add('active');
      if (isPhone()) setCollapsed(true);  // show the map, not the list
      map.fitBounds(JSON.parse(row.dataset.bounds), fitOptions());
      focusDay(row);
      setTimeout(declutter, 0);  // covers the case where the zoom level doesn't change
    }});
  }});
  map.fitBounds(tripBounds, fitOptions());
  updateOffsets();
  // Marker-type switches. Choices are remembered per trip in this browser (when storage is available).
  var storageKey = 'tripMapToggles:' + {json.dumps(trip.get("name", ""))};
  var saved = {{}};
  try {{ saved = JSON.parse(localStorage.getItem(storageKey) || '{{}}'); }} catch (e) {{}}
  document.querySelectorAll('#itinerary [data-kind]').forEach(function (box) {{
    var kind = box.dataset.kind;
    if (saved[kind] === false) box.checked = false;
    function apply() {{ map.getContainer().classList.toggle('hide-' + kind, !box.checked); }}
    apply();
    box.addEventListener('change', function () {{
      apply();
      saved[kind] = box.checked;
      try {{ localStorage.setItem(storageKey, JSON.stringify(saved)); }} catch (e) {{}}
      setTimeout(declutter, 0);  // hidden banners free up room for others
      syncMaster();
    }});
  }});
  // "Show all" reflects the individual switches: ticked when all are on, dashed when only some are.
  var master = document.querySelector('#itinerary [data-all]');
  var kindBoxes = Array.prototype.slice.call(document.querySelectorAll('#itinerary [data-kind]'));
  function syncMaster() {{
    if (!master) return;
    var on = kindBoxes.filter(function (b) {{ return b.checked; }}).length;
    master.checked = on === kindBoxes.length;
    master.indeterminate = on > 0 && on < kindBoxes.length;
  }}
  if (master) master.addEventListener('change', function () {{
    var show = master.checked;
    kindBoxes.forEach(function (b) {{
      if (b.checked !== show) {{ b.checked = show; b.dispatchEvent(new Event('change')); }}
    }});
    syncMaster();
  }});
  syncMaster();
  document.querySelectorAll('#itinerary [data-point]').forEach(function (item) {{
    item.addEventListener('click', function (e) {{
      e.stopPropagation();  // a highlight inside a day row shouldn't also trigger the row's zoom
      if (isPhone()) setCollapsed(true);
      map.setView(JSON.parse(item.dataset.point), 12);
    }});
  }});
  var dayBoxes = Array.prototype.slice.call(document.querySelectorAll('#itinerary [data-day-toggle]'));
  var daysMaster = document.querySelector('#itinerary [data-days-all]');
  function syncDaysMaster() {{
    var on = dayBoxes.filter(function (b) {{ return b.checked; }}).length;
    daysMaster.checked = on === dayBoxes.length;
    daysMaster.indeterminate = on > 0 && on < dayBoxes.length;
  }}
  dayBoxes.forEach(function (box) {{
    box.parentNode.addEventListener('click', function (e) {{ e.stopPropagation(); }});  // not a row click
    box.addEventListener('change', function () {{
      hiddenDays[box.dataset.dayToggle] = !box.checked;
      box.closest('.day-row').classList.toggle('day-hidden', !box.checked);
      syncDaysMaster();
      setTimeout(declutter, 0);
    }});
  }});
  daysMaster.addEventListener('change', function () {{
    var show = daysMaster.checked;
    dayBoxes.forEach(function (b) {{
      if (b.checked !== show) {{ b.checked = show; b.dispatchEvent(new Event('change')); }}
    }});
    syncDaysMaster();
  }});
}});
"""
    m.get_root().html.add_child(folium.Element(panel))
    m.get_root().script.add_child(folium.Element(script))
