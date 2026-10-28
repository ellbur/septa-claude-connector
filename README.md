# SEPTA Regional Rail and Metro connector for Claude

An MCP server that lets Claude answer questions like:

- "What times do trains leave Jefferson for Swarthmore on Saturday morning?"
- "When's the next train from 30th Street to Paoli, and is it late?"
- "Where is train 5305 right now?"
- "How do I get from Erie to 2nd Street on the subway?"
- "When's the next T3 trolley from 30th Street, and is it running late?"
- "Any problems on the Media/Wawa line, the El, or the 42 bus today?"

It covers Regional Rail and the SEPTA Metro: the subway (L1 Market-Frankford Line, B1/B2
Broad Street Line, B3 Broad-Ridge Spur), the trolleys (T1–T5, G1, D1, D2) and the M1
Norristown High Speed Line. It combines SEPTA's published **GTFS** timetables (for any date
in the current schedule) with SEPTA's **real-time API** (live delays, tracks, train and
trolley positions, and alerts for every mode, buses included).

## Tools

| Tool | Source | Use it for |
|---|---|---|
| `train_schedule(from_station, to_station, date, depart_after, depart_before)` | GTFS | Timetable questions for any day. Falls back to one-transfer itineraries when there's no direct train. |
| `next_trains(from_station, to_station, count)` | Live (NextToArrive) | "Next train" right now, with delay status. Falls back to the timetable if the live API is down. |
| `station_departures(station, count, direction)` | Live (Arrivals) | A station's departure board: final stops, delays, track numbers. |
| `train_status(train_number)` | Live (TrainView + RRSchedules) | Where a train is, how late, stop-by-stop estimates. |
| `find_station(query)` | GTFS | Resolving vague or ambiguous station names. |
| `metro_schedule(from_stop, to_stop, date, depart_after, depart_before, line)` | GTFS | Subway and trolley timetables for any day, with one-transfer itineraries (including short walks between stations) when there's no direct trip. |
| `metro_departures(stop, line, count)` | GTFS + live (TransitView) | Next departures from a Metro station or trolley stop, with live delays for trolleys and the M1. |
| `metro_vehicles(line)` | Live (TransitView) | Where a line's trolleys are, their next stops and how late. |
| `find_metro_stop(query, line)` | GTFS | Resolving Metro station and trolley stop names. |
| `service_alerts(line, mode)` | Live (Alerts) | Advisories and detours for Regional Rail, Metro and bus routes. |

The Regional Rail tools and the Metro tools are separate: none plans a trip that mixes the
two, so Claude plans each part and changes at a shared hub (Suburban ↔ 15th St/City Hall,
Gray 30th St ↔ Drexel Station at 30th St, Jefferson ↔ 13th St or 8th-Market, Fern Rock and
Norristown Transit Centers).

Station names are matched loosely: "Jefferson", "Market East", "30th Street", "Temple",
"Mt Airy" all work. Ambiguous names ("Chestnut Hill", "Airport", "Norristown") return the
candidates so Claude can ask which one you mean. Metro lines go by their current names or
the ones riders still use: "MFL" or "the El" is the L1, "BSL" is B1–B3, "Route 10" is the
T1, "Route 15" the G1, "Route 101" the D1, "NHSL" the M1.

### Example

`train_schedule("Jefferson", "Swarthmore", "saturday", "05:00", "12:00")` against the
v202609271 feed returns trains 5301, 5305, 5309, 5313, 5317 and 5321, leaving Jefferson at
:38 past each hour from 6:38 AM and arriving at Swarthmore 35 minutes later.

When no train reaches the destination (e.g. buses replace trains on the last stretch of a
line), results list the alerts for the line plus `partial_service`: the trains to the
closest station on the way that trains still reach, where riders change to the shuttle.

## SEPTA quirks it handles

**Through-routed trains.** Most Regional Rail trains change line name in Center City (a
Lansdale/Doylestown train becomes a Media/Wawa train at Suburban Station). GTFS models this
as two trips joined by an in-seat transfer, so a naive "trips serving both stations" query
finds only a handful of the Jefferson → Swarthmore trains. This server stitches those trips
back together, so you see every train.

Trains are reported by number and `final_stop`, the way platform boards list them,
rather than by line. The line name a train has at your boarding station can belong to the
other half of its run, e.g. a Suburban → Claymont train shows up as "Chestnut Hill East".

**Station names differ between feeds.** The real-time API predates the current GTFS and
uses older names for about 30 stations ("Temple U", "30th Street Station", "Ft Washington",
"Elwyn Station"), and its endpoints don't even agree with each other (NextToArrive wants
"Wayne Station", Arrivals wants "Wayne"). `realtime.py` translates automatically; the map is
checked against the live API by `tests/test_live.py`.

**Metro stations are many stops.** The Metro's GTFS (part of SEPTA's bus feed) has a stop
per platform or direction, and separate ones per line at transfer stations: 15th St/City
Hall is eight stops across the L1, B1, B2 and the T trolleys. `metro.py` merges them into
one stop per station, by parent station and by name, so transfers between lines work, and
links stations within a short walk of each other (Girard Av & Front St on the G1 and
Front-Girard on the L1) so itineraries can include the walk.

**No live subway data.** SEPTA's real-time JSON API reports trolley and M1 positions and
delays (TransitView), but nothing for the L1 or the Broad Street Line; its GTFS-realtime
feed has no real subway data either. And riders can't count on a subway train's minute,
only on how often trains come. So subway results always include at least three times per
line and direction (even past the requested window), each with `minutes_after_previous`,
plus `subway_frequency` ("L1 toward Frankford Transit Center runs about every 6 min around
8:02 AM"), and Claude is told to present them as approximate. At a busy stop that would
swamp the departure board (15th St/City Hall has six subway directions), so past 12 entries
the board cuts subway directions to two times each, then one; each keeps its gap to the
train before, and the result says to ask for one line to see more.

## Run it

Requires Python 3.11+.

```bash
pip install .
septa-mcp --transport http --port 8000   # Streamable HTTP at http://localhost:8000/mcp
septa-mcp                                # stdio, for local MCP clients
```

On first start it downloads SEPTA's schedules (≈27 MB) from SEPTA, falling back to SEPTA's
GitHub releases, and re-checks daily. It keeps the rail feed and the Metro routes cut from
the bus feed, about 2.3 MB together. SEPTA publishes a new feed every few weeks,
a few days before it takes effect, and its download link switches to it right away; until the
new feed starts, the server also keeps the one in effect (from the GitHub releases) and answers
each date from the feed that covers it. Results include `valid_from`/`valid_until`, and a
warning if you ask about a date outside it.
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
`deploy.sh` puts it on Google Cloud Run, which scales to zero when nobody's using it (low
traffic fits in Cloud Run's free tier):

```bash
gcloud auth login
PROJECT=my-gcp-project ./deploy.sh    # optional: REGION, SERVICE, BUCKET
```

It mounts a Cloud Storage bucket at `/data` so the schedule survives cold starts and is
downloaded at most once a day, and caps the service at one instance so the real-time API
cache and rate limit (10 s, 60 requests/minute by default) apply to the whole service.
When the limit is hit, tools fall back to the published schedule or report the error.

The included Dockerfile also works on any other container host (Fly.io, Render, Railway, a
VPS behind Caddy, etc.); run a single instance with a persistent `/data`:

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
| `SEPTA_GTFS_ZIP` | — | Use a local `gtfs_public.zip` instead of downloading (a bare `google_rail.zip` works too, without the Metro tools) |
| `SEPTA_GTFS_URLS` | SEPTA, then GitHub | Comma-separated download URLs |
| `SEPTA_API_BASE` | `https://www3.septa.org/api` | Real-time API base (point at the mock for offline dev) |
| `SEPTA_API_TIMEOUT` | `10` | Seconds per real-time request |
| `SEPTA_API_CACHE_SECONDS` | `10` | How long identical real-time requests share a response |
| `SEPTA_API_MAX_PER_MINUTE` | `60` | Most real-time requests sent to SEPTA in any 60 seconds (per process) |
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

- Bus schedules aren't covered (only bus alerts), nor are the trackless trolleys (59, 66,
  75, which run like buses), NJ Transit, PATCO and Amtrak. The bus routes are in the same
  GTFS download, but at ~3 million stop times they need a database rather than the
  in-memory approach used here.
- No trip planning across Regional Rail and the Metro.
- Transfer itineraries are limited to one transfer; that covers any pair of Regional Rail
  stations and most Metro trips.
- SEPTA's real-time API is unofficially documented and its response formats occasionally
  shift; `realtime.py` parses defensively but is the first place to look if a live tool breaks.
