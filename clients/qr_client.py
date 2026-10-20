"""Python caller for POST /v1/qr-codes with a timeout, limited retry with backoff,
and a circuit breaker. Rate limiting (429) is handled separately from failures.

Settings (all constructor arguments, defaults shown):
  timeout_s=1.0            give up on one attempt after 1 s
  max_attempts=3           1 first try + 2 retries
  backoff_base_s=0.2       wait 0.2 s, then 0.4 s (doubling), plus up to 50% random jitter
  failure_threshold=3      breaker opens after 3 failed attempts in a row
  open_seconds=10          breaker stays open 10 s, then lets one trial call through
"""
import random
import time
import uuid

import requests


class CircuitOpen(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__(f"rate limited, retry after {retry_after} s")
        self.retry_after = retry_after


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

    def __init__(self, failure_threshold=3, open_seconds=10, log=print):
        self.failure_threshold, self.open_seconds, self.log = failure_threshold, open_seconds, log
        self.state, self.failures, self.opened_at = self.CLOSED, 0, 0.0

    def before_call(self):
        if self.state == self.OPEN:
            if time.monotonic() - self.opened_at >= self.open_seconds:
                self._to(self.HALF_OPEN)  # one trial call is allowed
            else:
                raise CircuitOpen("circuit is open, failing fast without calling the server")

    def record_success(self):
        self.failures = 0
        if self.state != self.CLOSED:
            self._to(self.CLOSED)

    def record_failure(self):
        self.failures += 1
        if self.state == self.HALF_OPEN or self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic()
            self._to(self.OPEN)

    def _to(self, state):
        self.log(f"  [breaker] {self.state} -> {state} (failures={self.failures})")
        self.state = state


class QRClient:
    def __init__(self, base_url="http://localhost:5000", timeout_s=1.0, max_attempts=3,
                 backoff_base_s=0.2, failure_threshold=3, open_seconds=10, log=print):
        self.base_url, self.timeout_s, self.max_attempts = base_url, timeout_s, max_attempts
        self.backoff_base_s, self.log = backoff_base_s, log
        self.breaker = CircuitBreaker(failure_threshold, open_seconds, log)

    def create_qr(self, text, idempotency_key=None, wait_on_429=False):
        """Return the response JSON. The same Idempotency-Key is sent on every retry, so a
        retry after a timeout can never create a second QR code."""
        key = idempotency_key or str(uuid.uuid4())
        headers = {"Idempotency-Key": key, "X-Request-ID": str(uuid.uuid4())}
        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            self.breaker.before_call()
            try:
                r = requests.post(f"{self.base_url}/v1/qr-codes", json={"text": text},
                                  headers=headers, timeout=self.timeout_s)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_error = type(e).__name__
                self.breaker.record_failure()
                self.log(f"  attempt {attempt}/{self.max_attempts} failed: {last_error}")
            else:
                if r.status_code == 429:
                    # Not a server failure: do not touch the breaker, do not back off blindly.
                    retry_after = int(r.headers.get("Retry-After", "1"))
                    self.log(f"  attempt {attempt}: 429, Retry-After={retry_after}s")
                    if wait_on_429 and attempt < self.max_attempts:
                        time.sleep(retry_after)
                        continue
                    raise RateLimited(retry_after)
                if r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}"
                    self.breaker.record_failure()
                    self.log(f"  attempt {attempt}/{self.max_attempts} failed: {last_error}")
                elif r.status_code >= 400:
                    # The caller's mistake: retrying will not help.
                    raise ValueError(f"HTTP {r.status_code}: {r.json()['error']}")
                else:
                    self.breaker.record_success()
                    self.log(f"  attempt {attempt}/{self.max_attempts} ok: {r.status_code} "
                             f"id={r.json()['id']} replayed={r.headers.get('Idempotent-Replayed', 'false')}")
                    return r.json()
            if attempt < self.max_attempts:
                delay = self.backoff_base_s * (2 ** (attempt - 1))
                delay += random.uniform(0, delay * 0.5)
                self.log(f"  backing off {delay:.2f}s")
                time.sleep(delay)
        raise RuntimeError(f"gave up after {self.max_attempts} attempts: {last_error}")