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

from septa_mcp import server  # noqa: E402
from septa_mcp.server import parse_clock, parse_date  # noqa: E402


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
    trains = r["trains"]
    # Saturday Media/Wawa service is roughly hourly; these are through-routed trains
    # that a naive GTFS query would miss.
    assert len(trains) >= 4
    assert all(t["line"] == "Media/Wawa Line" for t in trains)
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
    assert r["trains"][0] == {
        "train": "5317", "line": "Media/Wawa", "depart": "10:38AM", "arrive": "11:13AM",
        "status": "3 min", "direct": True,
    }
    path, q = mock_septa_api.REQUESTS[-1]
    assert q["req1"] == ["Jefferson Station"] and q["req2"] == ["Swarthmore"]


def test_api_station_name_translation():
    mock_septa_api.REQUESTS.clear()
    call("next_trains", {"from_station": "Temple", "to_station": "Morton"})
    _, q = mock_septa_api.REQUESTS[-1]
    assert q["req1"] == ["Temple U"] and q["req2"] == ["Morton-Rutledge"]


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


def test_rail_alerts():
    r = call("rail_alerts", {"line": "Media/Wawa Line"})
    assert r["count"] == 1
    assert r["alerts"][0]["advisory"].startswith("Track work near Secane")


def test_live_api_down_falls_back_to_schedule(monkeypatch):
    from septa_mcp import realtime
    monkeypatch.setattr(realtime, "BASE_URL", "http://127.0.0.1:9/api")
    r = call("next_trains", {"from_station": "Jefferson", "to_station": "Swarthmore"})
    assert r["source"].startswith("published schedule")
    assert "live_error" in r
