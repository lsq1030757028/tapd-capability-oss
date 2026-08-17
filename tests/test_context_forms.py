"""Cold start must survive every missing form (spec AC9) and stay entity-scoped."""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "tests"))

from fixture_transport import FixtureTransport, failure, field  # noqa: E402

import context as ctx  # noqa: E402
from baseline import TIER_NO_SAMPLE, TIER_UNUSED, compare, normalize_schema  # noqa: E402
from tapd_capability import TapdCapability  # noqa: E402


def _rows(n, **overrides):
    out = []
    for i in range(n):
        row = {"id": f"100{i}", "name": f"需求 {i}"}
        row.update({k: (v[i] if isinstance(v, list) else v) for k, v in overrides.items()})
        out.append(row)
    return out


class MissingFormTests(unittest.TestCase):
    """Six shapes of "missing"; none may block cold start."""

    def _discover(self, transport, entity_types=("story",)):
        return ctx.discover(
            TapdCapability(transport), "W1", sample_size=20, entity_types=entity_types
        )

    def test_form1_no_custom_fields_needs_no_review(self):
        transport = FixtureTransport().set_schema("story", [])
        domain = self._discover(transport).domains["story"]
        self.assertFalse(domain.needs_review)
        self.assertEqual("no_custom_fields", domain.reason)

    def test_form2_no_data_defers_without_blocking(self):
        transport = FixtureTransport()
        transport.set_schema("story", [field("custom_field_1", "测试人员")]).set_entities("story", [])
        domain = self._discover(transport).domains["story"]
        self.assertFalse(domain.needs_review)
        self.assertEqual("no_data", domain.reason)
        self.assertIn("暂无数据", domain.detail)

    def test_form3_unavailable_is_stated_not_substituted(self):
        transport = FixtureTransport().set_schema("story", failure("AUTH_FAILED", "no permission"))
        domain = self._discover(transport).domains["story"]
        self.assertEqual("unavailable", domain.reason)
        self.assertIn("AUTH_FAILED", domain.detail)
        self.assertEqual([], domain.in_use)

    def test_form4_all_empty_goes_to_watchlist(self):
        transport = FixtureTransport()
        transport.set_schema("story", [field("custom_field_1", "技术负责人")])
        transport.set_entities("story", _rows(20, custom_field_1=""))
        domain = self._discover(transport).domains["story"]
        self.assertFalse(domain.needs_review)
        self.assertEqual(1, len(domain.watchlist))
        self.assertEqual(TIER_UNUSED, domain.watchlist[0].tier)

    def test_form5_sparse_field_is_reviewable_when_evidence_exists(self):
        values = [""] * 19 + ["安可聪"]
        transport = FixtureTransport()
        transport.set_schema("story", [field("custom_field_1", "终端开发")])
        transport.set_entities("story", _rows(20, custom_field_1=values))
        domain = self._discover(transport).domains["story"]
        self.assertTrue(domain.needs_review)
        usage = domain.in_use[0]
        self.assertEqual(1, usage.filled)
        self.assertEqual("安可聪", usage.sample_value)

    def test_form6_uncovered_fields_degrade_instead_of_blocking(self):
        # Three fields, each evidenced by a different entity: two samples cannot cover all.
        schema = [field(f"custom_field_{i}", f"角色{i}") for i in (1, 2, 3)]
        rows = _rows(3)
        rows[0]["custom_field_1"] = "甲"
        rows[1]["custom_field_2"] = "乙"
        rows[2]["custom_field_3"] = "丙"
        transport = FixtureTransport().set_schema("story", schema).set_entities("story", rows)
        domain = self._discover(transport).domains["story"]
        self.assertEqual(2, len(domain.samples))
        self.assertEqual(2, len(domain.in_use))
        degraded = [w for w in domain.watchlist if w.tier == TIER_NO_SAMPLE]
        self.assertEqual(1, len(degraded))

    def test_form4b_fields_exist_but_nobody_fills_them_still_states_a_reason(self):
        """Not one of the six forms, and the easiest place to leave a blank."""
        transport = FixtureTransport()
        transport.set_schema("story", [field("custom_field_1", "驱动组")])
        transport.set_entities("story", _rows(20, custom_field_1=""))
        domain = self._discover(transport).domains["story"]
        self.assertFalse(domain.needs_review)
        self.assertEqual("all_unused", domain.reason)
        self.assertTrue(domain.detail)
        card = ctx.build_review_card(
            self._discover(transport)
        )
        self.assertTrue(card["无需核对"][0]["为什么不用核"])

    def test_all_forms_still_yield_a_usable_context(self):
        transport = FixtureTransport()
        transport.set_schema("story", [field("custom_field_1", "测试人员")])
        transport.set_entities("story", _rows(20, custom_field_1="张三"))
        transport.set_schema("bug", [])
        transport.set_entities("task", [])
        transport.set_schema("task", [field("custom_field_1", "处理人")])
        transport.set_schema("iteration", failure("NETWORK_FAILED"))
        context = self._discover(transport, entity_types=("story", "bug", "task", "iteration"))
        self.assertEqual(4, len(context.domains))
        self.assertEqual(["story"], [d.entity_type for d in context.domains_needing_review])
        card = ctx.build_review_card(context)
        self.assertEqual(1, len(card["需要你核对"]))
        self.assertEqual(3, len(card["无需核对"]))
        for skipped in card["无需核对"]:
            self.assertTrue(skipped["为什么不用核"], "每个跳过的类都必须给出原因")


class EntityScopingTests(unittest.TestCase):
    """The same number means different things per work-item type."""

    def test_identical_number_is_not_shared_across_entity_types(self):
        transport = FixtureTransport()
        transport.set_schema("story", [field("custom_field_one", "需求来源方")])
        transport.set_entities("story", _rows(20, custom_field_one="经营督导部"))
        transport.set_schema("bug", [field("custom_field_one", "bug等级", ftype="select")])
        transport.set_entities("bug", [{"id": "b1", "title": "缺陷", "custom_field_one": "严重"}])
        context = ctx.discover(
            TapdCapability(transport), "fixture-workspace-context", sample_size=20, entity_types=("story", "bug")
        )
        story_label = context.domains["story"].in_use[0].business_name
        bug_label = context.domains["bug"].in_use[0].business_name
        self.assertEqual("需求来源方", story_label)
        self.assertEqual("bug等级", bug_label)
        self.assertNotEqual(story_label, bug_label)


class DriftTests(unittest.TestCase):
    def test_rename_removal_and_unrecorded_are_all_reported(self):
        stored = {
            "in_use": [
                {"field": "custom_field_one", "label": "业务分类"},
                {"field": "custom_field_gone", "label": "已删栏"},
                {"field": "custom_field_six", "label": "需求类型"},
            ],
            "watchlist": [],
        }
        fresh = normalize_schema(
            [
                field("custom_field_one", "需求来源方"),
                field("custom_field_six", "需求类型"),
                field("custom_field_new", "驱动组"),
            ]
        )
        kinds = {d["field"]: d["kind"] for d in compare(stored, fresh)}
        self.assertEqual("renamed", kinds["custom_field_one"])
        self.assertEqual("removed", kinds["custom_field_gone"])
        self.assertEqual("unrecorded", kinds["custom_field_new"])
        self.assertNotIn("custom_field_six", kinds, "未变动的栏位不应报漂移")


class NoFieldNumberLeakTests(unittest.TestCase):
    """spec AC4b: nothing shown to a person may carry an internal field number."""

    def test_review_card_contains_no_internal_field_identifier(self):
        transport = FixtureTransport()
        transport.set_schema("story", [field("custom_field_10", "测试人员")])
        transport.set_entities("story", _rows(20, custom_field_10="张三"))
        context = ctx.discover(
            TapdCapability(transport), "W1", sample_size=20, entity_types=("story",)
        )
        rendered = repr(ctx.build_review_card(context))
        self.assertNotIn("custom_field", rendered)
        self.assertIn("测试人员", rendered)


if __name__ == "__main__":
    unittest.main()
