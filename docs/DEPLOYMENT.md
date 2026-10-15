# Deployment

The app is one container: `uvicorn server:app`. It serves the shortener at the root and the dashboard at `/orchestrator/`.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | 8000 | Port (Render sets it) |
| `DATA_DIR` | `data` | SQLite databases and run workspaces |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Used to build `short_url` in API responses. Set to your public URL |
| `TRUST_PROXY` | 0 | Set to 1 behind a proxy so the rate limiter sees real client addresses |
| `ADMIN_TOKEN` | unset | If set, starting runs and approvals need header `X-Admin-Token` (the dashboard has a field for it) |
| `SHORTENER_RATE_LIMIT` | 30 | Link creations per client per minute |
| `ORCH_RUNS_PER_MINUTE` | 10 | Run starts per client per minute |
| `ORCH_MAX_ACTIVE_RUNS` | 3 | Concurrently *running* runs |

Run with **one worker**. Run state and rate limiting are in process.

## Local

```bash
pip install -r requirements-dev.txt
uvicorn server:app --port 8000
```

## Docker

```bash
docker build -t agentic-url-shortener .
docker run -p 8000:8000 -v agentic-data:/data agentic-url-shortener
```

## Render (free tier)

1. Push this repo to GitHub.
2. Render dashboard: New, Blueprint, pick the repo. `render.yaml` configures a Docker web service with the health check at `/healthz`.
3. After the first deploy set `PUBLIC_BASE_URL` to the service URL.
4. Optional: add `ADMIN_TOKEN` if you do not want strangers starting runs. Leave it unset for a reviewer demo; run starts are rate limited and capped.

Free-tier caveats: the service sleeps when idle (the first request after a pause is slow) and state resets on redeploy, because there is no persistent disk. For a demo that is fine: run a scenario and the whole history appears in the dashboard within seconds.

Railway and Fly.io work the same way: the Dockerfile is all they need. Mount a volume at `/data` if you want state to survive restarts.

## Checks after deploying

```bash
curl $URL/healthz
curl -X POST $URL/api/links -H 'content-type: application/json' -d '{"url":"https://example.org"}'
curl -s $URL/orchestrator/api/scenarios
```

Then open `$URL/orchestrator/`, start the greenfield scenario and wait for it to reach SUCCEEDED.
