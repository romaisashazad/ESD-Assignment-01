"""gRPC tests (in-process server). Run commands are in the README."""
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", ".."))   # services/  -> common
sys.path.insert(0, os.path.join(HERE, ".."))         # grpc-service/
os.environ["QR_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import grpc  # noqa: E402
import pytest  # noqa: E402

import qr_pb2  # noqa: E402
import qr_pb2_grpc  # noqa: E402
import server as grpc_server  # noqa: E402
from common import qrcore  # noqa: E402


@pytest.fixture(scope="module")
def stub():
    srv, port = grpc_server.build_server(0)
    srv.start()
    channel = grpc.insecure_channel(f"localhost:{port}")
    yield qr_pb2_grpc.QRServiceStub(channel)
    channel.close()
    srv.stop(0)


def test_success_returns_record_and_same_store_as_http(stub):
    r = stub.CreateQrCode(qr_pb2.CreateQrCodeRequest(text="https://example.com"), timeout=3)
    assert r.kind == "url" and r.image_url == f"/v1/qr-codes/{r.id}" and not r.replayed
    # Same source of truth: the HTTP service reads this record through the same qrcore module.
    assert qrcore.get_qr(r.id).png.startswith(b"\x89PNG")


@pytest.mark.parametrize("text,code", [("", "invalid_text"), ("x" * 1001, "text_too_long")],ids=["empty_text", "too_long"])
def test_invalid_text_is_invalid_argument(stub, text, code):
    with pytest.raises(grpc.RpcError) as e:
        stub.CreateQrCode(qr_pb2.CreateQrCodeRequest(text=text), timeout=3)
    assert e.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert e.value.details().startswith(code + ":")


def test_idempotency_replay_and_conflict(stub):
    req = qr_pb2.CreateQrCodeRequest(text="hello", idempotency_key="g-key")
    a, b = stub.CreateQrCode(req, timeout=3), stub.CreateQrCode(req, timeout=3)
    assert a.id == b.id and not a.replayed and b.replayed
    with pytest.raises(grpc.RpcError) as e:
        stub.CreateQrCode(qr_pb2.CreateQrCodeRequest(text="other", idempotency_key="g-key"), timeout=3)
    assert e.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def test_request_id_round_trips_in_metadata(stub):
    _, call = stub.CreateQrCode.with_call(qr_pb2.CreateQrCodeRequest(text="rid"),
                                         metadata=(("x-request-id", "trace-me-1"),), timeout=3)
    assert dict(call.initial_metadata())["x-request-id"] == "trace-me-1"


def test_request_id_generated_when_missing(stub):
    _, call = stub.CreateQrCode.with_call(qr_pb2.CreateQrCodeRequest(text="rid2"), timeout=3)
    assert len(dict(call.initial_metadata())["x-request-id"]) == 36