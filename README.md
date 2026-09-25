# QR Service with Observability (ESD Assignment 1)

A Flask API that turns text or a URL into a QR code PNG, with Prometheus metrics, structured JSON logs, and a monitoring stack: Prometheus, Grafana, Node Exporter, Filebeat, Elasticsearch and Kibana. The full write-up is in `REPORT.md`.

## Requirements

- Docker Desktop, with at least 4 GB of memory for Docker (Elasticsearch needs about 1 GB).
- PowerShell (tested on Windows 11 with the WSL2 backend).

## Start

```powershell
docker compose up -d --build
docker compose ps        # all 7 containers should be Up
```

| Service | URL |
|---|---|
| QR service | http://localhost:5000 (`/generate`, `/health`, `/metrics`) |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 (admin / admin; the dashboard loads automatically) |
| Kibana | http://localhost:5601 |
| Elasticsearch | http://localhost:9200 |

**Kibana setup (one time):** go to **Stack Management → Saved objects → Import** and choose `telemetry/kibana/saved-objects.ndjson`. This adds the "QR Service Logs" data view and the saved searches.

## Use

```powershell
Invoke-WebRequest -Uri http://localhost:5000/generate -Method POST -ContentType "application/json" -Body '{"text":"https://example.com"}' -OutFile qr.png
```

This returns `200` with a PNG. A missing `text` returns `400`. Every response has an `X-Request-ID` header, which you can search in Kibana with `request.id:"<id>"`.

## Test

```powershell
Invoke-RestMethod http://localhost:5000/health
powershell -ExecutionPolicy Bypass -File scripts\load-test.ps1 -Minutes 6    # repeatable load test

# Slow-request fault (every 5th request +500 ms), switched at runtime
Invoke-RestMethod -Method Post http://localhost:5000/admin/fault -ContentType "application/json" -Body '{"every_n":5,"delay_ms":500}'
Invoke-RestMethod -Method Post http://localhost:5000/admin/fault -ContentType "application/json" -Body '{"every_n":0,"delay_ms":0}'

# Cardinality demo (capped at 100 IDs)
$env:DEMO_REQUEST_ID_LABEL="true";  docker compose up -d --build qr-service
1..100 | ForEach-Object { Invoke-WebRequest -Method Post http://localhost:5000/demo -UseBasicParsing | Out-Null }
$env:DEMO_REQUEST_ID_LABEL="false"; docker compose up -d qr-service
```

`/admin/fault` and `/demo` are for local testing only and have no authentication.

## Clean up

```powershell
# 1. Undo the experiments
Invoke-RestMethod -Method Post http://localhost:5000/admin/fault -ContentType "application/json" -Body '{"every_n":0,"delay_ms":0}'
Remove-Item Env:DEMO_REQUEST_ID_LABEL -ErrorAction SilentlyContinue

# 2. Stop, keeping stored metrics and logs
docker compose down

# 3. Stop and delete ALL stored metrics, logs and settings (cannot be undone)
docker compose down -v
```

After `down -v`, the Grafana dashboard comes back on the next start, but the Kibana saved objects must be imported again.
