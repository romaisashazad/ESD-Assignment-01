"""Shows the caller's behaviour against the running service.
Usage:  python demo_reliability.py success|retry|breaker|ratelimit
Uses /admin/fault from Assignment 1 to make the server slow."""
import os
import sys
import time

import requests

from qr_client import CircuitOpen, QRClient, RateLimited

BASE = os.environ.get("QR_BASE_URL", "http://localhost:5000")


def fault(every_n, delay_ms):
    requests.post(f"{BASE}/admin/fault", json={"every_n": every_n, "delay_ms": delay_ms})


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}")


def main(mode):
    c = QRClient(BASE, log=lambda m: say(m))
    if mode == "success":
        say("call 1 (healthy server)")
        c.create_qr("https://example.com")
    elif mode == "retry":
        # Every 2nd request is delayed 2 s (longer than the 1 s timeout). The count restarts
        # when the fault is set, so the warm-up is request 1 and the real call starts on request 2.
        fault(2, 2000)
        c.create_qr("warm-up")
        say("call 2: first attempt hits the slow request, the retry is fast")
        c.create_qr("retry demo")
        fault(0, 0)
    elif mode == "breaker":
        fault(1, 5000)  # every request is much slower than the 1 s timeout
        say("call 1: server too slow, all attempts time out")
        try:
            c.create_qr("breaker demo 1")
        except Exception as e:
            say(f"  call 1 failed: {e}")
        say("call 2: breaker is open, fails fast")
        t = time.time()
        try:
            c.create_qr("breaker demo 2")
        except CircuitOpen as e:
            say(f"  call 2 failed in {time.time() - t:.3f}s: {e}")
        fault(0, 0)
        say("fault switched off; waiting for the 10 s open period")
        time.sleep(c.breaker.open_seconds + 0.5)
        say("call 3: trial call (half-open)")
        c.create_qr("breaker demo 3")
    elif mode == "ratelimit":
        say("waiting 11 s so earlier requests leave the 10 s rate-limit window")
        time.sleep(11)
        ok = limited = 0
        last = 0
        for i in range(35):
            try:
                c.create_qr(f"burst {i}")
                ok += 1
            except RateLimited as e:
                limited += 1
                last = e.retry_after
        say(f"burst of 35: {ok} accepted, {limited} rejected with 429 (Retry-After {last}s)")
        say("waiting for Retry-After, then one call with wait_on_429")
        time.sleep(last)
        c.create_qr("after wait", wait_on_429=True)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")