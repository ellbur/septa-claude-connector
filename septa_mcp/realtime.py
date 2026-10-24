"""Thin client for SEPTA's real-time JSON API (https://www3.septa.org/api/).

The real-time API predates the current GTFS feed and uses its own station names for
a handful of stops; `api_station_name` translates GTFS names to the ones it accepts.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = os.environ.get("SEPTA_API_BASE", "https://www3.septa.org/api")
USER_AGENT = "septa-mcp/0.1"
TIMEOUT = float(os.environ.get("SEPTA_API_TIMEOUT", "10"))

# GTFS stop_name -> name the real-time API expects (only where they differ).
_API_NAMES = {
    "Airport Terminals C & D": "Airport Terminal C-D",
    "Airport Terminals E & F": "Airport Terminal E-F",
    "Chelten Av": "Chelten Avenue",
    "Chester Transit Center": "Chester T.C.",
    "Claymont Transit Center": "Claymont",
    "Highland Av": "Highland Avenue",
    "Main St": "Main Street",
    "Morton": "Morton-Rutledge",
    "Prospect Park": "Prospect Park - Moore",
    "Queen Ln": "Queen Lane",
    "Richard Allen Ln": "Richard Allen Lane",
    "Temple University": "Temple U",
    "Trenton Transit Center": "Trenton",
    "Washington Ln": "Washington Lane",
    "Wynnefield Av": "Wynnefield Avenue",
}


def api_station_name(gtfs_name: str) -> str:
    return _API_NAMES.get(gtfs_name, gtfs_name)


class SeptaAPIError(RuntimeError):
    pass


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    q = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    url = f"{BASE_URL}{path}"
    if q:
        url += "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
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


def _boolish(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes", "y")


def _clean(v: Any) -> Any:
    if v in (None, "", "null"):
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
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", str(v), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text or None


# -- endpoints -----------------------------------------------------------------


def next_to_arrive(origin: str, dest: str, count: int = 5) -> list[dict]:
    data = _get(
        "/NextToArrive/index.php",
        {"req1": api_station_name(origin), "req2": api_station_name(dest), "req3": count},
    )
    out = []
    for r in data if isinstance(data, list) else []:
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
                        "line": t.get("line"),
                        "destination": t.get("destination"),
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


def rail_alerts() -> list[dict]:
    data = _get("/Alerts/get_alert_data.php")
    out = []
    for r in data if isinstance(data, list) else []:
        rid = str(r.get("route_id", ""))
        if not rid.startswith("rr_route") and rid != "generic":
            continue
        msgs = {
            "current": _strip_html(r.get("current_message")),
            "advisory": _strip_html(r.get("advisory_message")),
            "detour": _strip_html(r.get("detour_message")),
        }
        if not any(msgs.values()):
            continue
        out.append(
            {
                "route_id": rid,
                "route_name": r.get("route_name"),
                **{k: v for k, v in msgs.items() if v},
                "last_updated": r.get("last_updated"),
            }
        )
    return out
