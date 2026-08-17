import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tapd_capability import READ_OPERATIONS, WRITE_OPERATIONS, TapdCapability


class FakeTransport:
    def __init__(self):
        self.calls = []

    def read(self, operation, payload):
        self.calls.append((operation, payload))
        return {
            "status": "ok",
            "data": {"entity": operation},
            "page": {"cursor": None, "has_more": False},
            "evidence": {"fetched_at": "2026-07-23T00:00:00Z"},
        }


class FailingTransport:
    def read(self, operation, payload):
        raise RuntimeError("network unavailable")


class TapdCapabilityTests(unittest.TestCase):
    def test_platform_neutral_read_envelope(self):
        transport = FakeTransport()
        payload = {"workspace_id": "fixture-workspace-contract", "operation_id": "op-1"}
        result = TapdCapability(transport).read("entity.get", payload)
        self.assertEqual(
            {"operation_id", "status", "effect", "data", "page", "evidence", "error"},
            set(result.as_dict()),
        )
        self.assertEqual("ok", result.status)
        self.assertEqual("read", result.effect)
        self.assertEqual("op-1", result.operation_id)
        self.assertEqual("fixture-workspace-contract", result.evidence["workspace_id"])
        self.assertEqual("entity.get", transport.calls[0][0])
        self.assertIsNot(payload, transport.calls[0][1])

    def test_every_declared_read_operation_uses_the_same_envelope(self):
        transport = FakeTransport()
        capability = TapdCapability(transport)
        for operation in READ_OPERATIONS:
            with self.subTest(operation=operation):
                result = capability.read(operation, {"workspace_id": "1"})
                self.assertEqual("ok", result.status)
                self.assertEqual("read", result.effect)
                self.assertIsNone(result.error)
                self.assertEqual(
                    {"operation_id", "status", "effect", "data", "page", "evidence", "error"},
                    set(result.as_dict()),
                )

    def test_shared_authority_keeps_transport_injected(self):
        transport = FakeTransport()
        TapdCapability(transport).read("workspace.get", {"workspace_id": "1"})
        self.assertEqual(["workspace.get"], [call[0] for call in transport.calls])

    def test_write_operations_fail_closed_without_transport_call(self):
        for operation in (*WRITE_OPERATIONS, "write.extension"):
            with self.subTest(operation=operation):
                transport = FakeTransport()
                result = TapdCapability(transport).read(operation, {"workspace_id": "1", "operation_id": "op-write"})
                self.assertEqual("failed", result.status)
                self.assertEqual("WRITE_NOT_IMPLEMENTED", result.error["code"])
                self.assertEqual([], transport.calls)

    def test_missing_workspace_is_rejected(self):
        result = TapdCapability(FakeTransport()).read("entity.list", {})
        self.assertEqual("VALIDATION_FAILED", result.error["code"])

    def test_transport_failure_is_normalized(self):
        result = TapdCapability(FailingTransport()).read("schema.get", {"workspace_id": "1"})
        self.assertEqual("TRANSPORT_FAILED", result.error["code"])


if __name__ == "__main__":
    unittest.main()
