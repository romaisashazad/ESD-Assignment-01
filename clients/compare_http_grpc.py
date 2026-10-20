"""Sends one success and one failure through HTTP and through gRPC, using the SAME request ID,
and prints how each protocol reports the result.

Usage: python compare_http_grpc.py        (needs PYTHONPATH to include services/grpc-service)
"""
import os
import uuid

import grpc
import requests

import grpc_client

BASE = os.environ.get("QR_BASE_URL", "http://localhost:5000")


def http_create(text, rid):
    r = requests.post(f"{BASE}/v1/qr-codes", json={"text": text},
                      headers={"X-Request-ID": rid}, timeout=2)
    return r.status_code, r.json()


def grpc_create(text, rid):
    try:
        resp, _ = grpc_client.create_qr(text, request_id=rid)
        return "OK", {"id": resp.id, "kind": resp.kind, "image_url": resp.image_url}
    except grpc.RpcError as e:
        return e.code().name, e.details()


for label, text in (("SUCCESS", "https://example.com/grpc-demo"), ("FAILURE (empty text)", "")):
    rid = f"cmp-{uuid.uuid4().hex[:8]}"
    print(f"\n=== {label}   request id: {rid}")
    print("HTTP :", *http_create(text, rid))
    status, body = grpc_create(text, rid)
    print("gRPC :", status, body)
    if status == "OK":
        # Shared source of truth: the HTTP service serves the QR code that gRPC just created.
        r = requests.get(BASE + body["image_url"], timeout=2)
        print(f"HTTP GET {body['image_url']} -> {r.status_code} {r.headers['Content-Type']}")