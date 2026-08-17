"""MCP host adapter for tapd-capability (contract C9).

Translates MCP tool calls into typed Tool operations. TAPD reads keep the Tool's
envelope.  The three user-context helpers add one explicitly local effect:
``tapd_context_save`` writes a non-sensitive profile outside the package;
``tapd_context_resolve`` can instead issue a short-lived signed context without
persisting it. Neither mutates TAPD.

Two transports, one tool surface:

* ``stdio`` (default, unchanged) — one process per person; the credential is the
  process's own, from ``TAPD_ACCESS_TOKEN`` or a file outside the package.
* ``streamable-http`` (``--transport http``) — one process serving many people;
  the credential belongs to the *request*, is read from its headers on every
  call, and is never stored, cached, logged, or defaulted. A call that arrives
  without one is refused, not served with somebody else's token.

Everything that persists per-tenant (the pending review card, the baseline file)
is keyed by a non-reversible fingerprint of the caller's credential, so one
tenant can neither read nor overwrite another's.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

import context as ctx
import credentials
import semantics
import store
import user_context
from baseline import watchlist_recheck
from credentials import CredentialError
from tapd_capability import TapdCapability
from transport_http import TapdHttpTransport

#: Resolved once, at import: FastMCP takes host/port in its constructor. Absent
#: an explicit flag or environment variable this is stdio — the mode every
#: existing client is configured for.
RUNTIME = credentials.resolve_runtime(sys.argv[1:], os.environ)

mcp = FastMCP("tapd-capability", host=RUNTIME.host, port=RUNTIME.port)


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_request: Request) -> Response:
    """A constant liveness response: no credential, config, storage, or TAPD read."""
    return JSONResponse(
        {"status": "ok"},
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _mcp_authorization_error(headers: Mapping[str, str]) -> Response | None:
    """Require one non-empty Bearer header without ever echoing what arrived."""
    lookup = {str(key).lower(): str(value) for key, value in headers.items()}
    raw = lookup.get(credentials.AUTHORIZATION_HEADER, "").strip()
    scheme, separator, token = raw.partition(" ")
    if separator and scheme.lower() == credentials.BEARER_SCHEME and token.strip():
        return None
    return JSONResponse(
        {
            "status": "unauthorized",
            "error": {
                "code": "AUTH_REQUIRED",
                "message": "每个 /mcp 请求都必须携带 Authorization: Bearer <TAPD token>。",
            },
        },
        status_code=401,
        headers={
            "WWW-Authenticate": 'Bearer realm="tapd-capability"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


class _McpBearerGate:
    """ASGI edge gate for /mcp; it validates shape and retains no token."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            headers = {
                key.decode("latin-1"): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            error = _mcp_authorization_error(headers)
            if error is not None:
                await error(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _http_app() -> ASGIApp:
    """Build the SDK app once, then enforce per-request Bearer at its edge."""
    return _McpBearerGate(mcp.streamable_http_app())


#: Last review shown, keyed by (credential fingerprint, workspace). Confirming
#: without reviewing is refused — the human gate is "you looked at it against
#: TAPD", not "you typed yes" — and one tenant's review must not become another
#: tenant's confirmation, which is why the key is not the workspace alone.
_PENDING_REVIEW: dict[str, Any] = {}


def _request_headers() -> Mapping[str, str]:
    """Headers of the HTTP request currently being served.

    mcp 1.26.0: the streamable-HTTP transport attaches the Starlette ``Request``
    to the message metadata (``server/streamable_http.py`` →
    ``ServerMessageMetadata(request_context=request)``), the low-level server
    puts it on ``RequestContext.request``, and ``FastMCP.get_context()`` hands
    that context back. There is no ``get_http_headers()`` helper in this SDK —
    that one belongs to the separate ``fastmcp`` 2.x package.
    """
    try:
        request = mcp.get_context().request_context.request
    except (LookupError, ValueError) as exc:
        raise CredentialError(
            "HTTP 模式下令牌只能来自当前请求，但这次调用没有请求上下文，无法取到令牌。"
        ) from exc
    headers = getattr(request, "headers", None)
    if headers is None:
        raise CredentialError(
            "HTTP 模式下令牌只能来自当前请求，但这次调用没有携带 HTTP 请求头。"
        )
    return headers


def _credential() -> str:
    """The credential for *this* call — resolved fresh, held by nothing.

    In HTTP mode there is deliberately no fallback to the process environment:
    serving a request that brought no credential with whatever token the
    operator exported is lateral privilege escalation, not a convenience.
    """
    if RUNTIME.is_http:
        return credentials.token_from_headers(_request_headers())
    return credentials.token_from_environment(os.environ)


def _namespace(token: str) -> str:
    """Per-tenant storage namespace: a fingerprint in HTTP mode, none in stdio.

    Empty in stdio keeps every existing baseline file exactly where it is.
    """
    return store.fingerprint(token) if RUNTIME.is_http else ""


def _profile_namespace(token: str) -> str:
    """User profiles are credential-scoped in every transport, including stdio."""
    return store.fingerprint(token)


def _pending_key(workspace_id: str, token: str) -> str:
    return f"{store.fingerprint(token)}:{workspace_id}"


def _capability() -> TapdCapability:
    return TapdCapability(TapdHttpTransport(_credential))


def _envelope(result: Any) -> dict:
    """Pass the Tool envelope through unchanged (C9: no adapter-specific fields)."""
    return result.as_dict()


def _context_project_scope(token: str):
    """Discover this credential's current project range without retaining it."""
    capability = TapdCapability(TapdHttpTransport(lambda: token))
    try:
        result, projects = user_context.fetch_accessible_projects(capability)
    except user_context.ContextValidationError as exc:
        return (
            None,
            None,
            {
                "status": "blocked",
                "effect": "read",
                "tapd_write": False,
                "error": {"code": "PROJECT_SCOPE_INVALID", "message": str(exc)},
            },
        )
    if result.status != "ok":
        error = result.error or {}
        return (
            None,
            None,
            {
                "status": "failed",
                "effect": "read",
                "tapd_write": False,
                "error": {
                    "code": str(error.get("code") or "PROJECT_DISCOVERY_FAILED"),
                    "message": str(error.get("message") or "TAPD 项目范围读取失败"),
                },
            },
        )
    return projects, result.evidence, None


@mcp.tool()
def tapd_projects(user: str = "") -> dict:
    """列出该令牌能访问的 TAPD 项目（公司节点已由调用方自行过滤）。

    这是冷启动第一步：在不知道任何 workspace_id 的情况下发现可用项目。
    """
    result = _capability().read("workspace.list_accessible", {"user": user})
    payload = _envelope(result)
    if result.status == "ok":
        items = result.data.get("items", [])
        payload["data"] = {
            "projects": [
                {
                    "id": i.get("id"),
                    "name": i.get("name"),
                    "category": i.get("category"),
                }
                for i in items
                if i.get("category") != "organization"
            ],
            "excluded_organization_nodes": sum(
                1 for i in items if i.get("category") == "organization"
            ),
        }
    return payload


@mcp.tool()
def tapd_context_status() -> dict:
    """读取当前用户的 TAPD 业务上下文；信息缺失或失效时给业务化下一步。"""
    token = _credential()
    projects, _evidence, failure = _context_project_scope(token)
    if failure is not None:
        return failure
    return user_context.context_status(_profile_namespace(token), projects or [])


@mcp.tool()
def tapd_context_save(
    default_project: str,
    tapd_identity: str,
    business_role: str,
) -> dict:
    """保存非敏感用户上下文；只写本地工作区，绝不写 TAPD。

    ``default_project`` 可传用户看到的精确项目名称；内部调用方也可传项目 ID，
    但不得要求普通用户提供 ID。保存前会重新校验当前凭据的可访问项目范围。
    """
    token = _credential()
    projects, evidence, failure = _context_project_scope(token)
    if failure is not None:
        return failure
    return user_context.save_context(
        _profile_namespace(token),
        projects or [],
        default_project=default_project,
        tapd_identity=tapd_identity,
        business_role=business_role,
        projects_fetched_at=str((evidence or {}).get("fetched_at") or ""),
    )


@mcp.tool()
def tapd_context_resolve(
    project_hint: str = "",
    tapd_identity: str = "",
    business_role: str = "",
) -> dict:
    """确定本次项目：显式提示 > 已存默认 > 唯一可访问项目。

    同时传入刚确认的 TAPD 身份与业务角色时，签发短时 session_context，
    不保存默认项目；多候选只返回业务名称，撤权时拒绝猜测或静默切换。
    """
    token = _credential()
    projects, _evidence, failure = _context_project_scope(token)
    if failure is not None:
        return failure
    return user_context.resolve_context(
        _profile_namespace(token),
        projects or [],
        project_hint=project_hint,
        tapd_identity=tapd_identity,
        business_role=business_role,
        credential_token=token,
    )


@mcp.tool()
def tapd_workspace(workspace_id: str) -> dict:
    """读取单个项目的基本信息。"""
    return _envelope(
        _capability().read("workspace.get", {"workspace_id": workspace_id})
    )


@mcp.tool()
def tapd_field_config(workspace_id: str, entity_type: str) -> dict:
    """底层：读某类工作项的自定义栏位配置。entity_type = story|bug|task|iteration。

    低层取数接口，输出含内部栏位编号。面向用户的回答请改用基线类工具。
    """
    return _envelope(
        _capability().read(
            "schema.get", {"workspace_id": workspace_id, "entity_type": entity_type}
        )
    )


def tapd_list(
    workspace_id: str,
    entity_type: str,
    limit: int = 20,
    page: int = 1,
    fields: str = "",
    order: str = "created desc",
) -> dict:
    """底层：列出某类工作项。entity_type = story|bug|task|iteration。

    仅供能力内部诊断，输出为 TAPD 原始字段，禁止直接作为用户答复。
    面向用户的业务问题必须使用 tapd_business_query。
    """
    payload = {
        "workspace_id": workspace_id,
        "entity_type": entity_type,
        "limit": limit,
        "page": page,
        "order": order,
    }
    if fields:
        payload["fields"] = fields
    return _envelope(_capability().read("entity.list", payload))


@mcp.tool()
def tapd_semantic_status(workspace_id: str, session_context: str = "") -> dict:
    """取数前检查业务语义是否已确认、是否漂移，以及现在能答哪些问题。"""
    token = _credential()
    capability = TapdCapability(TapdHttpTransport(lambda: token))
    return semantics.semantic_status(
        capability,
        workspace_id,
        token=token,
        namespace=_namespace(token),
        session_context=session_context,
    )


@mcp.tool()
def tapd_semantic_review(
    workspace_id: str,
    predicate: str,
    values: list[str] | None = None,
) -> dict:
    """生成人闸核对卡；状态只接受 TAPD 页面业务名的精确选择，不近似猜测。"""
    token = _credential()
    capability = TapdCapability(TapdHttpTransport(lambda: token))
    return semantics.semantic_review(
        capability,
        workspace_id,
        predicate,
        token=token,
        values=values,
        namespace=_namespace(token),
    )


@mcp.tool()
def tapd_semantic_confirm(workspace_id: str, predicate: str) -> dict:
    """确认当前凭据刚刚看过的语义核对卡；只写本地文件，不写 TAPD。"""
    token = _credential()
    return semantics.semantic_confirm(
        workspace_id,
        predicate,
        token=token,
        namespace=_namespace(token),
    )


@mcp.tool()
def tapd_business_query(
    workspace_id: str,
    question: str,
    limit: int = 20,
    session_context: str = "",
) -> dict:
    """用已确认语义回答业务问题；缺基线、身份或语义时拒答，不回退原始列表。"""
    token = _credential()
    capability = TapdCapability(TapdHttpTransport(lambda: token))
    return semantics.business_query(
        capability,
        workspace_id,
        question,
        limit,
        token=token,
        namespace=_namespace(token),
        session_context=session_context,
    )


@mcp.tool()
def tapd_probe() -> dict:
    """连通性自检：跑一次只读探针，返回稳定错误码而非编造数据。

    先解析凭证再打网络：没有凭证是 AUTH_FAILED，不是"连不上"。
    """
    try:
        _credential()
    except CredentialError as exc:
        return {
            "status": "failed",
            "reachable": False,
            "error": {"code": "AUTH_FAILED", "message": str(exc)},
            "retryable": False,
        }
    outcome = TapdHttpTransport(_credential).probe()
    if outcome.get("status") == "ok":
        return {
            "status": "ok",
            "reachable": True,
            "projects_visible": outcome["data"].get("count", 0),
            "evidence": outcome.get("evidence", {}),
        }
    return {
        "status": "failed",
        "reachable": False,
        "error": {"code": outcome.get("code"), "message": outcome.get("message")},
        "retryable": outcome.get("retryable", False),
    }


def _workspace_name(capability: TapdCapability, workspace_id: str) -> str:
    result = capability.read("workspace.get", {"workspace_id": workspace_id})
    if result.status != "ok":
        return ""
    return str((result.data.get("record") or {}).get("name") or "")


@mcp.tool()
def tapd_baseline_status(workspace_id: str) -> dict:
    """这个项目的字段基线在不在、还作不作数。任何取数前先问它。"""
    token = _credential()
    loaded = store.load(workspace_id, token, _namespace(token))
    if not loaded.usable:
        return {
            "基线": "不可用",
            "原因": loaded.detail,
            "下一步": "跑一次 tapd_baseline_review 建立",
        }
    record = loaded.baseline or {}
    report = ctx.drift_report(_capability(), workspace_id, record.get("context", {}))
    return {
        "基线": "可用" if not report["drifted"] else "已漂移，暂不可信",
        "建立于": record.get("confirmed_at", ""),
        "各工作项": {
            ctx._ENTITY_LABELS.get(k, k): v.get("status")
            for k, v in report["domains"].items()
        },
        "下一步": "可以正常提问"
        if not report["drifted"]
        else "跑 tapd_baseline_drift 看明细，然后重建",
    }


@mcp.tool()
def tapd_baseline_review(workspace_id: str, sample_size: int = 20) -> dict:
    """建立基线第一步：算出哪几类工作项要核，各给真实条目让用户上 TAPD 对照。

    返回的每一栏都是业务名 + 真实取值 + 可点开的 TAPD 链接。用户对完再调 confirm。
    """
    token = _credential()
    capability = _capability()
    name = _workspace_name(capability, workspace_id)
    context = ctx.discover(
        capability, workspace_id, sample_size=sample_size, workspace_name=name
    )
    _PENDING_REVIEW[_pending_key(str(workspace_id), token)] = context
    card = ctx.build_review_card(context)
    card["项目"] = name or workspace_id
    card["取样条数"] = context.sample_size
    card["请你做的事"] = (
        "打开上面的链接，对一眼那几栏对不对；对得上就调 tapd_baseline_confirm"
    )
    return card


@mcp.tool()
def tapd_baseline_confirm(workspace_id: str) -> dict:
    """用户对照 TAPD 确认无误后存基线。未经 review 不允许确认。"""
    token = _credential()
    pending = _PENDING_REVIEW.get(_pending_key(str(workspace_id), token))
    if pending is None:
        return {
            "status": "refused",
            "原因": "还没生成过核对卡，不能确认——请先调 tapd_baseline_review 并让用户真的对一眼",
        }
    saved = store.save(
        workspace_id,
        pending.as_dict(),
        token,
        confirmed_at=pending.fetched_at or "",
        namespace=_namespace(token),
    )
    counts = {
        ctx._ENTITY_LABELS.get(k, k): len(d.in_use)
        for k, d in pending.domains.items()
        if d.needs_review
    }
    return {"status": "ok", "已存基线": str(saved.name), "各工作项收录栏数": counts}


@mcp.tool()
def tapd_baseline_drift(workspace_id: str) -> dict:
    """漂移明细：基线当时记的，和 TAPD 现在的实际，逐条对照。用位置说，不出编号。"""
    token = _credential()
    loaded = store.load(workspace_id, token, _namespace(token))
    if not loaded.usable:
        return {"status": "failed", "原因": loaded.detail}
    record = loaded.baseline or {}
    report = ctx.drift_report(_capability(), workspace_id, record.get("context", {}))
    rendered: dict[str, Any] = {}
    for entity_type, found in report["domains"].items():
        label = ctx._ENTITY_LABELS.get(entity_type, entity_type)
        if found.get("status") != "drifted":
            rendered[label] = found.get("status")
            continue
        rows = []
        for item in found.get("drift", []):
            position = _position_label(item["field"])
            if item["kind"] == "renamed":
                rows.append(
                    {
                        "位置": position,
                        "当时记作": item["was"],
                        "现在实际是": item["now"],
                    }
                )
            elif item["kind"] == "removed":
                rows.append(
                    {
                        "位置": position,
                        "当时记作": item["was"],
                        "现在实际是": "该栏已不存在",
                    }
                )
            else:
                rows.append(
                    {
                        "位置": position,
                        "当时记作": "基线里没记",
                        "现在实际是": item["now"],
                    }
                )
        rendered[label] = rows
    return {
        "status": "ok",
        "基线建立于": record.get("confirmed_at", ""),
        "是否漂移": report["drifted"],
        "逐项": rendered,
    }


@mcp.tool()
def tapd_watchlist_recheck(
    workspace_id: str, entity_type: str, sample_size: int = 20
) -> dict:
    """观察名单回补：以前没人填的栏位，现在有值了就提示收录。"""
    token = _credential()
    loaded = store.load(workspace_id, token, _namespace(token))
    if not loaded.usable:
        return {"status": "failed", "原因": loaded.detail}
    domain = ((loaded.baseline or {}).get("context", {}).get("domains") or {}).get(
        entity_type
    )
    if not domain:
        return {
            "status": "failed",
            "原因": f"基线里没有「{ctx._ENTITY_LABELS.get(entity_type, entity_type)}」这一类",
        }
    watchlist = domain.get("watchlist", [])
    if not watchlist:
        return {"status": "ok", "可回补": [], "说明": "该类没有观察名单"}
    listing = _capability().read(
        "entity.list",
        {
            "workspace_id": workspace_id,
            "entity_type": entity_type,
            "limit": sample_size,
            "order": "created desc",
            "fields": ",".join(["id"] + [w["field"] for w in watchlist]),
        },
    )
    if listing.status != "ok":
        return {"status": "failed", "原因": (listing.error or {}).get("message", "")}
    ready = watchlist_recheck(watchlist, listing.data.get("items", []))
    return {
        "status": "ok",
        "可回补": [{"栏位": r["label"], "现在的值": r["value"]} for r in ready],
        "说明": "这些栏位以前没人填，现在有值了；要收进基线就重新走一次 review + confirm"
        if ready
        else "观察名单里的栏位仍然没人填",
    }


@mcp.tool()
def tapd_field_evidence(
    workspace_id: str, entity_type: str, field_label: str, limit: int = 20
) -> dict:
    """用户说某栏读错了时调这个：拉真实条目交用户自己核，**绝不据此改基线**。

    人也会记错，唯一权威是 TAPD 实测。这里只把举证权交回给用户。
    """
    token = _credential()
    loaded = store.load(workspace_id, token, _namespace(token))
    if not loaded.usable:
        return {"status": "failed", "原因": loaded.detail}
    domain = ((loaded.baseline or {}).get("context", {}).get("domains") or {}).get(
        entity_type
    ) or {}
    entries = domain.get("in_use", []) + domain.get("watchlist", [])
    match = next((e for e in entries if e.get("label") == field_label), None)
    if match is None:
        known = sorted({e.get("label", "") for e in entries if e.get("label")})
        return {
            "status": "failed",
            "原因": f"基线里没有「{field_label}」这一栏",
            "这一类现有的栏位": known,
        }

    listing = _capability().read(
        "entity.list",
        {
            "workspace_id": workspace_id,
            "entity_type": entity_type,
            "limit": limit,
            "order": "created desc",
            "fields": f"id,{'title' if entity_type == 'bug' else 'name'},{match['field']}",
        },
    )
    if listing.status != "ok":
        return {"status": "failed", "原因": (listing.error or {}).get("message", "")}

    title_key = "title" if entity_type == "bug" else "name"
    evidence = []
    for row in listing.data.get("items", []):
        value = str(row.get(match["field"]) or "").strip().strip(";")
        if not value:
            continue
        evidence.append(
            {
                "标题": str(row.get(title_key) or ""),
                "链接": ctx.tapd_entity_url(
                    workspace_id, entity_type, str(row.get("id") or "")
                ),
                f"我读到的「{field_label}」": value,
            }
        )
        if len(evidence) >= 2:
            break

    if not evidence:
        return {
            "status": "ok",
            "基线是否被改": "否",
            "证据": [],
            "说明": f"最近 {limit} 条里「{field_label}」都没有值，拿不出证据；这栏可能确实没人用",
        }
    return {
        "status": "ok",
        "基线是否被改": "否",
        "证据": evidence,
        "说明": "打开链接看那一栏是不是这个值。是的话说明读对了；"
        "不是的话把那栏实际显示什么告诉我，我重新查——但我不会因为你说不对就直接改",
    }


def _position_label(field_key: str) -> str:
    """Describe a field by position, never by its internal identifier."""
    words = {
        "one": "一",
        "two": "二",
        "three": "三",
        "four": "四",
        "five": "五",
        "six": "六",
        "seven": "七",
        "eight": "八",
        "nine": "九",
        "ten": "十",
    }
    tail = field_key.replace("custom_field_", "")
    return f"第 {words.get(tail, tail)} 栏"


USAGE = f"""tapd-capability MCP server

  python adapters/mcp_server.py
      stdio（默认，不传参就是它）。凭证取自 {credentials.ENV_TOKEN} 或包外凭据文件。

  python adapters/mcp_server.py --transport http [--host H] [--port P]
      streamable-http。凭证按请求取自 HTTP 头，服务本身不持有任何人的凭证。
      默认 {credentials.DEFAULT_HTTP_HOST}:{credentials.DEFAULT_HTTP_PORT}，端点 /mcp。
      等价环境变量：{credentials.ENV_TRANSPORT} / {credentials.ENV_HOST} / {credentials.ENV_PORT}
"""


if __name__ == "__main__":
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print(USAGE)
        raise SystemExit(0)
    if RUNTIME.is_http:
        # 启动信息只说端点，绝不回显任何凭证；令牌由每个调用方自带。
        print(
            f"[tapd-capability] streamable-http on {RUNTIME.endpoint}", file=sys.stderr
        )
        import uvicorn

        uvicorn.run(
            _http_app(),
            host=RUNTIME.host,
            port=RUNTIME.port,
            log_level="info",
            access_log=True,
        )
    else:
        mcp.run()
