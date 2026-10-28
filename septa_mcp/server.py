"""MCP server exposing SEPTA Regional Rail and Metro schedules and real-time status as tools."""

from __future__ import annotations

import argparse
import logging
import os
import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import metro, realtime
from .gtfs import (
    TZ,
    AmbiguousStation,
    FeedManager,
    FeedUnavailable,
    StationNotFound,
    fmt_time,
    fmt_time_on,
    ride_to_dict,
    with_summary,
)

log = logging.getLogger("septa_mcp")

INSTRUCTIONS = """\
SEPTA (Philadelphia) schedules and live status for Regional Rail (commuter trains) and the
Metro: the subway (L1 Market-Frankford Line, B1/B2 Broad Street Line, B3 Broad-Ridge
Spur), the trolleys (T1-T5, G1, D1, D2) and the M1 Norristown High Speed Line.

- Regional Rail tools: train_schedule, next_trains, station_departures, train_status,
  find_station. Metro tools: metro_schedule, metro_departures, metro_vehicles,
  find_metro_stop. service_alerts covers every mode, buses included.
- Regional Rail trains are identified by number and final_stop (where the train ends),
  which is how station boards list them. A train's final_stop can be past the rider's stop
  or on another line's branch; that's normal, not a sign of the wrong train. depart and
  arrive are the times at board_at and get_off_at, the rider's own stations, not the
  train's ends.
- Station names are matched loosely ("Jefferson", "30th Street", "Temple"). If a name is
  ambiguous the tool returns the candidates; ask the user or pick from context.
- Shuttle buses that replace trains (see service_alerts) are part of the trip. If an alert
  says buses replace trains between two stations, plan the trip as a train to the end of
  rail service plus the shuttle. When no train reaches the destination, results include
  partial_service: the trains to the closest station on the way that trains still reach.
- Metro trips are identified by line and toward (the direction's last stop). Riders often
  use older names, which work as line names too: "MFL"/"the El" = L1, "BSL" = B1-B3,
  "Route 10" = T1, "Route 15" = G1, "Route 101" = D1, "NHSL" = M1.
- No tool plans a trip mixing Regional Rail and Metro. Plan each part and change at a hub:
  Suburban Station <-> 15th St/City Hall, Gray 30th St Station <-> Drexel Station at
  30th St, Jefferson Station <-> 13th St or 8th-Market (short walks), Fern Rock and
  Norristown Transit Centers.
All times are America/New_York local time. Not covered: bus schedules (only bus alerts),
NJ Transit, PATCO and Amtrak.
"""

mcp = MCPServer(
    name="septa",
    title="SEPTA Regional Rail and Metro",
    description="SEPTA Regional Rail and Metro (subway, trolley) schedules (GTFS) and real-time status.",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)

feeds = FeedManager()
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(TZ)


def parse_date(s: str | None, today: date | None = None) -> date:
    today = today or now().date()
    if not s or not s.strip():
        return today
    t = s.strip().lower().replace(",", " ")
    t = re.sub(r"\s+", " ", t)
    if t in ("today", "tonight", "now"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    m = re.fullmatch(r"(this |next |coming )?(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*", t)
    if m:
        wd = next(i for i, w in enumerate(WEEKDAYS) if w.startswith(m.group(2)[:3]))
        delta = (wd - today.weekday()) % 7
        if m.group(1) == "next " and delta == 0:
            delta = 7
        return today + timedelta(days=delta)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    for fmt in ("%m/%d", "%b %d", "%B %d"):
        try:
            d = datetime.strptime(f"{t} {today.year}", f"{fmt} %Y").date()
            return d if d >= today - timedelta(days=1) else d.replace(year=today.year + 1)
        except ValueError:
            pass
    raise ValueError(f"Couldn't understand date '{s}'. Use YYYY-MM-DD, a weekday name, 'today' or 'tomorrow'.")


def parse_clock(s: str | None) -> dtime | None:
    if s is None or not str(s).strip():
        return None
    t = str(s).strip().lower().replace(".", "")
    if t == "noon":
        return dtime(12, 0)
    if t == "midnight":
        return dtime(0, 0)
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?\s*(am|pm|a|p)?", t)
    if not m:
        raise ValueError(f"Couldn't understand time '{s}'. Use e.g. '07:30', '7:30 AM' or '19:30'.")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if h == 12:
            h = 0
        if ap.startswith("p"):
            h += 12
    if h == 24 and mi == 0:
        return dtime(23, 59, 59)
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise ValueError(f"Invalid time '{s}'.")
    return dtime(h, mi)


def _fmt_window(start: datetime, end: datetime) -> str:
    """'Sat Oct 31, 5:00 AM – 12:00 PM', or for one running past midnight
    'Sun Oct 25, 12:00 AM – Mon Oct 26, 3:30 AM (next day)'. A bare "12:00 AM – 3:30 AM"
    reads as just the overnight hours when it means the whole day."""
    def day(dt: datetime) -> str:
        return f"{dt:%a %b} {dt.day}"

    if end.date() == start.date():
        return f"{day(start)}, {fmt_time(start)} – {fmt_time(end)}"
    return f"{day(start)}, {fmt_time(start)} – {day(end)}, {fmt_time(end)} (next day)"


def _station_error(e: Exception) -> dict:
    if isinstance(e, AmbiguousStation):
        return {"error": str(e), "candidates": e.candidates}
    if isinstance(e, StationNotFound):
        return {"error": str(e), "suggestions": e.suggestions}
    return {"error": str(e)}


def _line_parts(name: str) -> set[str]:
    """Words of a slashed line name, for matching across feeds: the alerts feed says
    "Media/Elwyn" where GTFS says "Media/Wawa Line", so they have to match on "media"."""
    return {p for p in (re.sub(r"[^a-z]", "", x) for x in name.lower().replace("line", "").split("/")) if p}


def _alerts_for_lines(alerts: list[dict], line_names: list[str]) -> list[dict]:
    """Alerts for any of the given lines, plus system-wide ("generic") ones."""
    want = [_line_parts(n) for n in line_names]
    return [
        a for a in alerts
        if a["route_id"] == "generic" or any(w & _line_parts(str(a.get("route_name", ""))) for w in want)
    ]


def _station_alerts(*stations) -> dict:
    """Current alerts for the lines serving these stations, to explain an empty result.

    Uses the lines the stations have in common (Suburban→Wilmington: just Wilmington/Newark,
    not all 13 lines through Suburban); with none in common, every line serving either.
    """
    feed = feeds.get()
    route_sets = [s.routes for s in stations]
    routes = set.intersection(*route_sets) or set.union(*route_sets)
    try:
        alerts = realtime.rail_alerts()
    except realtime.SeptaAPIError as e:
        return {"alerts_error": str(e)}
    relevant = _alerts_for_lines(alerts, [feed.route_name(r) for r in routes])
    return {"service_alerts": relevant} if relevant else {}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def find_station(query: str) -> dict:
    """Look up SEPTA Regional Rail stations by (partial) name.

    Returns matching stations with the lines that serve them. Use this when a station
    name is ambiguous or you're unsure what the user means.
    """
    feed = feeds.get()
    matches = feed.search_stations(query)
    return {
        "query": query,
        "stations": [
            {
                "name": s.name,
                "stop_id": s.stop_id,
                "lines": sorted(feed.route_name(r) for r in s.routes),
                "wheelchair_accessible": s.wheelchair,
            }
            for s in matches
        ],
    }


@mcp.tool(annotations=READ_ONLY)
async def train_schedule(
    from_station: str,
    to_station: str,
    date: str = "today",
    depart_after: str | None = None,
    depart_before: str | None = None,
    max_results: int = 25,
) -> dict:
    """Scheduled Regional Rail trains from one station to another on a given day.

    Use for "what times do trains leave X for Y on <day>" questions, or any future date.
    Reads SEPTA's published GTFS timetable, handles weekday/weekend/holiday service, and
    treats through-routed trains (which change line name in Center City) as one train.

    Args:
        from_station: Boarding station, e.g. "Jefferson", "30th Street", "Swarthmore".
        to_station: Destination station.
        date: "today", "tomorrow", a weekday name ("saturday" = the next Saturday, or
            today if it is Saturday), or YYYY-MM-DD.
        depart_after: Earliest departure, e.g. "05:00" or "7:30 AM". Defaults to now when
            the date is today, otherwise start of day. For "morning" use 05:00–12:00,
            "afternoon" 12:00–17:00, "evening" 17:00–23:59.
        depart_before: Latest departure. Defaults to end of service day.
        max_results: Cap on trains returned.

    Returns direct trains (train number, departure, arrival, line). If there are no direct
    trains, returns one-transfer itineraries instead. Times are scheduled, not live; for
    trips in the next hour or so, also call next_trains for delays.
    """
    return await anyio.to_thread.run_sync(
        lambda: _train_schedule(from_station, to_station, date, depart_after, depart_before, max_results)
    )


def _train_schedule(
    from_station: str,
    to_station: str,
    date: str = "today",
    depart_after: str | None = None,
    depart_before: str | None = None,
    max_results: int = 25,
    with_alerts: bool = True,
) -> dict:
    try:
        day = parse_date(date)
        after = parse_clock(depart_after)
        before = parse_clock(depart_before)
    except ValueError as e:
        return {"error": str(e)}

    feed = feeds.get(day)
    try:
        origin = feed.resolve_station(from_station)
        dest = feed.resolve_station(to_station)
    except (StationNotFound, AmbiguousStation) as e:
        return _station_error(e)
    if origin.stop_id == dest.stop_id:
        return {"error": "Origin and destination are the same station."}

    ws, we = _window(day, after, before)
    result = _schedule_header(feed, origin, dest, day, ws, we)

    rides = feed.direct_rides(origin, dest, ws, we)
    if rides:
        result["trains"] = [ride_to_dict(feed, r, day) for r in rides[:max_results]]
        if len(rides) > max_results:
            result["more_available"] = len(rides) - max_results
    else:
        journeys = feed.transfer_journeys(origin, dest, ws, we)
        if journeys:
            result["note"] = "No direct trains; these itineraries need one transfer."
            result["itineraries"] = [
                {
                    "summary": (
                        f"Leave {origin.name} at {fmt_time_on(l1.depart_dt, day)} on train {l1.train}, "
                        f"change at {feed.stops[l1.alight.stop_id].name} to train {l2.train} "
                        f"({int((l2.depart_dt - l1.arrive_dt).total_seconds() // 60)} min wait), "
                        f"arrive at {dest.name} at {fmt_time_on(l2.arrive_dt, day)}."
                    ),
                    "depart": fmt_time_on(l1.depart_dt, day),
                    "arrive": fmt_time_on(l2.arrive_dt, day),
                    "duration_min": int((l2.arrive_dt - l1.depart_dt).total_seconds() // 60),
                    "transfer_at": feed.stops[l1.alight.stop_id].name,
                    "transfer_wait_min": int((l2.depart_dt - l1.arrive_dt).total_seconds() // 60),
                    "legs": [ride_to_dict(feed, l1, day), ride_to_dict(feed, l2, day)],
                }
                for l1, l2 in journeys[:max_results]
            ]
        else:
            result["trains"] = []
            result["note"] = "No scheduled service between these stations in this window."
            if with_alerts:
                result.update(_station_alerts(origin, dest))
            partial = _partial_service(feed, origin, dest, ws, we)
            if partial:
                end, rides = partial
                result["partial_service"] = {
                    "rail_service_ends_at": end.name,
                    "trains": [ride_to_dict(feed, r, day) for r in rides[:max_results]],
                }
                result["note"] = _partial_note(dest, end, "service_alerts" in result)

    result["schedule_source"] = feed.summary()
    return result


def _window(day: date, after: dtime | None, before: dtime | None) -> tuple[datetime, datetime]:
    """Departure window on day: from after (default now if day is today, else midnight) to
    before (default the end of the service day, 3:30 the next morning)."""
    current = now()
    if after is None:
        after = current.time().replace(second=0, microsecond=0) if day == current.date() else dtime(0, 0)
    ws = datetime.combine(day, after, TZ)
    we = datetime.combine(day, before, TZ) if before else datetime.combine(day + timedelta(days=1), dtime(3, 30), TZ)
    if we < ws:  # e.g. 22:00 to 01:00
        we += timedelta(days=1)
    return ws, we


def _schedule_header(feed, origin, dest, day: date, ws: datetime, we: datetime) -> dict:
    result: dict = {
        "from": origin.name,
        "to": dest.name,
        "date": day.isoformat(),
        "weekday": WEEKDAYS[day.weekday()].capitalize(),
        "window": _fmt_window(ws, we),
    }
    vs, ve = feed.feed_range()
    if vs and ve and not (vs <= day <= ve):
        result["warning"] = (
            f"{day.isoformat()} is outside the published schedule ({vs.isoformat()} to "
            f"{ve.isoformat()}); SEPTA hasn't released timetables for that date yet, so results "
            "may be empty or inaccurate."
        )
    return result


def _partial_service(feed, origin, dest, ws: datetime, we: datetime):
    """When no train reaches dest, the station closest to dest on the way that trains do
    reach in the window, with those trains: during a shuttle-bus replacement this is where
    riders change to the bus (Claymont, for Wilmington). None if trains reach none of them."""
    for stop in reversed(feed.stops_between(origin, dest)):
        rides = feed.direct_rides(origin, stop, ws, we)
        if rides:
            return stop, rides
    return None


def _partial_note(dest, end, have_alerts: bool) -> str:
    note = (
        f"No trains reach {dest.name} in this window, but trains still run as far as "
        f"{end.name}, the closest station on the way (see partial_service)."
    )
    if have_alerts:
        note += f" Check service_alerts for a shuttle bus from {end.name} to {dest.name}."
    return note


def _with_final_stop(feed, train: dict, board_at: str, get_off_at: str) -> dict:
    """Swap the live API's line name for the train's final stop, looked up by train
    number in the timetable, and name the stations that depart/arrive refer to. The line
    is the one at the boarding station, so a through-routed train to Claymont shows as
    "Chestnut Hill East" at Suburban."""
    train = {k: v for k, v in train.items() if k != "at"}
    head = {"train": train.pop("train", None)}
    today = now().date()
    # After midnight a train may belong to yesterday's service day.
    for day in (today, today - timedelta(days=1)):
        final = feed.train_destination(str(head["train"] or ""), day)
        if final:
            head["final_stop"] = final
            train.pop("line", None)
            break
    else:  # not in the timetable (e.g. an extra train): the line is all there is
        head["line"] = train.pop("line", None)
    head["board_at"] = board_at
    head["depart"] = train.pop("depart", None)
    head["get_off_at"] = get_off_at
    head["arrive"] = train.pop("arrive", None)
    return with_summary({**head, **train})


def _with_final_stops(feed, trains: list[dict], origin: str, dest: str) -> list[dict]:
    out = []
    for t in trains:
        if "connection" in t:
            at = t["connection"].get("at")
            try:  # the live API's name for the station, e.g. "Temple U"
                at = feed.resolve_station(at).name
            except (StationNotFound, AmbiguousStation, AttributeError):
                pass
            t["connection"] = _with_final_stop(feed, t["connection"], at, dest)
            out.append(_with_final_stop(feed, t, origin, at))
        else:
            out.append(_with_final_stop(feed, t, origin, dest))
    return out


@mcp.tool(annotations=READ_ONLY)
async def next_trains(from_station: str, to_station: str, count: int = 5) -> dict:
    """Next trains from one station to another right now, with live delay status. Use for
    "when's the next train" questions.

    Uses SEPTA's real-time NextToArrive API (includes connecting trips when there is
    no direct train). Falls back to the published schedule if the live API is down.
    """
    feed = feeds.get()
    try:
        origin = feed.resolve_station(from_station)
        dest = feed.resolve_station(to_station)
    except (StationNotFound, AmbiguousStation) as e:
        return _station_error(e)

    base = {"from": origin.name, "to": dest.name, "as_of": fmt_time(now())}
    try:
        trains = await anyio.to_thread.run_sync(realtime.next_to_arrive, origin.name, dest.name, count)
    except realtime.SeptaAPIError as e:
        # The alerts come from the same API that just failed, so don't try for them.
        fallback = _train_schedule(origin.stop_id, dest.stop_id, "today", max_results=count, with_alerts=False)
        return {
            **base,
            "source": "published schedule (live data unavailable)",
            "live_error": str(e),
            "trains": fallback.get("trains") or fallback.get("itineraries") or [],
            **{k: fallback[k] for k in ("partial_service", "note") if k in fallback},
        }
    trains = _with_final_stops(feed, trains, origin.name, dest.name)
    result = {**base, "source": "SEPTA real-time API", "trains": trains}
    if not trains:
        result.update(await anyio.to_thread.run_sync(_station_alerts, origin, dest))
        result.update(await _live_partial_service(feed, origin, dest, count, "service_alerts" in result))
    return result


async def _live_partial_service(feed, origin, dest, count: int, have_alerts: bool) -> dict:
    """next_trains' version of _partial_service: the timetable finds where rail service
    ends tonight, then the live API gives that station's trains."""
    current = now()
    end_of_service = datetime.combine(current.date() + timedelta(days=1), dtime(3, 30), TZ)
    partial = await anyio.to_thread.run_sync(_partial_service, feed, origin, dest, current, end_of_service)
    if not partial:
        return {}
    end, rides = partial
    out: dict = {"rail_service_ends_at": end.name}
    try:
        live = await anyio.to_thread.run_sync(realtime.next_to_arrive, origin.name, end.name, count)
        out["trains"] = _with_final_stops(feed, live, origin.name, end.name)
    except realtime.SeptaAPIError as e:
        live = []
        out["live_error"] = str(e)
    if not live:
        out["trains"] = [ride_to_dict(feed, r, current.date()) for r in rides[:count]]
        out["source"] = "published schedule"
    return {"partial_service": out, "note": _partial_note(dest, end, have_alerts)}


@mcp.tool(annotations=READ_ONLY)
async def station_departures(
    station: str,
    count: int = 10,
    direction: Literal["N", "S"] | None = None,
) -> dict:
    """Live departure board for a Regional Rail station: upcoming trains, final stops,
    delays and track numbers.

    direction: "N" or "S" as SEPTA labels trains at that station (roughly: N = headed
    toward the northern end of the system, S = toward the south/west); omit for both.
    """
    feed = feeds.get()
    try:
        st = feed.resolve_station(station)
    except (StationNotFound, AmbiguousStation) as e:
        return _station_error(e)
    try:
        board = await anyio.to_thread.run_sync(realtime.arrivals, st.name, count, direction)
    except realtime.SeptaAPIError as e:
        return {"station": st.name, "error": str(e)}
    result = {"station": st.name, **board}
    if not any(board.values()):
        result.update(await anyio.to_thread.run_sync(_station_alerts, st))
    return result


@mcp.tool(annotations=READ_ONLY)
async def train_status(train_number: str) -> dict:
    """Where a specific train is right now and how late it's running, plus its stop-by-stop
    estimated times. train_number is SEPTA's train number, e.g. "5305"."""
    train_number = re.sub(r"\D", "", train_number)
    out: dict = {"train": train_number}
    try:
        out["position"] = await anyio.to_thread.run_sync(realtime.train_position, train_number)
        if out["position"] is None:
            out["position_note"] = "Train isn't currently reporting a position (not running yet, finished, or not in service today)."
    except realtime.SeptaAPIError as e:
        out["position_error"] = str(e)
    try:
        stops = await anyio.to_thread.run_sync(realtime.train_schedule_live, train_number)
        if stops:
            out["stops"] = stops
    except realtime.SeptaAPIError as e:
        out["stops_error"] = str(e)
    if "stops" not in out:
        static = feeds.get().trip_stops(train_number, now().date())
        if static:
            out["stops"] = static
            out["stops_source"] = "published schedule"
    return out


@mcp.tool(annotations=READ_ONLY)
async def service_alerts(
    line: str | None = None,
    mode: Literal["regional_rail", "metro", "bus"] | None = None,
) -> dict:
    """Current SEPTA service alerts, advisories and detours for Regional Rail, the Metro
    (subway and trolleys) and buses.

    line: a Regional Rail line ("Media/Wawa", "Paoli"), Metro line ("L1", "MFL", "Broad
        Street Line", "T3", "Route 101") or bus route ("42"). Omit for every line.
    mode: only alerts for this mode. Without line or mode, bus alerts (mostly detours,
        often 100+) are summarized as a list of routes rather than listed.
    System-wide alerts are always included.
    """
    try:
        alerts = await anyio.to_thread.run_sync(realtime.alerts)
    except realtime.SeptaAPIError as e:
        return {"error": str(e)}
    for a in alerts:
        if a["mode"] == "metro":
            a["lines"] = metro.ALERT_LINES.get(a["route_id"], [])
    if mode:
        alerts = [a for a in alerts if a["mode"] in (mode, "systemwide")]
    if line:
        matched = _match_alerts(alerts, line)
        result: dict = {"line": line, "alerts": matched, "count": len(matched)}
        if not any(a["mode"] != "systemwide" for a in matched):
            result["note"] = f"No current alerts for '{line}'."
            result.update(_lines_with_alerts(alerts))
        return result
    if mode is None:
        shown = [a for a in alerts if a["mode"] != "bus"]
        result = {"alerts": shown, "count": len(shown)}
        if len(shown) < len(alerts):
            result["bus_routes_with_alerts"] = _lines_with_alerts(alerts)["bus_routes_with_alerts"]
            result["note"] = "Bus alerts aren't listed here; pass line='<route>' or mode='bus' for them."
        return result
    return {"alerts": alerts, "count": len(alerts)}


def _lines_with_alerts(alerts: list[dict]) -> dict:
    def names(modes):
        return sorted({str(a["route_name"]) for a in alerts if a["mode"] in modes}, key=lambda n: (len(n), n))

    out = {}
    if rail_metro := names(("regional_rail", "metro")):
        out["lines_with_alerts"] = rail_metro
    if bus := names(("bus",)):
        out["bus_routes_with_alerts"] = bus
    return out


def _match_alerts(alerts: list[dict], line: str) -> list[dict]:
    try:
        metro_lines = set(metro.resolve_lines(line))
    except ValueError:
        metro_lines = set()
    bus_route = re.sub(r"^(?:bus|route|rt)\.?\s+", "", line.strip().lower())
    rail_words = _line_parts(line)
    return [
        a for a in alerts
        if a["mode"] == "systemwide"
        or (a["mode"] == "regional_rail" and rail_words & _line_parts(str(a.get("route_name", ""))))
        or (a["mode"] == "metro" and metro_lines & set(a.get("lines", [])))
        or (a["mode"] == "bus" and str(a.get("route_name", "")).lower() == bus_route)
    ]


# ---------------------------------------------------------------------------
# Metro (subway, trolley, Norristown High Speed Line)
# ---------------------------------------------------------------------------


def _metro_feed(day: date | None = None):
    return feeds.get(day, "metro")


def _line_codes(line: str | None) -> set[str] | None:
    return set(metro.resolve_lines(line)) if line else None


def _metro_alerts(*stations) -> dict:
    """Current alerts for the Metro lines serving these stations (those they share, or
    else all of them), to explain an empty result."""
    route_sets = [s.routes for s in stations]
    lines = set.intersection(*route_sets) or set.union(*route_sets)
    try:
        alerts = realtime.alerts()
    except realtime.SeptaAPIError as e:
        return {"alerts_error": str(e)}
    relevant = [
        {**a, "lines": metro.ALERT_LINES.get(a["route_id"], [])}
        for a in alerts
        if a["mode"] == "systemwide" or set(metro.ALERT_LINES.get(a["route_id"], [])) & lines
    ]
    return {"service_alerts": relevant} if relevant else {}


def _line_labels(feed, routes) -> list[str]:
    order = list(metro.LINES)
    return [feed.line(r).label for r in sorted(routes, key=lambda r: (order.index(r) if r in order else len(order), r))]


@mcp.tool(annotations=READ_ONLY)
def find_metro_stop(query: str, line: str | None = None) -> dict:
    """Look up SEPTA Metro stations and trolley stops by (partial) name, optionally on one
    line ("L1", "Broad Street Line", "T3", "Route 15"...).

    Returns matching stops with the lines that serve them. Trolley street stops are named
    by intersection, e.g. "Lancaster Av & 40th St".
    """
    try:
        codes = _line_codes(line)
        feed = _metro_feed()
    except (ValueError, FeedUnavailable) as e:
        return {"error": str(e)}
    return {
        "query": query,
        "stops": [
            {
                "name": s.name,
                "stop_id": s.stop_id,
                "lines": _line_labels(feed, s.routes),
                "wheelchair_accessible": s.wheelchair,
            }
            for s in feed.search_stations(query, routes=codes)
        ],
    }


@mcp.tool(annotations=READ_ONLY)
async def metro_schedule(
    from_stop: str,
    to_stop: str,
    date: str = "today",
    depart_after: str | None = None,
    depart_before: str | None = None,
    line: str | None = None,
    max_results: int = 20,
) -> dict:
    """Scheduled SEPTA Metro trips from one station or stop to another on a given day:
    the subway (L1 Market-Frankford Line; B1/B2 Broad Street Line, B3 Broad-Ridge Spur),
    trolleys (T1–T5, G1, D1, D2) and the M1 Norristown High Speed Line.

    Args:
        from_stop: Station or trolley stop, e.g. "15th St", "Frankford Transit Center",
            "Lancaster Av & 40th St".
        to_stop: Destination station or stop.
        date: "today", "tomorrow", a weekday name, or YYYY-MM-DD.
        depart_after / depart_before: Departure window, e.g. "07:00" or "7:30 AM". Defaults
            to now (or start of day) through the end of the service day.
        line: Only this line, e.g. "L1", "MFL", "Broad Street Line", "T3" or "Route 101".
            Also picks the right stop when a name is shared by several lines.
        max_results: Cap on trips returned.

    Trips are identified by line and toward (the direction's last stop). Without a direct
    trip it returns one-transfer itineraries, including short walks between nearby stations
    (walk_to). Doesn't combine Metro with Regional Rail. For the next departures with live
    delays, use metro_departures.
    """
    return await anyio.to_thread.run_sync(
        lambda: _metro_schedule(from_stop, to_stop, date, depart_after, depart_before, line, max_results)
    )


def _metro_schedule(from_stop, to_stop, date, depart_after, depart_before, line, max_results) -> dict:
    try:
        day = parse_date(date)
        after = parse_clock(depart_after)
        before = parse_clock(depart_before)
        codes = _line_codes(line)
        feed = _metro_feed(day)
    except (ValueError, FeedUnavailable) as e:
        return {"error": str(e)}
    try:
        origin = feed.resolve_station(from_stop, codes)
        dest = feed.resolve_station(to_stop, codes)
    except (StationNotFound, AmbiguousStation) as e:
        return _station_error(e)
    if origin.stop_id == dest.stop_id:
        return {"error": "Origin and destination are the same stop."}

    ws, we = _window(day, after, before)
    result = _schedule_header(feed, origin, dest, day, ws, we)
    rides = feed.direct_rides(origin, dest, ws, we)
    if codes:
        on_line = [r for r in rides if r.board_trip.route_id in codes]
        if rides and not on_line:
            result["note"] = f"No {line} trips between these stops in this window; these are on other lines."
        rides = on_line or rides
    if rides:
        result["trips"] = [metro.ride_to_dict(feed, r, day) for r in rides[:max_results]]
        if len(rides) > max_results:
            result["more_available"] = len(rides) - max_results
    else:
        journeys = feed.transfer_journeys(origin, dest, ws, we)
        if journeys:
            result["note"] = "No direct trips; these itineraries need one transfer."
            result["itineraries"] = [_metro_itinerary(feed, l1, l2, day) for l1, l2 in journeys[:max_results]]
        else:
            result["trips"] = []
            result["note"] = "No scheduled service between these stops in this window."
            result.update(_metro_alerts(origin, dest))
    result["schedule_source"] = feed.summary()
    return result


def _metro_itinerary(feed, l1, l2, day: date) -> dict:
    a, b = metro.ride_to_dict(feed, l1, day), metro.ride_to_dict(feed, l2, day)
    wait = int((l2.depart_dt - l1.arrive_dt).total_seconds() // 60)
    walk = dict(feed.walk_links.get(l1.alight.stop_id, [])).get(l2.board.stop_id)
    change = f"change at {a['get_off_at']}"
    if walk is not None:
        change += f", walk to {b['board_at']} (about {walk} min),"
    out = {
        "summary": (
            f"Take the {a['line_label']} toward {a['toward']} from {a['board_at']} at {a['depart']}, "
            f"{change} and take the {b['line_label']} toward {b['toward']} at {b['depart']} "
            f"({wait} min after arriving), arrive at {b['get_off_at']} at {b['arrive']}."
        ),
        "depart": a["depart"],
        "arrive": b["arrive"],
        "duration_min": int((l2.arrive_dt - l1.depart_dt).total_seconds() // 60),
        "transfer_at": a["get_off_at"],
        "transfer_wait_min": wait,
    }
    if walk is not None:
        out["walk_to"] = b["board_at"]
        out["walk_min"] = walk
    out["legs"] = [a, b]
    return out


@mcp.tool(annotations=READ_ONLY)
async def metro_departures(stop: str, line: str | None = None, count: int = 10) -> dict:
    """Next departures from a SEPTA Metro station or trolley stop, with live delays where
    SEPTA reports them.

    line: only this line ("L1", "T3", "Route 15"...); also picks the right stop when a
    name is shared. Trolleys and the M1 report live delays (status, estimated); the
    subway lines (L1, B1-B3) don't, so their times are scheduled.
    """
    try:
        codes = _line_codes(line)
        feed = _metro_feed()
        st = feed.resolve_station(stop, codes)
    except (StationNotFound, AmbiguousStation) as e:  # before ValueError: they're ValueErrors
        return _station_error(e)
    except (ValueError, FeedUnavailable) as e:
        return {"error": str(e)}

    current = now()
    # From a while back: a trip that was due 20 minutes ago may be running 25 late.
    deps = feed.departures(st, current - timedelta(minutes=45), current + timedelta(hours=6))
    if codes:
        deps = [d for d in deps if d[1].route_id in codes]
    result: dict = {"stop": st.name, "lines": _line_labels(feed, st.routes & codes if codes else st.routes),
                    "as_of": fmt_time(current)}

    live_lines = sorted({t.route_id for _, t, _ in deps if feed.line(t.route_id).live})
    vehicles: dict[str, dict] = {}
    for code in live_lines:
        try:
            for v in await anyio.to_thread.run_sync(realtime.transit_view, code):
                if v["trip"]:
                    vehicles[str(v["trip"])] = v
        except realtime.SeptaAPIError as e:
            result["live_error"] = str(e)
            break

    out = []
    for sched, trip, i in deps:
        v = vehicles.get(trip.trip_id)
        if v and _already_passed(v, trip.times[i].seq):
            continue
        late = v["minutes_late"] if v else None
        expected = sched + timedelta(minutes=late) if late is not None else sched
        if expected < current - timedelta(minutes=1):
            continue
        line_ = feed.line(trip.route_id)
        d = {"line": line_.code, "line_label": line_.label, "toward": feed.toward(trip), "scheduled": fmt_time(sched)}
        if v:
            d["status"] = realtime.late_status(late) or "running, no delay estimate"
            if late:
                d["expected"] = fmt_time(expected)
            d["vehicle"] = v["vehicle"]
        else:
            d["status"] = "scheduled" if line_.live else "scheduled (no live data for the subway)"
        summary = f"{line_.label} toward {d['toward']} leaves at {d['scheduled']}"
        if "expected" in d:
            summary += f", expected {d['expected']}"
        out.append((expected, {"summary": f"{summary} ({d['status']}).", **d}))
    out.sort(key=lambda x: x[0])
    result["departures"] = [d for _, d in out[:count]]
    if not result["departures"]:
        result["note"] = "No departures from this stop in the next six hours."
        result.update(await anyio.to_thread.run_sync(_metro_alerts, st))
    vs, ve = feed.feed_range()
    if vs and ve and not (vs <= current.date() <= ve):
        result["warning"] = f"Today is outside the published Metro schedule ({vs.isoformat()} to {ve.isoformat()})."
    return result


def _already_passed(vehicle: dict, stop_sequence: int) -> bool:
    try:
        return int(vehicle.get("next_stop_sequence")) > stop_sequence
    except (TypeError, ValueError):
        return False


@mcp.tool(annotations=READ_ONLY)
async def metro_vehicles(line: str) -> dict:
    """Where the trolleys or Norristown High Speed Line cars on a line are right now: next
    stop, direction, and how late. line: e.g. "T1", "Route 15", "D", "M1". The subway
    lines (L1, B1-B3) don't report positions."""
    try:
        codes = metro.resolve_lines(line)
    except ValueError as e:
        return {"error": str(e)}
    try:
        feed = _metro_feed()
    except RuntimeError:  # FeedUnavailable, or the download failed
        feed = None  # vehicles still make sense without stop names
    out: dict = {"as_of": fmt_time(now()), "lines": []}
    for code in codes:
        info = metro.line_info(code)
        entry: dict = {"line": code, "line_label": info.label}
        if not info.live:
            entry["note"] = "SEPTA's public API has no live positions for the subway."
            out["lines"].append(entry)
            continue
        try:
            vehicles = await anyio.to_thread.run_sync(realtime.transit_view, code)
        except realtime.SeptaAPIError as e:
            entry["error"] = str(e)
            out["lines"].append(entry)
            continue
        entry["vehicles"] = [_vehicle(feed, v) for v in vehicles]
        out["lines"].append(entry)
    return out


def _vehicle(feed, v: dict) -> dict:
    next_stop = v["next_stop"]
    if not next_stop and feed and v["next_stop_id"]:
        sid = feed.stop_alias.get(str(v["next_stop_id"]), str(v["next_stop_id"]))
        next_stop = feed.stops[sid].name if sid in feed.stops else None
    d = {
        "vehicle": v["vehicle"],
        "direction": v["direction"],
        "toward": v["destination"],
        "next_stop": next_stop,
        "status": realtime.late_status(v["minutes_late"]),
        "minutes_late": v["minutes_late"],
        "reported_at": v["reported_at"],
        "lat": v["lat"],
        "lon": v["lon"],
    }
    if v["seats"]:
        d["seats"] = v["seats"]
    return d


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description="SEPTA Regional Rail and Metro MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("MCP_TRANSPORT", "stdio"))
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = p.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    # Load (and if needed download) the timetable up front so the first question is fast.
    log.info("Loaded SEPTA rail schedule: %s", feeds.get().summary())
    try:
        log.info("Loaded SEPTA Metro schedule: %s", feeds.get(kind="metro").summary()["feed_version"])
    except FeedUnavailable as e:
        log.warning("%s Metro tools will report it.", e)

    if args.transport == "stdio":
        anyio.run(mcp.run_stdio_async)
    else:
        anyio.run(
            lambda: mcp.run_streamable_http_async(
                host=args.host, port=args.port, streamable_http_path="/mcp", stateless_http=True
            )
        )


if __name__ == "__main__":
    main()
