"""Baseline persistence outside the package directory.

Two independent guards keep one person's baselines out of a copied directory:

1. Location — baselines live under a user-level path, never inside the package,
   so copying or zipping the tool cannot carry them along.
2. Credential fingerprint — each file records a short SHA-256 prefix of the
   token that produced it. A different credential invalidates the file even if
   it somehow travels. The token value itself is never stored.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

ENV_HOME = "TAPD_CAPABILITY_HOME"

FINGERPRINT_LENGTH = 12


def fingerprint(token: str) -> str:
    """Stable, non-reversible credential identity. Never store the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def home() -> Path:
    """User-level storage root, overridable for tests and for relocation."""
    override = os.environ.get(ENV_HOME, "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "tapd-capability"
    return Path.home() / ".tapd-capability"


def baselines_dir() -> Path:
    return home() / "baselines"


def path_for(workspace_id: str, namespace: str = "") -> Path:
    """Where one workspace's baseline lives.

    ``namespace`` is empty for the single-tenant (stdio) shape, which keeps the
    historical layout byte-for-byte. A multi-tenant host passes the caller's
    credential fingerprint: without it two people who both work on the same
    workspace share one file, and each confirmation overwrites the other's —
    the fingerprint check would refuse to *read* the foreign file, so the damage
    is availability rather than disclosure, but it is damage all the same.
    """
    if namespace:
        return baselines_dir() / namespace / f"{workspace_id}.json"
    return baselines_dir() / f"{workspace_id}.json"


@dataclass(frozen=True)
class LoadResult:
    """Why a baseline is or is not usable — never a silent empty."""

    status: str  # ok | missing | foreign_credential | unreadable
    baseline: dict[str, Any] | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "ok"


def save(
    workspace_id: str,
    context: Mapping[str, Any],
    token: str,
    *,
    confirmed_at: str,
    namespace: str = "",
) -> Path:
    target = path_for(workspace_id, namespace)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "kind": "TapdBaseline",
        "workspace_id": str(workspace_id),
        "credential_fingerprint": fingerprint(token),
        "confirmed_at": confirmed_at,
        "context": dict(context),
    }
    target.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def load(workspace_id: str, token: str, namespace: str = "") -> LoadResult:
    target = path_for(workspace_id, namespace)
    if not target.exists():
        return LoadResult("missing", detail="这个项目还没建过基线")
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - never echo file contents
        return LoadResult("unreadable", detail=f"基线文件读不了：{type(exc).__name__}")
    if record.get("credential_fingerprint") != fingerprint(token):
        return LoadResult(
            "foreign_credential",
            detail="这份基线是用另一个令牌建的，不能用——请重新建立",
        )
    return LoadResult("ok", baseline=record)


def save_semantic(
    workspace_id: str,
    predicate: str,
    mapping: Mapping[str, Any],
    token: str,
    *,
    namespace: str = "",
) -> LoadResult:
    """Atomically add one confirmed semantic to an existing usable baseline.

    This is a package-local write only.  Missing, unreadable, or foreign-token
    baselines remain untouched and are returned to the caller as fail-closed
    states.
    """
    loaded = load(workspace_id, token, namespace)
    if not loaded.usable:
        return loaded
    record = dict(loaded.baseline or {})
    context = dict(record.get("context") or {})
    semantics = dict(context.get("semantics") or {})
    semantics[str(predicate)] = dict(mapping)
    context["version"] = "1.2"
    context["semantics"] = semantics
    record["context"] = context

    target = path_for(workspace_id, namespace)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return LoadResult("ok", baseline=record)


def forget(workspace_id: str, namespace: str = "") -> bool:
    target = path_for(workspace_id, namespace)
    if target.exists():
        target.unlink()
        return True
    return False


def known_workspaces(namespace: str = "") -> list[str]:
    directory = baselines_dir() / namespace if namespace else baselines_dir()
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))
