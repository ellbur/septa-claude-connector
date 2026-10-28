"""End-to-end tests through a real MCP client.

Schedule tests need a GTFS file: set SEPTA_GTFS_ZIP to a downloaded gtfs_public.zip
(or google_rail.zip), otherwise the server downloads the current one.
Live-API tests use the mock in mock_septa_api.py.
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import mock_septa_api  # noqa: E402

_mock = mock_septa_api.start()
os.environ["SEPTA_API_BASE"] = f"http://127.0.0.1:{_mock.server_port}/api"

from mcp import Client  # noqa: E402

from septa_mcp import gtfs, metro, realtime, server  # noqa: E402
from septa_mcp.server import parse_clock, parse_date  # noqa: E402

# In case realtime was imported (e.g. by test_live.py) before the env var was set.
realtime.BASE_URL = os.environ["SEPTA_API_BASE"]


@pytest.fixture(autouse=True)
def _fresh_api_cache():
    realtime.clear_cache()


def call(tool: str, args: dict) -> dict:
    async def go():
        async with Client(server.mcp) as c:
            r = await c.call_tool(tool, args)
            assert not r.is_error, r
            if r.structured_content is not None:
                sc = r.structured_content
                return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
            return json.loads(r.content[0].text)

    return anyio.run(go)


def next_weekday(wd: int) -> date:
    feed = server.feeds.get()
    start, end = feed.feed_range()
    d = max(start, date.today())
    while d.weekday() != wd:
        d = date.fromordinal(d.toordinal() + 1)
    assert d <= end
    return d


# -- parsing -------------------------------------------------------------------


def test_parse_date():
    sat = date(2026, 10, 24)  # a Saturday
    assert parse_date("saturday", sat) == sat
    assert parse_date("next saturday", sat) == date(2026, 10, 31)
    assert parse_date("Sun", sat) == date(2026, 10, 25)
    assert parse_date("tomorrow", sat) == date(2026, 10, 25)
    assert parse_date("2026-11-03", sat) == date(2026, 11, 3)
    assert parse_date("11/3", sat) == date(2026, 11, 3)


def test_parse_clock():
    assert parse_clock("7:30 AM").hour == 7
    assert parse_clock("12am").hour == 0
    assert parse_clock("12 pm").hour == 12
    assert parse_clock("19:05").minute == 5
    assert parse_clock("noon").hour == 12


# -- schedule (GTFS) -------------------------------------------------------------


def test_jefferson_to_swarthmore_saturday_morning():
    sat = next_weekday(5)
    r = call("train_schedule", {
        "from_station": "Jefferson", "to_station": "Swarthmore",
        "date": sat.isoformat(), "depart_after": "05:00", "depart_before": "12:00",
    })
    assert r["from"] == "Jefferson Station" and r["to"] == "Swarthmore"
    assert r["weekday"] == "Saturday"
    assert r["window"] == f"{sat:%a %b} {sat.day}, 5:00 AM – 12:00 PM"
    trains = r["trains"]
    # Saturday Media/Wawa service is roughly hourly; these are through-routed trains
    # that a naive GTFS query would miss.
    assert len(trains) >= 4
    # Named by where the train ends up, as platform boards show it, not by line.
    assert all(t["final_stop"] == "Wawa" and "line" not in t for t in trains)
    # depart/arrive name their stations, so "arrive" isn't read as the final stop's time.
    assert all(t["board_at"] == "Jefferson Station" and t["get_off_at"] == "Swarthmore" for t in trains)
    t = trains[0]
    assert t["summary"] == (f"Train {t['train']} (final stop Wawa) leaves Jefferson Station at {t['depart']} "
                            f"and arrives at Swarthmore at {t['arrive']}.")
    assert all(20 <= t["duration_min"] <= 50 for t in trains)


def test_ambiguous_station():
    r = call("train_schedule", {"from_station": "Chestnut Hill", "to_station": "Jefferson"})
    assert "candidates" in r and "Chestnut Hill East" in r["candidates"]


def test_transfer_itinerary():
    mon = next_weekday(0)
    r = call("train_schedule", {
        "from_station": "Cynwyd", "to_station": "Media", "date": mon.isoformat(),
        "depart_after": "06:00", "depart_before": "10:00",
    })
    assert r.get("itineraries"), r
    assert r["itineraries"][0]["transfer_at"] in {
        "Suburban Station", "Gray 30th St Station", "Jefferson Station", "Penn Medicine Station"
    }
    it = r["itineraries"][0]
    assert it["summary"].startswith(f"Leave Cynwyd at {it['depart']}")
    assert f"change at {it['transfer_at']}" in it["summary"] and it["summary"].endswith(f"arrive at Media at {it['arrive']}.")


def test_find_station():
    r = call("find_station", {"query": "30th street"})
    assert r["stations"][0]["name"] == "Gray 30th St Station"


# -- real-time (mocked) ----------------------------------------------------------


def test_next_trains_live():
    mock_septa_api.REQUESTS.clear()
    r = call("next_trains", {"from_station": "Jefferson", "to_station": "Swarthmore", "count": 2})
    assert r["source"] == "SEPTA real-time API"
    # 5301 left hours ago; the real API still lists such trains and they must be dropped.
    assert [t["train"] for t in r["trains"]] == ["5317", "5321"]
    assert r["trains"][0]["status"] == "3 min" and r["trains"][0]["direct"] is True
    assert r["trains"][0]["board_at"] == "Jefferson Station" and r["trains"][0]["get_off_at"] == "Swarthmore"
    assert "arrives at Swarthmore" in r["trains"][0]["summary"] and r["trains"][0]["summary"].endswith("(3 min late).")
    path, q = mock_septa_api.REQUESTS[-1]
    assert q["req1"] == ["Jefferson Station"] and q["req2"] == ["Swarthmore"]


def test_api_station_name_translation():
    mock_septa_api.REQUESTS.clear()
    call("next_trains", {"from_station": "Temple", "to_station": "Morton"})
    _, q = mock_septa_api.REQUESTS[-1]
    assert q["req1"] == ["Temple U"] and q["req2"] == ["Morton"]

    r = call("next_trains", {"from_station": "30th Street", "to_station": "Wayne"})
    assert r["source"] == "SEPTA real-time API", r
    _, q = mock_septa_api.REQUESTS[-1]
    assert q["req1"] == ["30th Street Station"] and q["req2"] == ["Wayne Station"]

    call("station_departures", {"station": "Wayne"})
    _, q = mock_septa_api.REQUESTS[-1]
    assert q["station"] == ["Wayne"]  # Arrivals wants the plain name


def test_api_error_message_surfaces():
    with pytest.raises(realtime.SeptaAPIError, match="'Fort Washington' is not a valid station"):
        realtime._get("/NextToArrive/index.php", {"req1": "Fort Washington", "req2": "Jefferson Station"})


def test_api_responses_cached_briefly(monkeypatch):
    before = len(mock_septa_api.REQUESTS)
    realtime.train_view()
    realtime.train_view()
    assert len(mock_septa_api.REQUESTS) == before + 1
    monkeypatch.setattr(realtime, "CACHE_SECONDS", 0)
    realtime.clear_cache()
    realtime.train_view()
    realtime.train_view()
    assert len(mock_septa_api.REQUESTS) == before + 3


def test_api_rate_limit(monkeypatch):
    monkeypatch.setattr(realtime, "MAX_PER_MINUTE", 2)
    realtime.train_view()
    realtime.rail_alerts()
    realtime.train_view()  # cached, doesn't count
    with pytest.raises(realtime.SeptaAPIError, match=r"limit of 2 requests per minute.*shared by all its users"):
        realtime.train_schedule_live("5305")
    # Tools still answer from the timetable when the live API is throttled.
    r = call("next_trains", {"from_station": "Jefferson", "to_station": "Swarthmore"})
    assert r["source"] == "published schedule (live data unavailable)" and "limit of 2" in r["live_error"], r


def test_station_departures():
    r = call("station_departures", {"station": "Jefferson"})
    assert r["southbound"][0]["train"] == "5317"
    assert r["southbound"][0]["track"] == "4"  # track change wins
    assert r["southbound"][0]["departs"] == "10:38 AM"
    assert "title" not in r  # SEPTA's heading timestamp is UTC; must not leak through


def test_train_status():
    r = call("train_status", {"train_number": "#5317"})
    assert r["position"]["minutes_late"] == 3
    assert r["position"]["next_stop"] == "Jefferson Station"
    assert r["stops"][1]["estimated"] == "10:41 am"
    assert r["stops"][1]["actual"] is None  # SEPTA's "na"


def test_train_status_unknown_train():
    r = call("train_status", {"train_number": "9999"})
    assert r["position"] is None
    assert "No train found" in r["stops_error"]


def test_rail_alerts():
    r = call("service_alerts", {"line": "Media/Wawa Line"})
    assert r["count"] == 1
    assert r["alerts"][0]["advisory"] == (
        "Track Work\nTrack work near Secane this weekend; expect delays of up to 10 minutes."
    )
    assert "last_updated" not in r["alerts"][0]


def test_alerts_by_mode():
    r = call("service_alerts", {"mode": "regional_rail"})
    # The alerts feed files the subway under rr_route_* too.
    assert [a["route_id"] for a in r["alerts"]] == ["rr_route_med", "rr_route_wilm"]
    r = call("service_alerts", {"mode": "metro"})
    assert [(a["route_id"], a["lines"]) for a in r["alerts"]] == [
        ("rr_route_mfo", ["L1"]), ("trolley_route_10", ["T1"])
    ]


def test_alerts_without_filters_summarize_buses():
    r = call("service_alerts", {})
    assert {a["mode"] for a in r["alerts"]} == {"regional_rail", "metro"}  # no app or bus rows
    assert r["bus_routes_with_alerts"] == ["42"]


def test_alerts_by_line_across_modes():
    assert [a["route_id"] for a in call("service_alerts", {"line": "MFL"})["alerts"]] == ["rr_route_mfo"]
    assert [a["route_id"] for a in call("service_alerts", {"line": "Route 10"})["alerts"]] == ["trolley_route_10"]
    (bus,) = call("service_alerts", {"line": "42"})["alerts"]
    assert bus["detour"].startswith("EB via Spruce St")
    assert (bus["detour_from"], bus["detour_starts"]) == ("Spruce & 38th", "10/26/2026 7:41 AM")
    r = call("service_alerts", {"line": "T3"})
    assert r["alerts"] == [] and "Media/Elwyn" in r["lines_with_alerts"]


def test_empty_results_include_line_alerts():
    r = call("next_trains", {"from_station": "Suburban", "to_station": "Wilmington"})
    assert r["trains"] == []
    # Only the line shared by both stations, not every line through Suburban.
    assert [a["route_id"] for a in r["service_alerts"]] == ["rr_route_wilm"]

    r = call("station_departures", {"station": "Wilmington"})
    assert [a["route_id"] for a in r["service_alerts"]] == ["rr_route_wilm"]

    r = call("train_schedule", {
        "from_station": "Suburban", "to_station": "Wilmington",
        "date": next_weekday(2).isoformat(), "depart_after": "02:00", "depart_before": "02:30",
    })
    assert r["trains"] == []
    assert [a["route_id"] for a in r["service_alerts"]] == ["rr_route_wilm"]


def test_whole_day_window_shows_it_runs_into_next_morning():
    r = call("train_schedule", {"from_station": "Jefferson", "to_station": "Swarthmore", "date": "2026-11-01"})
    assert r["window"] == "Sun Nov 1, 12:00 AM – Mon Nov 2, 3:30 AM (next day)"


def test_times_after_midnight_marked_next_day():
    sat = next_weekday(5)
    r = call("train_schedule", {
        "from_station": "Jefferson", "to_station": "Swarthmore", "date": sat.isoformat(), "depart_after": "23:00",
    })
    late = [t for t in r["trains"] if t["depart"].endswith("PM")]
    assert late and any(t["arrive"].endswith("AM (next day)") for t in late), r["trains"]


def _cut_off_before(monkeypatch, dest_name: str):
    """Pretend trains stop short of dest_name, as during a shuttle-bus replacement."""
    feed = server.feeds.get()
    real = feed.direct_rides
    monkeypatch.setattr(feed, "direct_rides", lambda o, d, *a: [] if d.name == dest_name else real(o, d, *a))
    monkeypatch.setattr(feed, "transfer_journeys", lambda *a: [])


def test_partial_service_when_trains_stop_short(monkeypatch):
    _cut_off_before(monkeypatch, "Swarthmore")
    r = call("train_schedule", {
        "from_station": "Jefferson", "to_station": "Swarthmore",
        "date": next_weekday(5).isoformat(), "depart_after": "05:00", "depart_before": "12:00",
    })
    assert r["trains"] == []
    # Morton is the stop before Swarthmore on the Media/Wawa Line.
    assert r["partial_service"]["rail_service_ends_at"] == "Morton"
    assert r["partial_service"]["trains"][0]["depart"] == "6:38 AM"
    assert "as far as Morton" in r["note"]


def test_partial_service_live(monkeypatch):
    _cut_off_before(monkeypatch, "Wilmington")
    monkeypatch.setattr(server, "now", lambda: datetime.combine(next_weekday(0), dtime(12, 0), server.TZ))
    r = call("next_trains", {"from_station": "Suburban", "to_station": "Wilmington"})
    assert r["trains"] == []  # the mock has no Wilmington trains
    assert r["partial_service"]["rail_service_ends_at"] == "Claymont Transit Center"
    assert r["partial_service"]["trains"][0]["train"] == "5317"  # from the live API (mock)
    assert "shuttle bus from Claymont Transit Center" in r["note"]


def test_partial_service_reports_rate_limit(monkeypatch):
    _cut_off_before(monkeypatch, "Wilmington")
    monkeypatch.setattr(server, "now", lambda: datetime.combine(next_weekday(0), dtime(12, 0), server.TZ))
    monkeypatch.setattr(realtime, "MAX_PER_MINUTE", 2)  # next_trains + alerts use both
    r = call("next_trains", {"from_station": "Suburban", "to_station": "Wilmington"})
    p = r["partial_service"]
    assert p["source"] == "published schedule" and p["trains"]
    assert "limit of 2 requests per minute" in p["live_error"]


def test_no_partial_service_without_a_through_route():
    r = call("train_schedule", {"from_station": "Cynwyd", "to_station": "Media",
                                "date": next_weekday(0).isoformat(), "depart_after": "02:00", "depart_before": "02:30"})
    assert "partial_service" not in r


def test_nonempty_results_omit_alerts():
    assert "service_alerts" not in call("next_trains", {"from_station": "Jefferson", "to_station": "Swarthmore"})
    assert "service_alerts" not in call("station_departures", {"station": "Jefferson"})


def test_live_api_down_falls_back_to_schedule(monkeypatch):
    monkeypatch.setattr(realtime, "BASE_URL", "http://127.0.0.1:9/api")
    r = call("next_trains", {"from_station": "Jefferson", "to_station": "Swarthmore"})
    assert r["source"].startswith("published schedule")
    assert "live_error" in r


def test_schedule_source_explains_feed_version():
    src = call("train_schedule", {"from_station": "Jefferson", "to_station": "Swarthmore"})["schedule_source"]
    start, end = src["valid_from"], src["valid_until"]
    assert all(start < d <= end for d in src["timetable_changes_included"])
    assert "doesn't mean the data is stale" in src["note"]


def test_todays_timetable_even_before_newest_feed_starts():
    # SEPTA publishes each feed days before it starts; until then today needs the old one.
    today = server.now().date()
    start, end = server.feeds.get(today).feed_range()
    assert start <= today <= end
    r = call("train_schedule", {"from_station": "Jefferson", "to_station": "Swarthmore", "date": "today"})
    assert "warning" not in r, r


def test_in_effect_release_is_newest_started(monkeypatch):
    releases = [{"tag_name": t} for t in ("v202611010", "v202609271", "v202609270", "v202609061")]
    releases.append({"tag_name": "v202610250", "draft": True})

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(gtfs.urllib.request, "urlopen", lambda *a, **k: Resp(json.dumps(releases).encode()))
    urls = gtfs.FeedManager()._in_effect_urls(date(2026, 10, 28))
    assert urls == ["https://github.com/septadev/GTFS/releases/download/v202609271/gtfs_public.zip"]


def test_next_trains_use_final_stop_not_line():
    feed = server.feeds.get()
    today = server.now().date()
    active = feed.services_on(today)
    # A through-routed train running today: its live line name is the first segment's.
    t = next(t for t in feed.trips.values() if t.service_id in active and t.trip_id in feed.next_trip
             and t.trip_id not in feed.prev_trip)
    live = {"train": t.train, "line": feed.route_name(t.route_id), "depart": "1:00PM", "arrive": "1:30PM"}
    out = server._with_final_stop(feed, live, "Suburban Station", "Wayne")
    assert list(out)[:7] == ["summary", "train", "final_stop", "board_at", "depart", "get_off_at", "arrive"]
    assert "line" not in out
    last = list(feed._chain_forward(t, active))[-1]
    assert out["final_stop"] == last.headsign
    # Unknown trains keep the line, since there's nothing better to say.
    assert server._with_final_stop(feed, {"train": "0000", "line": "Airport"}, "A", "B")["line"] == "Airport"


def test_next_trains_connection_names_each_legs_stations():
    feed = server.feeds.get()
    live = [{"train": "0000", "line": "Paoli/Thorndale", "depart": "1:00PM", "arrive": "1:10PM", "direct": False,
             "connection": {"at": "Temple U", "train": "0001", "line": "Lansdale/Doylestown",
                            "depart": "1:20PM", "arrive": "2:00PM"}}]
    (t,) = server._with_final_stops(feed, live, "Wayne", "Lansdale")
    assert (t["board_at"], t["get_off_at"]) == ("Wayne", "Temple University")  # API name translated
    c = t["connection"]
    assert (c["board_at"], c["get_off_at"]) == ("Temple University", "Lansdale") and "at" not in c


# -- Metro ---------------------------------------------------------------------


def metro_feed():
    return server.feeds.get(next_weekday(2), "metro")


def test_resolve_lines():
    assert metro.resolve_lines("MFL") == ["L1"]
    assert metro.resolve_lines("the El") == ["L1"]
    assert metro.resolve_lines("Broad Street Line") == ["B1", "B2", "B3"]
    assert metro.resolve_lines("route 10") == ["T1"]
    assert metro.resolve_lines("Route 101") == ["D1"]
    assert metro.resolve_lines("t3 trolley") == ["T3"]
    with pytest.raises(ValueError, match="L1 \\(Market-Frankford Line\\)"):
        metro.resolve_lines("Purple Line")


def test_metro_platforms_merge_into_stations():
    feed = metro_feed()
    city_hall = feed.resolve_station("City Hall")
    assert city_hall.name == "15th St/City Hall"
    assert {"L1", "B1", "B2", "T1", "T5"} <= city_hall.routes
    assert len(feed.platforms[city_hall.stop_id]) > 1
    for platform in feed.platforms[city_hall.stop_id]:
        assert feed.resolve_station(platform) is city_hall
    assert feed.resolve_station("69th St").routes >= {"L1", "M1", "D1", "D2"}
    # Platform suffixes don't leak into names, and no-stop rows aren't findable.
    assert not [s.name for s in feed.stops.values() if s.name.endswith(" - B1")]
    with pytest.raises(gtfs.StationNotFound, match="No Metro station or stop"):
        feed.resolve_station("Not a stop")


def test_metro_schedule_direct():
    r = call("metro_schedule", {
        "from_stop": "15th St", "to_stop": "Frankford Transit Center",
        "date": next_weekday(2).isoformat(), "depart_after": "08:00", "depart_before": "09:00",
    })
    assert len(r["trips"]) >= 6  # every few minutes at rush hour
    t = r["trips"][0]
    assert (t["line"], t["toward"], t["board_at"]) == ("L1", "Frankford Transit Center", "15th St/City Hall")
    assert t["summary"].startswith("L1 (Market-Frankford Line) toward Frankford Transit Center leaves 15th St/City Hall")


def test_metro_schedule_line_filter():
    args = {"from_stop": "15th St", "to_stop": "30th St", "date": next_weekday(2).isoformat(),
            "depart_after": "08:00", "depart_before": "09:00"}
    both = {t["line"] for t in call("metro_schedule", args)["trips"]}
    assert "L1" in both and both & {"T1", "T2", "T3", "T4", "T5"}
    assert {t["line"] for t in call("metro_schedule", {**args, "line": "subway-surface trolleys"})["trips"]} <= {
        "T1", "T2", "T3", "T4", "T5"}
    assert call("metro_schedule", {**args, "line": "Purple"})["error"].startswith("No Metro line matches")


def test_metro_schedule_transfer():
    r = call("metro_schedule", {
        "from_stop": "Erie", "to_stop": "2nd St", "date": next_weekday(2).isoformat(),
        "depart_after": "08:00", "depart_before": "09:00",
    })
    assert "trips" not in r and r["itineraries"]
    it = r["itineraries"][0]
    assert it["transfer_at"] in ("15th St/City Hall", "8th-Market")
    assert [leg["line"] for leg in it["legs"]][1] == "L1"
    assert "walk_to" not in it


def test_metro_schedule_walking_transfer():
    # The G1 stops on Girard Av, a block's walk from the L1's Front-Girard station.
    r = call("metro_schedule", {
        "from_stop": "Girard Av & Broad St", "to_stop": "Spring Garden", "date": next_weekday(2).isoformat(),
        "depart_after": "10:00", "depart_before": "11:00",
    })
    walks = [it for it in r["itineraries"] if "walk_to" in it]
    assert walks, r
    assert walks[0]["walk_to"] == "Front-Girard" and walks[0]["walk_min"] >= 2
    assert ", walk to Front-Girard (about" in walks[0]["summary"]


def test_metro_departures_with_live_delays(monkeypatch):
    day = next_weekday(2)
    monkeypatch.setattr(server, "now", lambda: datetime.combine(day, dtime(8, 0), server.TZ))
    feed = server.feeds.get(day, "metro")
    late_trips = {t.trip_id for t in feed.trips.values() if t.route_id == "T3"}

    def transit_view(route, now=None):
        return [{"vehicle": "9050", "trip": tid, "minutes_late": 3, "next_stop_sequence": None}
                for tid in late_trips] if route == "T3" else []

    monkeypatch.setattr(realtime, "transit_view", transit_view)
    r = call("metro_departures", {"stop": "30th St", "count": 40})
    deps = r["departures"]
    t3 = [d for d in deps if d["line"] == "T3"]
    assert t3 and all(d["status"] == "3 min late" and d["expected"] for d in t3)
    assert any(d["line"] == "L1" and d["status"] == "approximate (no live subway data)" for d in deps)
    assert all(d["status"] == "scheduled" for d in deps if d["line"] in ("T1", "T2"))
    assert "warning" not in r


def test_metro_departures_skip_trips_already_past(monkeypatch):
    day = next_weekday(2)
    monkeypatch.setattr(server, "now", lambda: datetime.combine(day, dtime(8, 0), server.TZ))
    monkeypatch.setattr(realtime, "transit_view", lambda route, now=None: [
        {"vehicle": "1", "trip": t.trip_id, "minutes_late": 30, "next_stop_sequence": 10_000}
        for t in server.feeds.get(day, "metro").trips.values() if t.route_id == route
    ])
    r = call("metro_departures", {"stop": "30th St", "line": "T3"})
    assert r["departures"] == []  # every T3 is "30 min late" but past this stop already


def test_metro_vehicles():
    r = call("metro_vehicles", {"line": "Route 10"})
    (t1,) = r["lines"]
    assert t1["line"] == "T1" and len(t1["vehicles"]) == 2  # the stale one is dropped
    v, no_estimate = t1["vehicles"]
    assert (v["status"], v["toward"], v["seats"]) == ("3 min late", "13th St", "MANY_SEATS_AVAILABLE")
    assert v["next_stop"] == "63rd St & Lebanon Av"  # stop_id looked up in the schedule
    assert no_estimate["status"] is None and no_estimate["next_stop"] == "Lancaster Av & 40th St"
    assert "seats" not in no_estimate

    r = call("metro_vehicles", {"line": "BSL"})
    assert [line["line"] for line in r["lines"]] == ["B1", "B2", "B3"]
    assert all("no live positions" in line["note"] for line in r["lines"])
    assert ("/api/TransitView/index.php", {"route": ["B1"]}) not in mock_septa_api.REQUESTS


def test_find_metro_stop():
    r = call("find_metro_stop", {"query": "40th", "line": "T3"})
    assert r["stops"] and all("T3 trolley" in s["lines"] for s in r["stops"])
    r = call("find_metro_stop", {"query": "City Hall"})
    assert r["stops"][0]["name"] == "15th St/City Hall"
    assert r["stops"][0]["lines"][:2] == ["L1 (Market-Frankford Line)", "B1 (Broad Street Line local)"]


def test_metro_tools_without_metro_feed(monkeypatch):
    rail_only = server.feeds._path("", "rail")
    if server.feeds.local_zip:
        pytest.skip("rail zip not cached separately")
    monkeypatch.setattr(server, "feeds", gtfs.FeedManager(local_zip=str(rail_only)))
    r = call("metro_schedule", {"from_stop": "15th St", "to_stop": "2nd St"})
    assert "no Metro (subway and trolley) schedule" in r["error"]
    assert call("train_schedule", {"from_station": "Jefferson", "to_station": "Swarthmore",
                                   "date": next_weekday(5).isoformat()})["trains"]


def test_gtfs_subset_keeps_only_metro_routes():
    def table(*rows):
        return "\n".join(",".join(r) for r in rows) + "\n"

    src = io.BytesIO()
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("routes.txt", table(("route_id", "route_type"), ("T1", "0"), ("42", "3")))
        z.writestr("trips.txt", table(("route_id", "service_id", "trip_id"), ("T1", "s", "t1"), ("42", "s", "b1")))
        z.writestr("stop_times.txt", table(
            ("trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"),
            ("t1", "08:00:00", "08:00:00", "p1", "1"), ("b1", "08:00:00", "08:00:00", "bus", "1"),
        ))
        z.writestr("stops.txt", table(
            ("stop_id", "stop_name", "parent_station"), ("p1", "A - T1", "st"), ("st", "A", ""), ("bus", "B", ""),
        ))
        z.writestr("calendar.txt", "service_id\n")
    with zipfile.ZipFile(io.BytesIO(gtfs.gtfs_subset(src.getvalue(), gtfs.METRO_ROUTE_TYPES))) as out:
        def ids(name, col):
            return [line.split(",")[col] for line in out.read(name).decode().splitlines()[1:]]
        assert ids("routes.txt", 0) == ["T1"]
        assert ids("trips.txt", 2) == ["t1"]
        assert ids("stop_times.txt", 0) == ["t1"]
        assert ids("stops.txt", 0) == ["p1", "st"]  # the platform's parent station too
        assert "calendar.txt" in out.namelist()


def test_subway_times_come_with_how_often_trains_run():
    # A five-minute window still gets a few L1 trains, each with the gap before it.
    r = call("metro_schedule", {
        "from_stop": "15th St", "to_stop": "Frankford Transit Center", "date": next_weekday(2).isoformat(),
        "depart_after": "08:00", "depart_before": "08:05",
    })
    assert len(r["trips"]) >= metro.SUBWAY_MIN_TIMES
    for t in r["trips"]:
        assert 1 <= t["minutes_after_previous"] <= 15  # including the first, from the train before 8:00
        assert t["summary"].endswith(f"(approximate; {t['minutes_after_previous']} min after the previous train).")
    (freq,) = r["subway_frequency"]
    assert freq["line"] == "L1" and 3 <= freq["every_min"] <= 12
    assert freq["summary"].startswith(f"L1 (Market-Frankford Line) toward Frankford Transit Center runs about every {freq['every_min']} min")
    assert "approximate" in r["subway_note"]


def test_trolley_times_stay_exact():
    r = call("metro_schedule", {
        "from_stop": "15th St", "to_stop": "30th St", "date": next_weekday(2).isoformat(),
        "depart_after": "08:00", "depart_before": "08:10", "line": "T",
    })
    assert r["trips"] and not any("minutes_after_previous" in t or "approximate" in t["summary"] for t in r["trips"])
    assert "subway_frequency" not in r and "subway_note" not in r


def test_subway_transfer_legs_say_how_often_trains_run():
    r = call("metro_schedule", {
        "from_stop": "Erie", "to_stop": "2nd St", "date": next_weekday(2).isoformat(),
        "depart_after": "08:00", "depart_before": "08:30",
    })
    it = r["itineraries"][0]
    assert all(leg["runs_every_min"] > 0 for leg in it["legs"])
    assert f"The L1 (Market-Frankford Line) runs about every {it['legs'][1]['runs_every_min']} min then." in it["summary"]
    assert "subway_note" in r


def test_subway_departures_show_a_few_per_direction(monkeypatch):
    day = next_weekday(2)
    monkeypatch.setattr(server, "now", lambda: datetime.combine(day, dtime(8, 0), server.TZ))
    monkeypatch.setattr(realtime, "transit_view", lambda route, now=None: [])
    r = call("metro_departures", {"stop": "15th St", "count": 2})
    l1 = [d for d in r["departures"] if d["line"] == "L1"]
    for toward in ("69th St Transit Center", "Frankford Transit Center"):
        assert sum(d["toward"] == toward for d in l1) >= metro.SUBWAY_MIN_TIMES
    assert all(d["minutes_after_previous"] > 0 for d in l1)
    assert {(f["line"], f["toward"]) for f in r["subway_frequency"]} >= {
        ("L1", "69th St Transit Center"), ("L1", "Frankford Transit Center")}


def test_frequency_summary():
    t0 = datetime(2026, 11, 4, 8, 0, tzinfo=server.TZ)
    times = [t0 + timedelta(minutes=m) for m in (0, 5, 12, 18, 24)]
    f = metro.frequency(metro.LINES["B1"], "NRG", times, t0)
    assert (f["every_min"], f["gaps_min"]) == (6, "5-7")
    assert f["summary"] == "B1 (Broad Street Line local) toward NRG runs about every 6 min around 8:00 AM (gaps of 5-7 min)."
    even = metro.frequency(metro.LINES["L1"], "Frankford", [t0, t0 + timedelta(minutes=6)], t0)
    assert even["summary"].endswith("runs about every 6 min around 8:00 AM.")
    assert metro.frequency(metro.LINES["L1"], "Frankford", [t0], t0) is None
