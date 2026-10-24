"""Tiny stand-in for www3.septa.org/api, serving responses in SEPTA's real format.

Used by the tests (and handy for offline development):
    python tests/mock_septa_api.py 8765
    SEPTA_API_BASE=http://127.0.0.1:8765/api septa-mcp --transport http
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


def _clock(minutes_from_now: int) -> str:
    """'1:38PM', SEPTA's NextToArrive time format."""
    t = datetime.now(ZoneInfo("America/New_York")) + timedelta(minutes=minutes_from_now)
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'AM' if t.hour < 12 else 'PM'}"


def next_to_arrive(q):
    if q.get("req2") == ["Wilmington"]:  # trains replaced by buses (see ALERTS)
        return []
    return [
        # The real API sometimes keeps serving a train hours after it left.
        {"orig_train": "5301", "orig_line": "Media/Wawa", "orig_departure_time": _clock(-300),
         "orig_arrival_time": _clock(-265), "orig_delay": "4 mins", "isdirect": "true"},
        {"orig_train": "5317", "orig_line": "Media/Wawa", "orig_departure_time": _clock(10),
         "orig_arrival_time": _clock(45), "orig_delay": "3 min", "isdirect": "true"},
        {"orig_train": "5321", "orig_line": "Media/Wawa", "orig_departure_time": _clock(70),
         "orig_arrival_time": _clock(105), "orig_delay": "On time", "isdirect": "true"},
    ][: int(q.get("req3", ["5"])[0]) + 1]


ARRIVALS = {
    "Jefferson Departures: October 24, 2026, 10:20 am": [
        {"Northbound": [
            {"direction": "N", "path": "R2/5N", "train_id": "5314", "origin": "Wawa",
             "destination": "Lansdale", "line": "Lansdale/Doylestown", "status": "On Time",
             "service_type": "LOCAL", "next_station": "Suburban Station",
             "sched_time": "2026-10-24 10:31:00.000", "depart_time": "2026-10-24 10:31:00.000",
             "track": "2", "track_change": None, "platform": "", "platform_change": None}]},
        {"Southbound": [
            {"direction": "S", "path": "R5/3S", "train_id": "5317", "origin": "Doylestown",
             "destination": "Wawa", "line": "Media/Wawa", "status": "3 min",
             "service_type": "LOCAL", "next_station": "Temple U",
             "sched_time": "2026-10-24 10:38:00.000", "depart_time": "2026-10-24 10:38:00.000",
             "track": "3", "track_change": "4", "platform": "", "platform_change": None}]},
    ]
}

TRAIN_VIEW = [
    {"lat": "39.981", "lon": "-75.149", "trainno": "5317", "service": "LOCAL", "dest": "Wawa",
     "currentstop": "Temple U", "nextstop": "Jefferson Station", "line": "Media/Wawa",
     "consist": "862,861,716,705", "heading": "190.1", "late": 3, "SOURCE": "Doylestown",
     "TRACK": "", "TRACK_CHANGE": ""},
]

RR_SCHEDULE = [
    {"station": "Temple U", "sched_tm": "10:31 am", "est_tm": "10:34 am", "act_tm": "10:34 am"},
    {"station": "Jefferson Station", "sched_tm": "10:38 am", "est_tm": "10:41 am", "act_tm": "na"},
    {"station": "Swarthmore", "sched_tm": "11:13 am", "est_tm": "11:16 am", "act_tm": "na"},
]

ALERTS = [
    # The alerts feed still calls this line "Media/Elwyn".
    {"route_id": "rr_route_med", "route_name": "Media/Elwyn", "current_message": None,
     "advisory_message": '<h3 class="separated">Track Work</h3><p>Track work near <b>Secane</b> this weekend; expect delays of up to 10 minutes.</p>',
     "detour_message": "", "last_updated": "Oct 24 2026 08:02:11:000AM"},
    {"route_id": "rr_route_wilm", "route_name": "Wilmington/Newark", "current_message": None,
     "advisory_message": "<p>Shuttle Busing Wilmington Station to Claymont Transit Center</p>",
     "detour_message": "", "last_updated": ""},
    {"route_id": "rr_route_pao", "route_name": "Paoli/Thorndale", "current_message": "",
     "advisory_message": "", "detour_message": "", "last_updated": ""},
    {"route_id": "rr_route_mfo", "route_name": "Market Frankford Owl", "current_message": None,
     "advisory_message": "<p>Owl buses replace trains overnight.</p>", "detour_message": None,
     "last_updated": ""},
    {"route_id": "bus_route_42", "route_name": "42", "current_message": "Detour",
     "advisory_message": "", "detour_message": "", "last_updated": ""},
]

ROUTES = {
    "/api/NextToArrive/index.php": next_to_arrive,
    "/api/Arrivals/index.php": lambda q: {} if q.get("station") == ["Wilmington"] else ARRIVALS,
    "/api/TrainView/index.php": lambda q: TRAIN_VIEW,
    "/api/RRSchedules/index.php": lambda q: RR_SCHEDULE if q.get("req1") == ["5317"] else BadRequest(
        f"An invalid parameter was used. No train found with train #{q.get('req1', [''])[0]}."),
    "/api/Alerts/get_alert_data.php": lambda q: ALERTS,
}

REQUESTS: list[tuple[str, dict]] = []

# Station spellings the real NextToArrive rejects (with HTTP 400), e.g. the GTFS names.
NTA_INVALID = {"Gray 30th St Station", "Wayne", "Morton-Rutledge", "Fort Washington"}


class BadRequest(dict):
    """Real API errors are HTTP 400 with a JSON {"error": ...} body."""

    def __init__(self, msg: str):
        super().__init__(error=msg)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        REQUESTS.append((u.path, q))
        fn = ROUTES.get(u.path)
        data = fn(q) if fn else {"error": "not found"}
        if u.path.startswith("/api/NextToArrive"):
            bad = [n for n in q.get("req1", []) + q.get("req2", []) if n in NTA_INVALID]
            if bad:
                data = BadRequest(f"An error has occurred. '{bad[0]}' is not a valid station,")
        body = json.dumps(data).encode()
        self.send_response(400 if isinstance(data, BadRequest) else 200 if fn else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start(port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    s = start(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"Mock SEPTA API on http://127.0.0.1:{s.server_port}/api")
    threading.Event().wait()
