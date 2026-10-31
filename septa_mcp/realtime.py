"""Thin client for SEPTA's real-time JSON API (https://www3.septa.org/api/): Regional Rail
boards and train positions, trolley and bus positions (TransitView), and alerts, which
also come from SEPTA's GTFS-realtime alert feeds (https://www3.septa.org/gtfsrt/).

The real-time API predates the current GTFS feed and uses its own station names for
a couple dozen stops; `api_station_name` translates GTFS names to the ones it accepts.
Bad parameters come back as HTTP 400 with a JSON {"error": ...} body.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import gtfsrt

BASE_URL = os.environ.get("SEPTA_API_BASE", "https://www3.septa.org/api")
GTFSRT_BASE = os.environ.get("SEPTA_GTFSRT_BASE", "https://www3.septa.org/gtfsrt")
# Bus, trolley and subway alerts; Regional Rail alerts.
GTFSRT_ALERT_PATHS = ("/septa-pa-us/Service/rtServiceAlerts.pb", "/septarail-pa-us/Service/rtServiceAlerts.pb")
USER_AGENT = "septa-mcp/0.1"
TIMEOUT = float(os.environ.get("SEPTA_API_TIMEOUT", "10"))
# Identical requests within CACHE_SECONDS share one response, and at most MAX_PER_MINUTE
# requests go to SEPTA in any 60-second window. Both are per process; deploy with a
# single instance so the limit holds for the whole app.
CACHE_SECONDS = float(os.environ.get("SEPTA_API_CACHE_SECONDS", "10"))
MAX_PER_MINUTE = int(os.environ.get("SEPTA_API_MAX_PER_MINUTE", "60"))
# Alerts change slowly and the Metro tools fetch them with every answer, so keep them longer.
ALERTS_CACHE_SECONDS = float(os.environ.get("SEPTA_ALERTS_CACHE_SECONDS", "60"))
TZ = ZoneInfo("America/New_York")

# GTFS stop_name -> name the real-time API expects (only where they differ).
# Checked against the live NextToArrive and Arrivals endpoints for every GTFS station.
_API_NAMES = {
    "9th St Lansdale": "9th Street Lansdale",
    "Airport Terminals C & D": "Airport Terminal C-D",
    "Airport Terminals E & F": "Airport Terminal E-F",
    "Chelten Av": "Chelten Avenue",
    "Chester Transit Center": "Chester TC",
    "Churchman's Crossing": "Churchmans Crossing",
    "Claymont Transit Center": "Claymont",
    "Delaware Valley University": "Delaware Valley College",
    "Eastwick": "Eastwick Station",
    "Elwyn": "Elwyn Station",
    "Fern Rock Transit Center": "Fern Rock TC",
    "Fernwood-Yeadon": "Fernwood",
    "Fort Washington": "Ft Washington",
    "Gray 30th St Station": "30th Street Station",
    "Highland Av": "Highland Ave",
    "Holmesburg Junction": "Holmesburg Jct",
    "Mount Airy": "Mt Airy",
    "Newark DE": "Newark",
    "Norristown Elm Street": "Elm St",
    "Norristown Transit Center": "Norristown TC",
    "North Broad": "North Broad St",
    # The real-time API has a single "North Philadelphia" for both stations.
    "North Philadelphia Amtrak": "North Philadelphia",
    "North Philadelphia Septa": "North Philadelphia",
    "Queen Ln": "Queen Lane",
    "Richard Allen Ln": "Allen Lane",
    "Temple University": "Temple U",
    "Trenton Transit Center": "Trenton",
    "Washington Ln": "Washington Lane",
    "Wynnefield Av": "Wynnefield Avenue",
}

# NextToArrive alone wants these (Arrivals rejects them and takes the GTFS name).
_NTA_NAMES = {
    "Wayne": "Wayne Station",
}


def api_station_name(gtfs_name: str, next_to_arrive: bool = False) -> str:
    if next_to_arrive and gtfs_name in _NTA_NAMES:
        return _NTA_NAMES[gtfs_name]
    return _API_NAMES.get(gtfs_name, gtfs_name)


class SeptaAPIError(RuntimeError):
    pass


_cache: dict[str, tuple[float, Any]] = {}  # url -> (expires, data)
_url_locks: dict[str, threading.Lock] = {}
_sent: list[float] = []  # times of requests sent in the last minute
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _sent.clear()


def _take_slot(now: float) -> float:
    """Reserve a request slot. Returns 0 if one was free, else seconds until one frees up."""
    with _lock:
        while _sent and _sent[0] <= now - 60:
            _sent.pop(0)
        if len(_sent) >= MAX_PER_MINUTE:
            return _sent[0] + 60 - now
        _sent.append(now)
        return 0


def _get(path: str, params: dict[str, Any] | None = None, ttl: float | None = None) -> Any:
    q = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    url = f"{BASE_URL}{path}"
    if q:
        url += "?" + urllib.parse.urlencode(q)
    return _cached(url, _fetch, ttl)


def _cached(url: str, fetch, ttl: float | None = None) -> Any:
    """fetch(url), or its answer from the last ttl (default CACHE_SECONDS) seconds."""
    with _lock:
        url_lock = _url_locks.setdefault(url, threading.Lock())
    # One request per URL at a time, so concurrent callers wait for the first one's answer.
    with url_lock:
        now = time.monotonic()
        with _lock:
            hit = _cache.get(url)
        if hit and hit[0] > now:
            return hit[1]
        wait = _take_slot(now)
        if wait:
            raise SeptaAPIError(
                f"Live SEPTA data is paused: this server's limit of {MAX_PER_MINUTE} requests per minute "
                f"to SEPTA, shared by all its users, was reached. Try again in about {max(1, round(wait))} seconds."
            )
        data = fetch(url)
        with _lock:
            if len(_cache) > 500:
                for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
                    del _cache[k]
                    if not _url_locks[k].locked():
                        del _url_locks[k]
            _cache[url] = (now + (CACHE_SECONDS if ttl is None else ttl), data)
        return data


def _fetch(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # SEPTA reports bad stations / unknown trains as 400 {"error": "..."}.
        try:
            msg = json.loads(e.read().decode("utf-8", "replace"))["error"]
        except Exception:  # noqa: BLE001
            raise SeptaAPIError(f"SEPTA real-time API returned HTTP {e.code}") from e
        raise SeptaAPIError(f"SEPTA API error: {html.unescape(str(msg))}") from e
    except Exception as e:  # noqa: BLE001
        raise SeptaAPIError(f"SEPTA real-time API unreachable ({e})") from e
    body = body.strip()
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise SeptaAPIError(f"SEPTA API returned non-JSON: {body[:200]}") from e
    if isinstance(data, dict) and "error" in data and len(data) == 1:
        raise SeptaAPIError(f"SEPTA API error: {data['error']}")
    return data


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise SeptaAPIError(f"SEPTA GTFS-realtime feed returned HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise SeptaAPIError(f"SEPTA GTFS-realtime feed unreachable ({e})") from e


def _boolish(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes", "y")


def _clean(v: Any) -> Any:
    if v in (None, "", "null", "na"):
        return None
    return v


def _clock(v: Any) -> Any:
    """'2026-10-24 12:40:00.000' (local time) -> '12:40 PM'; anything else unchanged."""
    m = re.match(r"\d{4}-\d{2}-\d{2}[ T](\d{1,2}):(\d{2})", str(v or ""))
    if not m:
        return _clean(v)
    h, mi = int(m.group(1)), m.group(2)
    return f"{(h % 12) or 12}:{mi} {'AM' if h < 12 else 'PM'}"


def _strip_html(v: Any) -> str | None:
    if not v:
        return None
    text = re.sub(r"<br\s*/?>|</(?:p|li|div|h[1-6])>", "\n", str(v), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text or None


# -- endpoints -----------------------------------------------------------------


def _is_stale(depart: Any, delay: Any, now: datetime) -> bool:
    """NextToArrive sometimes keeps listing a train hours after it left (e.g. a 7:09AM
    departure still shown at 1PM). Treat anything well past its departure as gone."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*([AP])M\s*", str(depart or ""), re.I)
    if not m:
        return False
    h = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "P" else 0)
    dt = now.replace(hour=h, minute=int(m.group(2)), second=0, microsecond=0)
    if dt - now > timedelta(hours=12):  # e.g. 11:50PM seen at 12:10AM
        dt -= timedelta(days=1)
    elif now - dt > timedelta(hours=12):  # e.g. 12:10AM seen at 11:50PM
        dt += timedelta(days=1)
    late = re.match(r"\s*(\d+)", str(delay or ""))
    return dt + timedelta(minutes=int(late.group(1)) if late else 0) + timedelta(minutes=30) < now


def next_to_arrive(origin: str, dest: str, count: int = 5, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(TZ)
    data = _get(
        "/NextToArrive/index.php",
        {
            "req1": api_station_name(origin, next_to_arrive=True),
            "req2": api_station_name(dest, next_to_arrive=True),
            "req3": count,
        },
    )
    out = []
    for r in data if isinstance(data, list) else []:
        if _is_stale(r.get("orig_departure_time"), r.get("orig_delay"), now):
            continue
        item = {
            "train": r.get("orig_train"),
            "line": r.get("orig_line"),
            "depart": r.get("orig_departure_time"),
            "arrive": r.get("orig_arrival_time"),
            "status": r.get("orig_delay"),
            "direct": _boolish(r.get("isdirect")),
        }
        if not item["direct"]:
            item["connection"] = {
                "at": r.get("Connection"),
                "train": r.get("term_train"),
                "line": r.get("term_line"),
                "depart": r.get("term_depart_time"),
                "arrive": r.get("term_arrival_time"),
                "status": r.get("term_delay"),
            }
        out.append(item)
    return out


def arrivals(station: str, count: int = 10, direction: str | None = None) -> dict:
    """Departure board for one station, split Northbound/Southbound."""
    data = _get(
        "/Arrivals/index.php",
        {"station": api_station_name(station), "results": count, "direction": direction},
    )
    # The single top-level key is a heading like "Temple U Departures: <date>, <time>".
    # Its timestamp is in UTC, not local time, so it is deliberately not passed on.
    result: dict[str, Any] = {"northbound": [], "southbound": []}
    if not isinstance(data, dict) or not data:
        return result
    _heading, groups = next(iter(data.items()))
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        for key, trains in group.items():
            bucket = "northbound" if key.lower().startswith("north") else "southbound"
            for t in trains or []:
                result[bucket].append(
                    {
                        "train": t.get("train_id"),
                        "final_stop": t.get("destination"),
                        "origin": t.get("origin"),
                        "scheduled": _clock(t.get("sched_time")),
                        "departs": _clock(t.get("depart_time")),
                        "status": t.get("status"),
                        "service": t.get("service_type"),
                        "next_station": _clean(t.get("next_station")),
                        "track": _clean(t.get("track_change")) or _clean(t.get("track")),
                        "platform": _clean(t.get("platform_change")) or _clean(t.get("platform")),
                    }
                )
    return result


def train_view() -> list[dict]:
    data = _get("/TrainView/index.php")
    return data if isinstance(data, list) else []


def train_position(train: str) -> dict | None:
    for t in train_view():
        if str(t.get("trainno")) == str(train):
            late = t.get("late")
            return {
                "train": t.get("trainno"),
                "line": t.get("line"),
                "service": t.get("service"),
                "from": t.get("SOURCE"),
                "to": t.get("dest"),
                "current_stop": t.get("currentstop"),
                "next_stop": t.get("nextstop"),
                "minutes_late": late,
                "status": "On time" if late in (0, "0") else (f"{late} min late" if late is not None else None),
                "track": _clean(t.get("TRACK_CHANGE")) or _clean(t.get("TRACK")),
                "cars": _clean(t.get("consist")),
                "lat": t.get("lat"),
                "lon": t.get("lon"),
            }
    return None


def train_schedule_live(train: str) -> list[dict]:
    data = _get("/RRSchedules/index.php", {"req1": train})
    out = []
    for r in data if isinstance(data, list) else []:
        out.append(
            {
                "station": r.get("station"),
                "scheduled": _clean(r.get("sched_tm")),
                "estimated": _clean(r.get("est_tm")),
                "actual": _clean(r.get("act_tm")),
            }
        )
    return out


# The alerts feed files the subway and NHSL under rr_route_* too.
_METRO_RR_ROUTES = {"rr_route_bsl", "rr_route_bso", "rr_route_mfl", "rr_route_mfo", "rr_route_nhsl"}


def _alert_mode(route_id: str) -> str | None:
    if route_id == "generic":
        return "systemwide"
    if route_id in _METRO_RR_ROUTES or route_id.startswith("trolley_route"):
        return "metro"
    if route_id.startswith("rr_route"):
        return "regional_rail"
    if route_id.startswith("bus_route"):
        return "bus"
    return None  # the app, paratransit (CCT)


def alerts() -> list[dict]:
    """Current alerts for every mode, each tagged with its mode: regional_rail, metro,
    bus or systemwide. Rows with no message are left out.

    The JSON alerts API misses many alerts that SEPTA posts only to its GTFS-realtime
    feeds (station closures, Metro shuttle buses, platform changes, elevator outages), so
    those are added from there. If the GTFS-realtime feeds can't be read, the JSON alerts
    are still returned.
    """
    data = _get("/Alerts/get_alert_data.php", ttl=ALERTS_CACHE_SECONDS)
    out = []
    for r in data if isinstance(data, list) else []:
        rid = str(r.get("route_id", ""))
        mode = _alert_mode(rid)
        if not mode:
            continue
        msgs = {
            "current": _strip_html(r.get("current_message")),
            "advisory": _strip_html(r.get("advisory_message")),
            "detour": _strip_html(r.get("detour_message")),
        }
        if not any(msgs.values()):
            continue
        alert = {
            "route_id": rid,
            "route_name": r.get("route_name"),
            "mode": mode,
            # last_updated is deliberately dropped: it's the route row's timestamp
            # (years old for most lines), not when the message was posted.
            **{k: v for k, v in msgs.items() if v},
        }
        if msgs["detour"]:
            for key, field in (
                ("detour_reason", "detour_reason"),
                ("detour_from", "detour_start_location"),
                ("detour_starts", "detour_start_date_time"),
                ("detour_ends", "detour_end_date_time"),
            ):
                v = _clean(re.sub(r"\s+", " ", str(r.get(field) or "")).strip())
                if v:
                    alert[key] = v
        out.append(alert)
    return out + _gtfsrt_alerts(out)


# GTFS route_id -> the line name the JSON alerts API uses for Regional Rail.
_RAIL_LINES = {
    "AIR": "Airport", "CHE": "Chestnut Hill East", "CHW": "Chestnut Hill West", "CYN": "Cynwyd",
    "FOX": "Fox Chase", "LAN": "Lansdale/Doylestown", "MED": "Media/Wawa", "NOR": "Manayunk/Norristown",
    "PAO": "Paoli/Thorndale", "TRE": "Trenton", "WAR": "Warminster", "WIL": "Wilmington/Newark",
    "WTR": "West Trenton",
}
# Metro line codes ("L1", "T3"); the feed also has "L1 OWL" for the overnight bus.
_METRO_ROUTE = re.compile(r"^([BDGLMT]\d+)\b")
# How much of a message to compare when deciding whether both sources have an alert.
_SAME_TEXT_CHARS = 60


def _squash(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def _gtfsrt_alerts(have: list[dict], now: datetime | None = None) -> list[dict]:
    """Alerts in SEPTA's GTFS-realtime feeds that aren't already in `have` (the JSON
    API's), in the same shape. Alerts that name only trips (single cancelled bus trips)
    are left out."""
    parsed = []
    for path in GTFSRT_ALERT_PATHS:
        try:
            parsed += _cached(
                GTFSRT_BASE + path, lambda url: gtfsrt.parse_alerts(_fetch_bytes(url)), ALERTS_CACHE_SECONDS
            )
        except (SeptaAPIError, ValueError, IndexError):
            continue  # the JSON alerts still stand
    seen = " ".join(_squash(a.get(k)) for a in have for k in ("current", "advisory", "detour"))
    now_ts = (now or datetime.now(TZ)).timestamp()
    out = []
    for a in parsed:
        header = _strip_html(a.header)
        text = _strip_html(a.description) or header
        if not text or not a.route_ids:
            continue
        if a.periods and all(end and end < now_ts for _, end in a.periods):
            continue  # over
        key = _squash(text)[:_SAME_TEXT_CHARS]
        if key in seen:
            continue
        seen += " " + key
        upcoming = [p for p in a.periods if not p[1] or p[1] >= now_ts]
        start, end = min(upcoming, key=lambda p: p[0] or 0) if upcoming else (None, None)
        # a.effect is left out: SEPTA's are often wrong (delays filed as "additional service").
        common = {}
        if start and start > now_ts:
            common["upcoming"] = True
        if a.url and a.url.rstrip("/") != "https://www.septa.org/alerts":
            common["url"] = a.url

        when = {k: _when(t) for k, t in (("starts", start), ("ends", end)) if t}
        body = text if not header or _squash(text).startswith(_squash(header)) else f"{header}\n{text}"
        advisory = {"advisory": body, **when}
        # A bus detour: the header is the reason, the text the routing.
        detour = {"detour": text, **({"detour_reason": header} if header and header != text else {}),
                  **{f"detour_{k}": v for k, v in when.items()}}

        metro_lines = list(dict.fromkeys(m.group(1) for r in a.route_ids if (m := _METRO_ROUTE.match(r))))
        if metro_lines:
            out.append({"route_id": a.route_ids[0], "route_name": ", ".join(metro_lines), "mode": "metro",
                        "lines": metro_lines, **advisory, **common})
        for r in a.route_ids:
            if r in _RAIL_LINES:
                out.append({"route_id": r, "route_name": _RAIL_LINES[r], "mode": "regional_rail",
                            **advisory, **common})
            elif not _METRO_ROUTE.match(r):
                out.append({"route_id": r, "route_name": r, "mode": "bus",
                            **(detour if a.effect == "detour" else advisory), **common})
    return out


def rail_alerts() -> list[dict]:
    return [a for a in alerts() if a["mode"] in ("regional_rail", "systemwide")]


# TransitView's "late" when the vehicle has no schedule estimate.
_NO_ESTIMATE = {998, 999}
# Vehicles that haven't reported for this long are left out (often parked, still listed).
STALE_REPORT_MINUTES = 15


def transit_view(route: str, now: datetime | None = None) -> list[dict]:
    """Vehicles on a bus, trolley or NHSL route right now. The subway lines aren't in it.

    trip is the GTFS trip_id the vehicle is running; minutes_late is negative when early
    and None when SEPTA has no estimate.
    """
    data = _get("/TransitView/index.php", {"route": route})
    vehicles = data.get("bus") if isinstance(data, dict) else None
    return _vehicles(vehicles, now or datetime.now(TZ))


def transit_view_all(now: datetime | None = None) -> dict[str, list[dict]]:
    """Every bus, trolley and NHSL vehicle right now, by route, in one request (~300 KB):
    cheaper than transit_view per route at stops many routes serve."""
    data = _get("/TransitViewAll/index.php")
    groups = data.get("routes") if isinstance(data, dict) else None
    now = now or datetime.now(TZ)
    out: dict[str, list[dict]] = {}
    for group in groups if isinstance(groups, list) else []:
        for route, vehicles in (group.items() if isinstance(group, dict) else []):
            out.setdefault(str(route), []).extend(_vehicles(vehicles, now))
    return out


def _vehicles(vehicles: Any, now: datetime) -> list[dict]:
    out = []
    for v in vehicles if isinstance(vehicles, list) else []:
        if not isinstance(v, dict):
            continue
        try:
            reported = datetime.fromtimestamp(int(v.get("timestamp")), TZ)
        except (TypeError, ValueError):
            reported = None
        if reported and now - reported > timedelta(minutes=STALE_REPORT_MINUTES):
            continue
        try:
            late = int(v.get("late"))
        except (TypeError, ValueError):
            late = None
        if late in _NO_ESTIMATE:
            late = None
        seats = _clean(v.get("estimated_seat_availability"))
        out.append(
            {
                "vehicle": _clean(v.get("label")) or _clean(v.get("VehicleID")),
                "trip": _clean(v.get("trip")),
                "direction": _clean(v.get("Direction")),
                "destination": _clean(v.get("destination")),
                "minutes_late": late,
                "next_stop_id": _clean(v.get("next_stop_id")),
                "next_stop": _clean(v.get("next_stop_name")),
                "next_stop_sequence": _clean(v.get("next_stop_sequence")),
                "seats": None if seats == "NOT_AVAILABLE" else seats,
                "reported_at": reported.strftime("%I:%M %p").lstrip("0") if reported else None,
                "lat": _clean(v.get("lat")),
                "lon": _clean(v.get("lng")),
            }
        )
    return out


def late_status(minutes_late: int | None) -> str | None:
    if minutes_late is None:
        return None
    if minutes_late == 0:
        return "on time"
    return f"{minutes_late} min late" if minutes_late > 0 else f"{-minutes_late} min early"
