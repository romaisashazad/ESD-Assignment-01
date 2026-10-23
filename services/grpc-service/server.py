"""gRPC version of POST /v1/qr-codes. Runs as its own process/container.

It imports common.qrcore, the same module the HTTP service uses, so validation,
idempotency and the stored data (SQLite on the shared qr-data volume) are identical.
It does NOT call the HTTP service.
"""
import json
import logging
import os
import sys
import time
import uuid
from concurrent import futures

import grpc
from prometheus_client import Counter, Gauge, Histogram, start_http_server

import qr_pb2
import qr_pb2_grpc
from common import qrcore

PORT = int(os.environ.get("GRPC_PORT", "50051"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "8000"))
METHOD = "CreateQrCode"

# --- Metrics (measured on the server, inside the handler) ---
# Labels are bounded: method has one value per RPC defined in qr.proto, code is a gRPC status name.
grpc_server_calls_total = Counter(
    "grpc_server_calls_total", "gRPC calls finished, by method and status code", ["method", "code"])
grpc_server_call_duration_seconds = Histogram(
    "grpc_server_call_duration_seconds",
    "Time spent in the server handler, in seconds (excludes network and queueing before the handler)",
    ["method"],
    buckets=[0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.075, 0.1, 0.15, 0.2,
             0.3, 0.4, 0.5, 0.55, 0.6, 0.75, 1, 2.5])
grpc_server_in_flight = Gauge(
    "grpc_server_in_flight", "gRPC calls being handled right now", ["method"])
qr_idempotent_replays_total = Counter(
    "qr_idempotent_replays_total",
    "Requests answered from storage because the Idempotency-Key was already used")

# Create every series up front so dashboards show 0 instead of "No data".
for _code in ("OK", "INVALID_ARGUMENT", "FAILED_PRECONDITION", "INTERNAL"):
    grpc_server_calls_total.labels(method=METHOD, code=_code)
grpc_server_call_duration_seconds.labels(method=METHOD)
grpc_server_in_flight.labels(method=METHOD).set(0)

# gRPC edge of the error mapping (the HTTP edge maps the same categories to 400 / 422 / 404).
GRPC_CODES = {
    "invalid": grpc.StatusCode.INVALID_ARGUMENT,
    "conflict": grpc.StatusCode.FAILED_PRECONDITION,
    "not_found": grpc.StatusCode.NOT_FOUND,
}

logger = logging.getLogger("qr-grpc-service")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler(sys.stdout))
logger.propagate = False


def log_event(level, message, **fields):
    entry = {
        "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "service.name": "qr-grpc-service",
        "log.level": level,
        "message": message,
    }
    entry.update(fields)
    logger.info(json.dumps(entry))


class QRService(qr_pb2_grpc.QRServiceServicer):
    def CreateQrCode(self, request, context):
        # Metrics wrapper: counts the call, times it, and tracks how many are in flight.
        state = {"code": "OK"}
        grpc_server_in_flight.labels(method=METHOD).inc()
        start = time.time()
        try:
            return self._create(request, context, state)
        finally:
            grpc_server_in_flight.labels(method=METHOD).dec()
            grpc_server_call_duration_seconds.labels(method=METHOD).observe(time.time() - start)
            grpc_server_calls_total.labels(method=METHOD, code=state["code"]).inc()

    def _create(self, request, context, state):
        # The request ID travels in gRPC metadata, like the X-Request-ID header in HTTP.
        # Metadata keys are lower-case. The same ID is logged here and sent back.
        md = dict(context.invocation_metadata())
        request_id = md.get("x-request-id") or str(uuid.uuid4())
        context.send_initial_metadata((("x-request-id", request_id),))
        start = time.time()
        fields = {"grpc.method": "/qr.v1.QRService/CreateQrCode", "request.id": request_id}

        try:
            record, created = qrcore.create_qr(request.text, request.idempotency_key or None)
        except qrcore.ServiceError as e:
            status = GRPC_CODES[e.category]
            state["code"] = status.name
            log_event("ERROR", f"Request rejected: {e.code}",
                      **fields, **{"grpc.status_code": status.name, "error.code": e.code,
                                   "duration.ms": round((time.time() - start) * 1000, 2)})
            context.abort(status, f"{e.code}: {e.message}")
        except Exception as e:  # unexpected: do not leak details to the caller
            state["code"] = "INTERNAL"
            log_event("ERROR", "Unhandled error", **fields,
                      **{"grpc.status_code": "INTERNAL", "error.message": str(e)})
            context.abort(grpc.StatusCode.INTERNAL, "internal_error: Something went wrong on our side.")

        if not created:
            qr_idempotent_replays_total.inc()
        log_event("INFO", "QR code created" if created else "QR code returned (idempotent replay)",
                  **fields, **{"grpc.status_code": "OK", "qr.kind": record.kind, "qr.id": record.id,
                               "text.length": record.text_length, "idempotent.replay": not created,
                               "duration.ms": round((time.time() - start) * 1000, 2)})
        return qr_pb2.CreateQrCodeResponse(
            id=record.id, kind=record.kind, text_length=record.text_length,
            created_at=record.created_at, image_url=f"/v1/qr-codes/{record.id}",
            replayed=not created)


def build_server(port=PORT):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    qr_pb2_grpc.add_QRServiceServicer_to_server(QRService(), server)
    bound = server.add_insecure_port(f"[::]:{port}")
    return server, bound


if __name__ == "__main__":
    start_http_server(METRICS_PORT)  # Prometheus scrapes http://<container>:8000/metrics
    server, _ = build_server()
    server.start()
    log_event("INFO", f"gRPC server listening on {PORT}")
    server.wait_for_termination()