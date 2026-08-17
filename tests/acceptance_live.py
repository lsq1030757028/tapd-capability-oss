"""Live acceptance run against the real TAPD API.

Not collected by public CI on purpose. It needs an operator-supplied credential,
network access, and a complete JSON fixture in `TAPD_ACCEPTANCE_CONFIG_JSON`.
If the fixture is absent, the script exits as a safe skip before credential or
network access. See `tests/fixtures/acceptance_live.example.json` for the schema.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters"))
sys.path.insert(0, str(ROOT / "src"))

import mcp_server as srv  # noqa: E402
import store  # noqa: E402
import context as ctx  # noqa: E402
from baseline import compare, normalize_schema  # noqa: E402
from tapd_capability import READ_OPERATIONS, WRITE_OPERATIONS, TapdCapability  # noqa: E402
from transport_http import TapdHttpTransport  # noqa: E402

CONFIG_ENV = "TAPD_ACCEPTANCE_CONFIG_JSON"


class AcceptanceConfigError(ValueError):
    """The live-acceptance fixture is incomplete or malformed."""


def _require(mapping: dict, key: str, expected_type: type):
    value = mapping.get(key)
    if not isinstance(value, expected_type) or (expected_type is str and not value.strip()):
        raise AcceptanceConfigError(f"invalid required config key: {key}")
    return value


def load_acceptance_config() -> dict | None:
    """Load operator-supplied live expectations without touching TAPD."""
    raw = os.environ.get(CONFIG_ENV, "").strip()
    if not raw:
        return None
    try:
        config = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AcceptanceConfigError("acceptance config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise AcceptanceConfigError("acceptance config must be a JSON object")

    workspaces = _require(config, "workspaces", dict)
    for key in ("primary", "scoped", "sample"):
        _require(workspaces, key, str)

    review = _require(config, "review", dict)
    _require(review, "required_domains", list)
    for key in ("no_review_domain_count", "custom_field_total", "in_use_count", "watchlist_count", "sample_size"):
        _require(review, key, int)
    _require(review, "entity_type", str)
    comparisons = _require(review, "comparison_fields", list)
    if not comparisons or any(
        not isinstance(item, dict)
        or not isinstance(item.get("label"), str)
        or not isinstance(item.get("field"), str)
        for item in comparisons
    ):
        raise AcceptanceConfigError("review.comparison_fields must contain label/field objects")

    scoping = _require(config, "scoping", dict)
    for key in ("domain_a", "domain_b", "field", "expected_label_a", "expected_label_b"):
        _require(scoping, key, str)

    claims = _require(config, "legacy_claims", list)
    if not claims or any(
        not isinstance(item, dict)
        or not isinstance(item.get("field"), str)
        or not isinstance(item.get("label"), str)
        for item in claims
    ):
        raise AcceptanceConfigError("legacy_claims must contain field/label objects")

    drift = _require(config, "drift", dict)
    _require(drift, "renamed_count", int)
    _require(drift, "unrecorded_count", int)

    evidence = _require(config, "evidence", dict)
    _require(evidence, "domain", str)
    _require(evidence, "field_label", str)
    watchlist = _require(config, "watchlist", dict)
    _require(watchlist, "domain", str)
    return config

results: list[tuple[str, bool, str, str]] = []


def check(ac: str, desc: str, ok: bool, note: str = "") -> None:
    results.append((ac, ok, desc, note))


def cell(value: str) -> str:
    """TAPD terminates multi-value fields with a separator; the UI strips it."""
    return str(value or "").strip().strip(";").strip()


def main() -> int:
    try:
        config = load_acceptance_config()
    except AcceptanceConfigError:
        print("ERROR: invalid live-acceptance configuration; TAPD was not contacted")
        return 2
    if config is None:
        print(f"SKIP: {CONFIG_ENV} is not set; TAPD was not contacted")
        return 0

    workspaces = config["workspaces"]
    review_expected = config["review"]
    scoping_expected = config["scoping"]
    drift_expected = config["drift"]
    evidence_expected = config["evidence"]
    primary_workspace = workspaces["primary"]
    scoped_workspace = workspaces["scoped"]
    sample_workspace = workspaces["sample"]
    legacy_claims = [
        (item["field"], item["label"]) for item in config["legacy_claims"]
    ]

    capability = TapdCapability(TapdHttpTransport(srv._credential))

    # ---- AC1 bootstrap correctness -------------------------------------
    store.forget(primary_workspace)
    check("AC1", "清空后基线判为不可用", srv.tapd_baseline_status(primary_workspace)["基线"] == "不可用")

    card = srv.tapd_baseline_review(primary_workspace)
    needed = {r["工作项"] for r in card["需要你核对"]}
    check(
        "AC1③",
        "需核与无需核类别符合注入的验收基线",
        needed == set(review_expected["required_domains"])
        and len(card["无需核对"]) == review_expected["no_review_domain_count"],
    )

    domain_card = card["需要你核对"][0]
    check(
        "AC1②",
        f"字段分层符合注入基线：收 {domain_card['实际在用']} / 观察 {len(domain_card['不收的栏位'])}",
        domain_card["自定义栏位总数"] == review_expected["custom_field_total"]
        and domain_card["实际在用"] == review_expected["in_use_count"]
        and len(domain_card["不收的栏位"]) == review_expected["watchlist_count"],
    )

    sample = domain_card["样本"][0]
    entity_id = sample["链接"].rsplit("/", 1)[-1]
    live = capability.read(
        "entity.list",
        {
            "workspace_id": primary_workspace,
            "entity_type": review_expected["entity_type"],
            "id": entity_id,
            "fields": ",".join(
                ["id", "name", *(item["field"] for item in review_expected["comparison_fields"])]
            ),
        },
    ).data["items"]
    shown = {row["栏位"]: row["值"] for row in sample["逐栏"]}
    same = bool(live) and all(
        shown.get(label) == cell(live[0].get(key))
        for label, key in ((item["label"], item["field"]) for item in review_expected["comparison_fields"])
        if label in shown
    )
    check("AC1①", "样本展示值 == TAPD 实际值（同口径归一后）", same)
    srv.tapd_baseline_confirm(primary_workspace)

    # ---- AC1b entity scoping -------------------------------------------
    srv.tapd_baseline_review(scoped_workspace)
    srv.tapd_baseline_confirm(scoped_workspace)
    stored = json.loads(store.path_for(scoped_workspace).read_text(encoding="utf-8"))["context"]["domains"]

    def label_of(domain: str, key: str) -> str:
        entries = stored[domain]["in_use"] + stored[domain]["watchlist"]
        return next((e["label"] for e in entries if e["field"] == key), "")

    story_one, bug_one = label_of(scoping_expected["domain_a"], scoping_expected["field"]), label_of(scoping_expected["domain_b"], scoping_expected["field"])
    check(
        "AC1b",
        f"同号异义隔离符合注入基线：{story_one!r} / {bug_one!r}",
        story_one == scoping_expected["expected_label_a"]
        and bug_one == scoping_expected["expected_label_b"],
    )

    # ---- AC2 unfamiliar project ----------------------------------------
    unfamiliar = ctx.discover(capability, sample_workspace, sample_size=review_expected["sample_size"])
    reasons = {k: v.reason for k, v in unfamiliar.domains.items()}
    check(
        "AC2",
        f"陌生项目产出上下文或逐类诚实报因：{reasons}",
        all(bool(r) for r in reasons.values()),
    )

    # ---- AC3 drift regression target -----------------------------------
    fresh = normalize_schema(
        capability.read("schema.get", {"workspace_id": scoped_workspace, "entity_type": scoping_expected["domain_a"]}).data["items"]
    )
    drift = compare({"in_use": [{"field": k, "label": v} for k, v in legacy_claims], "watchlist": []}, fresh)
    renamed = [d for d in drift if d["kind"] == "renamed"]
    unrecorded = [d for d in drift if d["kind"] == "unrecorded"]
    check(
        "AC3",
        f"漂移计数符合注入基线：改名 {len(renamed)} / 漏记 {len(unrecorded)}",
        len(renamed) == drift_expected["renamed_count"]
        and len(unrecorded) == drift_expected["unrecorded_count"],
    )

    # ---- AC4 / AC4b evidence, not overwrite -----------------------------
    before = store.path_for(primary_workspace).read_text(encoding="utf-8")
    evidence = srv.tapd_field_evidence(primary_workspace, evidence_expected["domain"], evidence_expected["field_label"])
    after = store.path_for(primary_workspace).read_text(encoding="utf-8")
    check(
        "AC4",
        "声称读错后基线未被改写，且回带真实条目",
        before == after and evidence["基线是否被改"] == "否" and bool(evidence["证据"]),
    )
    human_facing = json.dumps(
        [srv.tapd_baseline_review(primary_workspace), srv.tapd_baseline_drift(primary_workspace), evidence],
        ensure_ascii=False,
    )
    check("AC4b", "面向人的三个工具输出零编号泄漏", "custom_field" not in human_facing)

    # ---- AC5 gate -------------------------------------------------------
    store.forget(sample_workspace)
    status = srv.tapd_baseline_status(sample_workspace)
    check("AC5", "无基线时明确不可用并给出下一步", status["基线"] == "不可用" and "下一步" in status)

    # ---- AC6 distributability ------------------------------------------
    stray = [p for p in ROOT.rglob("*") if p.is_file() and ("baselines" in str(p) or "credentials" in str(p))]
    check("AC6", "包目录内无基线/凭据残留", not stray)
    check(
        "AC6",
        "基线存储位于包目录之外",
        not str(store.baselines_dir().resolve()).startswith(str(ROOT.resolve())),
    )

    # ---- AC7 credential discipline --------------------------------------
    token = srv._credential()
    leaked = [
        p
        for p in ROOT.rglob("*")
        if p.is_file() and p.suffix in (".py", ".md", ".json", ".txt")
        and token in p.read_text(encoding="utf-8", errors="ignore")
    ]
    check("AC7", "包内任何文件不含令牌值", not leaked)
    check("AC7", "基线文件不含令牌值", token not in store.path_for(primary_workspace).read_text(encoding="utf-8"))

    # ---- AC9 / AC10 -----------------------------------------------------
    offline = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    check("AC9", "六形态 + 契约离线单测全绿", offline.returncode == 0)
    watch = srv.tapd_watchlist_recheck(primary_workspace, config["watchlist"]["domain"])
    check("AC10", "观察名单回补给出结论", watch["status"] == "ok" and bool(watch["说明"]))

    # ---- contract side ---------------------------------------------------
    writes = [
        TapdCapability(TapdHttpTransport(srv._credential)).read(op, {"workspace_id": "1"})
        for op in WRITE_OPERATIONS
    ]
    check(
        "契约C4",
        "全部写操作在触网前 fail closed",
        all(w.status == "failed" and w.error["code"] == "WRITE_NOT_IMPLEMENTED" for w in writes),
    )
    check("契约C3", f"读操作 {len(READ_OPERATIONS)} 个（v1 要求 9）", len(READ_OPERATIONS) == 9)

    # ---- report ----------------------------------------------------------
    width = 92
    print(f"{'AC':9s} {'结果':6s} 说明")
    print("-" * width)
    for ac, ok, desc, note in results:
        print(f"{ac:9s} {'PASS' if ok else 'FAIL':6s} {desc}" + (f"   << {note}" if note else ""))
    passed = sum(1 for r in results if r[1])
    print("-" * width)
    print(f"合计 {passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
