"""HTTP API tests. Run commands are in the README."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Use a throwaway database and a small rate limit so the tests are fast.
os.environ["QR_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["RATE_LIMIT_MAX"] = "5"
os.environ["RATE_LIMIT_WINDOW"] = "2"

import pytest  # noqa: E402
import app as appmod  # noqa: E402

PNG = b"\x89PNG"


@pytest.fixture()
def client():
    appmod._hits.clear()
    return appmod.app.test_client()


def test_create_valid_returns_201_and_location(client):
    r = client.post("/v1/qr-codes", json={"text": "https://example.com"})
    assert r.status_code == 201
    assert r.headers["Location"] == "/v1/qr-codes/" + r.json["id"]
    assert r.json["kind"] == "url"
    img = client.get(r.headers["Location"])
    assert img.status_code == 200 and img.data.startswith(PNG)
    assert img.headers["Content-Type"] == "image/png"


@pytest.mark.parametrize("body,status,code", [
    ({}, 400, "missing_text"),
    ({"text": ""}, 400, "invalid_text"),
    ({"text": 123}, 400, "invalid_text"),
    ({"text": "x" * 1001}, 400, "text_too_long"),
    ([1, 2], 400, "invalid_json"),
])
def test_invalid_bodies(client, body, status, code):
    r = client.post("/v1/qr-codes", json=body)
    assert r.status_code == status
    assert r.json["error"]["code"] == code
    assert r.json["error"]["request_id"] == r.headers["X-Request-ID"]


def test_wrong_content_type_415(client):
    r = client.post("/v1/qr-codes", data="text=hi", content_type="text/plain")
    assert r.status_code == 415 and r.json["error"]["code"] == "unsupported_media_type"


def test_body_too_large_413(client):
    r = client.post("/v1/qr-codes", json={"text": "a" * 20000})
    assert r.status_code == 413 and r.json["error"]["code"] == "payload_too_large"


def test_unknown_id_404_and_bad_id_404(client):
    for path in ("/v1/qr-codes/00000000-0000-4000-8000-000000000000", "/v1/qr-codes/nope"):
        r = client.get(path)
        assert r.status_code == 404 and r.json["error"]["code"] == "not_found"


def test_wrong_method_405_has_allow(client):
    r = client.delete("/v1/qr-codes")
    assert r.status_code == 405 and "POST" in r.headers["Allow"]
    assert r.json["error"]["code"] == "method_not_allowed"


def test_idempotency_replay_same_resource_not_double_counted(client):
    def made():
        return sum(appmod.qr_generated_total.labels(kind=k)._value.get() for k in ("url", "text"))
    before = made()
    h = {"Idempotency-Key": "order-1"}
    a = client.post("/v1/qr-codes", json={"text": "hello"}, headers=h)
    b = client.post("/v1/qr-codes", json={"text": "hello"}, headers=h)
    assert a.status_code == b.status_code == 201
    assert a.json["id"] == b.json["id"]
    assert b.headers["Idempotent-Replayed"] == "true" and "Idempotent-Replayed" not in a.headers
    assert made() == before + 1


def test_without_key_repeat_creates_new_resource(client):
    a = client.post("/v1/qr-codes", json={"text": "hello"})
    b = client.post("/v1/qr-codes", json={"text": "hello"})
    assert a.json["id"] != b.json["id"]


def test_key_reuse_with_different_text_422(client):
    h = {"Idempotency-Key": "k-conflict"}
    client.post("/v1/qr-codes", json={"text": "one"}, headers=h)
    r = client.post("/v1/qr-codes", json={"text": "two"}, headers=h)
    assert r.status_code == 422 and r.json["error"]["code"] == "idempotency_key_reused"


def test_bad_key_400(client):
    r = client.post("/v1/qr-codes", json={"text": "a"}, headers={"Idempotency-Key": "bad key!"})
    assert r.status_code == 400 and r.json["error"]["code"] == "invalid_idempotency_key"


def test_rate_limit_429_with_retry_after_then_recovers(client):
    import time
    codes = [client.post("/v1/qr-codes", json={"text": "r"}).status_code for _ in range(5)]
    assert codes == [201] * 5
    r = client.post("/v1/qr-codes", json={"text": "r"})
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 2
    assert r.json["error"]["code"] == "rate_limited"
    time.sleep(int(r.headers["Retry-After"]) + 0.1)
    assert client.post("/v1/qr-codes", json={"text": "r"}).status_code == 201


def test_rate_limit_is_per_ip(client):
    for _ in range(5):
        client.post("/v1/qr-codes", json={"text": "r"})
    other = client.post("/v1/qr-codes", json={"text": "r"},
                        environ_overrides={"REMOTE_ADDR": "10.9.9.9"})
    assert other.status_code == 201


def test_health_and_metrics_not_rate_limited(client):
    for _ in range(10):
        assert client.get("/health").status_code == 200
    assert b"http_requests_total" in client.get("/metrics").data


def test_legacy_generate_still_works_and_is_deprecated(client):
    r = client.post("/generate", json={"text": "hi"})
    assert r.status_code == 200 and r.data.startswith(PNG) and r.headers["Deprecation"] == "true"
    assert client.post("/generate", json={}).status_code == 400

def test_responses_match_openapi_schemas(client):
    """Check real responses against the schemas in openapi/openapi.yaml."""
    import jsonschema
    import yaml
    spec_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "openapi", "openapi.yaml")
    schemas = yaml.safe_load(open(spec_path))["components"]["schemas"]

    for name, resp in (("QrCode", client.post("/v1/qr-codes", json={"text": "spec check"})),
                       ("Error", client.post("/v1/qr-codes", json={})),
                       ("Error", client.get("/v1/qr-codes/nope"))):
        jsonschema.validate(resp.json, schemas[name])