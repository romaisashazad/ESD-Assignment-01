from flask import Flask, request, send_file, jsonify
import qrcode
import io

app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    text = data.get("text")

    if not text:
        return jsonify({"error": "Missing 'text' field"}), 400

    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return send_file(buf, mimetype="image/png")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)