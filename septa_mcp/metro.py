"""SEPTA Metro: the subway, trolley and Norristown High Speed lines.

SEPTA publishes these in its bus GTFS feed; gtfs.gtfs_subset cuts them out on download
and MetroFeed answers schedule questions with the rail feed's code. What differs:

- A station is several stops in the feed: one per platform or direction, and separate
  ones per line at transfer stations (15th St/City Hall has eight). MetroFeed merges
  them into one stop per station, by parent_station and by name, so "15th St" finds the
  L1, B1, B2 and the T trolleys alike.
- Some transfers are a short walk between differently named stations (40th St on the
  L1 and 40th-Market for the trolleys); those become walk_links.
- Vehicles have no public train number. Riders know a trip by line and direction:
  "L1 toward Frankford Transit Center".
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import combinations

from .gtfs import RailFeed, Ride, Stop, Trip, _to_dt, fmt_time_on


@dataclass(frozen=True)
class Line:
    code: str  # GTFS route_id, e.g. "L1"
    name: str
    mode: str
    aliases: tuple[str, ...] = ()
    formerly: str | None = None  # route number before SEPTA's Metro letter names
    alert_ids: tuple[str, ...] = ()  # route_ids the alerts feed files it under
    live: bool = True  # vehicles report positions to the TransitView API

    @property
    def label(self) -> str:
        """'L1 (Market-Frankford Line)', or 'T1 trolley' when the name says it already."""
        return self.name if self.name.startswith(self.code) else f"{self.code} ({self.name})"

    def to_dict(self) -> dict:
        d = {"line": self.code, "name": self.name, "mode": self.mode}
        if self.formerly:
            d["formerly"] = self.formerly
        return d


LINES = {
    line.code: line
    for line in [
        # The subway lines don't report vehicle positions to TransitView.
        Line("L1", "Market-Frankford Line", "subway", ("l", "mfl", "market frankford", "el", "blue"),
             alert_ids=("rr_route_mfl", "rr_route_mfo"), live=False),
        Line("B1", "Broad Street Line local", "subway", ("broad street local", "bsl local"),
             alert_ids=("rr_route_bsl", "rr_route_bso"), live=False),
        Line("B2", "Broad Street Line express", "subway", ("broad street express", "bsl express"),
             alert_ids=("rr_route_bsl",), live=False),
        Line("B3", "Broad-Ridge Spur", "subway", ("broad ridge spur", "broad ridge", "ridge spur"),
             alert_ids=("rr_route_bsl",), live=False),
        Line("M1", "Norristown High Speed Line", "light rail",
             ("m", "nhsl", "norristown high speed", "high speed"), "Route 100", ("rr_route_nhsl",)),
        Line("T1", "T1 trolley", "trolley", (), "Route 10", ("trolley_route_10",)),
        Line("T2", "T2 trolley", "trolley", (), "Route 34", ("trolley_route_34",)),
        Line("T3", "T3 trolley", "trolley", (), "Route 13", ("trolley_route_13",)),
        Line("T4", "T4 trolley", "trolley", (), "Route 11", ("trolley_route_11",)),
        Line("T5", "T5 trolley", "trolley", (), "Route 36", ("trolley_route_36",)),
        Line("G1", "G1 Girard Av trolley", "trolley", ("girard", "girard av"), "Route 15", ("trolley_route_15",)),
        Line("D1", "D1 Media trolley", "trolley", ("media",), "Route 101", ("trolley_route_101",)),
        Line("D2", "D2 Sharon Hill trolley", "trolley", ("sharon hill",), "Route 102", ("trolley_route_102",)),
    ]
}

# Alerts-feed route_id -> the lines it covers.
ALERT_LINES: dict[str, list[str]] = {}
for _line in LINES.values():
    for _aid in _line.alert_ids:
        ALERT_LINES.setdefault(_aid, []).append(_line.code)

_B, _T = ["B1", "B2", "B3"], ["T1", "T2", "T3", "T4", "T5"]
LINE_GROUPS = {
    "b": _B, "bsl": _B, "broad street": _B, "orange": _B,
    "t": _T, "subway surface": _T, "green": _T,
    "d": ["D1", "D2"],
}

# Words that don't tell lines apart: "the Market-Frankford Line", "route 10", "T1 trolley".
_FILLER = {"the", "line", "lines", "trolley", "trolleys", "route", "rt", "septa", "metro", "train", "trains"}


def _line_key(s: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", s.lower()).split()
    return " ".join(w for w in words if w not in _FILLER)


def _line_index() -> dict[str, list[str]]:
    index: dict[str, list[str]] = {_line_key(k): v for k, v in LINE_GROUPS.items()}
    for line in LINES.values():
        for name in (line.code, line.name, line.formerly or "", *line.aliases):
            if _line_key(name):
                index.setdefault(_line_key(name), [line.code])
    return index


_LINE_INDEX = _line_index()


def resolve_lines(query: str) -> list[str]:
    """Line codes a rider's name for a line means: "MFL" -> [L1], "Broad Street Line" ->
    [B1, B2, B3], "route 10" -> [T1]. Raises ValueError for anything else."""
    codes = _LINE_INDEX.get(_line_key(query))
    if codes:
        return list(codes)
    known = ", ".join(line.label + (f" (formerly {line.formerly})" if line.formerly else "") for line in LINES.values())
    raise ValueError(f"No Metro line matches '{query}'. The Metro lines are: {known}.")


def line_info(code: str, route: dict | None = None) -> Line:
    """The Line for a route_id, or one made up from the feed for a route added since."""
    if code in LINES:
        return LINES[code]
    route = route or {}
    mode = "trolley" if route.get("route_type") == "0" else "subway"
    return Line(code, route.get("route_long_name") or code, mode)


# Names riders use for Metro stations that don't match the feed.
_ALIASES = {
    "city hall": "15th St/City Hall",
    "15th st": "15th St/City Hall",
    "15th street": "15th St/City Hall",
    "30th st": "Drexel Station at 30th St",
    "30th street": "Drexel Station at 30th St",
    "drexel": "Drexel Station at 30th St",
    "69th st": "69th St Transit Center",
    "69th street": "69th St Transit Center",
    "69th st tc": "69th St Transit Center",
    "8th st": "8th-Market",
    "8th and market": "8th-Market",
    "independence hall": "5th St/Independence Hall",
    "5th st": "5th St/Independence Hall",
    "frankford": "Frankford Transit Center",
    "frankford tc": "Frankford Transit Center",
    "olney": "Olney Transit Center",
    "fern rock": "Fern Rock Transit Center",
    "nrg": "NRG Station",
    "sports complex": "NRG Station",
    "stadiums": "NRG Station",
    "norristown": "Norristown Transit Center",
    "darby": "Darby Transit Center",
}

# Feed suffixes naming which line uses a platform ("15th St/City Hall - B1") or which
# side of the intersection a stop is on ("Girard Av & 63rd St - MBFS": midblock far side).
_PLATFORM_SUFFIX = re.compile(r"\s+-\s+(?:(?:[A-Z]\d?\b[\s&,]*)+|MBFS|MBNS|FS|NS|MB)$")
SAME_STATION_METERS = 400  # same-named stops this close are one station
WALK_METERS = 200  # stations this close are a walkable transfer


def _platform_name(name: str) -> str:
    return _PLATFORM_SUFFIX.sub("", name).strip()


def _meters(a: Stop, b: Stop) -> float:
    dy = (a.lat - b.lat) * 111_000
    dx = (a.lon - b.lon) * 111_000 * math.cos(math.radians(a.lat))
    return math.hypot(dx, dy)


class MetroFeed(RailFeed):
    KIND = "Metro station or stop"
    ALIASES = _ALIASES
    GROUPS: dict[str, list[str]] = {}

    def _merge_platforms(self, parents: dict[str, str]) -> None:
        """Replace the feed's per-platform stops with one stop per station."""
        raw = {st.stop_id: self.stops[st.stop_id] for t in self.trips.values() for st in t.times if st.stop_id in self.stops}
        root = {i: i for i in raw}

        def find(i: str) -> str:
            while root[i] != i:
                root[i] = root[root[i]]
                i = root[i]
            return i

        same: dict[tuple[str, str], list[str]] = defaultdict(list)
        for i, s in raw.items():
            if parents.get(i):
                same[("parent", parents[i])].append(i)
            same[("name", _platform_name(s.name))].append(i)
        for (how, _), ids in same.items():
            for a, b in combinations(ids, 2):
                if how == "parent" or _meters(raw[a], raw[b]) < SAME_STATION_METERS:
                    root[find(a)] = find(b)

        members: dict[str, list[str]] = defaultdict(list)
        for i in raw:
            members[find(i)].append(i)
        self.stops, self.stop_alias, self.platforms = {}, {}, {}
        for ids in members.values():
            ids.sort(key=lambda i: (len(i), i))
            names = Counter(_platform_name(raw[i].name) for i in ids).most_common()
            top = names[0][1]
            name = min((n for n, c in names if c == top), key=len)
            sid = ids[0]
            self.stops[sid] = Stop(
                sid,
                name,
                sum(raw[i].lat for i in ids) / len(ids),
                sum(raw[i].lon for i in ids) / len(ids),
                any(raw[i].wheelchair for i in ids),
            )
            self.platforms[sid] = ids
            for i in ids:
                self.stop_alias[i] = sid
            for p in {parents[i] for i in ids if parents.get(i)}:
                self.stop_alias[p] = sid

        routes: dict[str, set[str]] = defaultdict(set)
        for t in self.trips.values():
            # Trips have no train number; train is the vehicle block, which keeps two
            # lines leaving a station at the same minute apart in direct_rides.
            t.train = t.train or t.trip_id
            times = []
            for st in t.times:
                st.stop_id = self.stop_alias.get(st.stop_id, st.stop_id)
                routes[st.stop_id].add(t.route_id)
                prev = times[-1] if times else None
                if prev and prev.stop_id == st.stop_id:  # two platforms of one station in a row
                    prev.dep, prev.pickup = st.dep, prev.pickup or st.pickup
                    continue
                times.append(st)
            t.times = times

        # Different stations with one name (street corners on two lines): add the lines.
        by_name: dict[str, list[Stop]] = defaultdict(list)
        for s in self.stops.values():
            by_name[s.name].append(s)
        for same_name in by_name.values():
            if len(same_name) > 1:
                for s in same_name:
                    s.name = f"{s.name} ({', '.join(sorted(routes[s.stop_id]))})"

        for a, b in combinations(list(self.stops.values()), 2):
            if routes[a.stop_id] == routes[b.stop_id]:
                continue
            d = _meters(a, b)
            if d <= WALK_METERS:
                minutes = max(2, round(d / 70))  # walking pace, plus a street to cross
                if routes[b.stop_id] - routes[a.stop_id]:
                    self.walk_links.setdefault(a.stop_id, []).append((b.stop_id, minutes))
                if routes[a.stop_id] - routes[b.stop_id]:
                    self.walk_links.setdefault(b.stop_id, []).append((a.stop_id, minutes))

    def line(self, route_id: str) -> Line:
        return line_info(route_id, self.routes.get(route_id))

    def route_name(self, route_id: str) -> str:
        return self.line(route_id).label

    def departures(self, station: Stop, start: datetime, end: datetime) -> list[tuple[datetime, Trip, int]]:
        """(scheduled departure, trip, index of the station in trip.times) for every trip
        leaving station between start and end, earliest first. Trips ending there aren't
        departures."""
        out = []
        d = start.date() - timedelta(days=1)
        while d <= end.date():
            active = self.services_on(d)
            for trip, i in self.stop_index.get(station.stop_id, []):
                st = trip.times[i]
                if trip.service_id not in active or not st.pickup or i == len(trip.times) - 1:
                    continue
                dt = _to_dt(d, st.dep)
                if start <= dt <= end:
                    out.append((dt, trip, i))
            d += timedelta(days=1)
        out.sort(key=lambda x: (x[0], x[1].route_id))
        return out

    def toward(self, trip: Trip) -> str:
        return trip.headsign or (self.stops[trip.times[-1].stop_id].name if trip.times else "")


def ride_summary(t: dict) -> str:
    s = f"{t['line_label']} toward {t['toward']} leaves {t['board_at']} at {t['depart']}"
    if t.get("get_off_at"):
        s += f" and arrives at {t['get_off_at']} at {t['arrive']}"
    status = t.get("status")
    if status and status != "scheduled":
        s += f" ({status})"
    return s + "."


def ride_to_dict(feed: MetroFeed, r: Ride, query_date: date | None = None) -> dict:
    line = feed.line(r.board_trip.route_id)
    dep, arr = r.depart_dt, r.arrive_dt
    d = {
        "line": line.code,
        "line_label": line.label,
        "toward": feed.toward(r.board_trip),
        "board_at": feed.stops[r.board.stop_id].name,
        "depart": fmt_time_on(dep, query_date),
        "get_off_at": feed.stops[r.alight.stop_id].name,
        "arrive": fmt_time_on(arr, query_date),
        "duration_min": int((arr - dep).total_seconds() // 60),
        "intermediate_stops": r.stops_between,
    }
    if query_date and dep.date() != query_date:
        d["depart_date"] = dep.date().isoformat()
    return {"summary": ride_summary(d), **d}
