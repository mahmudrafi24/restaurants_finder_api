# Restaurant Scraper API

HTTP API that collects cafe, restaurant and fast-food data from OpenStreetMap and exports it to XLSX, CSV or GeoJSON.

Built with **FastAPI** · [Render Free deployment](docs/DEPLOYMENT.md) · Data © OpenStreetMap contributors ([ODbL](https://www.openstreetmap.org/copyright))

---

## Prerequisites

- **Python 3.11+**
- **pip** (comes with Python)

---

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/mahmudrafi24/cafe_scraping.git
cd restaurant-scraper-api
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it:

| OS            | Command                       |
|---------------|-------------------------------|
| **Windows**   | `.venv\Scripts\activate`      |
| **macOS / Linux** | `source .venv/bin/activate` |

### 3. Install dependencies

```bash
# Runtime + dev dependencies (testing, linting, type checking)
pip install -e ".[dev]"
```

Or for runtime only:

```bash
pip install -e .
```

### 4. Configure environment variables

```bash
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
```

The defaults work out of the box for local development. See [`.env.example`](.env.example) for all available options.

### 5. Start the development server

```bash
uvicorn app.main:app --reload
```

The server starts at **http://127.0.0.1:8000** by default.

| URL | Description |
|-----|-------------|
| http://127.0.0.1:8000/docs | Swagger UI (interactive API docs) |
| http://127.0.0.1:8000/redoc | ReDoc (alternative docs) |
| http://127.0.0.1:8000/openapi.json | OpenAPI schema |

> **Tip:** Add `--host 0.0.0.0 --port 8080` to change the bind address or port.

---

## API Endpoints

All routes are prefixed with `/api/v1`.

### System

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/health` | Liveness check — returns status, version, environment |

### Cities

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/cities/search?q=<query>` | Autocomplete city names (min 2 chars). Returns candidate places with coordinates. |

**Query parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | *(required)* | Partial place name (2–200 chars) |
| `limit` | int | `8` | Max results (1–20) |

### Places

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/places/search` | Find cafes, restaurants and fast food near a city |
| `POST` | `/api/v1/places/export` | Download search results as XLSX, CSV or GeoJSON |

**Typical flow:**

```
1. GET  /api/v1/cities/search?q=hamb     → resolve a city name
2. POST /api/v1/places/search            → fetch amenities within a radius
3. POST /api/v1/places/export            → download the data as a file
```

---

## Accessing the API

Once the server is running (`uvicorn app.main:app --reload`):

| What | URL |
|------|-----|
| Base URL | `http://127.0.0.1:8000/api/v1` |
| Interactive docs (try requests in the browser) | `http://127.0.0.1:8000/docs` |
| Health check | `http://127.0.0.1:8000/api/v1/health` |

Quick check from a terminal:

```bash
curl http://127.0.0.1:8000/api/v1/health
curl "http://127.0.0.1:8000/api/v1/cities/search?q=hamb"
```

To reach it from another device on your network, start with `uvicorn app.main:app --host 0.0.0.0 --port 8000` and use `http://<your-pc-ip>:8000`. When deployed on Vercel, replace the base URL with your deployment URL (e.g. `https://<project>.vercel.app/api/v1`).

---

## Testing with Postman

### 1. Start the backend

```bash
.venv\Scripts\activate
uvicorn app.main:app --reload
```

Keep this terminal open. Confirm `http://127.0.0.1:8000/docs` loads in a browser.

### 2. Set up a Postman environment (optional but handy)

Create an environment with a variable `baseUrl` = `http://127.0.0.1:8000/api/v1`, then use `{{baseUrl}}` in the request URLs below.

> **Shortcut:** In Postman choose **Import → Link** and paste `http://127.0.0.1:8000/openapi.json` to auto-generate a collection with every endpoint.

### 3. Requests

#### Health check
- **Method:** `GET`
- **URL:** `{{baseUrl}}/health`
- Expect `200 OK` with status, version and environment.

#### City search
- **Method:** `GET`
- **URL:** `{{baseUrl}}/cities/search?q=hamburg&limit=5`
- Or add `q` / `limit` under the **Params** tab.
- Expect `200 OK` with a list of cities and coordinates.

#### Search places
- **Method:** `POST`
- **URL:** `{{baseUrl}}/places/search`
- **Body:** `raw` → `JSON` (Postman sets `Content-Type: application/json`)

```json
{
  "city": "Hamburg",
  "radius_km": 5,
  "amenities": ["cafe", "restaurant", "fast_food"]
}
```

| Field | Notes |
|-------|-------|
| `city` | Required, 2–200 chars |
| `radius_km` | 1–25, default `10` |
| `amenities` | At least one of `cafe`, `restaurant`, `fast_food` |
| `latitude`, `longitude` | Optional — skip geocoding by passing coordinates (e.g. from city search) |

The response contains the `places` list, stats and `elapsed_ms`. Large radii can take a while — Overpass is a public service.

#### Export places
- **Method:** `POST`
- **URL:** `{{baseUrl}}/places/export`
- **Body:** `raw` → `JSON`

```json
{
  "format": "csv",
  "city_name": "Hamburg",
  "places": [
    {
      "osm_id": "node/123456",
      "name": "Example Cafe",
      "amenity": "cafe",
      "latitude": 53.55,
      "longitude": 9.99
    }
  ]
}
```

`format` is `xlsx` (default), `csv` or `geojson`. For real data, copy the `places` array from the search response into this body. Match the `Place` schema in `/docs` if a `422` is returned for missing fields.

To save the file, click **Send and Download** (arrow next to **Send**). For CSV/GeoJSON you can also view the content in the response body.

### Common errors

| Status | Meaning |
|--------|---------|
| `422` | Invalid body/params — read the response's error details |
| `404`/`502`/`504` | City not found or an upstream service (Nominatim/Overpass/Photon) failed or timed out — retry |
| Connection refused | Server not running, or wrong host/port |

---

## Running Tests

The test suite uses **pytest** with **respx** to mock all upstream HTTP calls — no real network requests are made.

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_api.py

# Run with coverage report
pytest --cov=app --cov-report=term-missing
```

### Test files

| File | Covers |
|------|--------|
| `tests/test_api.py` | API endpoint integration tests |
| `tests/test_config.py` | Settings and environment configuration |
| `tests/test_exporters.py` | XLSX / CSV / GeoJSON export |
| `tests/test_geocoding.py` | City geocoding via Nominatim |
| `tests/test_overpass.py` | Overpass API query building and parsing |
| `tests/test_places_service.py` | Place search orchestration |
| `tests/test_suggest.py` | City autocomplete via Photon |

---

## Code Quality

```bash
# Lint and auto-fix
ruff check --fix .

# Format
ruff format .

# Type checking
mypy app
```

---

## Project Structure

```
restaurant-scraper-api/
├── api/
│   └── index.py              # Vercel serverless entrypoint
├── app/
│   ├── api/
│   │   ├── deps.py           # FastAPI dependency injection
│   │   └── v1/
│   │       ├── router.py     # v1 route aggregator
│   │       └── routes/
│   │           ├── cities.py  # City search / autocomplete
│   │           ├── health.py  # Liveness check
│   │           └── places.py  # Place search and export
│   ├── config.py             # Settings (pydantic-settings)
│   ├── core/                 # HTTP client, errors, logging
│   ├── schemas/              # Pydantic request/response models
│   ├── services/             # Business logic (geocoding, overpass, export)
│   └── main.py               # Application factory
├── tests/                    # pytest test suite
├── .env.example              # Environment variable template
├── pyproject.toml            # Project metadata and tool config
├── requirements.txt          # Vercel runtime dependencies
└── vercel.json               # Vercel deployment config
```

---

## License

MIT

## Request budgets and audit fixes

See [the bug fix report](docs/BUG_FIX_REPORT.md) for changes, test evidence and deployment limits.
The API enforces a 55-second total request deadline and a 4,000,000-byte request body limit for
search/export routes. Request admission defaults to four simultaneous expensive requests and
30 requests per client per minute, per worker. Exports also retain the 10,000-row cap.
Use the updated `.env.example` timeout values to leave room for fallback mirrors.
For multiple workers or replicas, enforce shared rate limits and provider pacing at ingress or
through a dedicated geocoding service.
