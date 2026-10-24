"""Load SEPTA's Regional Rail GTFS feed and answer schedule questions from it.

The rail feed is small (~150 stations, ~2,300 trips, ~35,000 stop times), so it is
parsed straight into memory; only the downloaded zip is cached on disk.

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
# (gtfs_public.zip containing google_rail.zip) or the rail zip itself.
DEFAULT_GTFS_URLS = [
    "https://www3.septa.org/developer/gtfs_public.zip",
    "https://github.com/septadev/GTFS/releases/latest/download/gtfs_public.zip",
]

USER_AGENT = "septa-mcp/0.1 (+https://github.com/septadev/GTFS)"


class StationNotFound(ValueError):
    def __init__(self, query: str, suggestions: list[str]):
        self.query = query
        self.suggestions = suggestions
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(f"No Regional Rail station matches '{query}'.{hint}")


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


class RailFeed:
    def __init__(self, rail_zip: bytes, source: str = "", fetched_at: float | None = None):
        self.source = source
        self.fetched_at = fetched_at or time.time()
        self.stops: dict[str, Stop] = {}
        self.routes: dict[str, dict] = {}
        self.trips: dict[str, Trip] = {}
        self.calendar: dict[str, dict] = {}
        self.cal_add: dict[str, set[date]] = {}
        self.cal_remove: dict[str, set[date]] = {}
        self.next_trip: dict[str, str] = {}
        self.prev_trip: dict[str, str] = {}
        self.feed_info: dict = {}
        self._between_cache: dict[tuple[str, str], list[Stop]] = {}
        self._load(rail_zip)
        self._name_index = {normalize(s.name): s for s in self.stops.values()}

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

            for r in rows("stops.txt"):
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
                for st in t.times:
                    if st.stop_id in self.stops:
                        self.stops[st.stop_id].routes.add(t.route_id)

            for r in rows("calendar.txt"):
                self.calendar[r["service_id"]] = r
            for r in rows("calendar_dates.txt"):
                d = datetime.strptime(r["date"], "%Y%m%d").date()
                target = self.cal_add if r["exception_type"] == "1" else self.cal_remove
                target.setdefault(r["service_id"], set()).add(d)

            for r in rows("transfers.txt"):
                # transfer_type 4 = in-seat transfer: same physical train continues.
                if r.get("transfer_type") == "4" and r.get("from_trip_id") and r.get("to_trip_id"):
                    self.next_trip[r["from_trip_id"]] = r["to_trip_id"]
                    self.prev_trip[r["to_trip_id"]] = r["from_trip_id"]

            fi = rows("feed_info.txt")
            self.feed_info = fi[0] if fi else {}

        # Per-stop index of (trip, position) for fast lookups.
        self.stop_index: dict[str, list[tuple[Trip, int]]] = {}
        for t in self.trips.values():
            for i, st in enumerate(t.times):
                self.stop_index.setdefault(st.stop_id, []).append((t, i))

    # -- metadata ----------------------------------------------------------

    def route_name(self, route_id: str) -> str:
        r = self.routes.get(route_id, {})
        return r.get("route_long_name") or r.get("route_short_name") or route_id

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
            out["note"] += " This is the latest feed SEPTA had published at downloaded_at; it's re-checked daily."
        return out

    # -- calendar ----------------------------------------------------------

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

    # -- stations ----------------------------------------------------------

    def resolve_station(self, query: str) -> Stop:
        q = query.strip()
        if q in self.stops:  # a stop_id
            return self.stops[q]
        n = normalize(q)
        if not n:
            raise StationNotFound(query, [])
        if n in self._name_index:
            return self._name_index[n]
        if n in _GROUPS:
            raise AmbiguousStation(query, _GROUPS[n])
        alias = _ALIASES.get(n)
        if alias:
            return self._name_index[normalize(alias)]

        # Whole-word containment: "swarthmore college" -> no; "gwynedd" -> Gwynedd Valley.
        qwords = n.split()
        contains = [
            s for key, s in self._name_index.items()
            if all(re.search(rf"\b{re.escape(w)}\b", key) for w in qwords)
        ]
        if len(contains) == 1:
            return contains[0]
        if len(contains) > 1:
            raise AmbiguousStation(query, sorted(s.name for s in contains))

        scored = sorted(
            ((SequenceMatcher(None, n, key).ratio(), s) for key, s in self._name_index.items()),
            key=lambda x: -x[0],
        )
        if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[0][0] - scored[1][0] > 0.05):
            return scored[0][1]
        raise StationNotFound(query, [s.name for score, s in scored[:4] if score >= 0.5])

    def search_stations(self, query: str, limit: int = 8) -> list[Stop]:
        n = normalize(query)
        if not n:
            return sorted(self.stops.values(), key=lambda s: s.name)[:limit]
        out = []
        for key, s in self._name_index.items():
            score = SequenceMatcher(None, n, key).ratio()
            if n in key:
                score += 1
            if any(n == normalize(a) for a, target in _ALIASES.items() if target == s.name):
                score += 2
            out.append((score, s))
        if n in _GROUPS:
            names = set(_GROUPS[n])
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
            earliest = leg1.arrive_dt + timedelta(minutes=min_transfer_min)
            latest = leg1.arrive_dt + timedelta(minutes=max_wait_min)
            for sd in (leg1.service_date, leg1.service_date + timedelta(days=1)):
                key = (xfer.stop_id, sd)
                if key not in cache:
                    cache[key] = self.rides_on_service_day(xfer, dest, sd)
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


def ride_to_dict(feed: RailFeed, r: Ride, query_date: date | None = None) -> dict:
    # Riders find their train by number and destination, which is what platform boards
    # show. Line names are left out: a through-routed train changes line in Center City,
    # so the line at the boarding station can look like the wrong train.
    dep, arr = r.depart_dt, r.arrive_dt
    d = {
        "train": r.train,
        "destination": feed.destination(r.board_trip, feed.services_on(r.service_date)),
        "depart": fmt_time_on(dep, query_date),
        "arrive": fmt_time_on(arr, query_date),
        "duration_min": int((arr - dep).total_seconds() // 60),
        "intermediate_stops": r.stops_between,
    }
    if query_date and dep.date() != query_date:
        d["depart_date"] = dep.date().isoformat()
    return d


# ---------------------------------------------------------------------------
# Download + cache
# ---------------------------------------------------------------------------


def _extract_rail_zip(blob: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        if "stop_times.txt" in names:
            return blob
        for n in names:
            if n.endswith("google_rail.zip"):
                return z.read(n)
    raise ValueError("Download did not contain google_rail.zip or GTFS files")


class FeedManager:
    """Keeps a current RailFeed, refreshing the cached zip when it gets stale."""

    def __init__(
        self,
        data_dir: str | os.PathLike | None = None,
        urls: list[str] | None = None,
        max_age_hours: float = 24.0,
        local_zip: str | None = None,
    ):
        self.data_dir = Path(data_dir or os.environ.get("SEPTA_DATA_DIR") or Path.home() / ".cache" / "septa-mcp")
        self.urls = urls or [u for u in os.environ.get("SEPTA_GTFS_URLS", "").split(",") if u] or DEFAULT_GTFS_URLS
        self.max_age = max_age_hours * 3600
        self.local_zip = local_zip or os.environ.get("SEPTA_GTFS_ZIP")
        self._feed: RailFeed | None = None
        self._lock = threading.Lock()
        self._last_attempt = 0.0

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "google_rail.zip"

    def _download(self) -> tuple[bytes, str]:
        errors = []
        for url in self.urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return _extract_rail_zip(resp.read()), url
            except Exception as e:  # noqa: BLE001 - try the next mirror
                errors.append(f"{url}: {e}")
                log.warning("GTFS download failed from %s: %s", url, e)
        raise RuntimeError("Could not download SEPTA GTFS. " + "; ".join(errors))

    def get(self) -> RailFeed:
        with self._lock:
            if self.local_zip:
                if self._feed is None:
                    blob = _extract_rail_zip(Path(self.local_zip).read_bytes())
                    self._feed = RailFeed(blob, source=self.local_zip, fetched_at=os.path.getmtime(self.local_zip))
                return self._feed

            now = time.time()
            cached_mtime = self.cache_path.stat().st_mtime if self.cache_path.exists() else 0
            stale = now - cached_mtime > self.max_age

            if stale and now - self._last_attempt > 900:  # don't hammer on repeated failures
                self._last_attempt = now
                try:
                    blob, url = self._download()
                    self.data_dir.mkdir(parents=True, exist_ok=True)
                    tmp = self.cache_path.with_suffix(".tmp")
                    tmp.write_bytes(blob)
                    tmp.replace(self.cache_path)
                    (self.data_dir / "source.txt").write_text(url)
                    self._feed = RailFeed(blob, source=url, fetched_at=now)
                    return self._feed
                except Exception:
                    if not self.cache_path.exists():
                        raise
                    log.exception("Refresh failed; using cached feed")

            if self._feed is None or self._feed.fetched_at < cached_mtime - 1:
                src_file = self.data_dir / "source.txt"
                self._feed = RailFeed(
                    self.cache_path.read_bytes(),
                    source=src_file.read_text() if src_file.exists() else str(self.cache_path),
                    fetched_at=cached_mtime,
                )
            return self._feed
