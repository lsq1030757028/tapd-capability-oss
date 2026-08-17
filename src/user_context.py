"""Credential-isolated, non-sensitive TAPD user context.

Persistent profiles receive only the existing non-reversible credential
fingerprint.  Short-lived session claims receive the current credential only
for signing or verification and never store it.  Project scope always comes
from TAPD's existing ``workspace.list_accessible`` read operation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import store

PROFILE_SCHEMA = "tapd-user-profile/v1"
SESSION_CONTEXT_SCHEMA = "tapd-session-context/v1"
SESSION_CONTEXT_PREFIX = "tapdsc1"
SESSION_CONTEXT_TTL_SECONDS = 15 * 60
SESSION_CONTEXT_CLOCK_SKEW_SECONDS = 30
MAX_SESSION_CONTEXT_LENGTH = 4096
MAX_BUSINESS_TEXT = 120
_FINGERPRINT = re.compile(r"^[0-9a-f]{12}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


class ContextValidationError(ValueError):
    """Invalid non-secret input.  Messages never contain credentials or paths."""


class SessionContextError(ValueError):
    """Stable fail-closed session-claim failure without secret diagnostics."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProfileLoad:
    status: str  # ok | missing | unreadable | foreign_credential
    profile: dict[str, Any] | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "ok"


def fetch_accessible_projects(capability, user: str = ""):
    """Reuse the existing TAPD participant-project discovery operation."""
    result = capability.read("workspace.list_accessible", {"user": str(user).strip()})
    if result.status != "ok":
        return result, []
    data = result.data
    if not isinstance(data, Mapping):
        raise ContextValidationError("项目范围返回格式不正确")
    return result, normalize_projects(data.get("items", []))


def normalize_projects(items: Any) -> list[dict[str, str]]:
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise ContextValidationError("项目范围必须是列表")
    projects: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise ContextValidationError("项目范围里存在无效条目")
        category = str(item.get("category") or "").strip()
        if category.casefold() == "organization":
            continue
        workspace_id = _business_text(item.get("id"), "项目标识")
        name = _business_text(item.get("name"), "项目名称")
        if workspace_id in seen_ids:
            raise ContextValidationError("项目范围里存在重复项目标识")
        seen_ids.add(workspace_id)
        projects.append({"id": workspace_id, "name": name, "category": category})
    return projects


def profiles_dir() -> Path:
    return store.home() / "profiles"


def profile_path(credential_fingerprint: str) -> Path:
    fingerprint = _validated_fingerprint(credential_fingerprint)
    return profiles_dir() / fingerprint / "profile.json"


def load_profile(credential_fingerprint: str) -> ProfileLoad:
    try:
        fingerprint = _validated_fingerprint(credential_fingerprint)
        target = profile_path(fingerprint)
    except ContextValidationError as exc:
        return ProfileLoad("unreadable", detail=str(exc))
    if not target.exists():
        return ProfileLoad("missing", detail="还没有保存 TAPD 用户上下文")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        profile = _validated_record(raw, fingerprint)
    except Exception as exc:  # noqa: BLE001 - never echo file contents or paths
        return ProfileLoad(
            "unreadable", detail=f"用户上下文读不了：{type(exc).__name__}"
        )
    if profile.get("credential_fingerprint") != fingerprint:
        return ProfileLoad(
            "foreign_credential",
            detail="这份用户上下文属于另一份凭据，不能使用",
        )
    return ProfileLoad("ok", profile=profile)


def save_context(
    credential_fingerprint: str,
    projects: Sequence[Mapping[str, Any]],
    *,
    default_project: str,
    tapd_identity: str,
    business_role: str,
    projects_fetched_at: str = "",
) -> dict[str, Any]:
    """Validate current project scope, then perform one local workspace write."""
    try:
        fingerprint = _validated_fingerprint(credential_fingerprint)
        normalized = normalize_projects(projects)
        chosen = _choose_project(normalized, default_project)
        identity = _business_text(tapd_identity, "TAPD 身份")
        role = _business_text(business_role, "业务角色")
        fetched_at = _metadata_text(projects_fetched_at, "项目范围验证时间")
    except ContextValidationError as exc:
        code = (
            "PROJECT_NOT_ACCESSIBLE"
            if "当前可访问项目" in str(exc)
            else "VALIDATION_FAILED"
        )
        if "多个同名项目" in str(exc):
            code = "PROJECT_AMBIGUOUS"
        return _error("failed", code, str(exc), effect="read")

    verified_at = _utcnow()
    record = {
        "schema_version": PROFILE_SCHEMA,
        "credential_fingerprint": fingerprint,
        "default_project": {"id": chosen["id"], "name": chosen["name"]},
        "tapd_identity": identity,
        "business_role": role,
        "verification": {
            "verified_at": verified_at,
            "projects_fetched_at": fetched_at,
            "accessible_project_count": len(normalized),
            "project_scope_source": "workspace.list_accessible",
            "identity_basis": "user_confirmed",
        },
    }
    try:
        _atomic_save(fingerprint, record)
    except OSError:
        # OS exceptions commonly carry the full data directory and temporary
        # filename.  Neither may cross the public MCP boundary.
        return _error(
            "failed",
            "PROFILE_WRITE_FAILED",
            "用户上下文暂时无法保存，请稍后重试",
            effect="workspace-write",
        )
    return {
        "status": "ok",
        "effect": "workspace-write",
        "tapd_write": False,
        "saved": _public_profile(record),
        "verification": dict(record["verification"]),
        "error": None,
    }


def context_status(
    credential_fingerprint: str,
    projects: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    try:
        normalized = normalize_projects(projects)
    except ContextValidationError as exc:
        return _error("blocked", "PROJECT_SCOPE_INVALID", str(exc))
    names = [p["name"] for p in normalized]
    loaded = load_profile(credential_fingerprint)
    if loaded.status == "missing":
        if not normalized:
            result = _error(
                "blocked",
                "NO_ACCESSIBLE_PROJECTS",
                "当前凭据没有可访问的 TAPD 项目",
            )
            result["profile_state"] = "missing"
            return result
        return {
            "status": "needs_input",
            "effect": "read",
            "profile_state": "missing",
            "profile": None,
            "accessible_project_names": names,
            "next_action": "补全默认项目、TAPD 身份和业务角色",
            "error": None,
        }
    if not loaded.usable:
        return _profile_load_error(loaded)
    profile = loaded.profile or {}
    current = _find_by_id(normalized, profile["default_project"]["id"])
    if current is None:
        return {
            "status": "blocked",
            "effect": "read",
            "profile_state": "stale",
            "profile": _public_profile(profile),
            "accessible_project_names": names,
            "next_action": "请用项目业务名称重新确认默认项目",
            "error": {
                "code": "SAVED_DEFAULT_OUT_OF_SCOPE",
                "message": "已保存的默认项目不在当前可访问范围，不能继续使用",
            },
        }
    public = _public_profile(profile)
    public["default_project_name"] = current["name"]
    return {
        "status": "ok",
        "effect": "read",
        "profile_state": "ready",
        "profile": public,
        "accessible_project_names": names,
        "next_action": "可以继续 TAPD 查询或测试流程",
        "error": None,
    }


def resolve_context(
    credential_fingerprint: str,
    projects: Sequence[Mapping[str, Any]],
    *,
    project_hint: str = "",
    tapd_identity: str = "",
    business_role: str = "",
    credential_token: str = "",
) -> dict[str, Any]:
    try:
        normalized = normalize_projects(projects)
        hint = _metadata_text(project_hint, "项目提示")
    except ContextValidationError as exc:
        return _error("blocked", "VALIDATION_FAILED", str(exc))

    claim_requested = bool(
        str(tapd_identity or "").strip() or str(business_role or "").strip()
    )
    claim_identity = ""
    claim_role = ""
    if claim_requested:
        try:
            claim_identity = _business_text(tapd_identity, "TAPD 身份")
            claim_role = _business_text(business_role, "业务角色")
            fingerprint = _validated_fingerprint(credential_fingerprint)
            token = _credential_text(credential_token)
            if store.fingerprint(token) != fingerprint:
                raise ContextValidationError("会话凭据上下文不一致")
        except (ContextValidationError, SessionContextError) as exc:
            return _error(
                "blocked",
                "SESSION_CONTEXT_INPUT_INVALID",
                str(exc),
            )

    def resolved(project: Mapping[str, str], source: str) -> dict[str, Any]:
        result = _resolved(project, source)
        if not claim_requested:
            return result
        try:
            issued = issue_session_context(
                credential_token,
                workspace_id=project["id"],
                tapd_identity=claim_identity,
                business_role=claim_role,
            )
        except SessionContextError as exc:
            return _error("blocked", exc.code, str(exc))
        result.update(issued)
        result["session_context_source"] = "user_confirmed"
        return result

    names = [p["name"] for p in normalized]
    if hint:
        matches = _matches(normalized, hint)
        if len(matches) == 1:
            return resolved(matches[0], "explicit")
        if len(matches) > 1:
            return _confirmation(
                names,
                "PROJECT_AMBIGUOUS",
                "我找到多个同名项目，请确认这次具体使用哪个项目",
            )
        return _confirmation(
            names,
            "PROJECT_NOT_ACCESSIBLE",
            "你提到的项目不在当前可访问范围，请从业务名称中确认",
        )

    loaded = load_profile(credential_fingerprint)
    if loaded.usable:
        profile = loaded.profile or {}
        chosen = _find_by_id(normalized, profile["default_project"]["id"])
        if chosen is None:
            return _error(
                "blocked",
                "SAVED_DEFAULT_OUT_OF_SCOPE",
                "已保存的默认项目不在当前可访问范围，不能自动改用其他项目",
                project_options=names,
            )
        return resolved(chosen, "saved_default")
    if loaded.status not in {"missing"}:
        return _profile_load_error(loaded)
    if len(normalized) == 1:
        return resolved(normalized[0], "unique_accessible")
    if not normalized:
        return _error(
            "blocked",
            "NO_ACCESSIBLE_PROJECTS",
            "当前凭据没有可访问的 TAPD 项目",
        )
    return _confirmation(
        names, "PROJECT_CONFIRMATION_REQUIRED", "我找到多个项目，这次要看哪个？"
    )


def issue_session_context(
    credential_token: str,
    *,
    workspace_id: str,
    tapd_identity: str,
    business_role: str,
    now: int | None = None,
) -> dict[str, Any]:
    """Issue one short-lived opaque claim without writing profile state."""
    token = _credential_text(credential_token)
    try:
        workspace = _business_text(workspace_id, "项目标识")
        identity = _business_text(tapd_identity, "TAPD 身份")
        role = _business_text(business_role, "业务角色")
    except ContextValidationError as exc:
        raise SessionContextError("SESSION_CONTEXT_INPUT_INVALID", str(exc)) from None
    issued_at = _epoch(now)
    expires_at = issued_at + SESSION_CONTEXT_TTL_SECONDS
    payload = {
        "schema_version": SESSION_CONTEXT_SCHEMA,
        "workspace_id": workspace,
        "tapd_identity": identity,
        "business_role": role,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    encoded = _b64url_encode(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signature = _session_signature(token, encoded)
    return {
        "session_context": f"{SESSION_CONTEXT_PREFIX}.{encoded}.{signature}",
        "session_context_expires_at": _iso_epoch(expires_at),
        "session_context_ttl_seconds": SESSION_CONTEXT_TTL_SECONDS,
    }


def verify_session_context(
    credential_token: str,
    session_context: str,
    *,
    expected_workspace: str,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify signature, credential binding, expiry, shape, and workspace."""
    token = _credential_text(credential_token)
    opaque = str(session_context or "").strip()
    if not opaque or len(opaque) > MAX_SESSION_CONTEXT_LENGTH:
        raise SessionContextError("SESSION_CONTEXT_INVALID", "本次会话上下文格式无效")
    parts = opaque.split(".")
    if len(parts) != 3 or parts[0] != SESSION_CONTEXT_PREFIX:
        raise SessionContextError("SESSION_CONTEXT_INVALID", "本次会话上下文格式无效")
    encoded, supplied_signature = parts[1], parts[2]
    if not _BASE64URL.fullmatch(encoded) or not _BASE64URL.fullmatch(
        supplied_signature
    ):
        raise SessionContextError("SESSION_CONTEXT_INVALID", "本次会话上下文格式无效")
    expected_signature = _session_signature(token, encoded)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise SessionContextError(
            "SESSION_CONTEXT_INVALID",
            "本次会话上下文已失效或不属于当前凭据",
        )
    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise SessionContextError(
            "SESSION_CONTEXT_INVALID", "本次会话上下文格式无效"
        ) from None
    required = {
        "schema_version",
        "workspace_id",
        "tapd_identity",
        "business_role",
        "issued_at",
        "expires_at",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise SessionContextError("SESSION_CONTEXT_INVALID", "本次会话上下文格式无效")
    if payload.get("schema_version") != SESSION_CONTEXT_SCHEMA:
        raise SessionContextError(
            "SESSION_CONTEXT_INVALID", "本次会话上下文版本不受支持"
        )
    try:
        workspace = _business_text(payload.get("workspace_id"), "项目标识")
        identity = _business_text(payload.get("tapd_identity"), "TAPD 身份")
        role = _business_text(payload.get("business_role"), "业务角色")
        issued_at = _claim_epoch(payload.get("issued_at"))
        expires_at = _claim_epoch(payload.get("expires_at"))
    except ContextValidationError as exc:
        raise SessionContextError("SESSION_CONTEXT_INVALID", str(exc)) from None
    if expires_at - issued_at != SESSION_CONTEXT_TTL_SECONDS:
        raise SessionContextError(
            "SESSION_CONTEXT_INVALID", "本次会话上下文有效期格式无效"
        )
    current = _epoch(now)
    if issued_at > current + SESSION_CONTEXT_CLOCK_SKEW_SECONDS:
        raise SessionContextError(
            "SESSION_CONTEXT_INVALID", "本次会话上下文签发时间无效"
        )
    if expires_at <= current:
        raise SessionContextError("SESSION_CONTEXT_EXPIRED", "本次会话上下文已过期")
    if workspace != str(expected_workspace):
        raise SessionContextError(
            "SESSION_CONTEXT_WORKSPACE_MISMATCH",
            "本次会话上下文不属于当前项目",
        )
    return {
        "workspace_id": workspace,
        "tapd_identity": identity,
        "business_role": role,
        "issued_at": _iso_epoch(issued_at),
        "expires_at": _iso_epoch(expires_at),
        "identity_basis": "user_confirmed_session",
    }


def _validated_record(raw: Any, expected_fingerprint: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("schema_version") != PROFILE_SCHEMA:
        raise ContextValidationError("用户上下文格式不受支持")
    fingerprint = _validated_fingerprint(raw.get("credential_fingerprint"))
    default = raw.get("default_project")
    verification = raw.get("verification")
    if not isinstance(default, Mapping) or not isinstance(verification, Mapping):
        raise ContextValidationError("用户上下文缺少必要字段")
    return {
        "schema_version": PROFILE_SCHEMA,
        "credential_fingerprint": fingerprint,
        "default_project": {
            "id": _business_text(default.get("id"), "默认项目标识"),
            "name": _business_text(default.get("name"), "默认项目名称"),
        },
        "tapd_identity": _business_text(raw.get("tapd_identity"), "TAPD 身份"),
        "business_role": _business_text(raw.get("business_role"), "业务角色"),
        "verification": {
            "verified_at": _metadata_text(verification.get("verified_at"), "验证时间"),
            "projects_fetched_at": _metadata_text(
                verification.get("projects_fetched_at"), "项目范围验证时间"
            ),
            "accessible_project_count": _non_negative_int(
                verification.get("accessible_project_count"), "可访问项目数"
            ),
            "project_scope_source": _fixed_metadata(
                verification.get("project_scope_source"),
                "workspace.list_accessible",
                "项目范围来源",
            ),
            "identity_basis": _fixed_metadata(
                verification.get("identity_basis"), "user_confirmed", "身份依据"
            ),
        },
    }


def _atomic_save(fingerprint: str, record: Mapping[str, Any]) -> None:
    target = profile_path(fingerprint)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(record), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Cleanup failure must not replace the original safe outcome with
            # an exception containing a credential-scoped filesystem path.
            pass


def _choose_project(
    projects: Sequence[Mapping[str, str]], hint: Any
) -> Mapping[str, str]:
    text = _business_text(hint, "默认项目")
    matches = _matches(projects, text)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ContextValidationError("当前可访问项目中有多个同名项目，无法确定默认项目")
    raise ContextValidationError("默认项目不在当前可访问项目范围")


def _matches(
    projects: Sequence[Mapping[str, str]], hint: str
) -> list[Mapping[str, str]]:
    folded = hint.casefold()
    return [p for p in projects if p["id"] == hint or p["name"].casefold() == folded]


def _find_by_id(projects: Sequence[Mapping[str, str]], workspace_id: str):
    return next((p for p in projects if p["id"] == workspace_id), None)


def _public_profile(record: Mapping[str, Any]) -> dict[str, Any]:
    default = record.get("default_project") or {}
    verification = record.get("verification") or {}
    return {
        "default_project_name": default.get("name", ""),
        "tapd_identity": record.get("tapd_identity", ""),
        "business_role": record.get("business_role", ""),
        "verified_at": verification.get("verified_at", ""),
    }


def _resolved(project: Mapping[str, str], source: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "effect": "read",
        "resolution": {
            "workspace_id": project["id"],
            "project_name": project["name"],
            "source": source,
        },
        "error": None,
    }


def _confirmation(names: list[str], code: str, message: str) -> dict[str, Any]:
    return {
        "status": "needs_confirmation",
        "effect": "read",
        "project_options": names,
        "question": message,
        "error": {"code": code, "message": message},
    }


def _profile_load_error(loaded: ProfileLoad) -> dict[str, Any]:
    code = (
        "PROFILE_CREDENTIAL_MISMATCH"
        if loaded.status == "foreign_credential"
        else "PROFILE_UNREADABLE"
    )
    result = _error("blocked", code, loaded.detail)
    result["profile_state"] = "invalid"
    return result


def _error(
    status: str,
    code: str,
    message: str,
    *,
    effect: str = "read",
    project_options: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "effect": effect,
        "tapd_write": False,
        "error": {"code": code, "message": message},
    }
    if project_options is not None:
        result["project_options"] = project_options
    return result


def _validated_fingerprint(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not _FINGERPRINT.fullmatch(text):
        raise ContextValidationError("凭据上下文标识格式无效")
    return text


def _business_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContextValidationError(f"{label}不能为空")
    if len(text) > MAX_BUSINESS_TEXT:
        raise ContextValidationError(f"{label}不能超过 {MAX_BUSINESS_TEXT} 个字符")
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        raise ContextValidationError(f"{label}不能包含控制字符")
    return text


def _metadata_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_BUSINESS_TEXT:
        raise ContextValidationError(f"{label}不能超过 {MAX_BUSINESS_TEXT} 个字符")
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        raise ContextValidationError(f"{label}不能包含控制字符")
    return text


def _non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContextValidationError(f"{label}格式无效")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ContextValidationError(f"{label}格式无效") from None
    if number < 0:
        raise ContextValidationError(f"{label}不能小于零")
    return number


def _fixed_metadata(value: Any, expected: str, label: str) -> str:
    if value != expected:
        raise ContextValidationError(f"{label}格式无效")
    return expected


def _credential_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionContextError(
            "SESSION_CONTEXT_INPUT_INVALID",
            "当前请求缺少可用于绑定会话上下文的凭据",
        )
    return value.strip()


def _epoch(value: int | None) -> int:
    if value is None:
        return int(time.time())
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SessionContextError("SESSION_CONTEXT_INVALID", "本次会话时间格式无效")
    return value


def _claim_epoch(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContextValidationError("本次会话时间格式无效")
    return value


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _session_signature(credential_token: str, encoded_payload: str) -> str:
    message = f"{SESSION_CONTEXT_SCHEMA}.{encoded_payload}".encode("ascii")
    digest = hmac.new(
        credential_token.encode("utf-8"), message, hashlib.sha256
    ).digest()
    return _b64url_encode(digest)


def _iso_epoch(value: int) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _utcnow() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
