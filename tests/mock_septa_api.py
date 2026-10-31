"""Tiny stand-in for www3.septa.org/api, serving responses in SEPTA's real format.

Used by the tests (and handy for offline development):
    python tests/mock_septa_api.py 8765
    SEPTA_API_BASE=http://127.0.0.1:8765/api SEPTA_GTFSRT_BASE=http://127.0.0.1:8765/gtfsrt \
        septa-mcp --transport http
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from septa_mcp.gtfsrt import Alert, encode_alerts


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
    {"route_id": "bus_route_42", "route_name": "42", "current_message": None,
     "advisory_message": "", "detour_message": "EB via Spruce St, Left on 38th St, Reg Rt",
     "detour_start_location": "Spruce & 38th", "detour_start_date_time": "10/26/2026   7:41 AM",
     "detour_end_date_time": "10/30/2026   4:00 PM", "detour_reason": "Road Work", "last_updated": ""},
    {"route_id": "trolley_route_10", "route_name": "10", "current_message": None,
     "advisory_message": "<p>Buses replace T1 trolleys west of 40th St this weekend.</p>",
     "detour_message": None, "last_updated": ""},
    {"route_id": "trolley_route_13", "route_name": "13", "current_message": None,
     "advisory_message": None, "detour_message": None, "last_updated": ""},
    {"route_id": "APP", "route_name": "Mobile APP", "current_message": "Update the app",
     "advisory_message": None, "detour_message": None, "last_updated": ""},
]


DAY = 86400


def gtfsrt_alerts(q):
    """SEPTA's GTFS-realtime alerts (bus, trolley and subway): many aren't in ALERTS."""
    now = int(time.time())
    return encode_alerts([
        Alert("1", [(now - 30 * DAY, now + 300 * DAY)], ["L1"], cause="construction", effect="modified service",
              header="11th St Station Closed",
              description="11th St and the 11th St concourse is closed to fast-track elevator construction and "
                          "renovations. Please use 13th St during construction."),
        # Also in ALERTS (as trolley_route_10).
        Alert("2", [(now - DAY, now + DAY)], ["T1"], header="Shuttle Buses",
              description="Buses replace T1 trolleys west of 40th St this weekend."),
        Alert("3", [(now - 3 * DAY, now - DAY)], ["M1"], header="Over", description="Shuttle buses yesterday."),
        Alert("4", [(now + 2 * DAY, now + 6 * DAY)], ["B1"], header="Southbound Platform Boarding Snyder and Oregon",
              description="Passengers must board on the southbound platforms at Snyder and Oregon stations."),
        Alert("5", [], ["76"], effect="detour", header="Motorcycle Toy Run",
              description="SB via Frankford Ave, Right on Girard Ave, Reg Rt"),
        Alert("6", [], trip_ids=["869070"], effect="no service", header="Trip cancellation for trip 869070."),
    ])


def gtfsrt_rail_alerts(q):
    return encode_alerts([
        Alert("7", [(int(time.time()) - DAY, None)], ["WTR"], header="West Trenton Station Waiting Room Closed",
              description="The waiting room at West Trenton Station is closed until further notice."),
    ])


BUS_42 = [
    {"lat": "39.95265", "lng": "-75.20951", "label": "3677", "VehicleID": "3677", "route_id": "42",
     "trip": "880000", "BlockID": "4201", "Direction": "Westbound", "destination": "Wycombe", "heading": 270,
     "late": 6, "next_stop_id": "10266", "next_stop_name": None, "next_stop_sequence": 12,
     "estimated_seat_availability": "FEW_SEATS_AVAILABLE", "Offset": 6, "Offset_sec": "360"},
]


def transit_view(q):
    """TransitView: vehicles on a bus/trolley route. Subway routes come back empty."""
    now = int(datetime.now().timestamp())
    if q.get("route") == ["42"]:
        return {"bus": [{**v, "timestamp": now - 20} for v in BUS_42]}
    if q.get("route") != ["T1"]:
        return []
    return {"bus": [
        {"lat": "39.976337", "lng": "-75.236602", "label": "9024", "route_id": "T1", "trip": "968292",
         "VehicleID": "9024", "BlockID": "9551", "Direction": "Eastbound", "destination": "13th St",
         "heading": 81.36, "late": 3, "next_stop_id": "20611", "next_stop_name": None,
         "next_stop_sequence": 3, "estimated_seat_availability": "MANY_SEATS_AVAILABLE", "Offset": 3,
         "Offset_sec": "180", "timestamp": now - 30},
        # No schedule estimate.
        {"lat": "39.955208", "lng": "-75.19216", "label": "9084", "route_id": "T1", "trip": "968269",
         "VehicleID": "9084", "BlockID": "9552", "Direction": "Westbound", "destination": "63rd-Malvern",
         "heading": 113.18, "late": 998, "next_stop_id": None, "next_stop_name": "Lancaster Av & 40th St",
         "next_stop_sequence": None, "estimated_seat_availability": "NOT_AVAILABLE", "Offset": 998,
         "Offset_sec": "0", "timestamp": now - 60},
        # Hasn't reported for an hour: parked, but still listed.
        {"lat": "39.96", "lng": "-75.25", "label": "9001", "route_id": "T1", "trip": "968000",
         "VehicleID": "9001", "BlockID": "9550", "Direction": "Eastbound", "destination": "13th St",
         "heading": 0, "late": 0, "next_stop_id": "20611", "next_stop_name": None, "next_stop_sequence": 1,
         "estimated_seat_availability": "NOT_AVAILABLE", "Offset": 0, "Offset_sec": "0",
         "timestamp": now - 3600},
    ]}

def _place(name, kind, category, lat, lon, bbox=None, **extra):
    """One result as Nominatim's /search?format=jsonv2 gives it."""
    s, n, w, e = bbox or (lat - 0.0005, lat + 0.0005, lon - 0.0005, lon + 0.0005)
    return {"place_id": 1, "lat": str(lat), "lon": str(lon), "category": category, "type": kind,
            "addresstype": kind, "name": name, "display_name": f"{name}, Philadelphia, Pennsylvania, United States",
            "boundingbox": [str(s), str(n), str(w), str(e)], "importance": 0.5, **extra}


def _square(lat, lon, d):
    return {"type": "Polygon", "coordinates": [[[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d],
                                                [lon - d, lat + d], [lon - d, lat - d]]]}


# Stand-in for a Nominatim geocoder (SEPTA_GEOCODER_URL), by lower-cased query.
PLACES = {
    "reading terminal market": [_place("Reading Terminal Market", "amenity", "amenity", 39.9533, -75.1590)],
    "1500 market st": [_place("1500 Market Street", "building", "building", 39.9524, -75.1659)],
    # A neighborhood the map has only as a point.
    "fishtown": [_place("Fishtown", "neighbourhood", "place", 39.9718, -75.1340)],
    # A borough with an outline (when asked for one), and a namesake elsewhere.
    "media": [_place("Media", "town", "boundary", 39.9168, -75.3877, (39.908, 39.925, -75.397, -75.378)),
              _place("Media Theatre", "amenity", "amenity", 39.9180, -75.3880)],
}


def geocoder(q):
    hits = PLACES.get(q.get("q", [""])[0].strip().lower(), [])
    if q.get("polygon_geojson") == ["1"]:
        hits = [{**h, "geojson": _square(float(h["lat"]), float(h["lon"]), 0.008)} if h["category"] == "boundary"
                else h for h in hits]
    return hits[: int(q.get("limit", ["10"])[0])]


ROUTES = {
    "/api/NextToArrive/index.php": next_to_arrive,
    "/api/Arrivals/index.php": lambda q: {} if q.get("station") == ["Wilmington"] else ARRIVALS,
    "/api/TrainView/index.php": lambda q: TRAIN_VIEW,
    "/api/RRSchedules/index.php": lambda q: RR_SCHEDULE if q.get("req1") == ["5317"] else BadRequest(
        f"An invalid parameter was used. No train found with train #{q.get('req1', [''])[0]}."),
    "/api/Alerts/get_alert_data.php": lambda q: ALERTS,
    "/gtfsrt/septa-pa-us/Service/rtServiceAlerts.pb": gtfsrt_alerts,
    "/gtfsrt/septarail-pa-us/Service/rtServiceAlerts.pb": gtfsrt_rail_alerts,
    "/nominatim/search": geocoder,
    "/api/TransitView/index.php": transit_view,
    # Every route at once; the real one is ~300 KB.
    "/api/TransitViewAll/index.php": lambda q: {"routes": [{
        "42": transit_view({"route": ["42"]})["bus"],
        "T1": transit_view({"route": ["T1"]})["bus"],
    }]},
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
        binary = isinstance(data, bytes)
        body = data if binary else json.dumps(data).encode()
        self.send_response(400 if isinstance(data, BadRequest) else 200 if fn else 404)
        self.send_header("Content-Type", "application/x-protobuf" if binary else "application/json")
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
    print(f"Mock SEPTA API on http://127.0.0.1:{s.server_port}/api (GTFS-realtime under /gtfsrt)")
    threading.Event().wait()
