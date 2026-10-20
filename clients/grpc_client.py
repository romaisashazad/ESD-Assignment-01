"""Python gRPC client for QRService.CreateQrCode, with a deadline.

The generated code (qr_pb2, qr_pb2_grpc) lives in services/grpc-service; put that folder on PYTHONPATH.
"""
import os
import uuid

import grpc

import qr_pb2
import qr_pb2_grpc

TARGET = os.environ.get("GRPC_TARGET", "localhost:50051")
DEADLINE_S = 2.0  # the call fails with DEADLINE_EXCEEDED if it takes longer than this


def create_qr(text, idempotency_key="", request_id=None, target=TARGET, deadline_s=DEADLINE_S):
    """Returns (response, request_id_echoed). Raises grpc.RpcError on failure."""
    request_id = request_id or str(uuid.uuid4())
    with grpc.insecure_channel(target) as channel:
        stub = qr_pb2_grpc.QRServiceStub(channel)
        response, call = stub.CreateQrCode.with_call(
            qr_pb2.CreateQrCodeRequest(text=text, idempotency_key=idempotency_key),
            metadata=(("x-request-id", request_id),),   # same job as the X-Request-ID header
            timeout=deadline_s)
        return response, dict(call.initial_metadata()).get("x-request-id")


if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    try:
        resp, rid = create_qr(text)
        print("OK", resp.id, "kind=" + resp.kind, "replayed=" + str(resp.replayed), "request_id=" + rid)
    except grpc.RpcError as e:
        print("FAILED", e.code().name, "-", e.details())