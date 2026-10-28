"""Find landmarks, addresses, neighborhoods and towns with a Nominatim (OpenStreetMap)
geocoder, limited to SEPTA's region.

The public server at nominatim.openstreetmap.org asks for at most one request a second, a
User-Agent naming the app, and caching; this does all three. SEPTA_GEOCODER_URL points it
at another Nominatim-compatible server (self-hosted, or a hosted one with a key in the URL).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

GEOCODER_URL = os.environ.get("SEPTA_GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
USER_AGENT = "septa-mcp/0.1 (+https://github.com/ellbur/septa-claude-connector)"
TIMEOUT = float(os.environ.get("SEPTA_GEOCODER_TIMEOUT", "10"))
CACHE_SECONDS = 24 * 3600
MIN_INTERVAL = 1.0  # seconds between requests, per Nominatim's usage policy
ATTRIBUTION = "Place data © OpenStreetMap contributors (ODbL)"

# west, north, east, south: Philadelphia and its Pennsylvania suburbs, out to Trenton,
# Wilmington and the edges of Chester and Bucks counties.
VIEWBOX = (-76.15, 40.65, -74.55, 39.5)

# Nominatim address types that are areas a rider might mean as a whole.
AREA_KINDS = {
    "neighbourhood", "quarter", "suburb", "city_district", "borough", "hamlet", "village", "town",
    "city", "municipality", "county", "state_district", "district", "administrative",
}


class GeocoderError(RuntimeError):
    pass


@dataclass
class Place:
    name: str
    display_name: str
    kind: str  # Nominatim's addresstype, e.g. "neighbourhood", "town", "road", "amenity"
    category: str  # e.g. "amenity", "tourism", "place", "boundary", "building"
    lat: float
    lon: float
    bbox: tuple[float, float, float, float] | None  # south, north, west, east
    geometry: dict | None  # GeoJSON Polygon/MultiPolygon outline, when asked for and known

    @property
    def is_area(self) -> bool:
        return self.kind in AREA_KINDS or (self.category == "boundary" and self.geometry is not None)

    def to_dict(self) -> dict:
        d = {
            "name": self.name or self.display_name.split(",")[0],
            "address": self.display_name,
            "kind": self.kind,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
        }
        if self.is_area:
            d["is_area"] = True
        return d


_cache: dict[tuple, tuple[float, list[Place]]] = {}
_lock = threading.Lock()
_last_request = 0.0


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def search(query: str, limit: int = 5, outline: bool = False) -> list[Place]:
    """Places in SEPTA's region matching query, best first. outline=True also fetches
    area outlines (simplified), for telling what's inside a town or neighborhood."""
    global _last_request
    key = (query.strip().lower(), limit, outline)
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": limit,
        "countrycodes": "us",
        "viewbox": ",".join(str(v) for v in VIEWBOX),
        "bounded": 1,
    }
    if outline:
        params.update({"polygon_geojson": 1, "polygon_threshold": 0.0005})
    if os.environ.get("SEPTA_GEOCODER_EMAIL"):
        params["email"] = os.environ["SEPTA_GEOCODER_EMAIL"]
    url = GEOCODER_URL + ("&" if "?" in GEOCODER_URL else "?") + urllib.parse.urlencode(params)
    with _lock:  # also spaces requests MIN_INTERVAL apart
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        wait = _last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise GeocoderError(f"Couldn't look up places ({e}).") from e
        places = [p for p in (_place(r) for r in data if isinstance(r, dict)) if p] if isinstance(data, list) else []
        if len(_cache) > 1000:
            _cache.clear()
        _cache[key] = (time.time() + CACHE_SECONDS, places)
        return places


def _place(r: dict) -> Place | None:
    try:
        lat, lon = float(r["lat"]), float(r["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        s, n, w, e = (float(v) for v in r.get("boundingbox") or ())
        bbox = (s, n, w, e)
    except ValueError:
        bbox = None
    geometry = r.get("geojson")
    if not (isinstance(geometry, dict) and geometry.get("type") in ("Polygon", "MultiPolygon")):
        geometry = None
    return Place(
        str(r.get("name") or ""),
        str(r.get("display_name") or ""),
        str(r.get("addresstype") or r.get("type") or ""),
        str(r.get("category") or r.get("class") or ""),
        lat,
        lon,
        bbox,
        geometry,
    )


def contains(geometry: dict, lat: float, lon: float) -> bool:
    """Whether a GeoJSON Polygon or MultiPolygon contains the point (even-odd rule, holes
    included)."""
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    inside = False
    for rings in polygons:
        for ring in rings:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
                if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
    return inside
