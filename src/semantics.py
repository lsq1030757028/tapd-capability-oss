"""Fail-closed TAPD business semantics and query orchestration.

This module is platform-neutral.  It receives the existing read capability and
credential from an adapter, reuses the credential-scoped baseline/profile
stores, and never performs a TAPD mutation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import context as ctx
import store
import user_context

SUPPORTED_PREDICATE = "未开始测试"
SUPPORTED_ENTITY_TYPE = "story"
SUPPORTED_QUESTION = "最近分配给我且未开始测试的需求"
QUERY_PAGE_SIZE = 200
MAX_QUERY_PAGES = 100

_PENDING_REVIEW: dict[str, dict[str, Any]] = {}


def semantic_status(
    capability,
    workspace_id: str,
    *,
    token: str,
    namespace: str = "",
    session_context: str = "",
) -> dict[str, Any]:
    """Report semantic readiness without exposing field keys or raw status codes."""
    identity_context: dict[str, str] | None = None
    if session_context:
        identity_context, identity_error = _session_identity_context(
            capability,
            str(workspace_id),
            token,
            session_context,
        )
        if identity_error is not None:
            return _semantic_status_blocked(
                "blocked",
                identity_error[0],
                identity_error[1],
                identity_error[2],
            )
    loaded = store.load(str(workspace_id), token, namespace)
    if not loaded.usable:
        return _semantic_status_blocked(
            "needs_review",
            "BASELINE_UNAVAILABLE",
            loaded.detail,
            "运行 tapd_baseline_review 建立并确认字段基线",
        )
    record = loaded.baseline or {}
    mappings = (record.get("context") or {}).get("semantics") or {}
    mapping = (
        mappings.get(SUPPORTED_PREDICATE) if isinstance(mappings, Mapping) else None
    )
    if not isinstance(mapping, Mapping):
        return _semantic_status_blocked(
            "needs_review",
            "SEMANTIC_UNCONFIRMED",
            "这个项目还没有确认「未开始测试」的业务口径",
            "运行 tapd_semantic_review 指认精确状态，并对照真实条目后确认",
        )
    if not _mapping_is_valid(
        mapping,
        token=token,
        expected_workspace=str(workspace_id),
    ):
        return _semantic_status_blocked(
            "blocked",
            "SEMANTIC_RECORD_INVALID",
            "已存语义记录格式无效",
            "重新运行 tapd_semantic_review 并确认",
            invalid_predicate=SUPPORTED_PREDICATE,
        )
    if identity_context is None:
        identity_context, identity_error = _persistent_identity_context(
            str(workspace_id), token
        )
        if identity_error is not None:
            return _semantic_status_blocked(
                "blocked",
                identity_error[0],
                identity_error[1],
                identity_error[2],
            )
    workflow = capability.read(
        "workflow.get",
        {"workspace_id": str(workspace_id), "entity_type": SUPPORTED_ENTITY_TYPE},
    )
    if workflow.status != "ok":
        error = workflow.error or {}
        return _semantic_status_blocked(
            "blocked",
            str(error.get("code") or "WORKFLOW_UNAVAILABLE"),
            str(error.get("message") or "当前读不到需求工作流"),
            "恢复工作流读取后重试",
        )
    current = _status_map(workflow.data.get("record"))
    if current is None:
        return _semantic_status_blocked(
            "blocked",
            "WORKFLOW_SCHEMA_UNSUPPORTED",
            "需求工作流返回格式尚未核实，不能验证已存语义",
            "核实 /workflows/status_map 实际返回形状后重新 review",
            invalid_predicate=SUPPORTED_PREDICATE,
        )
    if _snapshot_id(current) != str(mapping.get("workflow_snapshot") or ""):
        return _semantic_status_blocked(
            "blocked",
            "STATUS_WORKFLOW_DRIFT",
            "需求工作流状态图已变化，已确认语义失效",
            "重新运行 tapd_semantic_review 并确认",
            invalid_predicate=SUPPORTED_PREDICATE,
        )
    if not _mapping_is_valid(
        mapping,
        current,
        token=token,
        expected_workspace=str(workspace_id),
    ):
        return _semantic_status_blocked(
            "blocked",
            "SEMANTIC_RECORD_INVALID",
            "已存语义引用了当前工作流中不存在的状态",
            "重新运行 tapd_semantic_review 并确认",
            invalid_predicate=SUPPORTED_PREDICATE,
        )
    return {
        "status": "ok",
        "effect": "read",
        "tapd_write": False,
        "语义层可用": True,
        "已确认谓词": [
            {
                "谓词": SUPPORTED_PREDICATE,
                "工作项": "需求",
                "口径": f"需求状态属于：{'、'.join(mapping.get('display_values') or [])}",
                "取值": list(mapping.get("display_values") or []),
                "确认时间": mapping.get("confirmed_at", ""),
            }
        ],
        "失效谓词": [],
        "现在能答": [SUPPORTED_QUESTION],
        "现在不能答": [],
        "身份上下文": identity_context["source"],
        "下一步": "可以运行 tapd_business_query",
        "error": None,
    }


def _semantic_status_blocked(
    status: str,
    code: str,
    reason: str,
    next_action: str,
    *,
    invalid_predicate: str = "",
) -> dict[str, Any]:
    invalid = [{"谓词": invalid_predicate, "原因": reason}] if invalid_predicate else []
    return {
        "status": status,
        "effect": "read",
        "tapd_write": False,
        "语义层可用": False,
        "已确认谓词": [],
        "失效谓词": invalid,
        "现在能答": [],
        "现在不能答": [SUPPORTED_QUESTION],
        "下一步": next_action,
        "error": {"code": code, "message": reason},
    }


def semantic_review(
    capability,
    workspace_id: str,
    predicate: str,
    *,
    token: str,
    values: Sequence[str] | None = None,
    namespace: str = "",
) -> dict[str, Any]:
    """Prepare, but never persist, one exact status mapping for human review."""
    loaded = store.load(str(workspace_id), token, namespace)
    if not loaded.usable:
        return _refusal(
            "needs_review",
            "BASELINE_UNAVAILABLE",
            loaded.detail,
            "先运行 tapd_baseline_review，并对照 TAPD 确认字段基线",
        )
    if str(predicate).strip() != SUPPORTED_PREDICATE:
        return _refusal(
            "blocked",
            "PREDICATE_UNSUPPORTED",
            "当前业务查询尚未定义这个谓词",
            "请先为这个真实业务问题补充独立的语义规格",
        )
    workflow = capability.read(
        "workflow.get",
        {"workspace_id": str(workspace_id), "entity_type": SUPPORTED_ENTITY_TYPE},
    )
    if workflow.status != "ok":
        error = workflow.error or {}
        return _refusal(
            "blocked",
            str(error.get("code") or "WORKFLOW_UNAVAILABLE"),
            str(error.get("message") or "当前读不到需求工作流"),
            "恢复需求工作流读取后重新运行 tapd_semantic_review",
        )
    status_map = _status_map(workflow.data.get("record"))
    if status_map is None:
        return _refusal(
            "blocked",
            "WORKFLOW_SCHEMA_UNSUPPORTED",
            "需求工作流返回格式尚未核实，不能猜状态语义",
            "先核实 /workflows/status_map 的实际返回形状再继续",
        )
    labels = sorted(set(status_map.values()))
    chosen = [str(value).strip() for value in (values or ()) if str(value).strip()]
    if not chosen:
        return {
            "status": "needs_input",
            "effect": "read",
            "tapd_write": False,
            "谓词": SUPPORTED_PREDICATE,
            "工作项": "需求",
            "可选状态": labels,
            "候选映射": [],
            "下一步": "请按 TAPD 页面显示名明确指出哪些状态算「未开始测试」，不会做近似匹配",
            "error": None,
        }
    unknown = sorted(set(chosen) - set(labels))
    if unknown:
        return _refusal(
            "blocked",
            "SEMANTIC_VALUE_UNKNOWN",
            f"需求工作流里没有这些精确状态：{'、'.join(unknown)}",
            "请从 tapd_semantic_review 返回的业务状态中明确选择；不会近似匹配",
        )
    chosen = list(dict.fromkeys(chosen))
    selected_codes = {code for code, label in status_map.items() if label in chosen}
    listing = capability.read(
        "entity.list",
        {
            "workspace_id": str(workspace_id),
            "entity_type": SUPPORTED_ENTITY_TYPE,
            "limit": 50,
            "page": 1,
            "order": "modified desc",
            "fields": "id,name,status",
        },
    )
    if listing.status != "ok":
        error = listing.error or {}
        return _refusal(
            "blocked",
            str(error.get("code") or "SEMANTIC_EVIDENCE_UNAVAILABLE"),
            str(error.get("message") or "当前读不到用于核对的真实需求"),
            "恢复需求读取后重新运行 tapd_semantic_review",
        )
    evidence: list[dict[str, str]] = []
    counterexamples: list[dict[str, str]] = []
    evidence_entity_ids: list[str] = []
    for row in listing.data.get("items", []):
        code = str(row.get("status") or "").strip()
        label = status_map.get(code)
        if not label:
            continue
        card = {
            "标题": str(row.get("name") or ""),
            "链接": ctx.tapd_entity_url(
                str(workspace_id), "story", str(row.get("id") or "")
            ),
            "状态": label,
        }
        target = evidence if code in selected_codes else counterexamples
        if len(target) < 2:
            target.append(card)
            if target is evidence:
                evidence_entity_ids.append(str(row.get("id") or ""))
    if not evidence:
        return _refusal(
            "blocked",
            "SEMANTIC_EVIDENCE_MISSING",
            "最近的真实需求里没有选中状态的样本，无法完成语义人闸",
            "扩大可核样本或在 TAPD 中找到真实条目后重新 review",
        )
    snapshot = _snapshot_id(status_map)
    _PENDING_REVIEW[_pending_key(token, str(workspace_id), SUPPORTED_PREDICATE)] = {
        "workspace_id": str(workspace_id),
        "predicate": SUPPORTED_PREDICATE,
        "entity_type": SUPPORTED_ENTITY_TYPE,
        "field": "status",
        "field_label": "状态",
        "source": "workflow_status_map",
        "raw_values": sorted(selected_codes),
        "display_values": sorted(chosen),
        "evidence": evidence,
        "evidence_entity_ids": evidence_entity_ids,
        "workflow_snapshot": snapshot,
        "reviewed_at": str(listing.evidence.get("fetched_at") or ""),
    }
    return {
        "status": "needs_confirmation",
        "effect": "read",
        "tapd_write": False,
        "谓词": SUPPORTED_PREDICATE,
        "候选映射": [
            {
                "工作项": "需求",
                "栏位": "状态",
                "取值": sorted(chosen),
                "来源": "工作流状态图",
            }
        ],
        "证据条目": evidence,
        "对照条目": counterexamples,
        "请你做的事": "打开链接逐条对照；确认这些状态都属于「未开始测试」后再运行 tapd_semantic_confirm",
        "error": None,
    }


def semantic_confirm(
    workspace_id: str,
    predicate: str,
    *,
    token: str,
    namespace: str = "",
) -> dict[str, Any]:
    """Persist only the exact candidate most recently shown to this credential."""
    key = _pending_key(token, str(workspace_id), str(predicate).strip())
    pending = _PENDING_REVIEW.get(key)
    if pending is None:
        return {
            "status": "refused",
            "effect": "read",
            "tapd_write": False,
            "原因": "还没有给当前凭据展示过这条语义的核对卡",
            "下一步": "先运行 tapd_semantic_review，并让用户对照真实 TAPD 条目",
            "error": {
                "code": "REVIEW_REQUIRED",
                "message": "semantic review is required",
            },
        }
    mapping = dict(pending)
    mapping["confirmed_at"] = _utcnow()
    mapping["confirmation_basis"] = {
        "kind": "human_reviewed_live_items",
        "reviewed_at": mapping.get("reviewed_at", ""),
        "evidence_entity_ids": list(mapping.get("evidence_entity_ids") or []),
    }
    mapping["confirmation_proof"] = _confirmation_proof(mapping, token)
    if not _mapping_is_valid(
        mapping,
        token=token,
        expected_workspace=str(workspace_id),
    ):
        return _refusal(
            "blocked",
            "SEMANTIC_EVIDENCE_UNVERIFIED",
            "刚才的语义核对记录缺少可验证的确认依据",
            "重新运行 tapd_semantic_review，对照真实条目后再确认",
        )
    saved = store.save_semantic(
        str(workspace_id),
        str(predicate).strip(),
        mapping,
        token,
        namespace=namespace,
    )
    if not saved.usable:
        return _refusal(
            "blocked",
            "BASELINE_UNAVAILABLE",
            saved.detail,
            "重新建立字段基线后再次 review；不会把语义写进不可用基线",
        )
    _PENDING_REVIEW.pop(key, None)
    return {
        "status": "ok",
        "effect": "workspace-write",
        "tapd_write": False,
        "已确认": {
            "谓词": mapping["predicate"],
            "工作项": "需求",
            "栏位": mapping["field_label"],
            "取值": list(mapping["display_values"]),
            "确认时间": mapping["confirmed_at"],
        },
        "error": None,
    }


def _status_map(record: Any) -> dict[str, str] | None:
    """Accept only the explicit status_map object; unknown shapes fail closed."""
    if not isinstance(record, Mapping):
        return None
    raw = record.get("status_map")
    if not isinstance(raw, Mapping) or not raw:
        return None
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        code = str(key).strip()
        label = str(value).strip()
        if not code or not label or isinstance(value, (Mapping, list, tuple)):
            return None
        normalized[code] = label
    return normalized


def _snapshot_id(status_map: Mapping[str, str]) -> str:
    payload = json.dumps(
        dict(sorted(status_map.items())), ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pending_key(token: str, workspace_id: str, predicate: str) -> str:
    return f"{store.fingerprint(token)}:{workspace_id}:{predicate}"


def _utcnow() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def business_query(
    capability,
    workspace_id: str,
    question: str,
    limit: int,
    *,
    token: str,
    namespace: str = "",
    session_context: str = "",
) -> dict[str, Any]:
    """Answer one supported business question only from confirmed semantics."""
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > 100
    ):
        return _refusal(
            "blocked",
            "VALIDATION_FAILED",
            "结果上限必须是 1 到 100 的整数",
            "修正 limit 后重试",
        )
    identity_context: dict[str, str] | None = None
    if session_context:
        identity_context, identity_error = _session_identity_context(
            capability,
            str(workspace_id),
            token,
            session_context,
        )
        if identity_error is not None:
            return _refusal(
                "blocked",
                identity_error[0],
                identity_error[1],
                identity_error[2],
            )

    loaded = store.load(str(workspace_id), token, namespace)
    if not loaded.usable:
        return _refusal(
            "needs_review",
            "BASELINE_UNAVAILABLE",
            loaded.detail,
            "先运行 tapd_baseline_review，并对照 TAPD 确认字段基线",
        )
    record = loaded.baseline or {}
    mappings = (record.get("context") or {}).get("semantics") or {}
    if not isinstance(mappings, dict) or "未开始测试" not in mappings:
        return _refusal(
            "needs_review",
            "SEMANTIC_UNCONFIRMED",
            "这个项目还没有确认「未开始测试」对应哪些 TAPD 状态",
            "运行 tapd_semantic_review，指认业务状态并对照真实条目后再确认",
        )
    if str(question).strip().rstrip("？?。") != SUPPORTED_QUESTION:
        return _refusal(
            "blocked",
            "UNSUPPORTED_BUSINESS_QUESTION",
            "当前只实现了“最近分配给我且未开始测试的需求”这一条已定口径问题",
            "请补充对应业务谓词的独立规格；不会把自然语言翻成任意 TAPD 查询",
        )
    if identity_context is None:
        identity_context, identity_error = _persistent_identity_context(
            str(workspace_id), token
        )
        if identity_error is not None:
            return _refusal(
                "blocked",
                identity_error[0],
                identity_error[1],
                identity_error[2],
            )
    mapping = mappings[SUPPORTED_PREDICATE]
    if not isinstance(mapping, Mapping):
        return _refusal(
            "blocked",
            "SEMANTIC_RECORD_INVALID",
            "已存语义记录格式无效",
            "重新运行 tapd_semantic_review 并确认",
        )
    if not _mapping_is_valid(
        mapping,
        token=token,
        expected_workspace=str(workspace_id),
    ):
        return _refusal(
            "blocked",
            "SEMANTIC_RECORD_INVALID",
            "已存语义缺少可验证的确认时间、确认依据或完整性凭证",
            "重新运行 tapd_semantic_review 并确认",
        )
    workflow = capability.read(
        "workflow.get",
        {"workspace_id": str(workspace_id), "entity_type": SUPPORTED_ENTITY_TYPE},
    )
    if workflow.status != "ok":
        error = workflow.error or {}
        return _refusal(
            "blocked",
            str(error.get("code") or "WORKFLOW_UNAVAILABLE"),
            str(error.get("message") or "当前读不到需求工作流"),
            "恢复需求工作流读取后重试；不会沿用无法核验的旧口径",
        )
    current_statuses = _status_map(workflow.data.get("record"))
    if current_statuses is None:
        return _refusal(
            "blocked",
            "WORKFLOW_SCHEMA_UNSUPPORTED",
            "需求工作流返回格式尚未核实，不能验证已存语义",
            "核实 /workflows/status_map 实际返回形状后重新 review",
        )
    if _snapshot_id(current_statuses) != str(mapping.get("workflow_snapshot") or ""):
        return _refusal(
            "blocked",
            "STATUS_WORKFLOW_DRIFT",
            "需求工作流状态图已变化，已确认的「未开始测试」口径失效",
            "重新运行 tapd_semantic_review，对照真实条目并确认新口径",
        )
    raw_values = mapping.get("raw_values")
    display_values = mapping.get("display_values")
    if not _mapping_is_valid(
        mapping,
        current_statuses,
        token=token,
        expected_workspace=str(workspace_id),
    ):
        return _refusal(
            "blocked",
            "SEMANTIC_RECORD_INVALID",
            "已存语义缺少可核验的需求状态映射",
            "重新运行 tapd_semantic_review 并确认",
        )

    identity = str(identity_context.get("tapd_identity") or "").strip()
    if not identity:
        return _refusal(
            "blocked",
            "USER_PROFILE_REQUIRED",
            "已确认用户档案里没有可用的 TAPD 身份",
            "先运行 tapd_context_save 明确 TAPD 身份",
        )

    matches: list[tuple[datetime, str, dict[str, str]]] = []
    seen_ids: set[str] = set()
    fetched_at = ""
    for page_number in range(1, MAX_QUERY_PAGES + 1):
        listing = capability.read(
            "entity.list",
            {
                "workspace_id": str(workspace_id),
                "entity_type": SUPPORTED_ENTITY_TYPE,
                "limit": QUERY_PAGE_SIZE,
                "page": page_number,
                "order": "modified desc",
                "fields": "id,name,status,owner,modified",
            },
        )
        if listing.status != "ok":
            error = listing.error or {}
            return _refusal(
                "blocked",
                str(error.get("code") or "STORY_READ_FAILED"),
                str(error.get("message") or "当前读不到需求列表"),
                "恢复需求读取后重试；不会返回部分结果",
            )
        fetched_at = str(listing.evidence.get("fetched_at") or fetched_at)
        items = listing.data.get("items", [])
        if not isinstance(items, list) or any(
            not isinstance(row, Mapping) for row in items
        ):
            return _refusal(
                "blocked",
                "STORY_SCHEMA_UNSUPPORTED",
                "需求列表返回格式不受支持",
                "核实 TAPD 需求列表实际返回形状后重试",
            )
        page_error = _pagination_error(listing.page, page_number)
        if page_error:
            return _refusal(
                "blocked",
                "PAGINATION_UNVERIFIED",
                page_error,
                "核实 TAPD 分页元数据后重试；不会把未知范围当成完整结果",
            )
        for row in items:
            entity_id = str(row.get("id") or "").strip()
            if (
                not entity_id
                or "status" not in row
                or "owner" not in row
                or "modified" not in row
            ):
                return _refusal(
                    "blocked",
                    "STORY_SCHEMA_UNSUPPORTED",
                    "需求条目缺少用于完整筛选或排序的字段",
                    "核实 TAPD 需求列表字段形状后重试",
                )
            if entity_id in seen_ids:
                return _refusal(
                    "blocked",
                    "PAGINATION_UNVERIFIED",
                    "分页之间出现重复需求，无法证明结果快照稳定完整",
                    "在稳定分页快照上重试；不会返回可能漏项或重复的结果",
                )
            seen_ids.add(entity_id)
            code = str(row.get("status") or "").strip()
            if code not in raw_values or not _owner_matches(row.get("owner"), identity):
                continue
            modified = str(row.get("modified") or "").strip()
            modified_at = _parse_timestamp(modified)
            if modified_at is None:
                return _refusal(
                    "blocked",
                    "MODIFIED_TIME_UNVERIFIED",
                    "命中的需求缺少可解析的最近更新时间，无法验证“最近”排序",
                    "核实 modified 时间字段后重试；不会宣称未经验证的最近顺序",
                )
            matches.append(
                (
                    modified_at,
                    entity_id,
                    {
                        "标题": str(row.get("name") or ""),
                        "链接": ctx.tapd_entity_url(
                            str(workspace_id), "story", entity_id
                        ),
                        "状态": current_statuses[code],
                        "指派人": identity,
                        "最近更新": modified,
                    },
                )
            )
        if listing.page.get("has_more") is False:
            break
    else:
        return _refusal(
            "blocked",
            "RESULT_SCOPE_INCOMPLETE",
            "需求结果超过安全分页上限，无法证明总数完整",
            "缩小项目范围或另立经确认的查询条件；不会返回部分结果",
        )

    matches.sort(key=lambda item: (-item[0].timestamp(), item[1]))
    ordered = [item[2] for item in matches]
    returned = ordered[:limit]
    return {
        "status": "ok",
        "effect": "read",
        "tapd_write": False,
        "可答": True,
        "口径": [
            {
                "谓词": "分配给我",
                "解释": f"指派人精确包含已确认的 TAPD 身份「{identity}」",
            },
            {
                "谓词": SUPPORTED_PREDICATE,
                "工作项": "需求",
                "栏位": "状态",
                "取值": list(display_values),
            },
            {
                "谓词": "最近",
                "解释": "按需求最近更新时间倒序，不把它解释为指派动作发生时间",
            },
        ],
        "条目": returned,
        "计数": {"符合": len(matches), "返回": len(returned)},
        "是否截断": len(matches) > limit,
        "证据": {
            "取数时间": fetched_at,
            "工作区": str(workspace_id),
            "字段基线确认时间": record.get("confirmed_at", ""),
            "语义确认时间": mapping.get("confirmed_at", ""),
            "身份上下文": identity_context["source"],
            "当前可见范围": "仅统计当前已确认 TAPD 身份与当前凭据可见的需求",
        },
        "error": None,
    }


def _session_identity_context(
    capability,
    workspace_id: str,
    token: str,
    session_context: str,
) -> tuple[dict[str, str] | None, tuple[str, str, str] | None]:
    """Verify a transient identity and re-check its current project scope.

    Local authenticity checks intentionally happen before any TAPD read.  Once
    the claim is valid, only project-scope discovery is allowed before the
    caller proceeds to workflow or entity reads.
    """
    try:
        claim = user_context.verify_session_context(
            token,
            session_context,
            expected_workspace=workspace_id,
        )
    except user_context.SessionContextError as exc:
        return None, (
            exc.code,
            str(exc),
            "重新运行 tapd_context_resolve，并用当前凭据和本次项目取得新的 session_context",
        )
    try:
        result, projects = user_context.fetch_accessible_projects(capability)
    except user_context.ContextValidationError:
        return None, (
            "SESSION_CONTEXT_SCOPE_INVALID",
            "当前凭据返回的项目范围格式无法核实",
            "核实 TAPD 项目范围返回后重新取得 session_context",
        )
    if result.status != "ok":
        error = result.error or {}
        return None, (
            "SESSION_CONTEXT_SCOPE_UNAVAILABLE",
            str(error.get("message") or "当前凭据的项目访问范围暂时无法核实"),
            "恢复 TAPD 项目范围读取后重新取得 session_context",
        )
    if not any(project["id"] == workspace_id for project in projects):
        return None, (
            "SESSION_CONTEXT_REVOKED",
            "当前凭据已不能访问这个项目，本次会话上下文已撤销",
            "重新确认当前可访问项目；不会继续读取工作流或业务数据",
        )
    return {
        "tapd_identity": str(claim["tapd_identity"]),
        "business_role": str(claim["business_role"]),
        "source": "session_context",
    }, None


def _persistent_identity_context(
    workspace_id: str,
    token: str,
) -> tuple[dict[str, str] | None, tuple[str, str, str] | None]:
    """Load the backward-compatible saved profile only when no claim exists."""
    profile_load = user_context.load_profile(store.fingerprint(token))
    if not profile_load.usable:
        return None, (
            "USER_PROFILE_REQUIRED",
            "还没有可用于业务查询的已确认用户档案",
            "运行 tapd_context_save 保存默认，或用 tapd_context_resolve 取得本次 session_context",
        )
    profile = profile_load.profile or {}
    default_project = profile.get("default_project") or {}
    if str(default_project.get("id") or "") != workspace_id:
        return None, (
            "PROFILE_WORKSPACE_MISMATCH",
            "已保存的默认项目不是本次查询项目",
            "确认项目后保存新的默认，或用 tapd_context_resolve 取得本次 session_context",
        )
    identity = str(profile.get("tapd_identity") or "").strip()
    if not identity:
        return None, (
            "USER_PROFILE_REQUIRED",
            "已确认用户档案里没有可用的 TAPD 身份",
            "运行 tapd_context_save 明确 TAPD 身份",
        )
    return {
        "tapd_identity": identity,
        "business_role": str(profile.get("business_role") or ""),
        "source": "persistent_profile",
    }, None


def _owner_matches(value: Any, identity: str) -> bool:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        owners = [str(item).strip() for item in value]
    else:
        owners = [part.strip() for part in str(value or "").split(";")]
    return identity in {owner for owner in owners if owner}


def _mapping_is_valid(
    mapping: Mapping[str, Any],
    current_statuses: Mapping[str, str] | None = None,
    *,
    token: str,
    expected_workspace: str,
) -> bool:
    raw = mapping.get("raw_values")
    display = mapping.get("display_values")
    basis = mapping.get("confirmation_basis")
    if (
        mapping.get("workspace_id") != expected_workspace
        or mapping.get("predicate") != SUPPORTED_PREDICATE
        or mapping.get("entity_type") != SUPPORTED_ENTITY_TYPE
        or mapping.get("field") != "status"
        or mapping.get("source") != "workflow_status_map"
        or not isinstance(raw, list)
        or not raw
        or any(not isinstance(value, str) or not value for value in raw)
        or not isinstance(display, list)
        or not display
        or any(not isinstance(value, str) or not value for value in display)
        or not str(mapping.get("workflow_snapshot") or "")
        or raw != sorted(set(raw))
        or display != sorted(set(display))
        or _parse_timestamp(mapping.get("confirmed_at")) is None
        or not isinstance(basis, Mapping)
        or basis.get("kind") != "human_reviewed_live_items"
        or _parse_timestamp(basis.get("reviewed_at")) is None
        or not isinstance(basis.get("evidence_entity_ids"), list)
        or not basis.get("evidence_entity_ids")
        or any(
            not isinstance(value, str) or not value
            for value in basis.get("evidence_entity_ids", [])
        )
        or not hmac.compare_digest(
            str(mapping.get("confirmation_proof") or ""),
            _confirmation_proof(mapping, token),
        )
    ):
        return False
    if current_statuses is None:
        return True
    if _snapshot_id(current_statuses) != str(mapping.get("workflow_snapshot") or ""):
        return False
    expected_display = sorted(
        {current_statuses[value] for value in raw if value in current_statuses}
    )
    expected_raw = sorted(
        code for code, label in current_statuses.items() if label in set(display)
    )
    return (
        all(value in current_statuses for value in raw)
        and display == expected_display
        and raw == expected_raw
    )


def _confirmation_proof(mapping: Mapping[str, Any], token: str) -> str:
    protected = {
        key: value for key, value in mapping.items() if key != "confirmation_proof"
    }
    payload = json.dumps(
        protected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(token.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pagination_error(page: Any, page_number: int) -> str:
    if not isinstance(page, Mapping) or page.get("verified") is False:
        return "需求列表没有可验证的分页元数据"
    has_more = page.get("has_more")
    if not isinstance(has_more, bool):
        return "需求列表分页 has_more 缺失或形状未知"
    cursor = page.get("cursor")
    if not has_more:
        return "" if cursor in (None, "") else "分页结束标记与 cursor 相互矛盾"
    try:
        next_page = int(str(cursor))
    except (TypeError, ValueError):
        return "分页仍有后续，但 next cursor 缺失或不可解析"
    if next_page != page_number + 1:
        return "分页 cursor 不连续，无法证明完整抓取"
    return ""


def _refusal(status: str, code: str, reason: str, next_action: str) -> dict[str, Any]:
    return {
        "status": status,
        "effect": "read",
        "tapd_write": False,
        "可答": False,
        "原因": reason,
        "下一步": next_action,
        "当前还能回答": [],
        "口径": [],
        "条目": [],
        "计数": 0,
        "是否截断": False,
        "error": {"code": code, "message": reason},
    }
