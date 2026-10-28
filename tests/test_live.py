"""Checks against SEPTA's real servers. Skipped unless SEPTA_LIVE=1:

    SEPTA_LIVE=1 pytest -q tests/test_live.py

Mainly guards the GTFS -> real-time station name map, which drifts as SEPTA renames stops.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from septa_mcp import realtime
from septa_mcp.gtfs import FeedManager

pytestmark = pytest.mark.skipif(os.environ.get("SEPTA_LIVE") != "1", reason="set SEPTA_LIVE=1")

LIVE_BASE = "https://www3.septa.org/api"


@pytest.fixture(autouse=True)
def live_api(monkeypatch):
    monkeypatch.setattr(realtime, "BASE_URL", LIVE_BASE)


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
