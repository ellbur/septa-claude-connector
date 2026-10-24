# SEPTA Regional Rail connector for Claude

An MCP server that lets Claude answer questions like:

- "What times do trains leave Jefferson for Swarthmore on Saturday morning?"
- "When's the next train from 30th Street to Paoli, and is it late?"
- "Where is train 5305 right now?"
- "Any problems on the Media/Wawa line today?"

It combines SEPTA's published **GTFS** timetable (for any date in the current schedule)
with SEPTA's **real-time API** (live delays, tracks, train positions, alerts).

## Tools

| Tool | Source | Use it for |
|---|---|---|
| `train_schedule(from_station, to_station, date, depart_after, depart_before)` | GTFS | Timetable questions for any day. Falls back to one-transfer itineraries when there's no direct train. |
| `next_trains(from_station, to_station, count)` | Live (NextToArrive) | "Next train" right now, with delay status. Falls back to the timetable if the live API is down. |
| `station_departures(station, count, direction)` | Live (Arrivals) | A station's departure board: destinations, delays, track numbers. |
| `train_status(train_number)` | Live (TrainView + RRSchedules) | Where a train is, how late, stop-by-stop estimates. |
| `rail_alerts(line)` | Live (Alerts) | Service advisories and disruptions. |
| `find_station(query)` | GTFS | Resolving vague or ambiguous station names. |

Station names are matched loosely: "Jefferson", "Market East", "30th Street", "Temple",
"Mt Airy" all work. Ambiguous names ("Chestnut Hill", "Airport", "Norristown") return the
candidates so Claude can ask which one you mean.

### Example

`train_schedule("Jefferson", "Swarthmore", "saturday", "05:00", "12:00")` against the
v202609271 feed returns trains 5301, 5305, 5309, 5313, 5317 and 5321, leaving Jefferson at
:38 past each hour from 6:38 AM and arriving at Swarthmore 35 minutes later.

## Two SEPTA quirks it handles

**Through-routed trains.** Most Regional Rail trains change line name in Center City (a
Lansdale/Doylestown train becomes a Media/Wawa train at Suburban Station). GTFS models this
as two trips joined by an in-seat transfer, so a naive "trips serving both stations" query
finds only a handful of the Jefferson → Swarthmore trains. This server stitches those trips
back together, so you see every train.

Trains are reported by number and final `destination`, the way platform boards list them,
rather than by line. The line name a train has at your boarding station can belong to the
other half of its run, e.g. a Suburban → Claymont train shows up as "Chestnut Hill East".

**Station names differ between feeds.** The real-time API predates the current GTFS and
uses older names for about 30 stations ("Temple U", "30th Street Station", "Ft Washington",
"Elwyn Station"), and its endpoints don't even agree with each other (NextToArrive wants
"Wayne Station", Arrivals wants "Wayne"). `realtime.py` translates automatically; the map is
checked against the live API by `tests/test_live.py`.

## Run it

Requires Python 3.11+.

```bash
pip install .
septa-mcp --transport http --port 8000   # Streamable HTTP at http://localhost:8000/mcp
septa-mcp                                # stdio, for local MCP clients
```

On first start it downloads the rail schedule (≈750 KB) from SEPTA, falling back to
SEPTA's GitHub releases, and re-checks daily. SEPTA publishes a new feed every few weeks;
results include `valid_from`/`valid_until`, and a warning if you ask about a date outside it.
A feed's version is named for its start date, and SEPTA folds later schedule changes into the
same feed, so results also list `timetable_changes_included` to show the data is current.

### Use it in Claude Code (development version)

The repo includes a `.mcp.json`, so starting `claude` in a checkout loads the server from
your working copy. It needs [uv](https://docs.astral.sh/uv/), which builds a `.venv` from
`uv.lock` on first start and installs the package in editable mode:

```bash
git clone https://github.com/ellbur/septa-claude-connector && cd septa-claude-connector
claude          # approve the "septa" server when prompted, then check with /mcp
```

Code edits take effect the next time the server starts: restart Claude Code, or reconnect
from `/mcp`.

### Add it to Claude as a custom connector

Claude's custom connectors need a public HTTPS URL, so deploy the server somewhere first.
The included Dockerfile works on any container host (Fly.io, Render, Railway, Cloud Run, a VPS
behind Caddy, etc.):

```bash
docker build -t septa-mcp .
docker run -p 8000:8000 -v septa-data:/data septa-mcp
```

Then add a custom connector in Claude's settings with the URL
`https://<your-host>/mcp`. No authentication is required: everything it serves is SEPTA's
public data and every tool is read-only.

For local use only, point a desktop MCP client at the stdio command instead:

```json
{
  "mcpServers": {
    "septa": { "command": "septa-mcp" }
  }
}
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `SEPTA_DATA_DIR` | `~/.cache/septa-mcp` | Where the downloaded schedule is cached |
| `SEPTA_GTFS_ZIP` | — | Use a local `gtfs_public.zip` / `google_rail.zip` instead of downloading |
| `SEPTA_GTFS_URLS` | SEPTA, then GitHub | Comma-separated download URLs |
| `SEPTA_API_BASE` | `https://www3.septa.org/api` | Real-time API base (point at the mock for offline dev) |
| `SEPTA_API_TIMEOUT` | `10` | Seconds per real-time request |
| `MCP_TRANSPORT`, `HOST`, `PORT` | `stdio`, `0.0.0.0`, `8000` | Server transport settings |

## Tests

```bash
uv run --extra dev pytest -q      # or: pip install -e ".[dev]" && pytest -q
SEPTA_LIVE=1 pytest -q tests/test_live.py   # against SEPTA's real servers
```

Schedule tests run against the real GTFS feed (downloaded, or `SEPTA_GTFS_ZIP`).
Real-time tests use `tests/mock_septa_api.py`, which serves responses in SEPTA's format;
you can also run it standalone for offline development.

## Limits

- Regional Rail only. Buses, the Metro (subway/trolley) lines, NJ Transit and Amtrak are
  not covered. The bus/Metro GTFS is in the same SEPTA download if you want to extend it.
- Transfer itineraries are limited to one transfer; that covers any pair of Regional Rail
  stations.
- SEPTA's real-time API is unofficially documented and its response formats occasionally
  shift; `realtime.py` parses defensively but is the first place to look if a live tool breaks.
