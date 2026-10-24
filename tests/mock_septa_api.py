"""Tiny stand-in for www3.septa.org/api, serving responses in SEPTA's real format.

Used by the tests (and handy for offline development):
    python tests/mock_septa_api.py 8765
    SEPTA_API_BASE=http://127.0.0.1:8765/api septa-mcp --transport http
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NEXT_TO_ARRIVE = [
    {"orig_train": "5317", "orig_line": "Media/Wawa", "orig_departure_time": "10:38AM",
     "orig_arrival_time": "11:13AM", "orig_delay": "3 min", "isdirect": "true"},
    {"orig_train": "5321", "orig_line": "Media/Wawa", "orig_departure_time": "11:38AM",
     "orig_arrival_time": "12:13PM", "orig_delay": "On time", "isdirect": "true"},
]

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
    {"route_id": "rr_route_med", "route_name": "Media/Wawa", "current_message": "",
     "advisory_message": "<p>Track work near <b>Secane</b> this weekend; expect delays of up to 10 minutes.</p>",
     "detour_message": "", "last_updated": "Oct 24 2026 08:02:11:000AM"},
    {"route_id": "rr_route_pao", "route_name": "Paoli/Thorndale", "current_message": "",
     "advisory_message": "", "detour_message": "", "last_updated": ""},
    {"route_id": "bus_route_42", "route_name": "42", "current_message": "Detour",
     "advisory_message": "", "detour_message": "", "last_updated": ""},
]

ROUTES = {
    "/api/NextToArrive/index.php": lambda q: NEXT_TO_ARRIVE[: int(q.get("req3", ["5"])[0])],
    "/api/Arrivals/index.php": lambda q: ARRIVALS,
    "/api/TrainView/index.php": lambda q: TRAIN_VIEW,
    "/api/RRSchedules/index.php": lambda q: RR_SCHEDULE if q.get("req1") == ["5317"] else [],
    "/api/Alerts/get_alert_data.php": lambda q: ALERTS,
}

REQUESTS: list[tuple[str, dict]] = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        REQUESTS.append((u.path, q))
        fn = ROUTES.get(u.path)
        body = json.dumps(fn(q) if fn else {"error": "not found"}).encode()
        self.send_response(200 if fn else 404)
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
