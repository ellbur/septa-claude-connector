"""Load SEPTA's GTFS feeds and answer schedule questions from them.

The rail feed is small (~150 stations, ~2,300 trips, ~35,000 stop times), so it is
parsed straight into memory; only the downloaded zip is cached on disk. The Metro
(subway and trolley) feed is cut out of SEPTA's much larger bus feed on download and
handled the same way by metro.MetroFeed.

SEPTA-specific wrinkle handled here: most Regional Rail trains "through-run" across
Center City and change line names on the way (a West Trenton train becomes a
Media/Wawa train at Suburban Station). The feed models that as two separate trips
joined by an in-seat transfer (transfers.txt, transfer_type=4). A naive query for
"trips that serve both Jefferson and Swarthmore" misses almost all of them, so trips
are chained along those in-seat links before searching.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TZ = ZoneInfo("America/New_York")

# Official URL first, GitHub release mirror second. Either may be a bundle
# (gtfs_public.zip containing google_rail.zip and google_bus.zip) or the rail zip itself.
DEFAULT_GTFS_URLS = [
    "https://www3.septa.org/developer/gtfs_public.zip",
    "https://github.com/septadev/GTFS/releases/latest/download/gtfs_public.zip",
]

USER_AGENT = "septa-mcp/0.1 (+https://github.com/septadev/GTFS)"

# GTFS route_types kept from the bus feed for the Metro: 0 = trolley/light rail,
# 1 = subway (SEPTA files the Norristown High Speed Line there too).
METRO_ROUTE_TYPES = {"0", "1"}


class StationNotFound(ValueError):
    def __init__(self, query: str, suggestions: list[str], what: str = "Regional Rail station"):
        self.query = query
        self.suggestions = suggestions
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(f"No {what} matches '{query}'.{hint}")


class FeedUnavailable(RuntimeError):
    pass


class AmbiguousStation(ValueError):
    def __init__(self, query: str, candidates: list[str]):
        self.query = query
        self.candidates = candidates
        super().__init__(
            f"'{query}' matches several stations: {', '.join(candidates)}. Please pick one."
        )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Stop:
    stop_id: str
    name: str
    lat: float
    lon: float
    wheelchair: bool
    routes: set[str] = field(default_factory=set)


@dataclass(slots=True)
class StopTime:
    stop_id: str
    seq: int
    arr: int  # seconds after service-day midnight (can exceed 86400)
    dep: int
    pickup: bool
    dropoff: bool


@dataclass(slots=True)
class Trip:
    trip_id: str
    route_id: str
    service_id: str
    headsign: str
    train: str  # public train number, e.g. "2338"
    direction_id: str
    times: list[StopTime] = field(default_factory=list)


@dataclass
class Ride:
    """One way to get from A to B on a single physical train (possibly several linked trips)."""

    service_date: date
    train: str
    trips: list[Trip]
    board: StopTime
    alight: StopTime
    board_trip: Trip
    alight_trip: Trip
    stops_between: int

    @property
    def depart_dt(self) -> datetime:
        return _to_dt(self.service_date, self.board.dep)

    @property
    def arrive_dt(self) -> datetime:
        return _to_dt(self.service_date, self.alight.arr)


def _to_dt(service_date: date, secs: int) -> datetime:
    # GTFS times are measured from "noon minus 12h" on the service day; for an
    # America/New_York feed that is local midnight except on DST change days,
    # where the one-hour difference is irrelevant for rider-facing output.
    base = datetime(service_date.year, service_date.month, service_date.day, tzinfo=TZ)
    return base + timedelta(seconds=secs)


def _parse_time(s: str) -> int:
    h, m, sec = s.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


# ---------------------------------------------------------------------------
# Station name matching
# ---------------------------------------------------------------------------

_ABBREV = [
    (r"\bstreet\b", "st"),
    (r"\bavenue\b", "av"),
    (r"\bave\b", "av"),
    (r"\blane\b", "ln"),
    (r"\bsaint\b", "st"),
    (r"\bmount\b", "mt"),
    (r"\bdelaware\b", "de"),
    (r"\bthirtieth\b", "30th"),
    (r"\btransportation center\b", "tc"),
    (r"\btransit center\b", "tc"),
    (r"\bt c\b", "tc"),
    (r"\bstation\b", ""),
    (r"\bsta\b", ""),
    (r"\buniversity\b", "u"),
    (r"\buniv\b", "u"),
]

# Common names riders (and Claude) use that don't match GTFS stop_name.
_ALIASES = {
    "market east": "Jefferson Station",
    "jefferson": "Jefferson Station",
    "30th st": "Gray 30th St Station",
    "30th": "Gray 30th St Station",
    "gray 30th": "Gray 30th St Station",
    "amtrak 30th st": "Gray 30th St Station",
    "university city": "Penn Medicine Station",
    "penn medicine": "Penn Medicine Station",
    "temple": "Temple University",
    "temple u": "Temple University",
    "suburban": "Suburban Station",
    "trenton": "Trenton Transit Center",
    "chester": "Chester Transit Center",
    "claymont": "Claymont Transit Center",
    "newark": "Newark DE",
    "fern rock": "Fern Rock Transit Center",
    "norristown tc": "Norristown Transit Center",
    "morton rutledge": "Morton",
    "prospect park moore": "Prospect Park",
    "elwyn station": "Elwyn",
    "main street": "Main St",
}

# Queries that genuinely mean several stations.
_GROUPS = {
    "airport": [
        "Airport Terminal A",
        "Airport Terminal B",
        "Airport Terminals C & D",
        "Airport Terminals E & F",
    ],
    "chestnut hill": ["Chestnut Hill East", "Chestnut Hill West"],
    "norristown": ["Norristown Transit Center", "Norristown Elm Street"],
    "north philadelphia": ["North Philadelphia Amtrak", "North Philadelphia Septa"],
    "center city": ["Jefferson Station", "Suburban Station", "Gray 30th St Station"],
}


# Stops in the feed that riders can't use, kept out of name lookups.
_HIDDEN_STOP = re.compile(r"not a stop|employees only", re.I)


def normalize(name: str) -> str:
    s = name.lower().replace("&", " and ").replace("'", "")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for pat, rep in _ABBREV:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------


class ServiceCalendar:
    """Which services run on a day, the dates a feed covers, and where it came from: the
    parts every feed type (rail, Metro, bus) shares."""

    def __init__(self, source: str = "", fetched_at: float | None = None):
        self.source = source
        self.fetched_at = fetched_at or time.time()
        self.calendar: dict[str, dict] = {}
        self.cal_add: dict[str, set[date]] = {}
        self.cal_remove: dict[str, set[date]] = {}
        self.feed_info: dict = {}
        # Service days whose late trips were copied in from the feed before this one.
        self.overnight_added: set[date] = set()

    def _read_calendar(self, rows) -> None:
        """Load calendar.txt, calendar_dates.txt and feed_info.txt via rows(fname)."""
        for r in rows("calendar.txt"):
            self.calendar[r["service_id"]] = r
        for r in rows("calendar_dates.txt"):
            d = datetime.strptime(r["date"], "%Y%m%d").date()
            target = self.cal_add if r["exception_type"] == "1" else self.cal_remove
            target.setdefault(r["service_id"], set()).add(d)
        fi = rows("feed_info.txt")
        self.feed_info = fi[0] if fi else {}

    def feed_range(self) -> tuple[date | None, date | None]:
        def p(k):
            v = self.feed_info.get(k)
            return datetime.strptime(v, "%Y%m%d").date() if v else None

        start, end = p("feed_start_date"), p("feed_end_date")
        if not start or not end:
            all_dates = [
                datetime.strptime(c[k], "%Y%m%d").date()
                for c in self.calendar.values()
                for k in ("start_date", "end_date")
            ]
            if all_dates:
                start, end = start or min(all_dates), end or max(all_dates)
        return start, end

    def timetable_changes(self) -> list[date]:
        """Dates after the feed's start on which a new timetable period begins.

        SEPTA revises a feed in place: the Oct 18 schedule change went out as an edit to
        the feed starting Sep 27, so these show changes the feed's version date hides.
        """
        start, end = self.feed_range()
        starts = {datetime.strptime(c["start_date"], "%Y%m%d").date() for c in self.calendar.values()}
        return sorted(d for d in starts if (not start or d > start) and (not end or d <= end))

    def summary(self) -> dict:
        start, end = self.feed_range()
        out = {
            "feed_version": self.feed_info.get("feed_version"),
            "valid_from": start.isoformat() if start else None,
            "valid_until": end.isoformat() if end else None,
            "timetable_changes_included": [d.isoformat() for d in self.timetable_changes()],
            "downloaded_from": self.source,
            "downloaded_at": datetime.fromtimestamp(self.fetched_at, TZ).isoformat(timespec="minutes"),
            "note": (
                "SEPTA names a feed version after the date the feed starts, not when it was "
                "published, and adds later schedule changes to the same feed (see "
                "timetable_changes_included). An early version date doesn't mean the data is stale."
            ),
        }
        if self.source.startswith("http"):
            out["note"] += " The server checks SEPTA for newer feeds weekly."
        return out

    def services_on(self, d: date) -> set[str]:
        wd = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][d.weekday()]
        active = set()
        for sid, c in self.calendar.items():
            start = datetime.strptime(c["start_date"], "%Y%m%d").date()
            end = datetime.strptime(c["end_date"], "%Y%m%d").date()
            if start <= d <= end and c.get(wd) == "1":
                active.add(sid)
        for sid, ds in self.cal_add.items():
            if d in ds:
                active.add(sid)
        for sid, ds in self.cal_remove.items():
            if d in ds:
                active.discard(sid)
        return active


# A feed's first day starts with the previous feed's late trips still running (see
# add_overnight): those of its last service day still running at this time of night,
# which covers the 45-minute look-back of the departure boards.
OVERNIGHT_FROM = 22 * 3600
OVERNIGHT_PREFIX = "prev:"


def _zip_rows(z: zipfile.ZipFile, fname: str) -> list[dict]:
    if fname not in z.namelist():
        return []
    with z.open(fname) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")))


def feed_range_of(gtfs_zip: bytes) -> tuple[date | None, date | None]:
    """The dates a GTFS zip covers, read from its calendar alone (no stops or trips)."""
    cal = ServiceCalendar()
    with zipfile.ZipFile(io.BytesIO(gtfs_zip)) as z:
        cal._read_calendar(lambda fname: _zip_rows(z, fname))
    return cal.feed_range()


def _covers(rng: tuple[date | None, date | None], day: date) -> bool:
    start, end = rng
    return bool(start) and start <= day and (not end or day <= end)


class RailFeed(ServiceCalendar):
    # What resolve_station looks up (for error messages), and the names riders use for it.
    KIND = "Regional Rail station"
    ALIASES = _ALIASES
    GROUPS = _GROUPS

    def __init__(self, rail_zip: bytes, source: str = "", fetched_at: float | None = None):
        super().__init__(source, fetched_at)
        self.stops: dict[str, Stop] = {}
        self.routes: dict[str, dict] = {}
        self.trips: dict[str, Trip] = {}
        self.next_trip: dict[str, str] = {}
        self.prev_trip: dict[str, str] = {}
        # Other stop_ids that resolve to a stop (e.g. a Metro station's platforms).
        self.stop_alias: dict[str, str] = {}
        # stop_id -> [(nearby stop_id, minutes to walk there)], for changing between lines.
        self.walk_links: dict[str, list[tuple[str, int]]] = {}
        self._between_cache: dict[tuple[str, str], list[Stop]] = {}
        self._load(rail_zip)
        self._name_index = {normalize(s.name): s for s in self.stops.values() if not _HIDDEN_STOP.search(s.name)}

    # -- loading -----------------------------------------------------------

    def _load(self, rail_zip: bytes) -> None:
        with zipfile.ZipFile(io.BytesIO(rail_zip)) as z:
            names = set(z.namelist())

            def rows(fname: str):
                if fname not in names:
                    return []
                with z.open(fname) as fh:
                    text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                    return list(csv.DictReader(text))

            parents = {}
            for r in rows("stops.txt"):
                if r.get("parent_station"):
                    parents[r["stop_id"]] = r["parent_station"]
                self.stops[r["stop_id"]] = Stop(
                    r["stop_id"],
                    r["stop_name"].strip(),
                    float(r.get("stop_lat") or 0),
                    float(r.get("stop_lon") or 0),
                    r.get("wheelchair_boarding") == "1",
                )
            for r in rows("routes.txt"):
                self.routes[r["route_id"]] = r
            for r in rows("trips.txt"):
                short = r.get("trip_short_name") or ""
                digits = re.sub(r"\D", "", short) or r.get("block_id") or short
                self.trips[r["trip_id"]] = Trip(
                    r["trip_id"],
                    r["route_id"],
                    r["service_id"],
                    r.get("trip_headsign", ""),
                    digits,
                    r.get("direction_id", ""),
                )
            for r in rows("stop_times.txt"):
                t = self.trips.get(r["trip_id"])
                if not t:
                    continue
                arr = r["arrival_time"] or r["departure_time"]
                dep = r["departure_time"] or r["arrival_time"]
                if not arr:
                    continue
                t.times.append(
                    StopTime(
                        r["stop_id"],
                        int(r["stop_sequence"]),
                        _parse_time(arr),
                        _parse_time(dep),
                        r.get("pickup_type", "") not in ("1",),
                        r.get("drop_off_type", "") not in ("1",),
                    )
                )
            for t in self.trips.values():
                t.times.sort(key=lambda st: st.seq)
            self._merge_platforms(parents)
            for t in self.trips.values():
                for st in t.times:
                    if st.stop_id in self.stops:
                        self.stops[st.stop_id].routes.add(t.route_id)

            self._read_calendar(rows)
            for r in rows("transfers.txt"):
                # transfer_type 4 = in-seat transfer: same physical train continues.
                if r.get("transfer_type") == "4" and r.get("from_trip_id") and r.get("to_trip_id"):
                    self.next_trip[r["from_trip_id"]] = r["to_trip_id"]
                    self.prev_trip[r["to_trip_id"]] = r["from_trip_id"]

        # Per-stop index of (trip, position) for fast lookups.
        self.stop_index: dict[str, list[tuple[Trip, int]]] = {}
        for t in self.trips.values():
            for i, st in enumerate(t.times):
                self.stop_index.setdefault(st.stop_id, []).append((t, i))

    def add_overnight(self, prev: "RailFeed", day: date) -> int:
        """Copy in prev's trips of service day `day` that run late into the night (past
        OVERNIGHT_FROM), for a feed that starts the next day. Those trips run after
        midnight on this feed's first day, but only the feed they belong to has them, so
        without this they'd vanish from answers at midnight. Returns how many were added."""
        if self.services_on(day):  # this feed has that day itself
            return 0
        active = prev.services_on(day)
        platforms = getattr(prev, "platforms", {})

        def stop_here(sid: str) -> str | None:
            for raw in platforms.get(sid, [sid]):
                mine = self.stop_alias.get(raw, raw)
                if mine in self.stops:
                    return mine
            return None

        renamed: dict[str, str] = {}
        for t in prev.trips.values():
            if t.service_id not in active or not t.times or t.times[-1].arr < OVERNIGHT_FROM:
                continue
            times: list[StopTime] = []
            for st in t.times:
                sid = stop_here(st.stop_id)
                if sid is None:
                    continue
                if times and times[-1].stop_id == sid:  # two platforms of one station in a row
                    times[-1].dep, times[-1].pickup = st.dep, times[-1].pickup or st.pickup
                    continue
                times.append(StopTime(sid, st.seq, st.arr, st.dep, st.pickup, st.dropoff))
            if len(times) < 2:
                continue
            # Keep the trip_id (live vehicle data names trips by it) unless this feed uses it.
            tid = t.trip_id if t.trip_id not in self.trips else OVERNIGHT_PREFIX + t.trip_id
            renamed[t.trip_id] = tid
            sid = OVERNIGHT_PREFIX + t.service_id
            self.cal_add.setdefault(sid, set()).add(day)
            if t.route_id not in self.routes and t.route_id in prev.routes:
                self.routes[t.route_id] = prev.routes[t.route_id]
            trip = Trip(tid, t.route_id, sid, t.headsign, t.train, t.direction_id, times)
            self.trips[tid] = trip
            for i, st in enumerate(times):
                self.stop_index.setdefault(st.stop_id, []).append((trip, i))
                self.stops[st.stop_id].routes.add(t.route_id)
        for old, new in renamed.items():
            nxt = prev.next_trip.get(old)
            if nxt in renamed:
                self.next_trip[new] = renamed[nxt]
                self.prev_trip[renamed[nxt]] = new
        self._between_cache.clear()
        return len(renamed)

    def _merge_platforms(self, parents: dict[str, str]) -> None:
        """Hook for feeds whose stations are split into several stops (see MetroFeed);
        Regional Rail has one stop per station."""

    # -- metadata ----------------------------------------------------------

    def route_name(self, route_id: str) -> str:
        r = self.routes.get(route_id, {})
        return r.get("route_long_name") or r.get("route_short_name") or route_id

    # -- stations ----------------------------------------------------------

    def resolve_station(self, query: str, routes: set[str] | None = None) -> Stop:
        """The station `query` names. With routes, only stations those routes serve count."""
        q = query.strip()
        q = self.stop_alias.get(q, q)
        if q in self.stops:  # a stop_id
            return self.stops[q]
        n = normalize(q)
        if not n:
            raise StationNotFound(query, [], self.KIND)
        index = self._name_index
        if routes:
            index = {k: s for k, s in index.items() if s.routes & routes}
        if n in index:
            return index[n]
        group = [name for name in self.GROUPS.get(n, []) if normalize(name) in index]
        if len(group) > 1:
            raise AmbiguousStation(query, group)
        alias = group[0] if group else self.ALIASES.get(n)
        if alias and normalize(alias) in index:
            return index[normalize(alias)]

        # Whole-word containment: "swarthmore college" -> no; "gwynedd" -> Gwynedd Valley.
        qwords = n.split()
        contains = [
            s for key, s in index.items()
            if all(re.search(rf"\b{re.escape(w)}\b", key) for w in qwords)
        ]
        if len(contains) == 1:
            return contains[0]
        if len(contains) > 1:
            raise AmbiguousStation(query, sorted(s.name for s in contains))

        scored = sorted(
            ((SequenceMatcher(None, n, key).ratio(), s) for key, s in index.items()),
            key=lambda x: -x[0],
        )
        if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[0][0] - scored[1][0] > 0.05):
            return scored[0][1]
        raise StationNotFound(query, [s.name for score, s in scored[:4] if score >= 0.5], self.KIND)

    def search_stations(self, query: str, limit: int = 8, routes: set[str] | None = None) -> list[Stop]:
        index = self._name_index
        if routes:
            index = {k: s for k, s in index.items() if s.routes & routes}
        n = normalize(query)
        if not n:
            return sorted(index.values(), key=lambda s: s.name)[:limit]
        out = []
        for key, s in index.items():
            score = SequenceMatcher(None, n, key).ratio()
            if n in key:
                score += 1
            if any(n == normalize(a) for a, target in self.ALIASES.items() if target == s.name):
                score += 2
            out.append((score, s))
        if n in self.GROUPS:
            names = set(self.GROUPS[n])
            out = [(sc + (3 if s.name in names else 0), s) for sc, s in out]
        out.sort(key=lambda x: -x[0])
        return [s for sc, s in out[:limit] if sc > 0.45]

    # -- schedule search ---------------------------------------------------

    def _chain_forward(self, trip: Trip, active: set[str]):
        """Yield trip, then each in-seat continuation that also runs that day."""
        seen = set()
        while trip and trip.trip_id not in seen:
            seen.add(trip.trip_id)
            yield trip
            nxt = self.next_trip.get(trip.trip_id)
            trip = self.trips.get(nxt) if nxt else None
            if trip and trip.service_id not in active:
                break

    def rides_on_service_day(
        self, origin: Stop, dest: Stop, service_date: date
    ) -> list[Ride]:
        active = self.services_on(service_date)
        rides: list[Ride] = []
        for trip, i in self.stop_index.get(origin.stop_id, []):
            if trip.service_id not in active:
                continue
            board = trip.times[i]
            if not board.pickup:
                continue
            # A train passing the transfer point appears at the end of one trip and the
            # start of the next; only board on the segment the train departs on.
            if i == len(trip.times) - 1:
                continue
            stops_between = 0
            found = None
            first = True
            for seg in self._chain_forward(trip, active):
                start = i + 1 if first else 1  # skip duplicated junction stop
                first = False
                for st in seg.times[start:]:
                    if st.stop_id == dest.stop_id and st.dropoff:
                        found = (seg, st)
                        break
                    if st.stop_id == origin.stop_id:
                        break  # looped back; not a sensible ride
                    stops_between += 1
                if found:
                    break
            if found:
                seg, st = found
                chain = list(self._chain_until(trip, seg, active))
                rides.append(
                    Ride(service_date, trip.train, chain, board, st, trip, seg, stops_between)
                )
        return rides

    def _chain_until(self, start: Trip, end: Trip, active: set[str]):
        for t in self._chain_forward(start, active):
            yield t
            if t is end:
                return

    def direct_rides(
        self, origin: Stop, dest: Stop, window_start: datetime, window_end: datetime
    ) -> list[Ride]:
        rides = []
        d = window_start.date() - timedelta(days=1)
        while d <= window_end.date():
            for r in self.rides_on_service_day(origin, dest, d):
                if window_start <= r.depart_dt <= window_end:
                    rides.append(r)
            d += timedelta(days=1)
        rides.sort(key=lambda r: (r.depart_dt, r.arrive_dt))
        # De-duplicate (same train boarding time) defensively.
        seen, out = set(), []
        for r in rides:
            k = (r.depart_dt, r.train)
            if k not in seen:
                seen.add(k)
                out.append(r)
        return out

    def departures(self, stop: Stop, window_start: datetime, window_end: datetime) -> list[dict]:
        """Every train scheduled to leave stop in the window, in any direction:
        {train, depart (datetime), final_stop}, by departure time."""
        out, seen = [], set()
        d = window_start.date() - timedelta(days=1)
        while d <= window_end.date():
            active = self.services_on(d)
            for trip, i in self.stop_index.get(stop.stop_id, []):
                if trip.service_id not in active or i == len(trip.times) - 1 or not trip.times[i].pickup:
                    continue
                dep = _to_dt(d, trip.times[i].dep)
                if window_start <= dep <= window_end and (trip.train, dep) not in seen:
                    seen.add((trip.train, dep))
                    out.append({"train": trip.train, "depart": dep, "final_stop": self.destination(trip, active)})
            d += timedelta(days=1)
        return sorted(out, key=lambda x: x["depart"])

    def transfer_journeys(
        self,
        origin: Stop,
        dest: Stop,
        window_start: datetime,
        window_end: datetime,
        min_transfer_min: int = 4,
        max_wait_min: int = 75,
    ) -> list[tuple[Ride, Ride]]:
        """One-transfer itineraries, best (earliest-arriving) per first-leg train."""
        active_days = {}
        d = window_start.date() - timedelta(days=1)
        while d <= window_end.date() + timedelta(days=1):
            active_days[d] = self.services_on(d)
            d += timedelta(days=1)

        # All places reachable from origin on one train departing within the window.
        first_legs: list[tuple[Ride, Stop]] = []
        for sd, active in active_days.items():
            for trip, i in self.stop_index.get(origin.stop_id, []):
                if trip.service_id not in active or not trip.times[i].pickup:
                    continue
                if i == len(trip.times) - 1:
                    continue
                board = trip.times[i]
                if not (window_start <= _to_dt(sd, board.dep) <= window_end):
                    continue
                first = True
                n = 0
                for seg in self._chain_forward(trip, active):
                    start = i + 1 if first else 1
                    first = False
                    for st in seg.times[start:]:
                        if st.stop_id == origin.stop_id:
                            break
                        if st.dropoff and st.stop_id in self.stops and st.stop_id != dest.stop_id:
                            chain = list(self._chain_until(trip, seg, active))
                            first_legs.append(
                                (Ride(sd, trip.train, chain, board, st, trip, seg, n), self.stops[st.stop_id])
                            )
                        n += 1

        best: dict[tuple, tuple[Ride, Ride]] = {}
        cache: dict[tuple[str, date], list[Ride]] = {}
        for leg1, xfer in first_legs:
            # Change trains where this one stops, or walk to a nearby stop on another line.
            for board_id, walk in [(xfer.stop_id, 0), *self.walk_links.get(xfer.stop_id, [])]:
                if board_id == dest.stop_id:
                    continue
                self._best_connections(
                    leg1, self.stops[board_id], dest, min_transfer_min + walk, max_wait_min + walk, cache, best
                )

        journeys = sorted(best.values(), key=lambda j: (j[0].depart_dt, j[1].arrive_dt))
        # Drop itineraries dominated by a later departure arriving at the same time or earlier.
        out = []
        for idx, j in enumerate(journeys):
            if any(
                k[0].depart_dt >= j[0].depart_dt and k[1].arrive_dt <= j[1].arrive_dt and k is not j
                and (k[0].depart_dt, k[1].arrive_dt) != (j[0].depart_dt, j[1].arrive_dt)
                for k in journeys[idx + 1:]
            ):
                continue
            out.append(j)
        return out

    def _best_connections(
        self,
        leg1: Ride,
        board: Stop,
        dest: Stop,
        min_wait_min: int,
        max_wait_min: int,
        cache: dict[tuple[str, date], list[Ride]],
        best: dict[tuple, tuple[Ride, Ride]],
    ) -> None:
        """Record in best the earliest-arriving ride from board to dest that leg1 connects to."""
        earliest = leg1.arrive_dt + timedelta(minutes=min_wait_min)
        latest = leg1.arrive_dt + timedelta(minutes=max_wait_min)
        for sd in (leg1.service_date, leg1.service_date + timedelta(days=1)):
            key = (board.stop_id, sd)
            if key not in cache:
                cache[key] = self.rides_on_service_day(board, dest, sd)
            for leg2 in cache[key]:
                if leg2.train == leg1.train:
                    continue
                if earliest <= leg2.depart_dt <= latest:
                    k = (leg1.depart_dt, leg1.train)
                    cur = best.get(k)
                    if (
                        cur is None
                        or leg2.arrive_dt < cur[1].arrive_dt
                        # Same arrival: board the second train as late as possible,
                        # i.e. transfer before you'd have to backtrack.
                        or (leg2.arrive_dt == cur[1].arrive_dt and leg2.depart_dt > cur[1].depart_dt)
                    ):
                        best[k] = (leg1, leg2)

    def stops_between(self, origin: Stop, dest: Stop) -> list[Stop]:
        """Stations a train passes between origin and dest, in order, on any day in the
        feed. Uses the stopping pattern with the most stops, so locals' stations are all
        there. Empty when no train ever runs origin → dest directly."""
        key = (origin.stop_id, dest.stop_id)
        if key in self._between_cache:
            return self._between_cache[key]
        every_service = set(self.calendar) | set(self.cal_add)
        best: list[str] = []
        for trip, i in self.stop_index.get(origin.stop_id, []):
            path: list[str] = []
            first = True
            for seg in self._chain_forward(trip, every_service):
                for st in seg.times[i + 1 if first else 1:]:
                    if st.stop_id == dest.stop_id:
                        if len(path) > len(best):
                            best = path
                        path = []
                        break
                    if st.stop_id == origin.stop_id:
                        path = []
                        break
                    path.append(st.stop_id)
                else:
                    first = False
                    continue
                break
        self._between_cache[key] = [self.stops[s] for s in best if s in self.stops]
        return self._between_cache[key]

    def destination(self, trip: Trip, active: set[str]) -> str:
        """Where the physical train ends up, as station boards show it: through-routed
        trains change line in Center City, so this follows the chain to its last trip."""
        last = trip
        for last in self._chain_forward(trip, active):
            pass
        if last.headsign:
            return last.headsign
        return self.stops[last.times[-1].stop_id].name if last.times else ""

    def _train_starts(self, train: str, active: set[str]) -> list[Trip]:
        starts = [
            t for t in self.trips.values()
            if t.train == train and t.service_id in active and t.trip_id not in self.prev_trip
        ]
        if not starts:
            # The train number might only appear mid-chain.
            starts = [t for t in self.trips.values() if t.train == train and t.service_id in active]
        return starts

    def train_destination(self, train: str, service_date: date) -> str | None:
        active = self.services_on(service_date)
        starts = self._train_starts(train, active)
        return self.destination(starts[0], active) if starts else None

    def trip_stops(self, train: str, service_date: date) -> list[dict]:
        active = self.services_on(service_date)
        starts = self._train_starts(train, active)
        out = []
        for t0 in starts[:1]:
            first = True
            for seg in self._chain_forward(t0, active):
                for st in seg.times[0 if first else 1:]:
                    out.append(
                        {
                            "station": self.stops[st.stop_id].name if st.stop_id in self.stops else st.stop_id,
                            "scheduled": fmt_time(_to_dt(service_date, st.dep)),
                            "line": self.route_name(seg.route_id),
                        }
                    )
                first = False
        return out


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_time_on(dt: datetime, query_date: date | None) -> str:
    """fmt_time, marked "(next day)" when it falls after the day the rider asked about."""
    return fmt_time(dt) + (" (next day)" if query_date and dt.date() > query_date else "")


def train_summary(t: dict) -> str:
    """One sentence restating a train entry, so the model needn't infer what
    final_stop, depart and arrive refer to."""
    ident = f"Train {t.get('train')}"
    if t.get("final_stop"):
        ident += f" (final stop {t['final_stop']})"
    elif t.get("line"):
        ident += f" ({t['line']} line)"
    s = f"{ident} leaves {t.get('board_at')} at {t.get('depart')} and arrives at {t.get('get_off_at')} at {t.get('arrive')}"
    status = str(t.get("status") or "").strip()
    if status:
        late = re.fullmatch(r"(\d+) mins?", status, re.I)
        s += f" ({late.group(1)} min late)" if late else f" ({status})"
    return s + "."


def with_summary(t: dict) -> dict:
    return {"summary": train_summary(t), **t}


def ride_to_dict(feed: RailFeed, r: Ride, query_date: date | None = None) -> dict:
    # Riders find their train by number and final stop, which is what platform boards
    # show. Line names are left out: a through-routed train changes line in Center City,
    # so the line at the boarding station can look like the wrong train. Both stations
    # are named so "arrive" isn't mistaken for the arrival at the final stop.
    dep, arr = r.depart_dt, r.arrive_dt
    d = {
        "train": r.train,
        "final_stop": feed.destination(r.board_trip, feed.services_on(r.service_date)),
        "board_at": feed.stops[r.board.stop_id].name,
        "depart": fmt_time_on(dep, query_date),
        "get_off_at": feed.stops[r.alight.stop_id].name,
        "arrive": fmt_time_on(arr, query_date),
        "duration_min": int((arr - dep).total_seconds() // 60),
        "intermediate_stops": r.stops_between,
    }
    if query_date and dep.date() != query_date:
        d["depart_date"] = dep.date().isoformat()
    return with_summary(d)


# ---------------------------------------------------------------------------
# Download + cache
# ---------------------------------------------------------------------------


def gtfs_subset(gtfs_zip: bytes, route_types: set[str]) -> bytes:
    """A GTFS zip cut down to the routes of the given route_types, with their trips,
    stop times and stops. SEPTA's bus feed is ~150 MB unzipped, almost all of it bus
    stop times; its subway and trolley routes are under a tenth of that."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(gtfs_zip)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        names = set(src.namelist())

        def rows(fname: str) -> tuple[list[str], list[dict]]:
            with src.open(fname) as fh:
                reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""))
                return list(reader.fieldnames or []), list(reader)

        def write(fname: str, fields: list[str], keep: list[dict]) -> None:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fields, lineterminator="\n")
            w.writeheader()
            w.writerows(keep)
            dst.writestr(fname, buf.getvalue())

        fields, routes = rows("routes.txt")
        routes = [r for r in routes if r.get("route_type") in route_types]
        route_ids = {r["route_id"] for r in routes}
        write("routes.txt", fields, routes)
        fields, trips = rows("trips.txt")
        trips = [t for t in trips if t["route_id"] in route_ids]
        trip_ids = {t["trip_id"] for t in trips}
        write("trips.txt", fields, trips)

        # Millions of rows: filter lines by their trip_id column instead of parsing each one.
        used_stops: set[str] = set()
        with src.open("stop_times.txt") as fh, dst.open("stop_times.txt", "w") as oh:
            text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
            header = text.readline()
            cols = next(csv.reader([header]))
            ti, si = cols.index("trip_id"), cols.index("stop_id")
            last = max(ti, si) + 1
            oh.write(header.encode())
            chunk = []
            for line in text:
                parts = line.split(",", last)
                if len(parts) > last - 1 and parts[ti].strip('"') in trip_ids:
                    used_stops.add(parts[si].strip('"'))
                    chunk.append(line)
                    if len(chunk) >= 10000:
                        oh.write("".join(chunk).encode())
                        chunk = []
            oh.write("".join(chunk).encode())

        fields, stops = rows("stops.txt")
        parents = {s["parent_station"] for s in stops if s["stop_id"] in used_stops and s.get("parent_station")}
        write("stops.txt", fields, [s for s in stops if s["stop_id"] in used_stops | parents])
        for fname in ("agency.txt", "calendar.txt", "calendar_dates.txt", "feed_info.txt"):
            if fname in names:
                dst.writestr(fname, src.read(fname))
    return out.getvalue()


def _extract_feeds(blob: bytes) -> dict[str, bytes]:
    """{"rail": ..., "metro": ..., "bus": ...} from a gtfs_public.zip bundle: the rail GTFS,
    the Metro routes cut from the bus GTFS, and the bus routes in bus.build's compact form.
    A bare GTFS zip is taken to be the rail feed, and then there's no Metro or bus feed."""
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        if "stop_times.txt" in names:
            return {"rail": blob}
        for n in names:
            if n.endswith("google_rail.zip"):
                out["rail"] = z.read(n)
            elif n.endswith("google_bus.zip"):
                bus_gtfs = z.read(n)
                try:
                    out["metro"] = gtfs_subset(bus_gtfs, METRO_ROUTE_TYPES)
                except Exception:  # noqa: BLE001 - rail still works without it
                    log.exception("Could not extract the Metro routes from %s", n)
                try:
                    from . import bus  # bus builds on this module

                    out["bus"] = bus.build(bus_gtfs)
                except Exception:  # noqa: BLE001
                    log.exception("Could not extract the bus routes from %s", n)
    if "rail" not in out:
        raise ValueError("Download did not contain google_rail.zip or GTFS files")
    return out


def _parse(kind: str, blob: bytes, source: str, fetched_at: float):
    # metro and bus build on this module
    if kind == "metro":
        from .metro import MetroFeed

        return MetroFeed(blob, source=source, fetched_at=fetched_at)
    if kind == "bus":
        from .bus import BusFeed

        return BusFeed(blob, source=source, fetched_at=fetched_at)
    return RailFeed(blob, source=source, fetched_at=fetched_at)


FEED_NAMES = {"rail": "Regional Rail", "metro": "Metro (subway and trolley)", "bus": "bus"}
# Bumped when _extract_feeds' output changes, so caches made before are re-downloaded once.
CACHE_FORMAT = 2

# SEPTA's download URLs only ever have the newest feed, which it publishes a few days
# before that feed starts. Other days need an older feed: the cached one that newest feed
# replaced, or the GitHub release that covers the day. Releases are tagged
# v<start date><revision> (v202609271 = second revision of the feed starting 9/27) and
# listed from git's tag list, which isn't rate-limited like GitHub's REST API (60
# unauthenticated requests an hour, which Cloud Run's shared addresses run into); the
# REST API is the fallback.
RELEASE_REFS = "https://github.com/septadev/GTFS.git/info/refs?service=git-upload-pack"
RELEASES_API = "https://api.github.com/repos/septadev/GTFS/releases?per_page=100"
RELEASE_ZIP = "https://github.com/septadev/GTFS/releases/download/{tag}/gtfs_public.zip"
RELEASE_TAG = re.compile(r"v(\d{8})(\d+)")
# Release feeds kept on disk besides the newest one (about 12 MB each), most recently used first.
MAX_RELEASE_SLOTS = 4


def _tag_key(tag: str) -> tuple[str, int]:
    m = RELEASE_TAG.fullmatch(tag)
    return m.group(1), int(m.group(2))


def _today() -> date:
    return datetime.now(TZ).date()


class FeedManager:
    """Keeps current rail, Metro and bus feeds, refreshing the cached zips when they get
    stale.

    Normally that's just the newest feeds. For a day before they start (SEPTA publishes
    them days early, or the day is past) it uses the cached feeds they replaced, the
    "_in_effect" set, or else downloads the release in effect that day from GitHub and
    keeps a few such releases. Each download is cached as one zip per kind
    (google_rail.zip, google_metro.zip, google_bus.zip) plus a source.json naming where it
    came from; other sets have the same files with a suffix ("_in_effect", "_v202609271").
    Feeds are parsed the first time they're asked for.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike | None = None,
        urls: list[str] | None = None,
        max_age_hours: float | None = None,
        local_zip: str | None = None,
    ):
        self.data_dir = Path(data_dir or os.environ.get("SEPTA_DATA_DIR") or Path.home() / ".cache" / "septa-mcp")
        self.urls = urls or [u for u in os.environ.get("SEPTA_GTFS_URLS", "").split(",") if u] or DEFAULT_GTFS_URLS
        # SEPTA announces timetable changes well ahead, so a weekly check is plenty; on Cloud
        # Run a scheduled job (refresh(), via `septa-mcp --refresh-feeds`) does it instead.
        if max_age_hours is None:
            max_age_hours = float(os.environ.get("SEPTA_GTFS_MAX_AGE_HOURS", 7 * 24))
        self.max_age = max_age_hours * 3600
        self.local_zip = local_zip or os.environ.get("SEPTA_GTFS_ZIP")
        self._feeds: dict[tuple[str, str], RailFeed] = {}  # (slot, kind) -> parsed feed
        self._local: dict[str, bytes] | None = None
        self._lock = threading.Lock()
        self._last_attempt: dict[str, float] = {}
        self._tags: list[str] | None = None
        self._tags_at = 0.0

    def _path(self, slot: str, kind: str) -> Path:
        return self.data_dir / f"google_{kind}{slot}.zip"

    def _meta(self, slot: str) -> dict | None:
        try:
            return json.loads((self.data_dir / f"source{slot}.json").read_text())
        except (OSError, ValueError):
            return None

    def _download(self, urls: list[str]) -> tuple[bytes, str]:
        errors = []
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.read(), url
            except Exception as e:  # noqa: BLE001 - try the next mirror
                errors.append(f"{url}: {e}")
                log.warning("GTFS download failed from %s: %s", url, e)
        raise RuntimeError("Could not download SEPTA GTFS. " + "; ".join(errors))

    def _tags_from_git(self) -> list[str]:
        req = urllib.request.Request(RELEASE_REFS, headers={"User-Agent": "git/2 " + USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
        return sorted(set(re.findall(r"refs/tags/(v\d{9,})(?![\w.-])", body)))

    def _tags_from_api(self) -> list[str]:
        req = urllib.request.Request(RELEASES_API, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            releases = json.load(resp)
        return [r["tag_name"] for r in releases if not r.get("draft") and not r.get("prerelease")]

    def _release_tags(self) -> list[str]:
        """Tags of SEPTA's feed releases, newest start first (cached for an hour)."""
        now = time.time()
        if self._tags is not None and now - self._tags_at < 3600:
            return self._tags
        if now - self._last_attempt.get("releases", 0) <= 900:  # don't hammer
            raise RuntimeError("Listing SEPTA's GTFS releases failed recently; retrying later")
        self._last_attempt["releases"] = now
        errors = []
        for fetch in (self._tags_from_git, self._tags_from_api):
            try:
                tags = fetch()
                break
            except Exception as e:  # noqa: BLE001 - try the other listing
                errors.append(f"{e}")
        else:
            raise RuntimeError("Could not list SEPTA's GTFS releases: " + "; ".join(errors))
        self._tags = sorted((t for t in tags if RELEASE_TAG.fullmatch(t)), key=_tag_key, reverse=True)
        self._tags_at = time.time()
        return self._tags

    def _releases_for(self, day: date) -> list[str]:
        """Release tags that had started by `day`, newest first: the first is the feed in
        effect that day."""
        return [t for t in self._release_tags() if _tag_key(t)[0] <= day.strftime("%Y%m%d")]

    def _cached_release_slots(self) -> list[str]:
        """Slots of the release feeds on disk, most recently used first."""
        slots = [p.stem[len("google_rail"):] for p in self.data_dir.glob("google_rail_v*.zip")]
        slots = [s for s in slots if RELEASE_TAG.fullmatch(s[1:])]

        def used(slot):
            meta = self.data_dir / f"source{slot}.json"
            return (meta if meta.exists() else self._path(slot, "rail")).stat().st_mtime

        return sorted(slots, key=used, reverse=True)

    def _fetch_release(self, tag: str, raise_errors: bool = False) -> bool:
        """Download release `tag` into its own slot unless it's already there (a released
        feed never changes). Returns whether the slot has it."""
        slot = f"_{tag}"
        if self._path(slot, "rail").exists() and (self._meta(slot) or {}).get("format") == CACHE_FORMAT:
            return True
        try:
            self._refresh(slot, lambda: [RELEASE_ZIP.format(tag=tag)], force=True, raise_errors=raise_errors)
        except Exception:
            if raise_errors:
                raise
            log.exception("Could not download SEPTA's %s feed", tag)
        if not self._path(slot, "rail").exists():
            return False
        self._prune_releases(keep=slot)
        return True

    def _prune_releases(self, keep: str) -> None:
        for slot in [s for s in self._cached_release_slots() if s != keep][MAX_RELEASE_SLOTS - 1:]:
            for kind in FEED_NAMES:
                self._path(slot, kind).unlink(missing_ok=True)
                self._feeds.pop((slot, kind), None)
            (self.data_dir / f"source{slot}.json").unlink(missing_ok=True)

    def _release_feed(self, day: date, kind: str, raise_errors: bool = False):
        """The `kind` feed of the release in effect on `day`, downloading it if needed, or
        None. Without the release list, the best cached release."""
        try:
            tags = self._releases_for(day)
        except Exception:
            if raise_errors:
                raise
            log.exception("Could not list SEPTA's GTFS releases")
            tags = None
        if tags is None:
            ymd = day.strftime("%Y%m%d")
            cached = [s[1:] for s in self._cached_release_slots() if _tag_key(s[1:])[0] <= ymd]
            candidates = sorted(cached, key=_tag_key, reverse=True)[:1]
        else:
            candidates = tags[:2]  # the newest might be a tag without a feed attached
        for tag in candidates:
            if tags is not None and not self._fetch_release(tag, raise_errors=raise_errors):
                continue
            feed = self._load(f"_{tag}", kind)
            if feed is not None and _covers(feed.feed_range(), day):
                meta = self.data_dir / f"source_{tag}.json"
                if meta.exists():
                    os.utime(meta)  # most recently used (not the zip: that would re-parse it)
                return feed
        return None

    def _refresh(self, slot: str, urls, force: bool = False, raise_errors: bool = False) -> bool:
        """Re-download slot's feeds from urls() when older than max_age, when cached in an
        older CACHE_FORMAT (before the Metro or bus feeds were), or when forced. Returns
        whether it downloaded. Failures are logged, and the cached feeds kept, unless
        raise_errors or there's no cached feed."""
        rail = self._path(slot, "rail")
        now = time.time()
        stale = (
            force
            or now - (rail.stat().st_mtime if rail.exists() else 0) > self.max_age
            or (self._meta(slot) or {}).get("format") != CACHE_FORMAT
        )
        if not stale or (not raise_errors and now - self._last_attempt.get(slot, 0) <= 900):  # don't hammer
            return False
        self._last_attempt[slot] = now
        try:
            blob, url = self._download(urls())
            feeds = _extract_feeds(blob)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if slot == "":
                self._keep_in_effect(feeds)
            for kind in FEED_NAMES:
                path = self._path(slot, kind)
                if kind in feeds:
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(feeds[kind])
                    tmp.replace(path)
                else:
                    path.unlink(missing_ok=True)
            (self.data_dir / f"source{slot}.json").write_text(
                json.dumps({"source": url, "kinds": sorted(feeds), "format": CACHE_FORMAT})
            )
            log.info("Downloaded SEPTA GTFS (%s) from %s", ", ".join(sorted(feeds)), url)
            return True
        except Exception:
            if raise_errors or not rail.exists():
                raise
            log.exception("Refresh of the%s feed failed; using cached feed", slot.replace("_", " "))
            return False

    def _slot_covers(self, slot: str, day: date) -> bool:
        rail = self._path(slot, "rail")
        try:
            return rail.exists() and _covers(feed_range_of(rail.read_bytes()), day)
        except (OSError, zipfile.BadZipFile, KeyError, ValueError):
            return False

    def _keep_in_effect(self, new: dict[str, bytes]) -> None:
        """Before new feeds replace the cached ones: if the new timetable hasn't started yet,
        the cached one is still the one in effect today, so keep it as the "_in_effect"
        set (unless that already covers today). SEPTA publishes each feed days before it
        starts, and looking up the in-effect release on GitHub can fail (its API rate-limits
        unauthenticated requests, which Cloud Run's shared addresses run into), which left
        no timetable for today."""
        today = _today()
        if "rail" not in new or _covers(feed_range_of(new["rail"]), today):
            return
        if self._slot_covers("_in_effect", today) or not self._slot_covers("", today):
            return
        for kind in FEED_NAMES:
            src, dst = self._path("", kind), self._path("_in_effect", kind)
            if src.exists():
                tmp = dst.with_suffix(".tmp")
                tmp.write_bytes(src.read_bytes())
                tmp.replace(dst)
            else:
                dst.unlink(missing_ok=True)
        meta = self.data_dir / "source.json"
        if meta.exists():
            (self.data_dir / "source_in_effect.json").write_text(meta.read_text())
        log.info("Kept the cached feeds as the ones in effect until the new feed starts")

    def refresh(self) -> None:
        """Download the newest feeds now, whatever the cache's age, plus the ones in effect
        today if the newest haven't started: for a scheduled job. Raises if a download fails."""
        with self._lock:
            self._refresh("", lambda: self.urls, force=True, raise_errors=True)
            start, _ = self._load("", "rail").feed_range()
            today = _today()
            if start and start > today and not self._slot_covers("_in_effect", today):
                self._release_feed(today, "rail", raise_errors=True)

    def _load(self, slot: str, kind: str) -> RailFeed | None:
        """The parsed feed cached for (slot, kind), re-parsed when the file is newer."""
        path = self._path(slot, kind)
        if not path.exists():
            return None
        mtime = path.stat().st_mtime
        feed = self._feeds.get((slot, kind))
        if feed is None or feed.fetched_at < mtime - 1:
            source = (self._meta(slot) or {}).get("source") or str(path)
            feed = self._feeds[(slot, kind)] = _parse(kind, path.read_bytes(), source, mtime)
        return feed

    def get(self, day: date | None = None, kind: str = "rail"):
        """The `kind` feed ("rail", "metro" or "bus") with the timetable for `day`
        (default today): the newest one covering it."""
        with self._lock:
            if self.local_zip:
                if self._local is None:
                    self._local = _extract_feeds(Path(self.local_zip).read_bytes())
                if kind not in self._local:
                    raise FeedUnavailable(f"{self.local_zip} has no {FEED_NAMES[kind]} schedule.")
                if ("local", kind) not in self._feeds:
                    self._feeds[("local", kind)] = _parse(
                        kind, self._local[kind], self.local_zip, os.path.getmtime(self.local_zip)
                    )
                return self._feeds[("local", kind)]

            today = _today()
            self._refresh("", lambda: self.urls)
            latest = self._load("", kind)
            if latest is not None:
                # Past the cached timetable's last day (the weekly refresh missed a new one):
                # don't wait for the cache to age out.
                _, end = latest.feed_range()
                if end and end < today and self._refresh("", lambda: self.urls, force=True):
                    latest = self._load("", kind)
            if latest is None:
                if not self._path("", "rail").exists():
                    raise RuntimeError("SEPTA GTFS download failed recently; retrying later")
                if (self._meta("") or {}).get("format") != CACHE_FORMAT:  # old cache, and the refresh failed
                    raise FeedUnavailable(f"The {FEED_NAMES[kind]} schedule isn't downloaded yet; retrying later.")
                raise FeedUnavailable(f"SEPTA's download had no {FEED_NAMES[kind]} schedule.")
            day = day or today
            feed = self._for_day(latest, day, kind)
            if feed.feed_range()[0] == day and day not in feed.overnight_added:
                feed.overnight_added.add(day)
                self._add_overnight(feed, latest, day, kind)
            return feed

    def _add_overnight(self, feed, latest, day: date, kind: str) -> None:
        """On a feed's first day, add the late trips of the night before from the feed in
        effect then (see RailFeed.add_overnight)."""
        eve = day - timedelta(days=1)
        try:
            prev = self._for_day(latest, eve, kind)
            if prev is not feed and _covers(prev.feed_range(), eve):
                n = feed.add_overnight(prev, eve)
                log.info("Added %d late %s trips of %s from the feed before", n, kind, eve)
        except Exception:  # noqa: BLE001 - the day's own trips still work
            log.exception("Could not add the late %s trips of %s from the feed before", kind, eve)

    def _for_day(self, latest, day: date, kind: str):
        start, _ = latest.feed_range()
        if _covers(latest.feed_range(), day) or not start or day > start:
            return latest  # (past its end, no older feed has the day either)
        # Before the newest feed starts (SEPTA publishes it days early, or `day` is in the
        # past): the cached feed it replaced, else the release in effect that day.
        in_effect = self._load("_in_effect", kind)
        if in_effect is not None and _covers(in_effect.feed_range(), day):
            return in_effect
        return self._release_feed(day, kind) or latest
