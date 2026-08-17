"""Host-neutral TAPD P0 read-operation core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import uuid4


READ_OPERATIONS = frozenset(
    {
        "workspace.get",
        "workspace.list_accessible",
        "workspace.resolve",
        "schema.get",
        "workflow.get",
        "entity.get",
        "entity.list",
        "attachment.list",
        "relation.list",
    }
)

#: Discovery entry points: the caller cannot know a workspace_id before listing
#: what the credential can reach, so these two are exempt from the workspace
#: precondition. Every other read still requires it (contract C2).
WORKSPACE_OPTIONAL_OPERATIONS = frozenset({"workspace.list_accessible", "workspace.resolve"})
WRITE_OPERATIONS = frozenset(
    {
        "write.prepare",
        "write.commit",
        "attachment.upload",
        "entity.create",
        "entity.update",
        "entity.delete",
        "entity.transition",
        "relation.create",
        "relation.delete",
    }
)


class TapdTransport(Protocol):
    def read(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Execute one typed TAPD read without exposing credentials to the core."""


@dataclass(frozen=True)
class TapdResult:
    operation_id: str
    status: str
    effect: str
    data: Mapping[str, Any]
    page: Mapping[str, Any]
    evidence: Mapping[str, Any]
    error: Mapping[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "effect": self.effect,
            "data": dict(self.data),
            "page": dict(self.page),
            "evidence": dict(self.evidence),
            "error": dict(self.error) if self.error else None,
        }


class TapdCapability:
    """Normalize typed read operations and block all mutation effects in P0."""

    def __init__(self, transport: TapdTransport) -> None:
        self._transport = transport

    def read(self, operation: str, payload: Mapping[str, Any]) -> TapdResult:
        operation_id = _operation_id(payload)
        if operation in WRITE_OPERATIONS or operation.startswith("write."):
            return _failure(operation_id, "WRITE_NOT_IMPLEMENTED", "P0 permits read operations only")
        if operation not in READ_OPERATIONS:
            return _failure(operation_id, "UNSUPPORTED_OPERATION", f"Unsupported TAPD operation: {operation}")
        workspace_id = payload.get("workspace_id")
        has_workspace = workspace_id is not None and str(workspace_id).strip()
        if not has_workspace and operation not in WORKSPACE_OPTIONAL_OPERATIONS:
            return _failure(operation_id, "VALIDATION_FAILED", "workspace_id is required")

        request = dict(payload)
        try:
            response = self._transport.read(operation, request)
        except Exception as exc:  # Transport implementations own detailed diagnostics.
            return _failure(operation_id, "TRANSPORT_FAILED", str(exc))
        if not isinstance(response, Mapping):
            return _failure(operation_id, "TRANSPORT_FAILED", "Transport returned a non-object response")

        status = str(response.get("status", "ok"))
        if status != "ok":
            code = str(response.get("code", "TRANSPORT_FAILED"))
            detail = str(response.get("message", "TAPD read failed"))
            return _failure(operation_id, code, detail)
        data = response.get("data", {})
        if not isinstance(data, Mapping):
            return _failure(operation_id, "TRANSPORT_FAILED", "Transport data must be an object")
        page = response.get("page", {"cursor": None, "has_more": False})
        if not isinstance(page, Mapping):
            return _failure(operation_id, "TRANSPORT_FAILED", "Transport page must be an object")
        evidence = response.get("evidence", {})
        if not isinstance(evidence, Mapping):
            return _failure(operation_id, "TRANSPORT_FAILED", "Transport evidence must be an object")
        normalized_evidence = {
            "workspace_id": str(workspace_id) if has_workspace else "",
            "source": "tapd-transport",
            **dict(evidence),
        }
        return TapdResult(operation_id, "ok", "read", data, page, normalized_evidence, None)


def _operation_id(payload: Mapping[str, Any]) -> str:
    candidate = payload.get("operation_id")
    return str(candidate) if candidate else str(uuid4())


def _failure(operation_id: str, code: str, message: str) -> TapdResult:
    return TapdResult(
        operation_id=operation_id,
        status="failed",
        effect="read",
        data={},
        page={"cursor": None, "has_more": False},
        evidence={},
        error={"code": code, "message": message},
    )

