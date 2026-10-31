# SEPTA Regional Rail, Metro and bus connector for Claude

**Public MCP URL:** https://septa-mcp-60753635813.us-east4.run.app/mcp

An MCP server that lets Claude answer questions like:

- "What times do trains leave Jefferson for Swarthmore on Saturday morning?"
- "When's the next train from 30th Street to Paoli, and is it late?"
- "Where is train 5305 right now?"
- "How do I get from Erie to 2nd Street on the subway?"
- "When's the next T3 trolley from 30th Street, and is it running late?"
- "When does the 42 leave Broad and Walnut, and where is it now?"
- "What's the closest stop to Reading Terminal Market?" / "What buses run through Fishtown?"
- "Any problems on the Media/Wawa line, the El, or the 42 bus today?"

It covers Regional Rail, buses and the SEPTA Metro: the subway (L1 Market-Frankford Line,
B1/B2 Broad Street Line, B3 Broad-Ridge Spur), the trolleys (T1–T5, G1, D1, D2) and the M1
Norristown High Speed Line. It combines SEPTA's published **GTFS** timetables (for any date
in the current schedule) with SEPTA's **real-time API** (live delays, tracks, train and
trolley and bus positions, and alerts for every mode).

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
| `bus_schedule(from_stop, to_stop, date, depart_after, depart_before, route)` | GTFS | Buses that go directly between two stops, for any day. |
| `bus_departures(stop, route, count)` | GTFS + live (TransitViewAll) | Next buses from a stop, with live delays and which side of the street each leaves from. |
| `bus_vehicles(route)` | Live (TransitView) | Where a route's buses are and how late. |
| `find_bus_stop(query, route)` | GTFS | Bus stops by cross streets or stop number. |
| `service_alerts(line, mode)` | Live (Alerts + GTFS-realtime) | Advisories and detours for Regional Rail, Metro and bus routes, including closed stations, shuttle buses and platform changes. |
| `find_place(query)` | Geocoder + GTFS | A landmark, address, neighborhood or town → coordinates, plus SEPTA stops named like it. |
| `stops_near(lat, lon \| place, radius_m, modes)` | GTFS (+ geocoder) | Stops of every mode around a point, with walking minutes; each nearby bus route's closest stop; the nearest rail and Metro stations. |
| `stops_on_street(street, place \| lat/lon, radius_m)` | GTFS (+ geocoder) | Stops along a street in order, by cross street, one stretch per town or segment. |
| `transit_in_area(place)` | Geocoder + GTFS | What serves a neighborhood or town: stations, trolley lines, bus routes and how many stops each has there. |

The place tools are meant to be combined: Claude can look up a landmark, list the stops
near it, then ask a mode's own tools about one of them (every result carries the
`stop_id` those tools accept).

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

Metro results (`metro_schedule`, `metro_departures`) always carry the current alerts for
the lines they use (`service_alerts`), since the timetable doesn't show a station that
trains bypass or a stretch run by shuttle buses. When a rider names a stop that isn't in
the schedule (11th St on the L1 is closed and bypassed until August 2027), the closest name
is used, and `station_note` says so and points to SEPTA's alert about the stop, if any.
`find_metro_stop` returns those alerts too.

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

**The bus feed is big.** It has ~2.8 million stop times, about 15 times the Metro's;
parsed into Python objects like the rail and Metro feeds it would need several hundred MB.
`bus.py` converts it once per download into a compact form (stop times as flat int
arrays), which loads in about a second and takes ~70 MB.

**Bus stops are corners.** The feed names each stop after the street the bus is on first,
so the same intersection is "Broad St & Walnut St" for one route and "Walnut St & Broad St"
for another, with a stop on each side. Stops are grouped by their set of streets, so
"Broad and Walnut" in either order, or the number on the stop's sign, finds the corner, and
results say which stop there (side of the street, and its number) each bus leaves from.

**Places come from OpenStreetMap.** Landmarks, addresses, neighborhoods and towns are
looked up with a [Nominatim](https://nominatim.org/) geocoder, limited to SEPTA's region
(Trenton to Wilmington, out to Chester and Bucks counties). The public server allows one
request a second from an app that identifies itself, which this keeps to and caches for a
day; set `SEPTA_GEOCODER_URL` to use another Nominatim-compatible server. Stops near
coordinates, along a street, or by name work without it. Results credit OpenStreetMap, as
its license requires.

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

**Two alert feeds.** SEPTA's JSON alerts API (`/Alerts/get_alert_data.php`) leaves out
many alerts that its GTFS-realtime alert feeds have: station closures (11th St on the L1),
M1 and D shuttle buses, platform changes and elevator outages. `service_alerts` reads both
and drops GTFS-realtime alerts whose text the JSON API already has. GTFS-realtime alerts
that name only single trips (cancelled bus trips) are left out. They're read with a small
built-in protobuf decoder (`septa_mcp/gtfsrt.py`), so there's no protobuf dependency.

## Run it

Requires Python 3.11+.

```bash
pip install .
septa-mcp --transport http --port 8000   # Streamable HTTP at http://localhost:8000/mcp
septa-mcp                                # stdio, for local MCP clients
```

On first start it downloads SEPTA's schedules (≈27 MB) from SEPTA, falling back to SEPTA's
GitHub releases. It keeps the rail feed and the Metro and bus routes from the bus feed,
about 12 MB together; processing a download takes 10–15 seconds. It re-checks weekly, since
SEPTA announces timetable changes well ahead, and sooner if the cached timetable runs out.
`septa-mcp --refresh-feeds` downloads the newest feeds into the cache and exits, for running
on a schedule instead. SEPTA publishes a new feed every few weeks,
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
PROJECT=my-gcp-project ./deploy.sh    # optional: REGION, SERVICE, BUCKET, JOB, REFRESH_SCHEDULE
```

Besides the service, it creates a Cloud Run job, `septa-mcp-refresh`, that runs
`septa-mcp --refresh-feeds` against the same bucket, and a Cloud Scheduler job that starts
it every Saturday at 11:17 PM Philadelphia time. The service picks up the new timetables on
its next request, so downloading and processing never hold up a rider's question. The
service only downloads them itself if the job hasn't succeeded in 8 days, or the cached
timetable has run out. To refresh now: `gcloud run jobs execute septa-mcp-refresh --region us-east4`.

To redeploy from GitHub on every push to the `deploy` branch, connect the repository to
Cloud Build; it's all sign-ins and taps, so it works from a phone. After running
`deploy.sh` once, in the Cloud console go to Cloud Build > Triggers > Create trigger:

- Event: push to a branch. Source: connect a new repository, pick GitHub, sign in, and
  install the Cloud Build app on this repository. Branch: `^deploy$`.
- Configuration: Cloud Build configuration file, `/cloudbuild.yaml`. It assumes Cloud Run
  region `us-east4`; if `deploy.sh` used another, add a substitution variable `_REGION`.
- Service account: the Compute Engine default service account.

`cloudbuild.yaml` builds the image and points both the service and the refresh job at it.
Then deploy with `git push origin HEAD:deploy`.

The service gets 1 GiB of memory: with every feed loaded, and the next schedule too in the
days before it starts, the server uses up to ~350 MB, and processing a download briefly
adds ~170 MB more.

It mounts a Cloud Storage bucket at `/data` so the schedule survives cold starts, and caps the service at one instance so the real-time API
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
| `SEPTA_GTFS_MAX_AGE_HOURS` | `168` (a week) | Re-download the timetables when the cache is older than this |
| `SEPTA_API_BASE` | `https://www3.septa.org/api` | Real-time API base (point at the mock for offline dev) |
| `SEPTA_GTFSRT_BASE` | `https://www3.septa.org/gtfsrt` | GTFS-realtime base, for the alerts the JSON API leaves out |
| `SEPTA_GEOCODER_URL` | `https://nominatim.openstreetmap.org/search` | Nominatim-compatible place search |
| `SEPTA_GEOCODER_EMAIL` | — | Contact address sent to the geocoder, which Nominatim asks heavier users for |
| `SEPTA_API_TIMEOUT` | `10` | Seconds per real-time request |
| `SEPTA_API_CACHE_SECONDS` | `10` | How long identical real-time requests share a response |
| `SEPTA_ALERTS_CACHE_SECONDS` | `60` | How long alerts are reused (the Metro tools fetch them with every answer) |
| `SEPTA_API_MAX_PER_MINUTE` | `60` | Most real-time requests sent to SEPTA in any 60 seconds (per process) |
| `MCP_TRANSPORT`, `HOST`, `PORT` | `stdio`, `0.0.0.0`, `8000` | Server transport settings |

## Tests

```bash
uv run --extra dev pytest -q      # or: pip install -e ".[dev]" && pytest -q
SEPTA_LIVE=1 pytest -q tests/test_live.py   # against SEPTA's real servers and Nominatim
```

Schedule tests run against the real GTFS feed (downloaded, or `SEPTA_GTFS_ZIP`).
Real-time tests use `tests/mock_septa_api.py`, which serves responses in SEPTA's format;
you can also run it standalone for offline development.

## Limits

- NJ Transit, PATCO and Amtrak aren't covered.
- No trip planning across modes, and bus trips only on routes that go directly: a bus
  trip that needs a change is planned leg by leg.
- Transfer itineraries are limited to one transfer; that covers any pair of Regional Rail
  stations and most Metro trips.
- SEPTA's real-time API is unofficially documented and its response formats occasionally
  shift; `realtime.py` parses defensively but is the first place to look if a live tool breaks.
