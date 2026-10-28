"""Finding stops by where they are, across Regional Rail, the Metro and buses: near a
point, along a street, or inside an area.

Each feed has its own kind of stop (a rail station, a Metro station or trolley stop, a bus
corner). StopIndex flattens them into one list of Located stops, with the id each mode's
tools accept, so a single search covers all three.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from .gtfs import normalize

MODES = ("regional_rail", "metro", "bus")
WALK_METERS_PER_MIN = 80
DETOUR = 1.25  # streets aren't straight lines
_STREET_TYPES = {"st", "av", "rd", "blvd", "dr", "ln", "pk", "pike", "pkwy", "hwy", "ct", "pl", "way", "ter", "cir", "sq"}


@dataclass
class Located:
    mode: str  # one of MODES
    stop_id: str  # what that mode's tools accept as the stop
    name: str
    lat: float
    lon: float
    lines: list[str]  # line names, or bus route numbers
    streets: dict[str, str]  # normalized -> as written, for corners named "A St & B Av"
    words: frozenset[str]

    def to_dict(self, meters: float | None = None) -> dict:
        d = {"mode": self.mode, "name": self.name, "stop_id": self.stop_id, "lines": self.lines}
        if meters is not None:
            d["distance_m"] = round(meters)
            d["walk_min"] = walk_minutes(meters)
        return d


def meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat1 - lat2) * 111_000
    dx = (lon1 - lon2) * 111_000 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def walk_minutes(m: float) -> int:
    return max(1, math.ceil(m * DETOUR / WALK_METERS_PER_MIN))


def street_names(name: str) -> dict[str, str]:
    """{"broad st": "Broad St", "walnut st": "Walnut St"} for "Broad St & Walnut St - FS";
    empty if the name isn't a corner."""
    base = re.sub(r"\s+-\s+.*$|\s*\(.*\)$", "", name)  # side of street; "(139; stop 28874)"
    parts = re.split(r"\s+(?:&|and|at)\s+", base)
    if len(parts) < 2:
        return {}
    return {normalize(p): p.strip() for p in parts if normalize(p)}


def street_core(street: str) -> str:
    """"lancaster" for "lancaster av": to match a street when the type is left off."""
    words = [w for w in street.split() if w not in _STREET_TYPES]
    return " ".join(words) or street


class StopIndex:
    def __init__(self, rail=None, metro=None, bus=None):
        self.stops: list[Located] = []
        if rail is not None:
            for s in rail.stops.values():
                if s.routes:
                    self._add("regional_rail", s.stop_id, s.name, s.lat, s.lon,
                              sorted(rail.route_name(r) for r in s.routes))
        if metro is not None:
            order = list(_metro_order())
            for s in metro._name_index.values():
                lines = sorted(s.routes, key=lambda r: (order.index(r) if r in order else len(order), r))
                self._add("metro", s.stop_id, s.name, s.lat, s.lon, [metro.line(r).code for r in lines])
        if bus is not None:
            for c in bus.corners:
                self._add("bus", c.stop_id, c.name, c.lat, c.lon, bus.routes_label(c.routes).split(", "))

    def _add(self, mode, stop_id, name, lat, lon, lines) -> None:
        if not (lat or lon):
            return
        self.stops.append(Located(
            mode, stop_id, name, lat, lon, lines, street_names(name), frozenset(normalize(name).split()),
        ))

    def _of(self, modes) -> list[Located]:
        return [s for s in self.stops if not modes or s.mode in modes]

    # -- by name -------------------------------------------------------------

    def named(self, query: str, modes=None, limit: int = 8) -> list[Located]:
        """Stops whose names contain every word of query, stations first."""
        words = set(normalize(query).split()) - {"and", "at", "the", "of"}
        if not words:
            return []
        hits = [s for s in self._of(modes) if words <= s.words]
        hits.sort(key=lambda s: (MODES.index(s.mode), len(s.words), s.name))
        return hits[:limit]

    # -- near a point --------------------------------------------------------

    def near(self, lat: float, lon: float, radius: float, modes=None) -> list[tuple[float, Located]]:
        # A quick box test first: most stops are nowhere near.
        dlat = radius / 111_000
        dlon = radius / (111_000 * max(0.2, math.cos(math.radians(lat))))
        out = []
        for s in self._of(modes):
            if abs(s.lat - lat) <= dlat and abs(s.lon - lon) <= dlon:
                m = meters(lat, lon, s.lat, s.lon)
                if m <= radius:
                    out.append((m, s))
        out.sort(key=lambda x: x[0])
        return out

    def nearest(self, lat: float, lon: float, mode: str) -> tuple[float, Located] | None:
        best = min(((meters(lat, lon, s.lat, s.lon), s) for s in self._of([mode])), default=None, key=lambda x: x[0])
        return best

    # -- along a street ------------------------------------------------------

    def on_street(self, street: str, modes=None) -> tuple[str, list[list[Located]]]:
        """Stops at corners on street, split into stretches (the same name in different
        towns, or gaps over 800 m), each in order along the street. Returns the street name
        as matched and the stretches, longest first."""
        q = normalize(street)
        candidates = [s for s in self._of(modes) if s.streets]
        # "Lancaster Av" means that street, if stops name it so; plain "Lancaster" (or a
        # type no stop uses) means any Lancaster Av, Rd or Pike.
        exact = [s for s in candidates if q in s.streets] if street_core(q) != q else []
        hits = exact or [s for s in candidates if street_core(q) in {street_core(x) for x in s.streets}]
        if not hits:
            return q, []
        root = list(range(len(hits)))

        def find(i):
            while root[i] != i:
                root[i] = root[root[i]]
                i = root[i]
            return i

        for i, j in combinations(range(len(hits)), 2):
            if meters(hits[i].lat, hits[i].lon, hits[j].lat, hits[j].lon) < 800:
                root[find(i)] = find(j)
        groups: dict[int, list[Located]] = defaultdict(list)
        for i, s in enumerate(hits):
            groups[find(i)].append(s)
        stretches = [_along(g) for g in groups.values()]
        stretches.sort(key=len, reverse=True)
        return q, stretches

    # -- inside an area ------------------------------------------------------

    def inside(self, test, modes=None) -> list[Located]:
        return [s for s in self._of(modes) if test(s.lat, s.lon)]


def _metro_order():
    from .metro import LINES

    return LINES


def _along(stops: list[Located]) -> list[Located]:
    """stops sorted along their main axis, west to east or south to north."""
    if len(stops) < 2:
        return stops
    lat0 = sum(s.lat for s in stops) / len(stops)
    lon0 = sum(s.lon for s in stops) / len(stops)
    k = math.cos(math.radians(lat0))
    pts = [((s.lon - lon0) * k, s.lat - lat0) for s in stops]
    sxx = sum(x * x for x, _ in pts)
    syy = sum(y * y for _, y in pts)
    sxy = sum(x * y for x, y in pts)
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)  # direction of greatest spread
    vx, vy = math.cos(angle), math.sin(angle)
    if (abs(vx) >= abs(vy) and vx < 0) or (abs(vy) > abs(vx) and vy < 0):
        vx, vy = -vx, -vy
    return [s for _, s in sorted(zip((x * vx + y * vy for x, y in pts), stops), key=lambda t: t[0])]


def direction(stops: list[Located]) -> str:
    """'west to east' or 'south to north' (or between), for a stretch from _along."""
    if len(stops) < 2:
        return ""
    a, b = stops[0], stops[-1]
    dx = (b.lon - a.lon) * math.cos(math.radians(a.lat))
    dy = b.lat - a.lat
    bearing = math.degrees(math.atan2(dx, dy)) % 360
    names = ["south to north", "southwest to northeast", "west to east", "northwest to southeast",
             "north to south", "northeast to southwest", "east to west", "southeast to northwest"]
    return names[round(bearing / 45) % 8]
