# QR Service: Observability (A1), then HTTP API, gRPC and Metrics (A2)

A Flask service that turns text or a URL into a QR code, plus a gRPC version of the same operation, with Prometheus metrics, structured JSON logs and a monitoring stack (Prometheus, Grafana, Node Exporter, Filebeat, Elasticsearch, Kibana). The Assignment 1 report is `REPORT.pdf`; the Assignment 2 report is `REPORT-A2.pdf`.

## Requirements

- Docker Desktop, with at least 4 GB of memory for Docker (Elasticsearch needs about 1 GB).
- PowerShell (tested on Windows 11 with the WSL2 backend). No local Python is needed: tests and clients run in temporary containers.

## Start

```powershell
docker compose up -d --build
docker compose ps        # 8 containers should be Up
```

| Service | URL / port |
|---|---|
| HTTP QR service | http://localhost:5000 |
| gRPC QR service | localhost:50051 |
| gRPC service metrics | http://localhost:8000/metrics |
| Prometheus | http://localhost:9090 (targets: qr-service, qr-grpc-service, node-exporter) |
| Grafana | http://localhost:3001 (admin / admin; the dashboard loads automatically) |
| Kibana | http://localhost:5601 |
| Elasticsearch | http://localhost:9200 |

**Kibana setup (one time):** **Stack Management → Saved objects → Import**, choose `telemetry/kibana/saved-objects.ndjson`.

## HTTP API

Full description in `openapi/openapi.yaml` (import it into Postman, or use any OpenAPI tool).

| Method and path | Purpose | Success | Errors |
|---|---|---|---|
| `POST /v1/qr-codes` | Create a QR code (JSON body `{"text": "..."}`) | `201` + `Location` | `400` `413` `415` `422` `429` |
| `GET /v1/qr-codes/{id}` | Download the PNG | `200` `image/png` | `404` |
| `POST /generate` | Deprecated: PNG directly, nothing stored | `200` | `400` `415` |
| `GET /health`, `GET /metrics` | Liveness, Prometheus metrics | `200` | |

All errors use one format: `{"error": {"code": "...", "message": "...", "request_id": "..."}}`.

- **Idempotency:** send an `Idempotency-Key` header. Repeating the request with the same key and text returns the same QR code (`Idempotent-Replayed: true`) and creates nothing new. The same key with different text gives `422`.
- **Rate limit:** 30 requests per 10 seconds per client IP on `POST /v1/qr-codes`. Over the limit gives `429` with a `Retry-After` header (seconds until the next call is allowed). The counts are kept in memory in one process.

```powershell
curl.exe -i -X POST http://localhost:5000/v1/qr-codes -H "Content-Type: application/json" -H "Idempotency-Key: demo-1" -d '{\"text\":\"https://example.com\"}'
curl.exe -D headers.txt http://localhost:5000/v1/qr-codes/<id from the response> -o qr.png
```

## gRPC service

`services/proto/qr.proto` defines `QRService.CreateQrCode` (same operation as `POST /v1/qr-codes`). `services/grpc-service/server.py` runs in its own container, imports the same business rules (`services/common/qrcore.py`) and writes to the same SQLite file (volume `qr-data`), so an HTTP `GET` can fetch a QR code created over gRPC. Error mapping: HTTP 400 is `INVALID_ARGUMENT`, 422 is `FAILED_PRECONDITION`, 404 is `NOT_FOUND`.

Regenerate the Python code (generated files are committed):

```powershell
docker run --rm -v "${PWD}/services:/s" -w /s python:3.12-slim sh -c "pip install -q --root-user-action=ignore --disable-pip-version-check grpcio-tools==1.66.2 && python -m grpc_tools.protoc -I proto --python_out=grpc-service --grpc_python_out=grpc-service proto/qr.proto"
```

Call-path diagram: `docs/call-paths.png`. The request ID travels as the `X-Request-ID` header (HTTP) and the `x-request-id` metadata (gRPC); both services log it as `request.id`, so one Kibana search (`request.id:"<id>"`) finds both.

## Python callers (run in temporary containers)

```powershell
# Reliability client: timeout, retry with backoff, circuit breaker. Modes: success | retry | breaker | ratelimit
docker run --rm -e QR_BASE_URL=http://host.docker.internal:5000 -e PYTHONDONTWRITEBYTECODE=1 -v "${PWD}/clients:/clients" -w /clients python:3.12-slim sh -c "pip install -q --root-user-action=ignore --disable-pip-version-check -r requirements.txt && python -u demo_reliability.py retry"

# One success and one failure through HTTP and through gRPC (same request ID)
docker run --rm -e QR_BASE_URL=http://host.docker.internal:5000 -e GRPC_TARGET=host.docker.internal:50051 -e PYTHONPATH=/grpc -e PYTHONDONTWRITEBYTECODE=1 -v "${PWD}/clients:/clients" -v "${PWD}/services/grpc-service:/grpc" -w /clients python:3.12-slim sh -c "pip install -q --root-user-action=ignore --disable-pip-version-check -r requirements.txt -r /grpc/requirements.txt && python -u compare_http_grpc.py"
```

Client settings: timeout 1 s, 3 attempts, back-off 0.2 s then 0.4 s (plus up to 50% jitter), breaker opens after 3 failures in a row and stays open 10 s. The gRPC client uses a 2 s deadline.

## Tests

```powershell
# HTTP API (19 tests)
docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "${PWD}/services:/app/services" -v "${PWD}/openapi:/app/openapi" -w /app/services/qr-service python:3.12-slim sh -c "pip install -q --root-user-action=ignore --disable-pip-version-check -r requirements.txt pytest pyyaml jsonschema && python -m pytest tests -v -p no:cacheprovider"

# gRPC (6 tests)
docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "${PWD}/services:/app/services" -w /app/services/grpc-service python:3.12-slim sh -c "pip install -q --root-user-action=ignore --disable-pip-version-check -r requirements.txt qrcode==7.4.2 Pillow==10.4.0 pytest && python -m pytest tests -v -p no:cacheprovider"
```

Load tests and traffic for the dashboard:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\load-test.ps1 -Minutes 1
docker run --rm -d --name traffic -e QR_BASE_URL=http://host.docker.internal:5000 -e GRPC_TARGET=host.docker.internal:50051 -e PYTHONPATH=/grpc -e PYTHONDONTWRITEBYTECODE=1 -v "${PWD}/clients:/clients" -v "${PWD}/services/grpc-service:/grpc" -w /clients python:3.12-slim sh -c "pip install -q --root-user-action=ignore --disable-pip-version-check -r requirements.txt -r /grpc/requirements.txt && python -u traffic.py 15"
```

## Metrics

Both services expose Prometheus metrics (HTTP on `:5000/metrics`, gRPC on `:8000/metrics`). The Grafana dashboard (`telemetry/grafana/dashboards/qr-service.json`) shows HTTP and gRPC calls and error rate by status, gRPC p50/p95 latency (5-minute window), the duration histogram, in-flight calls and idempotent replays. Each new metric is described in `REPORT-A2.pdf`.

Local-only test tools without authentication: `/admin/fault` (slow-request fault) and `/demo` (cardinality demo):

```powershell
Invoke-RestMethod -Method Post http://localhost:5000/admin/fault -ContentType "application/json" -Body '{"every_n":5,"delay_ms":500}'
Invoke-RestMethod -Method Post http://localhost:5000/admin/fault -ContentType "application/json" -Body '{"every_n":0,"delay_ms":0}'
```

## Clean up

```powershell
docker compose down      # stop, keep stored metrics, logs and QR codes
docker compose down -v   # stop and delete ALL stored data (cannot be undone)
```

After `down -v`, the Grafana dashboard comes back on the next start, but the Kibana saved objects must be imported again.

## Reused work

The gRPC structure follows the course gRPC workshop notes (`GRPC.md`). The observability stack, request-ID logging and fault injection come from Assignment 1 (earlier commits in this repository).
