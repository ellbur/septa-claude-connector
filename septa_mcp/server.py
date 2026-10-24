"""MCP server exposing SEPTA Regional Rail schedules and real-time status as tools."""

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

from . import realtime
from .gtfs import TZ, AmbiguousStation, FeedManager, StationNotFound, fmt_time, ride_to_dict

log = logging.getLogger("septa_mcp")

INSTRUCTIONS = """\
SEPTA Regional Rail (Philadelphia commuter trains) schedules and live status.

- Use train_schedule for "what times do trains leave X for Y on <day>" questions, or any
  future date. It reads SEPTA's published GTFS timetable, handles weekday/weekend/holiday
  service, and treats through-routed trains (which change line name in Center City) as
  one train.
- Use next_trains for "when's the next train" right now; it includes live delays.
- Use station_departures for a station's live departure board (tracks, delays).
- Use train_status for where a specific train number is and how late it is.
- Use rail_alerts for service disruptions.
- Station names are matched loosely ("Jefferson", "30th Street", "Temple"). If a name is
  ambiguous the tool returns the candidates; ask the user or pick from context.
All times are America/New_York local time. This covers Regional Rail only, not buses,
the Metro (subway/trolley) lines, or NJ Transit/Amtrak.
"""

mcp = MCPServer(
    name="septa",
    title="SEPTA Regional Rail",
    description="SEPTA Regional Rail schedules (GTFS) and real-time train status.",
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
    feed = feeds.get()
    try:
        origin = feed.resolve_station(from_station)
        dest = feed.resolve_station(to_station)
    except (StationNotFound, AmbiguousStation) as e:
        return _station_error(e)
    if origin.stop_id == dest.stop_id:
        return {"error": "Origin and destination are the same station."}

    try:
        day = parse_date(date)
        after = parse_clock(depart_after)
        before = parse_clock(depart_before)
    except ValueError as e:
        return {"error": str(e)}

    current = now()
    if after is None:
        after = current.time().replace(second=0, microsecond=0) if day == current.date() else dtime(0, 0)
    ws = datetime.combine(day, after, TZ)
    we = datetime.combine(day, before, TZ) if before else datetime.combine(day + timedelta(days=1), dtime(3, 30), TZ)
    if we < ws:  # e.g. 22:00 to 01:00
        we += timedelta(days=1)

    result: dict = {
        "from": origin.name,
        "to": dest.name,
        "date": day.isoformat(),
        "weekday": WEEKDAYS[day.weekday()].capitalize(),
        "window": f"{fmt_time(ws)} – {fmt_time(we)}",
    }

    vs, ve = feed.feed_range()
    if vs and ve and not (vs <= day <= ve):
        result["warning"] = (
            f"{day.isoformat()} is outside the published schedule ({vs.isoformat()} to "
            f"{ve.isoformat()}); SEPTA hasn't released timetables for that date yet, so results "
            "may be empty or inaccurate."
        )

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
                    "depart": fmt_time(l1.depart_dt),
                    "arrive": fmt_time(l2.arrive_dt),
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

    result["schedule_source"] = feed.summary()
    return result


@mcp.tool(annotations=READ_ONLY)
async def next_trains(from_station: str, to_station: str, count: int = 5) -> dict:
    """Next trains from one station to another right now, with live delay status.

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
        }
    result = {**base, "source": "SEPTA real-time API", "trains": trains}
    if not trains:
        result.update(await anyio.to_thread.run_sync(_station_alerts, origin, dest))
    return result


@mcp.tool(annotations=READ_ONLY)
async def station_departures(
    station: str,
    count: int = 10,
    direction: Literal["N", "S"] | None = None,
) -> dict:
    """Live departure board for a Regional Rail station: upcoming trains, destinations,
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
async def rail_alerts(line: str | None = None) -> dict:
    """Current SEPTA Regional Rail service alerts and advisories. Optionally filter by
    line name, e.g. "Media/Wawa" or "Paoli"."""
    try:
        alerts = await anyio.to_thread.run_sync(realtime.rail_alerts)
    except realtime.SeptaAPIError as e:
        return {"error": str(e)}
    if line:
        alerts = _alerts_for_lines(alerts, [line]) or alerts
    return {"alerts": alerts, "count": len(alerts)}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description="SEPTA Regional Rail MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("MCP_TRANSPORT", "stdio"))
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = p.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    # Load (and if needed download) the timetable up front so the first question is fast.
    log.info("Loaded SEPTA rail schedule: %s", feeds.get().summary())

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
