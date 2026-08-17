"""Official TAPD HTTP transport for the P0 read core (contract C1).

Owns base URL selection, a pinned client identity, connect/request timeouts,
retry classification for idempotent reads, rate-limit handling, and response
normalization. Credential values never enter results, evidence, or payloads.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

#: Pinned client identity (contract C1: pinned client implementation/version).
CLIENT_ID = "tapd-capability-transport/1.0 (+stdlib-urllib)"

DEFAULT_BASE_URL = "https://api.tapd.cn"

#: Documented entity enumeration (contract C3). Callers use the singular key.
ENTITY_PATHS = {
    "story": "stories",
    "bug": "bugs",
    "task": "tasks",
    "iteration": "iterations",
}

#: Which TAPD system name a workflow query belongs to.
WORKFLOW_SYSTEMS = {"story": "story", "bug": "bug"}

RETRYABLE_CODES = frozenset({"NETWORK_FAILED", "RATE_LIMITED"})


@dataclass(frozen=True)
class Timeouts:
    """Connect/request/total budget in seconds."""

    request: float = 20.0
    total: float = 60.0


@dataclass(frozen=True)
class RetryPolicy:
    """Retry is permitted for idempotent reads only."""

    max_attempts: int = 3
    backoff_seconds: float = 0.75


@dataclass
class _Attempt:
    code: str
    message: str
    status: int | None = None


def _now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


class TapdHttpTransport:
    """Translate typed read operations into official TAPD API calls."""

    def __init__(
        self,
        credential: Callable[[], str],
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeouts: Timeouts | None = None,
        retry: RetryPolicy | None = None,
        opener: Any | None = None,
    ) -> None:
        self._credential = credential
        self._base_url = base_url.rstrip("/")
        self._timeouts = timeouts or Timeouts()
        self._retry = retry or RetryPolicy()
        self._opener = opener or urllib.request.build_opener()

    # ---------------------------------------------------------------- probe

    def probe(self) -> Mapping[str, Any]:
        """Read-only startup probe (contract C1). Never synthesizes data."""
        return self.read(
            "workspace.list_accessible", {"workspace_id": "probe", "user": ""}
        )

    # ----------------------------------------------------------------- read

    def read(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            path, params, unwrap = self._route(operation, payload)
        except _RouteError as exc:
            return _fail(exc.code, str(exc))

        deadline = time.monotonic() + self._timeouts.total
        last: _Attempt | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            if time.monotonic() >= deadline:
                return _fail(
                    "NETWORK_FAILED", "total timeout budget exhausted", retryable=True
                )
            outcome = self._request(path, params)
            if isinstance(outcome, _Attempt):
                last = outcome
                if (
                    outcome.code in RETRYABLE_CODES
                    and attempt < self._retry.max_attempts
                ):
                    time.sleep(self._retry.backoff_seconds * attempt)
                    continue
                return _fail(
                    outcome.code,
                    outcome.message,
                    retryable=outcome.code in RETRYABLE_CODES,
                )
            return self._normalize(outcome, unwrap, params)
        return _fail(
            last.code if last else "TRANSPORT_FAILED",
            last.message if last else "read failed",
        )

    # -------------------------------------------------------------- routing

    def _route(self, operation: str, payload: Mapping[str, Any]):
        ws = str(payload.get("workspace_id", "")).strip()
        entity = str(payload.get("entity_type", "")).strip()
        extra = {
            k: v
            for k, v in payload.items()
            if k not in {"workspace_id", "entity_type", "operation_id"}
            and v not in (None, "")
        }

        if operation == "workspace.list_accessible":
            user = str(payload.get("user", "")).strip()
            params = {"user": user} if user else {}
            return "/workspaces/user_participant_projects", params, "list"

        if operation == "workspace.get":
            return "/workspaces/get_workspace_info", {"workspace_id": ws}, "dict"

        if operation == "workspace.resolve":
            # Resolution is list_accessible plus caller-side matching; the transport
            # only supplies the candidate set and never guesses the target.
            user = str(payload.get("user", "")).strip()
            return (
                "/workspaces/user_participant_projects",
                ({"user": user} if user else {}),
                "list",
            )

        if operation == "schema.get":
            seg = self._entity_segment(entity)
            return f"/{seg}/custom_fields_settings", {"workspace_id": ws}, "list"

        if operation == "workflow.get":
            system = WORKFLOW_SYSTEMS.get(entity)
            if not system:
                raise _RouteError(
                    "VALIDATION_FAILED",
                    f"workflow.get unsupported for entity_type={entity!r}",
                )
            params = {"workspace_id": ws, "system": system}
            params.update(extra)
            return "/workflows/status_map", params, "dict"

        if operation in {"entity.get", "entity.list"}:
            seg = self._entity_segment(entity)
            params = {"workspace_id": ws}
            params.update(extra)
            if operation == "entity.get" and not params.get("id"):
                raise _RouteError("VALIDATION_FAILED", "entity.get requires id")
            return f"/{seg}", params, "list"

        if operation == "attachment.list":
            params = {"workspace_id": ws}
            params.update(extra)
            return "/attachments", params, "list"

        if operation == "relation.list":
            params = {"workspace_id": ws}
            params.update(extra)
            return "/relations", params, "list"

        raise _RouteError(
            "VALIDATION_FAILED", f"transport has no route for {operation!r}"
        )

    @staticmethod
    def _entity_segment(entity: str) -> str:
        seg = ENTITY_PATHS.get(entity)
        if not seg:
            raise _RouteError(
                "VALIDATION_FAILED",
                f"entity_type must be one of {sorted(ENTITY_PATHS)}, got {entity!r}",
            )
        return seg

    # ------------------------------------------------------------- transport

    def _request(self, path: str, params: Mapping[str, Any]):
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")}
        )
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {self._credential()}")
        request.add_header("User-Agent", CLIENT_ID)
        request.add_header("Accept", "application/json")
        try:
            with self._opener.open(request, timeout=self._timeouts.request) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return _Attempt(
                _http_code(exc.code), f"TAPD returned HTTP {exc.code}", exc.code
            )
        except urllib.error.URLError as exc:
            return _Attempt("NETWORK_FAILED", f"network unreachable: {exc.reason}")
        except TimeoutError:
            return _Attempt("NETWORK_FAILED", "request timeout")
        except Exception as exc:  # noqa: BLE001 - diagnostics must stay credential-free
            return _Attempt("TRANSPORT_FAILED", f"{type(exc).__name__} during request")

        try:
            return json.loads(raw)
        except ValueError:
            return _Attempt("SCHEMA_FAILED", "TAPD response was not valid JSON")

    # ------------------------------------------------------------ normalize

    def _normalize(
        self, body: Any, unwrap: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if not isinstance(body, dict):
            return _fail("SCHEMA_FAILED", "TAPD response envelope was not an object")
        if body.get("status") != 1:
            return _fail("SCHEMA_FAILED", f"TAPD status={body.get('status')!r}")

        payload = body.get("data")
        if unwrap == "list":
            items = (
                [_flatten(item) for item in payload]
                if isinstance(payload, list)
                else []
            )
            page = _verified_page(body)
            return {
                "status": "ok",
                "data": {"items": items, "count": len(items)},
                "page": page,
                "evidence": {
                    "fetched_at": _now(),
                    "source": "tapd-api",
                    "client": CLIENT_ID,
                },
            }
        return {
            "status": "ok",
            "data": {"record": _flatten(payload) if payload is not None else {}},
            "page": {"cursor": None, "has_more": False},
            "evidence": {
                "fetched_at": _now(),
                "source": "tapd-api",
                "client": CLIENT_ID,
            },
        }


class _RouteError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _flatten(item: Any) -> Any:
    """TAPD wraps each record as {"Story": {...}}; unwrap one known level."""
    if isinstance(item, dict) and len(item) == 1:
        (inner,) = item.values()
        if isinstance(inner, dict):
            return inner
    return item


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _verified_page(body: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only explicit, coherent pagination metadata from the response.

    A full page of rows is not proof that another page exists, and a short page
    is not proof that the result is complete.  Business queries therefore need
    an explicit ``has_more`` boolean from TAPD (or a verified gateway) rather
    than a length heuristic manufactured by this adapter.
    """
    candidate = body.get("page")
    if not isinstance(candidate, Mapping):
        return {"cursor": None, "has_more": None, "verified": False}
    has_more = candidate.get("has_more")
    cursor = candidate.get("cursor")
    if not isinstance(has_more, bool):
        return {"cursor": None, "has_more": None, "verified": False}
    if has_more and (cursor is None or not str(cursor).strip()):
        return {"cursor": None, "has_more": None, "verified": False}
    if not has_more and cursor not in (None, ""):
        return {"cursor": None, "has_more": None, "verified": False}
    return {
        "cursor": str(cursor) if has_more else None,
        "has_more": has_more,
        "verified": True,
    }


def _http_code(status: int) -> str:
    if status in (401, 403):
        return "AUTH_FAILED"
    if status == 404:
        return "NOT_FOUND"
    if status == 429:
        return "RATE_LIMITED"
    if 500 <= status < 600:
        return "NETWORK_FAILED"
    return "TRANSPORT_FAILED"


def _fail(code: str, message: str, *, retryable: bool = False) -> Mapping[str, Any]:
    return {
        "status": "failed",
        "code": code,
        "message": message,
        "retryable": retryable,
    }
