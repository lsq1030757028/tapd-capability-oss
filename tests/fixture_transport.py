"""Fixture adapter: the second independently implemented transport (AC-01).

It speaks the same typed read contract as the HTTP transport but replays
programmed outcomes instead of touching the network, which lets the shared
conformance suite run offline and lets every failure form be constructed.
"""

from __future__ import annotations

from typing import Any, Mapping


class FixtureTransport:
    """Programmable stand-in honouring the transport protocol."""

    def __init__(self) -> None:
        self._schema: dict[str, Any] = {}
        self._entities: dict[str, Any] = {}
        self._workspace: dict[str, Any] = {"name": "fixture workspace"}
        self._accessible: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict]] = []

    # ------------------------------------------------------------- programming

    def set_schema(self, entity_type: str, items: Any) -> "FixtureTransport":
        """`items` is a list of raw CustomFieldConfig dicts, or a failure dict."""
        self._schema[entity_type] = items
        return self

    def set_entities(self, entity_type: str, items: Any) -> "FixtureTransport":
        self._entities[entity_type] = items
        return self

    def set_accessible(self, projects: list[dict[str, Any]]) -> "FixtureTransport":
        self._accessible = projects
        return self

    # -------------------------------------------------------------------- read

    def read(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((operation, dict(payload)))
        entity = str(payload.get("entity_type", ""))

        if operation == "workspace.list_accessible":
            return _ok_list(self._accessible)
        if operation == "workspace.get":
            return _ok_record(self._workspace)
        if operation == "schema.get":
            programmed = self._schema.get(entity, [])
            return programmed if _is_failure(programmed) else _ok_list(programmed)
        if operation == "entity.list":
            programmed = self._entities.get(entity, [])
            return programmed if _is_failure(programmed) else _ok_list(programmed)
        return _ok_list([])


def _is_failure(value: Any) -> bool:
    return isinstance(value, dict) and value.get("status") == "failed"


def failure(code: str, message: str = "fixture failure") -> dict[str, Any]:
    return {"status": "failed", "code": code, "message": message}


def _ok_list(items: list[Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "data": {"items": list(items), "count": len(items)},
        "page": {"cursor": None, "has_more": False},
        "evidence": {"fetched_at": "2026-08-03T00:00:00Z", "source": "fixture"},
    }


def _ok_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "data": {"record": dict(record)},
        "page": {"cursor": None, "has_more": False},
        "evidence": {"fetched_at": "2026-08-03T00:00:00Z", "source": "fixture"},
    }


def field(key: str, name: str, ftype: str = "user_chooser", modified: str = "2026-01-01 00:00:00") -> dict:
    """Shape one raw TAPD custom-field config as the API returns it."""
    return {
        "custom_field": key,
        "name": name,
        "type": ftype,
        "enabled": "1",
        "modified": modified,
    }
