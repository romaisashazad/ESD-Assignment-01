from flask import Flask, request, send_file, jsonify, Response, g
import uuid
import qrcode
import io
import time
import json
import logging
import sys
from prometheus_client import Counter, Gauge, Histogram, Summary, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
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

qr_generated_total = Counter(
    "qr_generated_total",
    "Total number of QR codes generated"
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

        duration = time.time() - start
        qr_generation_duration_seconds.observe(duration)
        qr_generation_duration_summary.observe(duration)
        qr_generated_total.inc()

        log_event(
            "INFO", "QR code generated successfully",
            **{"http.method": "POST", "http.path": "/generate",
               "http.response.status_code": 200,
               "duration.ms": round(duration * 1000, 2),
               "request.id": request_id,
               "text.length": len(text)}
        )

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