# Backend bug fixes

Date: 2026-11-03. Branch: `fix/backend-bug-audit`. Based on the backend `backend` branch.

## Implemented changes

| Audit ID | Resolution |
| --- | --- |
| 1 | XLSX text cells explicitly use string type. CSV prefixes formula-like text with an apostrophe, including whitespace/control-prefix cases. Coordinates remain numeric. |
| 2 | A total request deadline bounds provider calls, retries, fallback mirrors, parsing and response handling. Defaults: 55 seconds total, 16 seconds per upstream attempt, 12 seconds in Overpass, no same-endpoint retries. Mirrors provide fallback. |
| 3 | Overpass responses with remarks or malformed element lists trigger mirror fallback rather than returning partial data. |
| 4 | Same-type nearby objects survive deduplication. Only mutually unambiguous node/outline pairs within 10 metres with a matching address/contact identity and no conflicting address fields merge. Complementary fields are retained. |
| 5 | XML-illegal characters are removed from workbook cells and titles. |
| 6 | Geocoder JSON, collection shapes, coordinates and optional bounding boxes are checked. Invalid records are skipped; completely unusable responses raise an upstream error so Photon failures activate Nominatim fallback. Valid empty responses remain empty. |
| 10 | Website normalization validates HTTP(S), hostnames, ports and unsupported schemes; malformed URLs are dropped. |
| 11 | City strings and city query parameters are trimmed before length checks. |
| 12 | A 4,000,000-byte body limit applies before JSON parsing, including chunked requests. Export text is capped at 2,000 characters per field, cuisine at 100 entries and rows at 10,000. |
| 13 | The pytest async fixture loop scope is explicitly `function`. |
| 14 | Ruff errors were fixed. |

Frontend findings 7–9 and browser-side export checks are handled on `fix/frontend-bug-audit` in the frontend repository.

Additional changes address the audit's resilience concerns: expensive API requests have bounded concurrency and per-client admission limits; Nominatim calls are paced; concurrent geocoder cache misses are serialized and recheck the cache; export rendering runs in threads with two rendering slots that remain occupied until rendering ends, even after caller cancellation. Cheap dependency providers are async, and health reads settings from the current app factory. Background self-ping cancellation is awaited during shutdown.

## Verification

- Ruff: passed for application and tests.
- mypy: passed for 31 application files.
- Backend test suite: 206 tests passed, including regressions for formula text, illegal XML, nearby venues, malformed providers, total deadlines, rate/concurrency limits, byte limits, cache miss coalescing and health responsiveness during export.
- The original 165-test suite is included in that total; old malformed-Photon expectations were updated to assert deliberate fallback behavior, and the duplicate fixture now includes corroborating identity.

The available sandbox denies the self-pipe writes asyncio uses to wake the event loop after thread completion (`PermissionError: Operation not permitted`). Tests were run using a temporary launcher outside this repository that caps selector waits at 10 ms. It leaves application logic and test assertions unchanged. No sandbox workaround is committed. Rerun the standard commands below in ordinary CI before merging.

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check app tests
.venv/bin/mypy app
```

## Deployment configuration and limits

Update existing environment overrides to the values documented in `.env.example`; existing explicit 55-second upstream / 50-second Overpass values remain accepted but give fallback less time. `REQUEST_TIMEOUT_SECONDS` cannot exceed 55 seconds. Keep the hosting function deadline above it.

`MAX_CONCURRENT_REQUESTS` defaults to 4; `REQUESTS_PER_MINUTE` defaults to 30. Limits, request coalescing, pacing and cache storage are per process. Multiple workers/replicas need a shared ingress rate limiter and provider pacing, or a single geocoding worker/service. Configure trusted proxy handling at deployment so the ASGI client address reflects the actual client; this middleware does not trust arbitrary forwarded headers.

Exports above the byte limit must be narrowed using result filters. Text over the schema limits is rejected for submitted exports; external place tags are bounded during normalization. Conservative deduplication intentionally retains uncertain duplicates rather than deleting possible real venues. System-wide load testing, live-provider smoke testing and deployment verification remain external validation steps.

## Follow-up code refactor

Request limiting now separates client admission, bounded body reads, body replay and deadline
handling into focused helpers. Threaded export rendering and cancellation-safe slot management
live in `app/services/exporters/render.py`, keeping the export route focused on HTTP responses.
The existing 206 tests pass, Ruff passes, and mypy passes for 32 application files. The same
sandbox-only test-runner workaround described above was used.
