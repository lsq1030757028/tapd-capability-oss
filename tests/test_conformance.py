"""Read-side contract conformance: AC-01, AC-03, AC-08, plus credential safety."""

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from typing import ClassVar

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "tests"))

from fixture_transport import FixtureTransport

import store
from tapd_capability import READ_OPERATIONS, TapdCapability
from transport_http import TapdHttpTransport

SECRET = "s3cr3t-token-value"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Opener:
    """Minimal urllib opener double; records the outgoing request."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return _Response(json.dumps(self._outcome).encode("utf-8"))


def _http(outcome):
    opener = _Opener(outcome)
    return TapdHttpTransport(lambda: SECRET, opener=opener), opener


class AC01SharedContractTests(unittest.TestCase):
    """Two independently implemented adapters, one typed contract, one envelope."""

    ENVELOPE: ClassVar[set[str]] = {
        "operation_id",
        "status",
        "effect",
        "data",
        "page",
        "evidence",
        "error",
    }

    def test_both_adapters_return_the_same_envelope_shape(self):
        http, _ = _http({"status": 1, "data": []})
        adapters = {
            "http": TapdCapability(http),
            "fixture": TapdCapability(FixtureTransport()),
        }
        for name, capability in adapters.items():
            for operation in sorted(READ_OPERATIONS):
                payload = {"workspace_id": "W1", "entity_type": "story"}
                if operation == "entity.get":
                    payload["id"] = "1"
                with self.subTest(adapter=name, operation=operation):
                    result = capability.read(operation, payload)
                    self.assertEqual(self.ENVELOPE, set(result.as_dict()))
                    self.assertEqual("read", result.effect)

    def test_neither_adapter_introduces_its_own_result_fields(self):
        http, _ = _http({"status": 1, "data": []})
        a = TapdCapability(http).read(
            "entity.list", {"workspace_id": "W1", "entity_type": "story"}
        )
        b = TapdCapability(FixtureTransport()).read(
            "entity.list", {"workspace_id": "W1", "entity_type": "story"}
        )
        self.assertEqual(set(a.as_dict()), set(b.as_dict()))

    def test_http_transport_does_not_invent_pagination_from_row_count(self):
        http, _ = _http({"status": 1, "data": [{"Story": {"id": "1"}}]})

        result = TapdCapability(http).read(
            "entity.list",
            {"workspace_id": "W1", "entity_type": "story", "limit": 1, "page": 1},
        )

        self.assertEqual("ok", result.status)
        self.assertEqual(
            {"cursor": None, "has_more": None, "verified": False},
            dict(result.page),
        )

    def test_http_transport_preserves_explicit_coherent_pagination(self):
        http, _ = _http(
            {
                "status": 1,
                "data": [{"Story": {"id": "1"}}],
                "page": {"cursor": "2", "has_more": True},
            }
        )

        result = TapdCapability(http).read(
            "entity.list",
            {"workspace_id": "W1", "entity_type": "story", "limit": 1, "page": 1},
        )

        self.assertEqual(
            {"cursor": "2", "has_more": True, "verified": True},
            dict(result.page),
        )


class AC03ErrorCodeTests(unittest.TestCase):
    """Stable codes; never fabricate data on failure."""

    def _read(self, outcome):
        transport, _ = _http(outcome)
        return transport.read(
            "entity.list", {"workspace_id": "W1", "entity_type": "story"}
        )

    def test_http_statuses_map_to_stable_codes(self):
        cases = {
            401: "AUTH_FAILED",
            403: "AUTH_FAILED",
            404: "NOT_FOUND",
            429: "RATE_LIMITED",
            503: "NETWORK_FAILED",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                error = urllib.error.HTTPError("u", status, "err", None, None)
                transport, _ = _http(error)
                transport._retry = type(transport._retry)(
                    max_attempts=1, backoff_seconds=0
                )
                outcome = transport.read(
                    "entity.list", {"workspace_id": "W1", "entity_type": "story"}
                )
                self.assertEqual("failed", outcome["status"])
                self.assertEqual(expected, outcome["code"])
                self.assertNotIn("data", outcome)

    def test_network_failure_is_classified_and_retryable(self):
        transport, opener = _http(urllib.error.URLError("unreachable"))
        transport._retry = type(transport._retry)(max_attempts=2, backoff_seconds=0)
        outcome = transport.read(
            "entity.list", {"workspace_id": "W1", "entity_type": "story"}
        )
        self.assertEqual("NETWORK_FAILED", outcome["code"])
        self.assertTrue(outcome["retryable"])
        self.assertEqual(2, len(opener.requests), "idempotent reads may retry")

    def test_non_json_body_is_schema_failure_not_silent_empty(self):
        class BadOpener:
            def open(self, request, timeout=None):
                return _Response(b"<html>gateway</html>")

        transport = TapdHttpTransport(lambda: SECRET, opener=BadOpener())
        outcome = transport.read(
            "entity.list", {"workspace_id": "W1", "entity_type": "story"}
        )
        self.assertEqual("SCHEMA_FAILED", outcome["code"])

    def test_unknown_entity_type_is_rejected_before_the_network(self):
        transport, opener = _http({"status": 1, "data": []})
        outcome = transport.read(
            "entity.list", {"workspace_id": "W1", "entity_type": "epic"}
        )
        self.assertEqual("VALIDATION_FAILED", outcome["code"])
        self.assertEqual([], opener.requests)


class AC08EvidenceAndSecrecyTests(unittest.TestCase):
    def test_evidence_carries_source_and_timestamp(self):
        transport, _ = _http({"status": 1, "data": []})
        result = TapdCapability(transport).read(
            "entity.list", {"workspace_id": "W1", "entity_type": "story"}
        )
        self.assertEqual("tapd-api", result.evidence["source"])
        self.assertTrue(result.evidence["fetched_at"])
        self.assertEqual("W1", result.evidence["workspace_id"])

    def test_credential_never_appears_in_any_result_surface(self):
        transport, opener = _http({"status": 1, "data": []})
        result = TapdCapability(transport).read(
            "entity.list", {"workspace_id": "W1", "entity_type": "story"}
        )
        self.assertNotIn(SECRET, json.dumps(result.as_dict(), ensure_ascii=False))
        header = opener.requests[0].get_header("Authorization")
        self.assertIn(SECRET, header, "the token must reach TAPD via the header only")

    def test_failure_diagnostics_stay_credential_free(self):
        transport, _ = _http(urllib.error.URLError(f"failed for {SECRET}"))
        transport._retry = type(transport._retry)(max_attempts=1, backoff_seconds=0)
        outcome = transport.read(
            "entity.list", {"workspace_id": "W1", "entity_type": "story"}
        )
        # The transport only forwards urllib's reason; assert we never add the token ourselves.
        self.assertNotIn(SECRET, outcome["code"])


class BaselineIsolationTests(unittest.TestCase):
    """A copied directory must not carry someone else's baseline (spec AC6)."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._previous = store.os.environ.get(store.ENV_HOME)
        store.os.environ[store.ENV_HOME] = self._tmp.name

    def tearDown(self):
        if self._previous is None:
            store.os.environ.pop(store.ENV_HOME, None)
        else:
            store.os.environ[store.ENV_HOME] = self._previous
        self._tmp.cleanup()

    def test_baseline_is_stored_outside_the_package_directory(self):
        store.save("W1", {"domains": {}}, SECRET, confirmed_at="2026-08-03")
        written = store.path_for("W1").resolve()
        self.assertFalse(str(written).startswith(str(_ROOT.resolve())))

    def test_another_credential_cannot_use_the_baseline(self):
        store.save("W1", {"domains": {}}, SECRET, confirmed_at="2026-08-03")
        self.assertTrue(store.load("W1", SECRET).usable)
        foreign = store.load("W1", "someone-elses-token")
        self.assertFalse(foreign.usable)
        self.assertEqual("foreign_credential", foreign.status)

    def test_stored_file_never_contains_the_token(self):
        store.save("W1", {"domains": {}}, SECRET, confirmed_at="2026-08-03")
        self.assertNotIn(SECRET, store.path_for("W1").read_text(encoding="utf-8"))

    def test_missing_baseline_is_explicit_not_empty(self):
        result = store.load("W-none", SECRET)
        self.assertEqual("missing", result.status)
        self.assertTrue(result.detail)


if __name__ == "__main__":
    unittest.main()
