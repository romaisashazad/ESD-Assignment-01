"""Sends a steady mix of HTTP and gRPC calls so the Grafana panels have data.
Usage: python traffic.py [minutes]      (needs PYTHONPATH to include services/grpc-service)

Each half-second it makes one HTTP call and one gRPC call (2 per second each, below the
HTTP limit of 3 per second). Mix: 70% new, 15% invalid, 10% repeat (same key), 5% key conflict.
"""
import os
import random
import sys
import time
from collections import Counter

import grpc
import requests

import grpc_client

BASE = os.environ.get("QR_BASE_URL", "http://localhost:5000")
minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 5
end = time.time() + minutes * 60
seen = Counter()
i = 0

while time.time() < end:
    i += 1
    r = random.random()
    if r < 0.70:
        text, key = f"item-{i}", ""
    elif r < 0.85:
        text, key = "", ""                      # invalid: empty text
    elif r < 0.95:
        text, key = "repeat-me", "traffic-repeat"  # same key and text: replay
    else:
        text, key = "different-text", "traffic-repeat"  # same key, other text: conflict

    try:
        resp = requests.post(f"{BASE}/v1/qr-codes", json={"text": text}, timeout=3,
                             headers={"Idempotency-Key": key} if key else {})
        seen[f"HTTP {resp.status_code}"] += 1
    except requests.RequestException as e:
        seen[f"HTTP {type(e).__name__}"] += 1
    try:
        grpc_client.create_qr(text, idempotency_key=key)
        seen["gRPC OK"] += 1
    except grpc.RpcError as e:
        seen[f"gRPC {e.code().name}"] += 1
    time.sleep(0.5)

print(f"done after {i} rounds:", dict(seen))