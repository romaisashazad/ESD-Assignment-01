"""Business rules and storage for QR codes, shared by the HTTP and gRPC services.

Both services import this module, so validation, ID creation, idempotency and
the stored data (SQLite file on a shared volume) are the same in both. This module
has no web or metrics code on purpose.
"""
import hashlib
import io
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass

import qrcode

MAX_TEXT_LENGTH = 1000
MAX_KEY_LENGTH = 64
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
DB_PATH = os.environ.get("QR_DB_PATH", "/data/qr.db")

# Every error code the core can raise. Fixed set, so it is safe as a metric label.
ERROR_CODES = ("invalid_json", "missing_text", "invalid_text", "text_too_long",
               "invalid_idempotency_key", "idempotency_key_reused", "not_found")


class ServiceError(Exception):
    """category is one of: invalid, conflict, not_found. Each edge maps it to its own status."""

    def __init__(self, code, message, category):
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category


@dataclass
class QRRecord:
    id: str
    kind: str
    text_length: int
    created_at: str
    png: bytes


def kind_of(text):
    return "url" if text.lower().startswith(("http://", "https://")) else "text"


def validate_text(text):
    if text is None:
        raise ServiceError("missing_text", "The 'text' field is required.", "invalid")
    if not isinstance(text, str) or not text.strip():
        raise ServiceError("invalid_text", "'text' must be a non-empty string.", "invalid")
    if len(text) > MAX_TEXT_LENGTH:
        raise ServiceError("text_too_long",
                           f"'text' must be at most {MAX_TEXT_LENGTH} characters.", "invalid")


def validate_key(key):
    if key is None:
        return
    if len(key) > MAX_KEY_LENGTH or not KEY_PATTERN.match(key):
        raise ServiceError("invalid_idempotency_key",
                           f"Idempotency-Key must be 1-{MAX_KEY_LENGTH} characters of "
                           "letters, digits, '.', '_', ':' or '-'.", "invalid")


def render_png(text):
    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return buf.getvalue()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS qr_codes ("
                 "id TEXT PRIMARY KEY, kind TEXT, text_length INTEGER, "
                 "created_at TEXT, png BLOB)")
    conn.execute("CREATE TABLE IF NOT EXISTS idempotency ("
                 "key TEXT PRIMARY KEY, text_hash TEXT, qr_id TEXT)")
    return conn


def _row_to_record(row):
    return QRRecord(id=row[0], kind=row[1], text_length=row[2], created_at=row[3], png=row[4])


def _by_key(conn, key, text_hash):
    row = conn.execute(
        "SELECT i.text_hash, q.id, q.kind, q.text_length, q.created_at, q.png "
        "FROM idempotency i JOIN qr_codes q ON q.id = i.qr_id WHERE i.key = ?", (key,)).fetchone()
    if row is None:
        return None
    if row[0] != text_hash:
        raise ServiceError("idempotency_key_reused",
                           "This Idempotency-Key was already used with a different 'text'.",
                           "conflict")
    return _row_to_record(row[1:])


def create_qr(text, idempotency_key=None, on_render=None):
    """Validate, render and store a QR code. Returns (record, created).

    created is False when an Idempotency-Key matched an earlier request: the stored
    result is returned and nothing new is made. on_render runs just after rendering
    (used by the HTTP service for its fault-injection delay), only when a new code is made.
    """
    validate_text(text)
    validate_key(idempotency_key)
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    conn = _connect()
    try:
        if idempotency_key:
            existing = _by_key(conn, idempotency_key, text_hash)
            if existing:
                return existing, False
        png = render_png(text)
        if on_render:
            on_render()
        record = QRRecord(id=str(uuid.uuid4()), kind=kind_of(text), text_length=len(text),
                          created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), png=png)
        # BEGIN IMMEDIATE takes the write lock, so two requests with the same key
        # cannot both insert: the second one re-checks the key after the first commits.
        conn.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key:
                existing = _by_key(conn, idempotency_key, text_hash)
                if existing:
                    conn.execute("COMMIT")
                    return existing, False
            conn.execute("INSERT INTO qr_codes VALUES (?,?,?,?,?)",
                         (record.id, record.kind, record.text_length, record.created_at, record.png))
            if idempotency_key:
                conn.execute("INSERT INTO idempotency VALUES (?,?,?)",
                             (idempotency_key, text_hash, record.id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return record, True
    finally:
        conn.close()


def get_qr(qr_id):
    try:
        uuid.UUID(qr_id)
    except (ValueError, AttributeError, TypeError):
        raise ServiceError("not_found", "QR code not found.", "not_found")
    conn = _connect()
    try:
        row = conn.execute("SELECT id, kind, text_length, created_at, png FROM qr_codes "
                           "WHERE id = ?", (qr_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ServiceError("not_found", "QR code not found.", "not_found")
    return _row_to_record(row)