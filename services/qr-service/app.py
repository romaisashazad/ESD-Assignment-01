from flask import Flask, request, send_file, jsonify, Response, g
import qrcode
import io
import time
import json
import logging
import sys
import uuid
import threading
import os
from prometheus_client import Counter, Gauge, Histogram, Summary, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)


# --- Request IDs (Fix 1) ---
# Use the client's X-Request-ID if it sent one, otherwise generate a new one.
# The ID is returned in the response header so the client can quote it.
@app.before_request
def assign_request_id():
    g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())


@app.after_request
def return_request_id(resp):
    resp.headers["X-Request-ID"] = g.request_id
    return resp


# --- Structured JSON logger ---
logger = logging.getLogger("qr-service")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)
logger.propagate = False

# Fix 2b: silence Werkzeug's plain-text access log (one line per request,
# including every Prometheus scrape of /metrics). Only real errors are kept.
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def log_event(level, message, **fields):
    entry = {
        "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "service.name": "qr-service",
        "log.level": level,
        "message": message,
    }
    entry.update(fields)
    logger.info(json.dumps(entry))


# --- Metrics ---

# Business metric. "kind" has only two possible values (url / text),
# so it adds just two time series.
qr_generated_total = Counter(
    "qr_generated_total",
    "Total number of QR codes generated",
    ["kind"]
)

qr_generation_errors_total = Counter(
    "qr_generation_errors_total",
    "Total number of failed QR generation requests",
    ["reason"]
)

qr_in_progress = Gauge(
    "qr_in_progress",
    "Number of QR generation requests currently in progress"
)

qr_generation_duration_seconds = Histogram(
    "qr_generation_duration_seconds",
    "Time taken to generate a QR code, in seconds",
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5]
)

qr_generation_duration_summary = Summary(
    "qr_generation_duration_summary_seconds",
    "Summary of QR code generation duration, in seconds"
)

# Part E: 1 while fault injection is switched on, 0 otherwise.
# Lets the dashboard show exactly when the fault was active.
qr_fault_injection_active = Gauge(
    "qr_fault_injection_active",
    "1 if the Part E slow-request fault is switched on, else 0"
)

# Fix 6: create every label combination up front so each series exists at 0.
# Without this, a series only appears after its first increment, and panels
# show "No data" instead of 0.
for k in ("url", "text"):
    qr_generated_total.labels(kind=k)
for r in ("missing_text", "internal_error"):
    qr_generation_errors_total.labels(reason=r)


# --- Part E: fault injection ---
# Switched on and off at runtime through POST /admin/fault, so the container
# never restarts during the experiment (a restart would reset the counters and
# add a slow cold start, which would muddy the results).
# every_n = 0 means off. Local testing only: a real service must never expose
# an unauthenticated endpoint like this.
fault = {"every_n": 0, "delay_ms": 0}
fault_lock = threading.Lock()
generate_calls = 0


@app.route("/admin/fault", methods=["GET", "POST"])
def admin_fault():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        with fault_lock:
            fault["every_n"] = max(0, int(data.get("every_n", 0)))
            fault["delay_ms"] = max(0, int(data.get("delay_ms", 0)))
            active = fault["every_n"] > 0 and fault["delay_ms"] > 0
        qr_fault_injection_active.set(1 if active else 0)
        log_event("WARN" if active else "INFO",
                  "Fault injection switched " + ("ON" if active else "OFF"),
                  **{"fault.every_n": fault["every_n"],
                     "fault.delay_ms": fault["delay_ms"],
                     "request.id": g.request_id})
    return jsonify(fault), 200


def injected_delay_ms():
    """Return the delay to add to this request (0 for most requests)."""
    global generate_calls
    with fault_lock:
        generate_calls += 1
        n, delay = fault["every_n"], fault["delay_ms"]
        if n > 0 and delay > 0 and generate_calls % n == 0:
            return delay
    return 0


# --- Part E.2: cardinality experiment ---
# DEMO_REQUEST_ID_LABEL=true  -> demo_requests_total gets a request_id label,
#                                so every request creates a NEW time series.
# DEMO_REQUEST_ID_LABEL=false -> the same counter with no labels: one series.
# Capped at 100 unique IDs so the experiment can never grow out of control.
DEMO_LABELLED = os.environ.get("DEMO_REQUEST_ID_LABEL", "false").lower() == "true"
DEMO_MAX_IDS = 100
demo_ids_seen = set()

if DEMO_LABELLED:
    demo_requests_total = Counter(
        "demo_requests_total",
        "Cardinality demo counter, labelled by request_id (BAD practice)",
        ["request_id"]
    )
else:
    demo_requests_total = Counter(
        "demo_requests_total",
        "Cardinality demo counter, no labels (good practice)"
    )


@app.route("/demo", methods=["POST"])
def demo():
    if DEMO_LABELLED:
        with fault_lock:
            if g.request_id not in demo_ids_seen and len(demo_ids_seen) >= DEMO_MAX_IDS:
                return jsonify({"error": "cap of 100 unique IDs reached"}), 429
            demo_ids_seen.add(g.request_id)
        demo_requests_total.labels(request_id=g.request_id).inc()
    else:
        demo_requests_total.inc()
    return jsonify({"labelled": DEMO_LABELLED, "request_id": g.request_id}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/metrics", methods=["GET"])
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    request_id = g.request_id

    if not text:
        qr_generation_errors_total.labels(reason="missing_text").inc()
        log_event(
            "ERROR", "QR generation failed: missing text field",
            **{"http.method": "POST", "http.path": "/generate",
               "http.response.status_code": 400, "request.id": request_id}
        )
        return jsonify({"error": "Missing 'text' field"}), 400

    qr_in_progress.inc()
    start = time.time()

    try:
        img = qrcode.make(text)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        # Part E: the delay sits inside the timed section, so it shows up in
        # the histogram, the summary and the log's duration.ms, just like a
        # real slowdown would.
        delay = injected_delay_ms()
        if delay:
            time.sleep(delay / 1000)

        duration = time.time() - start
        qr_generation_duration_seconds.observe(duration)
        qr_generation_duration_summary.observe(duration)

        kind = "url" if text.lower().startswith(("http://", "https://")) else "text"
        qr_generated_total.labels(kind=kind).inc()

        # Only the length and kind of the text are logged, never the text itself,
        # so no user content ends up in the logs.
        fields = {"http.method": "POST", "http.path": "/generate",
                  "http.response.status_code": 200,
                  "duration.ms": round(duration * 1000, 2),
                  "request.id": request_id,
                  "qr.kind": kind,
                  "text.length": len(text)}
        if delay:
            fields["fault.delay_ms"] = delay
        log_event("INFO", "QR code generated successfully", **fields)

        return send_file(buf, mimetype="image/png")

    except Exception as e:
        qr_generation_errors_total.labels(reason="internal_error").inc()
        log_event(
            "ERROR", "QR generation failed: internal error",
            **{"http.method": "POST", "http.path": "/generate",
               "http.response.status_code": 500,
               "error.message": str(e), "request.id": request_id}
        )
        return jsonify({"error": str(e)}), 500

    finally:
        qr_in_progress.dec()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)