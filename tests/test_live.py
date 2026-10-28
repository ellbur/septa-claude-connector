"""Checks against SEPTA's real servers. Skipped unless SEPTA_LIVE=1:

    SEPTA_LIVE=1 pytest -q tests/test_live.py

Mainly guards the GTFS -> real-time station name map, which drifts as SEPTA renames stops,
and checks the place tools against the public Nominatim geocoder (whose results change as
OpenStreetMap is edited).
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest
from mcp import Client

from septa_mcp import geocode, realtime, server
from septa_mcp.gtfs import FeedManager

pytestmark = pytest.mark.skipif(os.environ.get("SEPTA_LIVE") != "1", reason="set SEPTA_LIVE=1")

LIVE_BASE = "https://www3.septa.org/api"
LIVE_GEOCODER = "https://nominatim.openstreetmap.org/search"


@pytest.fixture(autouse=True)
def live_api(monkeypatch):
    monkeypatch.setattr(realtime, "BASE_URL", LIVE_BASE)
    # test_server.py points these at its mock when both run.
    monkeypatch.setattr(geocode, "GEOCODER_URL", LIVE_GEOCODER)
    monkeypatch.setattr(geocode, "MIN_INTERVAL", 1.0)
    geocode.clear_cache()


def call(tool: str, args: dict) -> dict:
    async def go():
        async with Client(server.mcp) as c:
            r = await c.call_tool(tool, args)
            assert not r.is_error, r
            sc = r.structured_content if r.structured_content is not None else json.loads(r.content[0].text)
            return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc

    return anyio.run(go)


def test_every_station_name_accepted():
    names = sorted(s.name for s in FeedManager().get().stops.values())

    def errors(name):
        other = "Suburban Station" if name == "Jefferson Station" else "Jefferson Station"
        out = []
        for fn in (
            lambda: realtime.next_to_arrive(name, other, 1),
            lambda: realtime.next_to_arrive(other, name, 1),
            lambda: realtime.arrivals(name, 1),
        ):
            try:
                fn()
            except realtime.SeptaAPIError as e:
                out.append(str(e))
        return name, out

    with ThreadPoolExecutor(4) as ex:
        bad = {n: e for n, e in ex.map(errors, names) if e}
    assert not bad, bad


def test_live_endpoints_parse():
    assert isinstance(realtime.train_view(), list)
    assert realtime.rail_alerts(), "expected at least one Regional Rail advisory"
    board = realtime.arrivals("Suburban Station", 3)
    assert set(board) == {"northbound", "southbound"}


def test_live_metro_endpoints_parse():
    modes = {a["mode"] for a in realtime.alerts()}
    assert {"regional_rail", "bus"} <= modes
    vehicles = realtime.transit_view("G1")
    assert isinstance(vehicles, list)
    assert all(v["trip"] for v in vehicles)
    assert realtime.transit_view("L1") == []  # still no live subway data?
    by_route = realtime.transit_view_all()
    assert len(by_route) > 50 and all(v["trip"] for vs in by_route.values() for v in vs[:1])


def test_live_geocoder():
    r = call("find_place", {"query": "Penn Presbyterian Medical Center"})
    assert r["places"][0]["name"] == "Penn Presbyterian Medical Center"
    assert "OpenStreetMap" in r["attribution"]
    # Short names stay inside SEPTA's region (not Temple, Berks County).
    r = call("find_place", {"query": "Temple"})
    assert all(p["lon"] > -75.9 for p in r["places"])
    assert r["stops_named_like_it"][0]["name"] == "Temple University"

    r = call("stops_near", {"place": "Reading Terminal Market", "radius_m": 300, "modes": ["regional_rail"]})
    assert r["place"]["name"] == "Reading Terminal Market"
    assert r["stops"][0]["name"] == "Jefferson Station"

    r = call("stops_on_street", {"street": "Lancaster Av", "place": "Ardmore", "radius_m": 800})
    assert "103" in r["stretches"][0]["lines"]


def test_live_transit_in_area():
    r = call("transit_in_area", {"place": "Manayunk"})
    assert r["boundary"] == "its outline"
    assert [s["name"] for s in r["regional_rail_stations"]] == ["Manayunk"]
    assert "61" in [b["route"] for b in r["bus_routes"]]
    # Media station is just outside Media borough's line.
    r = call("transit_in_area", {"place": "Media"})
    assert {"name": "Media", "just_outside": True}.items() <= r["regional_rail_stations"][0].items()
    assert "D1" in [t["line"] for t in r["trolley_lines"]]
    r = call("transit_in_area", {"place": "Fishtown"})
    assert {"Front-Girard", "Berks"} <= {s["name"] for s in r["metro_stations"]}
