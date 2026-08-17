"""WorkspaceContext discovery (contract C2, extended through v1.2 semantics).

Discovery is per workspace and per work-item type. Field numbering is scoped to
one entity type and is never shared across types or workspaces — the same number
means different things (small-billiards: story field one = 需求来源方,
bug field one = bug等级).

Nothing here blocks: every missing form degrades with a stated reason so cold
start still produces a usable baseline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from baseline import (
    MAX_SAMPLES,
    REASON_ALL_UNUSED,
    REASON_NO_CUSTOM_FIELDS,
    REASON_NO_DATA,
    REASON_UNAVAILABLE,
    TIER_NO_SAMPLE,
    TITLE_KEYS,
    EntityDomain,
    FieldUsage,
    choose_samples,
    compare,
    measure_usage,
    normalize_schema,
    schema_revision,
    split_by_tier,
    watchlist_recheck,
)

CONTEXT_VERSION = "1.2"

#: Work-item types discovery walks. Order is the order a user sees them.
ENTITY_TYPES = ("story", "bug", "task", "iteration")

DEFAULT_SAMPLE_SIZE = 20


@dataclass
class WorkspaceContext:
    """Versioned, per-workspace field semantics with usage evidence."""

    workspace_id: str
    workspace_name: str = ""
    fetched_at: str = ""
    version: str = CONTEXT_VERSION
    domains: dict[str, EntityDomain] = dc_field(default_factory=dict)
    semantics: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    sample_size: int = DEFAULT_SAMPLE_SIZE

    @property
    def domains_needing_review(self) -> list[EntityDomain]:
        return [d for d in self.domains.values() if d.needs_review]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "fetched_at": self.fetched_at,
            "sample_size": self.sample_size,
            "domains": {k: v.as_dict() for k, v in self.domains.items()},
            "semantics": dict(self.semantics),
        }


def discover(
    capability,
    workspace_id: str,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    entity_types: Sequence[str] = ENTITY_TYPES,
    workspace_name: str = "",
) -> WorkspaceContext:
    """Walk every work-item type; never raise, always return a usable context."""
    context = WorkspaceContext(
        workspace_id=str(workspace_id),
        workspace_name=workspace_name,
        sample_size=sample_size,
    )

    for entity_type in entity_types:
        domain = _discover_domain(capability, workspace_id, entity_type, sample_size)
        context.domains[entity_type] = domain
        if not context.fetched_at and domain.detail.startswith("fetched_at="):
            context.fetched_at = domain.detail.split("=", 1)[1]

    if not context.fetched_at:
        probe = capability.read("workspace.get", {"workspace_id": workspace_id})
        context.fetched_at = str(probe.evidence.get("fetched_at", ""))
        record = probe.data.get("record") or {}
        if not context.workspace_name:
            context.workspace_name = str(record.get("name") or "")
    return context


def _discover_domain(
    capability, workspace_id: str, entity_type: str, sample_size: int
) -> EntityDomain:
    domain = EntityDomain(entity_type=entity_type)

    schema_result = capability.read(
        "schema.get", {"workspace_id": workspace_id, "entity_type": entity_type}
    )
    if schema_result.status != "ok":
        domain.reason = REASON_UNAVAILABLE
        domain.detail = _error_detail(schema_result)
        return domain

    schema = normalize_schema(schema_result.data.get("items", []))
    domain.custom_field_count = len(schema)
    domain.schema_revision = schema_revision(schema)

    # Form 1 — this type has no custom fields at all: built-in semantics, no review.
    if not schema:
        domain.reason = REASON_NO_CUSTOM_FIELDS
        domain.detail = "走 TAPD 内置栏位，语义全局固定，无需核对"
        return domain

    title_key = TITLE_KEYS.get(entity_type, "name")
    wanted = ["id", title_key] + [f["field"] for f in schema]
    listing = capability.read(
        "entity.list",
        {
            "workspace_id": workspace_id,
            "entity_type": entity_type,
            "limit": sample_size,
            "order": "created desc",
            "fields": ",".join(dict.fromkeys(wanted)),
        },
    )

    # Form 3 — cannot read this type (permission/transport): say so, do not substitute.
    if listing.status != "ok":
        domain.reason = REASON_UNAVAILABLE
        domain.detail = _error_detail(listing)
        return domain

    entities = listing.data.get("items", [])
    domain.detail = f"fetched_at={listing.evidence.get('fetched_at', '')}"

    # Form 2 — type exists but holds no data yet: defer, do not block.
    if not entities:
        domain.reason = REASON_NO_DATA
        domain.detail = "该类暂无数据；首次真正用到它时再核"
        return domain

    usages = measure_usage(entity_type, schema, entities)
    reviewable, watch = split_by_tier(usages)  # Forms 4/5 fall out here.
    samples, uncovered = choose_samples(reviewable, max_samples=MAX_SAMPLES)

    # Form 6 — two samples still leave gaps: degrade the remainder, keep going.
    if uncovered:
        watch = watch + [
            FieldUsage(
                field=u.field,
                label=u.label,
                field_type=u.field_type,
                filled=u.filled,
                sampled=u.sampled,
                tier=TIER_NO_SAMPLE,
                sample_entity_id="",
                sample_title="",
                sample_value="",
            )
            for u in uncovered
        ]
        covered_keys = {f for s in samples for f in (r["field"] for r in s["rows"])}
        reviewable = [u for u in reviewable if u.field in covered_keys]

    domain.in_use = reviewable
    domain.watchlist = watch
    domain.samples = samples

    # Fields exist and data exists, but nobody fills any of them. Not one of the
    # six enumerated forms, and easy to leave as a blank reason — which is exactly
    # the silent gap this capability exists to prevent.
    if not reviewable:
        domain.reason = REASON_ALL_UNUSED
        domain.detail = f"该类有 {domain.custom_field_count} 个自定义栏位，但最近 {len(entities)} 条里一个都没人填"
    return domain


def build_review_card(context: WorkspaceContext) -> dict[str, Any]:
    """The one-shot confirmation payload a human checks against TAPD.

    Business names only: internal field numbers stay out of anything shown to a
    person (spec AC4b).
    """
    review: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for entity_type, domain in context.domains.items():
        label = _ENTITY_LABELS.get(entity_type, entity_type)
        if not domain.needs_review:
            skipped.append(
                {
                    "工作项": label,
                    "为什么不用核": _REASON_TEXT.get(domain.reason, domain.reason),
                    "说明": domain.detail,
                }
            )
            continue
        samples = domain.samples
        review.append(
            {
                "工作项": label,
                "自定义栏位总数": domain.custom_field_count,
                "实际在用": len(domain.in_use),
                "样本": [
                    {
                        "标题": s["title"],
                        "链接": tapd_entity_url(
                            context.workspace_id, entity_type, s["entity_id"]
                        ),
                        "逐栏": [
                            {"栏位": r["label"], "值": r["value"]} for r in s["rows"]
                        ],
                    }
                    for s in samples
                ],
                "不收的栏位": [
                    {"栏位": u.business_name, "原因": _tier_text(u)}
                    for u in domain.watchlist
                ],
            }
        )
    return {"需要你核对": review, "无需核对": skipped}


_ENTITY_LABELS = {"story": "需求", "bug": "缺陷", "task": "任务", "iteration": "迭代"}

_REASON_TEXT = {
    REASON_NO_CUSTOM_FIELDS: "没有自定义栏位，全部走 TAPD 内置栏位",
    REASON_NO_DATA: "该类暂无数据",
    REASON_UNAVAILABLE: "该类当前读不到",
    REASON_ALL_UNUSED: "有自定义栏位，但最近的条目里没人填过",
}


def _tier_text(usage: FieldUsage) -> str:
    if usage.tier == TIER_NO_SAMPLE:
        return "有人填过，但两条样本都没覆盖到，暂无样本可核"
    if usage.filled == 0:
        return f"最近 {usage.sampled} 条里没人填过"
    return f"{usage.sampled} 条里只有 {usage.filled} 条有值"


def tapd_entity_url(workspace_id: str, entity_type: str, entity_id: str) -> str:
    segment = {
        "story": "stories",
        "bug": "bugs",
        "task": "tasks",
        "iteration": "iterations",
    }
    if entity_type == "bug":
        return f"https://www.tapd.cn/{workspace_id}/bugtrace/bugs/view/{entity_id}"
    return f"https://www.tapd.cn/{workspace_id}/prong/{segment.get(entity_type, entity_type)}/view/{entity_id}"


def _error_detail(result) -> str:
    error = result.error or {}
    return f"{error.get('code', 'UNKNOWN')}: {error.get('message', '')}"


def drift_report(
    capability,
    workspace_id: str,
    stored: Mapping[str, Any],
) -> dict[str, Any]:
    """Cheap path first: compare field-config revisions; only then re-read data."""
    findings: dict[str, Any] = {
        "workspace_id": workspace_id,
        "domains": {},
        "drifted": False,
    }
    for entity_type, domain in (stored.get("domains") or {}).items():
        result = capability.read(
            "schema.get", {"workspace_id": workspace_id, "entity_type": entity_type}
        )
        if result.status != "ok":
            findings["domains"][entity_type] = {
                "status": "unavailable",
                "detail": _error_detail(result),
            }
            continue
        fresh = normalize_schema(result.data.get("items", []))
        revision = schema_revision(fresh)
        stored_revision = domain.get("schema_revision", "")
        if revision and stored_revision and revision <= stored_revision:
            findings["domains"][entity_type] = {
                "status": "unchanged",
                "revision": revision,
            }
            continue
        drift = compare(domain, fresh)
        findings["domains"][entity_type] = {
            "status": "drifted" if drift else "unchanged",
            "revision": revision,
            "stored_revision": stored_revision,
            "drift": drift,
        }
        if drift:
            findings["drifted"] = True
    return findings


__all__ = [
    "CONTEXT_VERSION",
    "DEFAULT_SAMPLE_SIZE",
    "ENTITY_TYPES",
    "WorkspaceContext",
    "build_review_card",
    "discover",
    "drift_report",
    "tapd_entity_url",
    "watchlist_recheck",
]
