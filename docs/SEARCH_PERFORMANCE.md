# Search latency and scaling

A deployed Leipzig test (1 km, cafes, coordinates supplied) returned HTTP 504 in
55.28 seconds before these changes. Health and autocomplete succeeded in the
reported deployment logs. A healthy app therefore does not prove that its Overpass
provider is reachable or has capacity. The small-area reproduction means density
alone cannot explain this incident. Exact failing mirrors and deployed environment
overrides still need confirmation from deployment logs.

## Changes implemented

- Each Overpass endpoint has a 16-second maximum budget. Same-endpoint retries
  are disabled for Overpass, even if `MAX_RETRIES` was previously set to 2.
  The overall 55-second request deadline remains in force. The server-side query
  timeout is capped at 12 seconds by default, leaving transfer time.
- Successful normalized place results are cached for 10 minutes per process.
  Exact coordinates, radius, and sorted unique amenity types define the key.
  City aliases and amenity ordering can share results without changing coverage.
- Concurrent identical cache misses use one lookup when the result fits in the
  cache. Unrelated areas do not wait on the same lock. Failures are never cached;
  cancellation frees the lock and allows a waiter to take over.
- The cache retains at most eight serialized entries of up to 2,000,000 bytes
  each. Larger results are returned normally but not retained. Returned objects
  are independent of cached data. Empty successful responses are cached too.
- The frontend starts at 5 km, disables automatic search retries and reconnect
  refetches, and shows elapsed time with a Cancel button. Cancel aborts the browser
  fetch; it cannot guarantee an upstream server stops a query already submitted.

These changes avoid duplicate work and improve waiting behavior. They do not
guarantee fast uncached searches or make public Overpass capacity predictable.
Results may be up to 10 minutes old. Caches disappear on Render restarts and sleep.

## Deployment and verification

Merge the new backend changes into the branch Render actually deploys (`backend`
for this project). Merely pushing a previously merged feature branch does not
update that production branch. Deploy the corresponding frontend changes too.

The new cache settings have defaults; Render environment overrides are optional.
Review existing `OVERPASS_BASE_URL`, `OVERPASS_FALLBACK_URLS`,
`UPSTREAM_TIMEOUT_SECONDS`, `MAX_RETRIES`, and `REQUEST_TIMEOUT_SECONDS` settings
against `.env.example`. Verify the primary and fallback URLs from Render itself.
Do not raise concurrency simply to compensate for a slow public provider.

After deployment, time the same exact search twice, then test two simultaneous
identical requests, an unrelated city, all three export formats, and cancellation.
Confirm mirror fallback in logs and that failed queries are not cached. This
change passed 225 backend tests, Ruff and mypy locally. The Leipzig failure above
is a pre-change measurement; deployed post-change latency and load tests remain
unverified. Frontend production builds still need verification in Vercel.

## Scaling beyond the free instance

For sustained traffic, move the shared cache and per-key coordination to Redis
or another shared store; this cache only coordinates one Python process. Keep
shared provider rate limits and cap queue depth. Slow large-area queries should
run as background jobs with a job ID, polling status, and downloadable stored
results, rather than keeping a browser request open.

For predictable city searches at larger scale, ingest regional OpenStreetMap POI
data into PostgreSQL/PostGIS on a schedule, index amenity types and geography,
and serve bounded spatial queries with pagination. Keep exports as separate jobs.
Redis/jobs remove repeat work but cannot make an unavailable public provider fast;
a local spatial dataset removes that provider from the interactive request path.

Render Free is limited to 0.1 CPU and 512 MB RAM and sleeps after inactivity.
It is suitable for a demo, not a guarantee of scalable production performance.
See [Render compute plans](https://render.com/docs/compute-plans),
[free-service behavior](https://render.com/docs/free), and
[Overpass resource limits](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html).
