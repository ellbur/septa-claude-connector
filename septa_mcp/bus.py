"""SEPTA buses (and trackless trolleys): schedules from SEPTA's bus GTFS feed.

The bus feed is far bigger than the others: ~45,000 trips and 2.8 million stop times,
about 15 times the Metro's. Parsed into a Python object per stop time, as the rail and
Metro feeds are, it would take several hundred MB. So on download `build` converts it once
into a compact zip of its own: small CSVs for routes, trips and stops, plus the stop times
as flat arrays of ints, which BusFeed loads with a few byte copies (~60 MB in memory).

Riders know a stop by its corner, named either way round: the feed calls the same
intersection "Broad St & Walnut St" for buses on Broad and "Walnut St & Broad St" for buses
on Walnut, and has a stop for each side of the street. Stops are grouped by their set of
streets into a BusStop, one per corner, served by every route and direction there.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sys
import zipfile
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from .gtfs import (
    OVERNIGHT_FROM,
    OVERNIGHT_PREFIX,
    AmbiguousStation,
    ServiceCalendar,
    StationNotFound,
    _to_dt,
    fmt_time_on,
    normalize,
)

# GTFS route_types kept: 3 = bus, 11 = trackless trolley (SEPTA's 59, 66 and 75).
BUS_ROUTE_TYPES = {"3", "11"}
NO_PICKUP, NO_DROPOFF = 1, 2  # bits in st_flags
SAME_CORNER_METERS = 250  # stops with the same streets this close are one corner

# Stop-times arrays: name -> typecode. st_* have one entry per stop time, trips' stop times
# contiguous and in order; stop_pos lists stop-time positions by stop.
_ARRAYS = {
    "st_trip": "i",  # trip index
    "st_stop": "i",  # stop index
    "st_time": "i",  # departure, seconds after the service day's midnight
    "st_seq": "H",  # stop_sequence, which TransitView's next_stop_sequence counts in
    "st_flags": "B",  # NO_PICKUP | NO_DROPOFF
    "trip_first": "i",  # trip index -> position of its first stop time
    "trip_count": "i",  # trip index -> number of stop times
    "stop_pos_start": "i",  # stop index -> start of its run in stop_pos (one extra at the end)
    "stop_pos": "i",  # stop-time positions, grouped by stop
}


def build(gtfs_zip: bytes) -> bytes:
    """Convert SEPTA's google_bus.zip into the compact zip BusFeed loads.

    Expects stop_times.txt to list each trip's stops together and in stop_sequence order,
    as SEPTA's does; raises ValueError otherwise.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(gtfs_zip)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        names = set(src.namelist())

        def rows(fname: str) -> list[dict]:
            if fname not in names:
                return []
            with src.open(fname) as fh:
                return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")))

        def write(fname: str, fields: list[str], keep: list[dict]) -> None:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fields, extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            w.writerows(keep)
            dst.writestr(fname, buf.getvalue())

        routes = [r for r in rows("routes.txt") if r.get("route_type") in BUS_ROUTE_TYPES]
        write("routes.txt", ["route_id", "route_short_name", "route_long_name", "route_type"], routes)
        route_ids = {r["route_id"] for r in routes}
        trips = [t for t in rows("trips.txt") if t["route_id"] in route_ids]
        write("trips.txt", ["trip_id", "route_id", "service_id", "trip_headsign", "direction_id"], trips)
        trip_index = {t["trip_id"]: i for i, t in enumerate(trips)}
        trip_route = [t["route_id"] for t in trips]

        a = {name: array(code) for name, code in _ARRAYS.items()}
        a["trip_first"] = array("i", [-1]) * len(trips)
        a["trip_count"] = array("i", [0]) * len(trips)
        stop_index: dict[str, int] = {}
        stop_routes: list[set[str]] = []
        st_trip, st_stop, st_time, st_seq, st_flags = (a[k] for k in ("st_trip", "st_stop", "st_time", "st_seq", "st_flags"))
        with src.open("stop_times.txt") as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""))
            cols = next(reader)
            c_trip, c_stop, c_seq = cols.index("trip_id"), cols.index("stop_id"), cols.index("stop_sequence")
            c_arr, c_dep = cols.index("arrival_time"), cols.index("departure_time")
            c_pick = cols.index("pickup_type") if "pickup_type" in cols else None
            c_drop = cols.index("drop_off_type") if "drop_off_type" in cols else None
            prev, last_seq = -1, -1
            for r in reader:
                ti = trip_index.get(r[c_trip])
                if ti is None:
                    continue
                seq = int(r[c_seq])
                if ti != prev:
                    if a["trip_first"][ti] != -1:
                        raise ValueError("stop_times.txt doesn't list each trip's stops together")
                    a["trip_first"][ti] = len(st_time)
                    prev = ti
                elif seq <= last_seq:
                    raise ValueError("stop_times.txt isn't in stop_sequence order")
                last_seq = seq
                t = r[c_dep] or r[c_arr]
                if not t:
                    raise ValueError("stop_times.txt has a stop without a time")
                h, m, s = t.split(":")
                si = stop_index.get(r[c_stop])
                if si is None:
                    si = stop_index[r[c_stop]] = len(stop_routes)
                    stop_routes.append(set())
                stop_routes[si].add(trip_route[ti])
                st_trip.append(ti)
                st_stop.append(si)
                st_time.append(int(h) * 3600 + int(m) * 60 + int(s))
                st_seq.append(min(seq, 65535))
                st_flags.append(
                    (NO_PICKUP if c_pick is not None and r[c_pick] == "1" else 0)
                    | (NO_DROPOFF if c_drop is not None and r[c_drop] == "1" else 0)
                )
                a["trip_count"][ti] += 1

        # Positions by stop: a counting sort of st_stop.
        start = array("i", [0]) * (len(stop_routes) + 1)
        for si in st_stop:
            start[si + 1] += 1
        for i in range(len(stop_routes)):
            start[i + 1] += start[i]
        a["stop_pos_start"] = start
        cursor = array("i", start)
        pos = array("i", [0]) * len(st_stop)
        for p, si in enumerate(st_stop):
            pos[cursor[si]] = p
            cursor[si] += 1
        a["stop_pos"] = pos

        stops = {s["stop_id"]: s for s in rows("stops.txt")}
        used = []
        for sid in sorted(stop_index, key=stop_index.get):
            s = dict(stops.get(sid) or {"stop_id": sid, "stop_name": sid})
            s["routes"] = " ".join(sorted(stop_routes[stop_index[sid]]))
            used.append(s)
        write("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon", "wheelchair_boarding", "routes"], used)

        for fname in ("calendar.txt", "calendar_dates.txt", "feed_info.txt"):
            if fname in names:
                dst.writestr(fname, src.read(fname))
        for name, arr in a.items():
            dst.writestr(f"{name}.bin", arr.tobytes())
        dst.writestr("arrays.json", json.dumps({"byteorder": sys.byteorder, "arrays": _ARRAYS}))
    return out.getvalue()


@dataclass(slots=True)
class BusTrip:
    trip_id: str
    route_id: str
    service_id: str
    headsign: str
    direction_id: str


@dataclass
class BusStop:
    """A corner: the stops there for every route and direction."""

    stop_id: str  # the lowest of its stops' ids
    name: str
    lat: float
    lon: float
    wheelchair: bool
    stops: list[int] = field(default_factory=list)  # stop indexes
    routes: set[str] = field(default_factory=set)
    words: frozenset[str] = frozenset()  # every word of its stops' names, for lookups


@dataclass
class BusRide:
    service_date: date
    trip: int
    board: int  # stop-time positions
    alight: int
    depart_dt: datetime
    arrive_dt: datetime


# Feed suffixes saying which side of the corner a stop is on (far side, near side,
# midblock), sometimes with a bay number or note: "- FS", "- 2 MBFS", "- MBNS layover".
_SIDE_SUFFIX = re.compile(r"\s+-\s+(?:\d+\s+)?(?:MBFS|MBNS|MB|FS|NS)\b.*$")
_SIDE_MARK = re.compile(r"\s-\s+(?:\d+\s+)?(MBFS|MBNS|MB|FS|NS)\b")
# Unmarked stops are near side, unless the stops on the cross street at the corner are at
# least this far behind (meters along the way buses go): stop coordinates are rough, and
# the cross street's stops are off its middle by half its width.
CROSS_STREET_CLEARLY_BEHIND = 15
# Words that don't pick out a corner: street types, and joining words in queries.
_STREET_WORDS = {
    "st", "av", "rd", "blvd", "dr", "ln", "pk", "pike", "pkwy", "hwy", "ct", "pl", "way", "ter", "cir", "sq",
}
_JOINING_WORDS = {"and", "at", "the", "corner", "of", "stop"}
# The number on a stop's sign (its stop_id): "10266", "#10266", "stop 10266", "stop no. 10266".
_STOP_NUMBER = re.compile(r"(?:stop\s*(?:id|number|no\.?|#)?\s*)?#?\s*(\d+)", re.IGNORECASE)


def _street_parts(name: str) -> list[str]:
    """The streets in a stop's name, its own street first: "Lombard St & Broad St"."""
    return [p.strip() for p in re.split(r"\s+(?:&|and|at)\s+", _SIDE_SUFFIX.sub("", name)) if p.strip()]


def _corner_streets(name: str) -> frozenset[str]:
    base = _SIDE_SUFFIX.sub("", name)
    return frozenset(n for n in (normalize(p) for p in re.split(r"\s+(?:&|and|at)\s+", base)) if n)


def _meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat1 - lat2) * 111_000
    dx = (lon1 - lon2) * 111_000 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


class BusFeed(ServiceCalendar):
    KIND = "bus stop"

    def __init__(self, blob: bytes, source: str = "", fetched_at: float | None = None):
        super().__init__(source, fetched_at)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = set(z.namelist())

            def rows(fname: str) -> list[dict]:
                if fname not in names:
                    return []
                with z.open(fname) as fh:
                    return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")))

            meta = json.loads(z.read("arrays.json"))
            for name, code in meta["arrays"].items():
                arr = array(code)
                arr.frombytes(z.read(f"{name}.bin"))
                if meta["byteorder"] != sys.byteorder:
                    arr.byteswap()
                setattr(self, name, arr)
            self.routes: dict[str, dict] = {r["route_id"]: r for r in rows("routes.txt")}
            self.trips = [
                BusTrip(r["trip_id"], r["route_id"], r["service_id"], r.get("trip_headsign", ""), r.get("direction_id", ""))
                for r in rows("trips.txt")
            ]
            self.stop_rows = rows("stops.txt")
            self._read_calendar(rows)
        self.stop_index = {s["stop_id"]: i for i, s in enumerate(self.stop_rows)}
        self._services: dict[date, set[str]] = {}
        self._where: dict[int, str | None] = {}
        self._group_corners()
        self._route_index = {}
        for rid, r in self.routes.items():
            for name in (rid, r.get("route_short_name") or ""):
                if name:
                    self._route_index.setdefault(name.lower(), rid)

    def _group_corners(self) -> None:
        by_streets: dict[frozenset[str], list[list[int]]] = defaultdict(list)
        for i, s in enumerate(self.stop_rows):
            clusters = by_streets[_corner_streets(s["stop_name"]) or frozenset([s["stop_id"]])]
            lat, lon = float(s.get("stop_lat") or 0), float(s.get("stop_lon") or 0)
            for c in clusters:
                f = self.stop_rows[c[0]]
                if _meters(lat, lon, float(f.get("stop_lat") or 0), float(f.get("stop_lon") or 0)) < SAME_CORNER_METERS:
                    c.append(i)
                    break
            else:
                clusters.append([i])

        self.corners: list[BusStop] = []
        self.corner_of = array("i", [0]) * len(self.stop_rows)
        for clusters in by_streets.values():
            for c in clusters:
                rows = [self.stop_rows[i] for i in c]
                names = Counter(_SIDE_SUFFIX.sub("", r["stop_name"]) for r in rows).most_common()
                top = names[0][1]
                corner = BusStop(
                    min((r["stop_id"] for r in rows), key=lambda x: (len(x), x)),
                    min((n for n, k in names if k == top), key=len),
                    sum(float(r.get("stop_lat") or 0) for r in rows) / len(rows),
                    sum(float(r.get("stop_lon") or 0) for r in rows) / len(rows),
                    any(r.get("wheelchair_boarding") == "1" for r in rows),
                    c,
                    {rt for r in rows for rt in (r.get("routes") or "").split()},
                    frozenset(w for r in rows for w in normalize(r["stop_name"]).split()),
                )
                for i in c:
                    self.corner_of[i] = len(self.corners)
                self.corners.append(corner)

        # Same name in different places (Main St & 3rd Av, in several towns): add the routes.
        by_name: dict[str, list[BusStop]] = defaultdict(list)
        for corner in self.corners:
            by_name[corner.name].append(corner)
        for same in by_name.values():
            if len(same) > 1:
                labels = Counter(self.routes_label(c.routes) for c in same)
                for corner in same:
                    label = self.routes_label(corner.routes)
                    if labels[label] > 1:  # same routes too: the stop number tells them apart
                        label += f"; stop {corner.stop_id}"
                    corner.name = f"{corner.name} ({label})"
        self._corner_by_id = {c.stop_id: c for c in self.corners}

    # -- routes ------------------------------------------------------------

    def resolve_routes(self, query: str) -> list[str]:
        """Route ids a rider's name for a bus route means: "42", "route 47M", "the K"."""
        q = query.strip().lower()
        if q in self._route_index:
            return [self._route_index[q]]
        cleaned = re.sub(r"^(?:(?:the|route|rt|bus|no|number)\.?\s*|#)+", "", q)
        cleaned = re.sub(r"\s+(?:bus|route)$", "", cleaned).strip()
        if cleaned in self._route_index:
            return [self._route_index[cleaned]]
        raise ValueError(
            f"No bus route matches '{query}'. SEPTA bus routes are numbers like 17 or 125, or "
            "letters like K and R."
        )

    def route_label(self, route_id: str) -> str:
        r = self.routes.get(route_id, {})
        short = r.get("route_short_name") or route_id
        label = f"Route {short}" if re.fullmatch(r"\d+[A-Z]?|[A-Z]", short) else short
        if r.get("route_type") == "11":
            label += " (trackless trolley)"
        return label

    def routes_label(self, routes) -> str:
        return ", ".join(sorted((self.routes.get(r, {}).get("route_short_name") or r for r in routes), key=_route_order))

    # -- stops -------------------------------------------------------------

    def resolve_stop(self, query: str, routes: set[str] | None = None) -> BusStop:
        """The corner a rider means: "Broad and Walnut", "Walnut St & Broad St", or the stop
        number on the sign ("stop 10266"). With routes, only corners they serve count."""
        q = query.strip()
        m = _STOP_NUMBER.fullmatch(q)
        if m and m.group(1) in self.stop_index:
            return self.corners[self.corner_of[self.stop_index[m.group(1)]]]
        if q in self._corner_by_id:
            return self._corner_by_id[q]
        ranked = self._matches(q, routes)
        if not ranked:
            raise StationNotFound(query, self._suggest(q, routes), self.KIND)
        best = [c for extra, c in ranked if extra == ranked[0][0]]
        if len(best) == 1:
            return best[0]
        raise AmbiguousStation(query, sorted(c.name for c in best)[:12])

    def search_stops(self, query: str, routes: set[str] | None = None, limit: int = 10) -> list[BusStop]:
        q = query.strip()
        m = _STOP_NUMBER.fullmatch(q)
        if m and m.group(1) in self.stop_index:
            return [self.corners[self.corner_of[self.stop_index[m.group(1)]]]]
        ranked = self._matches(q, routes)
        if ranked:
            return [c for _, c in ranked[:limit]]
        names = set(self._suggest(q, routes, limit))
        return [c for c in self._candidates(routes) if c.name in names][:limit]

    def _candidates(self, routes: set[str] | None) -> list[BusStop]:
        return [c for c in self.corners if not routes or c.routes & routes]

    def _matches(self, query: str, routes: set[str] | None) -> list[tuple[int, BusStop]]:
        """Corners whose names have every word of query, fewest other words first (street
        types don't count)."""
        words = set(normalize(query).split()) - _JOINING_WORDS
        if not words:
            return []
        ranked = [
            (len(c.words - words - _STREET_WORDS), c) for c in self._candidates(routes) if words <= c.words
        ]
        ranked.sort(key=lambda x: (x[0], x[1].name))
        return ranked

    def _suggest(self, query: str, routes: set[str] | None, limit: int = 4) -> list[str]:
        n = normalize(query)
        scored = sorted(
            ((SequenceMatcher(None, n, normalize(c.name)).ratio(), c.name) for c in self._candidates(routes)),
            reverse=True,
        )
        return [name for score, name in scored[:limit] if score >= 0.5]

    def stop_name(self, stop: int) -> str:
        return self.stop_rows[stop]["stop_name"]

    def where(self, stop: int) -> str | None:
        """Which corner of the intersection a stop is on, for riders: "northeast corner
        (north side of Lombard St, just east of Broad St), for westbound buses". None when
        the feed doesn't show which way buses go there."""
        if stop not in self._where:
            spot = self.locate(stop)
            self._where[stop] = _describe(spot) if spot else None
        return self._where[stop]

    def locate(self, stop: int) -> dict | None:
        """Where a stop sits at its corner, worked out from the feed: the street it's on and
        the cross street, the compass direction buses travel (heading), the side of the
        street (buses board on their right), and whether it's before the cross street
        (near side), past it (far side) or midblock. None when that can't be told."""
        name = self.stop_name(stop)
        parts = _street_parts(name)
        if len(parts) != 2 or not float(self.stop_rows[stop].get("stop_lat") or 0):
            return None
        own, cross = parts
        lat0 = float(self.stop_rows[stop]["stop_lat"])
        sx, sy = self._xy(stop, lat0)
        mine = normalize(own)

        def on_my_street(i: int) -> bool:
            p = _street_parts(self.stop_name(i))
            return bool(p) and normalize(p[0]) == mine

        # Which way buses go: toward the next stop and from the last one, counting only
        # stops on this street (a bus turning here is on another street at one end).
        dx = dy = 0.0
        seen: set[tuple[int, int]] = set()
        lo, hi = self.stop_pos_start[stop], self.stop_pos_start[stop + 1]
        for k in range(lo, min(hi, lo + 200)):
            p = self.stop_pos[k]
            ti = self.st_trip[p]
            prev = self.st_stop[p - 1] if p > self.trip_first[ti] else -1
            nxt = self.st_stop[p + 1] if p < self.trip_first[ti] + self.trip_count[ti] - 1 else -1
            if (prev, nxt) in seen:
                continue
            seen.add((prev, nxt))
            segs = [(a, b) for a, b in ((prev, stop), (stop, nxt)) if -1 not in (a, b) and on_my_street(a if a != stop else b)]
            if not segs and -1 not in (prev, nxt):
                segs = [(prev, nxt)]
            for a, b in segs:
                (ax, ay), (bx, by) = self._xy(a, lat0), self._xy(b, lat0)
                n = math.hypot(bx - ax, by - ay)
                if n > 1:
                    dx, dy = dx + (bx - ax) / n, dy + (by - ay) / n
        n = math.hypot(dx, dy)
        if n < 0.5:  # no neighbours, or buses both ways through one stop
            return None
        dx, dy = dx / n, dy / n
        spot = {
            "street": own,
            "cross_street": cross,
            "heading": _compass(dx, dy, diagonal=True),
            "side": _compass(dy, -dx),  # the right of the way buses go
        }

        # Before or past the cross street. The feed marks far-side and midblock stops
        # ("- FS", "- MBNS"), and a stop without a mark is near side (SEPTA's usual stop),
        # unless the stops on the cross street clearly say otherwise.
        m = _SIDE_MARK.search(name)
        mark = m.group(1) if m else ""
        if mark == "MB":
            spot["position"] = "midblock"
            return spot
        if mark:
            ahead = mark.endswith("NS")
        else:
            corner = self.corners[self.corner_of[stop]]
            along = [
                (x - sx) * dx + (y - sy) * dy
                for x, y in (self._xy(i, lat0) for i in corner.stops if not on_my_street(i))
            ]
            mean = sum(along) / len(along) if along else 0.0
            ahead = mean > -CROSS_STREET_CLEARLY_BEHIND
        spot["position"] = ("midblock " if mark.startswith("MB") else "") + ("near side" if ahead else "far side")
        # The corner is across the cross street from where buses come from (near side) or go.
        end = _compass(*((-dx, -dy) if ahead else (dx, dy)), axis_of=spot["side"])
        spot["of_cross_street"] = end
        spot["corner"] = _corner_word(spot["side"], end)
        return spot

    def _xy(self, stop: int, lat0: float) -> tuple[float, float]:
        """A stop's position in meters, east and north."""
        r = self.stop_rows[stop]
        lat, lon = float(r.get("stop_lat") or 0), float(r.get("stop_lon") or 0)
        return lon * 111_000 * math.cos(math.radians(lat0)), lat * 111_000

    def corner_name(self, stop_id: str | None) -> str | None:
        i = self.stop_index.get(str(stop_id)) if stop_id is not None else None
        return self.corners[self.corner_of[i]].name if i is not None else None

    # -- schedule ----------------------------------------------------------

    def services_on(self, d: date) -> set[str]:
        if d not in self._services:
            self._services[d] = super().services_on(d)
        return self._services[d]

    def add_overnight(self, prev: "BusFeed", day: date) -> int:
        """RailFeed.add_overnight for buses: copy in prev's late trips of service day `day`."""
        if self.services_on(day):
            return 0
        active = prev.services_on(day)
        added: list[tuple[BusTrip, list[tuple[int, int, int, int]]]] = []
        mine = {t.trip_id for t in self.trips}
        for ti, t in enumerate(prev.trips):
            first, n = prev.trip_first[ti], prev.trip_count[ti]
            if t.service_id not in active or not n or prev.st_time[first + n - 1] < OVERNIGHT_FROM:
                continue
            times = []
            for p in range(first, first + n):
                s = self.stop_index.get(prev.stop_rows[prev.st_stop[p]]["stop_id"])
                if s is not None:
                    times.append((s, prev.st_time[p], prev.st_seq[p], prev.st_flags[p]))
            if len(times) < 2:
                continue
            tid = t.trip_id if t.trip_id not in mine else OVERNIGHT_PREFIX + t.trip_id
            sid = OVERNIGHT_PREFIX + t.service_id
            self.cal_add.setdefault(sid, set()).add(day)
            if t.route_id not in self.routes and t.route_id in prev.routes:
                self.routes[t.route_id] = prev.routes[t.route_id]
            added.append((BusTrip(tid, t.route_id, sid, t.headsign, t.direction_id), times))
        if not added:
            return 0
        extra: dict[int, list[int]] = defaultdict(list)
        for trip, times in added:
            ti = len(self.trips)
            self.trips.append(trip)
            self.trip_first.append(len(self.st_trip))
            self.trip_count.append(len(times))
            for s, t, seq, flags in times:
                extra[s].append(len(self.st_trip))
                self.st_trip.append(ti)
                self.st_stop.append(s)
                self.st_time.append(t)
                self.st_seq.append(seq)
                self.st_flags.append(flags)
        pos, starts = array("i"), array("i", [0])
        for s in range(len(self.stop_pos_start) - 1):
            pos.extend(self.stop_pos[self.stop_pos_start[s]:self.stop_pos_start[s + 1]])
            pos.extend(extra.get(s, ()))
            starts.append(len(pos))
        self.stop_pos, self.stop_pos_start = pos, starts
        self._services.clear()
        return len(added)

    def departures(
        self, corner: BusStop, start: datetime, end: datetime, routes: set[str] | None = None
    ) -> list[tuple[datetime, int, int]]:
        """(scheduled departure, trip index, stop-time position) for every bus leaving
        corner between start and end, earliest first. Buses ending there aren't departures."""
        out = []
        d = start.date() - timedelta(days=1)
        while d <= end.date():
            active = self.services_on(d)
            for s in corner.stops:
                for k in range(self.stop_pos_start[s], self.stop_pos_start[s + 1]):
                    p = self.stop_pos[k]
                    ti = self.st_trip[p]
                    trip = self.trips[ti]
                    if trip.service_id not in active or (routes and trip.route_id not in routes):
                        continue
                    if p == self.trip_first[ti] + self.trip_count[ti] - 1 or self.st_flags[p] & NO_PICKUP:
                        continue
                    dt = _to_dt(d, self.st_time[p])
                    if start <= dt <= end:
                        out.append((dt, ti, p))
            d += timedelta(days=1)
        out.sort(key=lambda x: (x[0], self.trips[x[1]].route_id))
        return out

    def rides(
        self, origin: BusStop, dest: BusStop, start: datetime, end: datetime, routes: set[str] | None = None
    ) -> list[BusRide]:
        """Buses from origin to dest, leaving between start and end, without changing."""
        from_stops, to_stops = set(origin.stops), set(dest.stops)
        out = []
        for dt, ti, p in self.departures(origin, start, end, routes):
            for q in range(p + 1, self.trip_first[ti] + self.trip_count[ti]):
                s = self.st_stop[q]
                if s in from_stops:
                    break  # back where it started: board here next time instead
                if s in to_stops and not self.st_flags[q] & NO_DROPOFF:
                    arrive = dt + timedelta(seconds=self.st_time[q] - self.st_time[p])
                    out.append(BusRide(self._service_day(dt, p), ti, p, q, dt, arrive))
                    break
        return out

    def _service_day(self, dt: datetime, p: int) -> date:
        return (dt - timedelta(seconds=self.st_time[p])).date()

    def toward(self, ti: int) -> str:
        trip = self.trips[ti]
        last = self.trip_first[ti] + self.trip_count[ti] - 1
        return trip.headsign or self.corners[self.corner_of[self.st_stop[last]]].name


_CARDINALS = [("north", 0, 1), ("east", 1, 0), ("south", 0, -1), ("west", -1, 0)]


def _compass(x: float, y: float, diagonal: bool = False, axis_of: str | None = None) -> str:
    """The compass direction nearest (x, y): "north", "east"... With diagonal, also
    "northeast" and so on. With axis_of, only the two directions across that one's axis
    (for a corner: north side and then east or west of the cross street)."""
    if diagonal:
        names = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
        return names[round(math.degrees(math.atan2(x, y)) % 360 / 45) % 8]
    options = _CARDINALS
    if axis_of:
        ns = axis_of in ("north", "south")
        options = [c for c in _CARDINALS if (c[1] != 0) == ns]
    return max(options, key=lambda c: c[1] * x + c[2] * y)[0]


def _corner_word(a: str, b: str) -> str:
    ns, ew = (a, b) if a in ("north", "south") else (b, a)
    return ns + ew


def _describe(spot: dict) -> str:
    buses = f"for {spot['heading']}bound buses"
    side = f"{spot['side']} side of {spot['street']}"
    if spot["position"] == "midblock":
        return f"{side}, midblock near {spot['cross_street']}, {buses}"
    off = f"{spot['of_cross_street']} of {spot['cross_street']}"
    if spot["position"].startswith("midblock"):
        return f"{side}, midblock {off} ({spot['position'].split(' ', 1)[1]}), {buses}"
    return f"{spot['corner']} corner ({side}, just {off}; {spot['position']}), {buses}"


def _route_order(short: str) -> tuple:
    m = re.match(r"(\d+)(.*)", short)
    return (0, int(m.group(1)), m.group(2)) if m else (1, 0, short)


def ride_to_dict(feed: BusFeed, r: BusRide, query_date: date | None = None) -> dict:
    trip = feed.trips[r.trip]
    board, alight = feed.st_stop[r.board], feed.st_stop[r.alight]
    d: dict = {
        "route": feed.routes.get(trip.route_id, {}).get("route_short_name") or trip.route_id,
        "route_label": feed.route_label(trip.route_id),
        "toward": feed.toward(r.trip),
        "board_at": feed.corners[feed.corner_of[board]].name,
        # The stop's own name, which corner it's on, and the number on its sign.
        "board_stop": feed.stop_name(board),
        "board_stop_where": feed.where(board),
        "board_stop_id": feed.stop_rows[board]["stop_id"],
        "depart": fmt_time_on(r.depart_dt, query_date),
        "get_off_at": feed.corners[feed.corner_of[alight]].name,
        "arrive": fmt_time_on(r.arrive_dt, query_date),
        "duration_min": int((r.arrive_dt - r.depart_dt).total_seconds() // 60),
        "stops": r.alight - r.board - 1,
    }
    if d["board_stop_where"] is None:
        del d["board_stop_where"]
    if query_date and r.depart_dt.date() != query_date:
        d["depart_date"] = r.depart_dt.date().isoformat()
    summary = (
        f"{d['route_label']} toward {d['toward']} leaves {d['board_at']} at {d['depart']} "
        f"and arrives at {d['get_off_at']} at {d['arrive']}."
    )
    return {"summary": summary, **d}
