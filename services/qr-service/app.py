from flask import Flask, request, send_file, jsonify, Response
import qrcode
import io
import time
from prometheus_client import Counter, Gauge, Histogram, Summary, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)

# --- Metrics ---

# Counter: total QR codes generated (business metric)
qr_generated_total = Counter(
    "qr_generated_total",
    "Total number of QR codes generated"
)

# Counter: total failed generation requests (app metric)
qr_generation_errors_total = Counter(
    "qr_generation_errors_total",
    "Total number of failed QR generation requests",
    ["reason"]
)

# Gauge: requests currently being processed (app metric)
qr_in_progress = Gauge(
    "qr_in_progress",
    "Number of QR generation requests currently in progress"
)

# Histogram: generation duration, with buckets for percentiles (app metric)
qr_generation_duration_seconds = Histogram(
    "qr_generation_duration_seconds",
    "Time taken to generate a QR code, in seconds",
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5]
)

# Summary: average generation duration (Python summary has no percentiles, so used for average only)
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

    if not text:
        qr_generation_errors_total.labels(reason="missing_text").inc()
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

        return send_file(buf, mimetype="image/png")

    except Exception as e:
        qr_generation_errors_total.labels(reason="internal_error").inc()
        return jsonify({"error": str(e)}), 500

    finally:
        qr_in_progress.dec()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)