"""Pure baseline computation for WorkspaceContext (contract C2 + v1.2 `usage`).

No network, no credentials, no clock beyond what the caller injects. Every
function here is mechanically checkable: sampling, fill-rate tiering, sample
selection, drift comparison, and watch-list re-inclusion. Keeping these out of
model prose is deliberate — hand-counted tallies have been observed to be wrong.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

#: Fields whose fill rate reaches this share of the sample are treated as in use.
IN_USE_THRESHOLD = 0.25

#: Per contract-card decision 04, at most two samples per entity domain.
MAX_SAMPLES = 2

#: Title attribute differs per TAPD entity; getting this wrong is silently ignored.
TITLE_KEYS = {"story": "name", "bug": "title", "task": "name", "iteration": "name"}

TIER_IN_USE = "in_use"
TIER_SPARSE = "sparse"
TIER_UNUSED = "unused"
TIER_NO_SAMPLE = "no_sample"

#: Why a domain needs no human review, or cannot be reviewed right now.
REASON_NO_CUSTOM_FIELDS = "no_custom_fields"
REASON_NO_DATA = "no_data"
REASON_UNAVAILABLE = "unavailable"
REASON_ALL_UNUSED = "all_unused"


@dataclass(frozen=True)
class FieldUsage:
    """One custom field plus the evidence that it is (or is not) really used."""

    field: str
    label: str
    field_type: str
    filled: int
    sampled: int
    tier: str
    sample_entity_id: str = ""
    sample_title: str = ""
    sample_value: str = ""

    @property
    def business_name(self) -> str:
        return self.label or self.field

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label": self.label,
            "type": self.field_type,
            "filled": self.filled,
            "sampled": self.sampled,
            "tier": self.tier,
            "sample_entity_id": self.sample_entity_id,
            "sample_title": self.sample_title,
            "sample_value": self.sample_value,
        }


@dataclass
class EntityDomain:
    """One work-item type. Field numbering is scoped here and never shared."""

    entity_type: str
    custom_field_count: int = 0
    in_use: list[FieldUsage] = dc_field(default_factory=list)
    watchlist: list[FieldUsage] = dc_field(default_factory=list)
    samples: list[dict[str, Any]] = dc_field(default_factory=list)
    reason: str = ""
    detail: str = ""
    schema_revision: str = ""

    @property
    def needs_review(self) -> bool:
        return bool(self.in_use)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "needs_review": self.needs_review,
            "custom_field_count": self.custom_field_count,
            "in_use": [u.as_dict() for u in self.in_use],
            "watchlist": [u.as_dict() for u in self.watchlist],
            "samples": self.samples,
            "reason": self.reason,
            "detail": self.detail,
            "schema_revision": self.schema_revision,
        }


def is_filled(value: Any) -> bool:
    """TAPD encodes empty as '', None, or a bare separator."""
    if value is None:
        return False
    text = str(value).strip().strip(";").strip()
    return bool(text)


def normalize_schema(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep enabled custom fields only, in a stable shape."""
    out: list[dict[str, Any]] = []
    for raw in items or ():
        if str(raw.get("enabled", "1")) != "1":
            continue
        key = str(raw.get("custom_field") or "").strip()
        if not key:
            continue
        out.append(
            {
                "field": key,
                "label": str(raw.get("name") or "").strip(),
                "type": str(raw.get("type") or "").strip(),
                "modified": str(raw.get("modified") or "").strip(),
            }
        )
    out.sort(key=lambda f: f["field"])
    return out


def schema_revision(schema: Sequence[Mapping[str, Any]]) -> str:
    """Newest field-config modification time = the cheap drift signal."""
    stamps = [str(f.get("modified") or "") for f in schema]
    stamps = [s for s in stamps if s and not s.startswith("0000")]
    return max(stamps) if stamps else ""


def tier_for(filled: int, sampled: int) -> str:
    if sampled <= 0:
        return TIER_NO_SAMPLE
    if filled <= 0:
        return TIER_UNUSED
    return TIER_IN_USE if filled / sampled >= IN_USE_THRESHOLD else TIER_SPARSE


def measure_usage(
    entity_type: str,
    schema: Sequence[Mapping[str, Any]],
    entities: Sequence[Mapping[str, Any]],
) -> list[FieldUsage]:
    """Count real fill per field and attach one concrete piece of evidence."""
    title_key = TITLE_KEYS.get(entity_type, "name")
    sampled = len(entities)
    usages: list[FieldUsage] = []
    for spec in schema:
        key = spec["field"]
        filled = 0
        ev_id = ev_title = ev_value = ""
        for row in entities:
            if is_filled(row.get(key)):
                filled += 1
                if not ev_id:
                    ev_id = str(row.get("id") or "")
                    ev_title = str(row.get(title_key) or "")
                    ev_value = str(row.get(key) or "").strip().strip(";")
        usages.append(
            FieldUsage(
                field=key,
                label=spec.get("label", ""),
                field_type=spec.get("type", ""),
                filled=filled,
                sampled=sampled,
                tier=tier_for(filled, sampled),
                sample_entity_id=ev_id,
                sample_title=ev_title,
                sample_value=ev_value,
            )
        )
    return usages


def split_by_tier(
    usages: Sequence[FieldUsage],
) -> tuple[list[FieldUsage], list[FieldUsage]]:
    """Reviewable = anyone actually fills it AND we hold concrete evidence.

    A rarely-filled field still counts once a sample with a value is found
    (flow figure 2: 少数条有值 -> 找到有值条目就进待核). Only fields with zero
    fill, or with no obtainable sample, degrade to the watch list — and that
    degradation never blocks cold start.
    """
    reviewable = [
        u for u in usages if u.tier in (TIER_IN_USE, TIER_SPARSE) and u.sample_entity_id
    ]
    keys = {id(u) for u in reviewable}
    watch = [u for u in usages if id(u) not in keys]
    return reviewable, watch


def choose_samples(
    in_use: Sequence[FieldUsage],
    *,
    max_samples: int = MAX_SAMPLES,
) -> tuple[list[dict[str, Any]], list[FieldUsage]]:
    """Greedy cover: fewest entities that evidence the most in-use fields.

    Returns the chosen sample cards and any field left without a sample; the
    latter degrades to the watch list rather than blocking cold start.
    """
    remaining = {u.field: u for u in in_use if u.sample_entity_id}
    samples: list[dict[str, Any]] = []
    while remaining and len(samples) < max_samples:
        counts: dict[str, list[FieldUsage]] = {}
        for usage in remaining.values():
            counts.setdefault(usage.sample_entity_id, []).append(usage)
        best_id = max(counts, key=lambda eid: (len(counts[eid]), eid))
        covered = counts[best_id]
        samples.append(
            {
                "entity_id": best_id,
                "title": covered[0].sample_title,
                "rows": [
                    {
                        "label": u.business_name,
                        "value": u.sample_value,
                        "field": u.field,
                    }
                    for u in sorted(covered, key=lambda x: -x.filled)
                ],
            }
        )
        for usage in covered:
            remaining.pop(usage.field, None)
    uncovered = list(remaining.values())
    return samples, uncovered


def compare(
    baseline_domain: Mapping[str, Any],
    fresh_schema: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Field-position drift between a stored baseline and a fresh schema read."""
    fresh = {f["field"]: f.get("label", "") for f in fresh_schema}
    drift: list[dict[str, str]] = []
    recorded: set[str] = set()

    for entry in baseline_domain.get("in_use", []) + baseline_domain.get(
        "watchlist", []
    ):
        key = entry.get("field", "")
        recorded.add(key)
        was = entry.get("label", "")
        now = fresh.get(key)
        if now is None:
            drift.append({"field": key, "was": was, "now": "", "kind": "removed"})
        elif now != was:
            drift.append({"field": key, "was": was, "now": now, "kind": "renamed"})

    for key, label in fresh.items():
        if key not in recorded:
            drift.append({"field": key, "was": "", "now": label, "kind": "unrecorded"})
    return drift


def watchlist_recheck(
    watchlist: Sequence[Mapping[str, Any]],
    entities: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Watch-list fields that now carry a value and can be offered for inclusion."""
    ready: list[dict[str, str]] = []
    for entry in watchlist:
        key = entry.get("field", "")
        for row in entities:
            if is_filled(row.get(key)):
                ready.append(
                    {
                        "field": key,
                        "label": entry.get("label", ""),
                        "value": str(row.get(key) or "").strip().strip(";"),
                        "entity_id": str(row.get("id") or ""),
                    }
                )
                break
    return ready
