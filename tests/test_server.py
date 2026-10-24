"""End-to-end tests through a real MCP client.

Schedule tests need a GTFS file: set SEPTA_GTFS_ZIP to a downloaded gtfs_public.zip
(or google_rail.zip), otherwise the server downloads the current one.
Live-API tests use the mock in mock_septa_api.py.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import mock_septa_api  # noqa: E402

_mock = mock_septa_api.start()
os.environ["SEPTA_API_BASE"] = f"http://127.0.0.1:{_mock.server_port}/api"

from mcp import Client  # noqa: E402

from septa_mcp import realtime, server  # noqa: E402
from septa_mcp.server import parse_clock, parse_date  # noqa: E402

# In case realtime was imported (e.g. by test_live.py) before the env var was set.
realtime.BASE_URL = os.environ["SEPTA_API_BASE"]


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
    assert r["window"] == f"{sat:%a %b} {sat.day}, 5:00 AM – {sat:%a %b} {sat.day}, 12:00 PM"
    trains = r["trains"]
    # Saturday Media/Wawa service is roughly hourly; these are through-routed trains
    # that a naive GTFS query would miss.
    assert len(trains) >= 4
    # Named by where the train ends up, as platform boards show it, not by line.
    assert all(t["destination"] == "Wawa" and "line" not in t for t in trains)
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
    r = call("rail_alerts", {"line": "Media/Wawa Line"})
    assert r["count"] == 1
    assert r["alerts"][0]["advisory"] == (
        "Track Work\nTrack work near Secane this weekend; expect delays of up to 10 minutes."
    )
    assert "last_updated" not in r["alerts"][0]


def test_rail_alerts_exclude_subway():
    r = call("rail_alerts", {})
    assert [a["route_id"] for a in r["alerts"]] == ["rr_route_med", "rr_route_wilm"]


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
    assert r["window"] == "Sun Nov 1, 12:00 AM – Mon Nov 2, 3:30 AM"


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


def test_next_trains_use_destination_not_line():
    feed = server.feeds.get()
    today = server.now().date()
    active = feed.services_on(today)
    # A through-routed train running today: its live line name is the first segment's.
    t = next(t for t in feed.trips.values() if t.service_id in active and t.trip_id in feed.next_trip
             and t.trip_id not in feed.prev_trip)
    live = {"train": t.train, "line": feed.route_name(t.route_id), "depart": "1:00PM"}
    out = server._with_destination(feed, live)
    assert list(out)[:2] == ["train", "destination"] and "line" not in out
    last = list(feed._chain_forward(t, active))[-1]
    assert out["destination"] == last.headsign
    # Unknown trains keep the line, since there's nothing better to say.
    assert server._with_destination(feed, {"train": "0000", "line": "Airport"})["line"] == "Airport"
