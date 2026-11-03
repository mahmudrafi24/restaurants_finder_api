# Render Free backend and Vercel frontend

## 1. Deploy the backend

In Render, choose **New → Blueprint**, connect
`mahmudrafi24/restaurants_finder_api`, and select **fix/backend-bug-audit**.
Use the repository-root `render.yaml`. The root directory is empty: this repository
already contains the backend at its root.

The Blueprint explicitly selects **Free**, Python 3.14, Frankfurt, one Uvicorn
worker, two concurrent API requests, and `/api/v1/health` as the health check.
It installs runtime dependencies using `pip install .`.

Enter a real monitored address for `CONTACT_EMAIL` and your intended Vercel origin
for `CORS_ORIGINS`, for example `https://your-project.vercel.app` (no trailing slash).
You can update the origin after Vercel assigns the actual domain. Comma-separate
multiple trusted origins. Broad preview-domain access is disabled.

After deployment, copy the actual backend URL from Render. Do not assume the
service name guarantees a particular URL. Verify:

```bash
curl --fail https://YOUR-ACTUAL-SERVICE.onrender.com/api/v1/health
```

## 2. Deploy the frontend

In Vercel, import `mahmudrafi24/restaurants_finder` with the Next.js preset.
Select **fix/frontend-bug-audit** as the production branch; the fixes are on that
branch. The root directory is the repository root (`.`), not `Frontend/`.

- Install command: `npm ci`
- Build command: `npm run build`
- Output directory: leave the Next.js default
- Node.js: 22.x
- Environment variable: `NEXT_PUBLIC_API_URL=https://YOUR-ACTUAL-SERVICE.onrender.com`

Set the API URL for Production and, if needed, Preview before deploying. Use the
backend origin without `/api/v1`; the frontend adds the API paths. Public variables
are embedded at build time, so changing this value requires a frontend redeploy.

## 3. Connect and verify

Update Render's `CORS_ORIGINS` to Vercel's actual production origin and wait for
the backend redeploy. Add custom domains explicitly when needed. Preview deployments
need their own trusted origins if you want to use them.

Open the frontend and test city autocomplete, a 1 km search, table filtering, and
CSV/XLSX/GeoJSON downloads. Verify cross-origin access with:

```bash
curl -i -X OPTIONS https://YOUR-ACTUAL-SERVICE.onrender.com/api/v1/places/search \
  -H 'Origin: https://YOUR-ACTUAL-FRONTEND.vercel.app' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type'
```

Expect `Access-Control-Allow-Origin` to match the frontend origin.

## Free-tier behavior

Render Free sleeps after 15 minutes without incoming traffic; waking takes about
a minute. The backend does not self-ping. Open its health URL and wait for it to
respond before the first search after an idle period. A cold start can exceed the
autocomplete timeout or consume much of the search timeout; retry after it wakes.
Caches reset on restart. This application needs no persistent disk or database.
Large searches and workbook exports share limited CPU and memory.

See [Render Free limits](https://render.com/docs/free),
[Render Blueprints](https://render.com/docs/blueprint-spec), and
[Vercel environment variables](https://vercel.com/docs/environment-variables).
