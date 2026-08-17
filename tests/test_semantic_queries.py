"""Business-query seams: confirmed semantics in, user-safe answers out."""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import semantics
import store
import user_context

TOKEN = "fixture-token-never-persisted"


class NeverReadCapability:
    def __init__(self) -> None:
        self.calls = []

    def read(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        raise AssertionError("fail-closed preconditions must run before TAPD reads")


class WorkflowCapability:
    def __init__(self) -> None:
        self.calls = []

    def read(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        if operation == "workflow.get":
            return SimpleNamespace(
                status="ok",
                data={
                    "record": {
                        "status_map": {"planning": "规划中", "testing": "测试中"}
                    }
                },
                page={"cursor": None, "has_more": False},
                evidence={"fetched_at": "2026-08-14T05:10:00Z"},
                error=None,
            )
        if operation == "entity.list":
            return SimpleNamespace(
                status="ok",
                data={
                    "items": [
                        {"id": "S1", "name": "新增测试旅程", "status": "planning"},
                        {"id": "S2", "name": "修复上传", "status": "testing"},
                    ]
                },
                page={"cursor": None, "has_more": False},
                evidence={"fetched_at": "2026-08-14T05:11:00Z"},
                error=None,
            )
        raise AssertionError(f"unexpected read: {operation}")


class ChangedWorkflowCapability(WorkflowCapability):
    def read(self, operation, payload):
        if operation == "workflow.get":
            self.calls.append((operation, dict(payload)))
            return SimpleNamespace(
                status="ok",
                data={
                    "record": {
                        "status_map": {
                            "planning": "规划中",
                            "testing": "测试中",
                            "done": "已完成",
                        }
                    }
                },
                page={"cursor": None, "has_more": False},
                evidence={"fetched_at": "2026-08-14T05:30:00Z"},
                error=None,
            )
        return super().read(operation, payload)


class PagedQueryCapability(WorkflowCapability):
    def read(self, operation, payload):
        if operation == "entity.list":
            self.calls.append((operation, dict(payload)))
            page = int(payload.get("page", 1))
            pages = {
                1: [
                    {
                        "id": "S1",
                        "name": "第一条",
                        "status": "planning",
                        "owner": "alice",
                        "modified": "2026-08-13 12:00:00",
                    },
                    {
                        "id": "S2",
                        "name": "第二条",
                        "status": "planning",
                        "owner": "bob;alice",
                        "modified": "2026-08-14 11:00:00",
                    },
                    {
                        "id": "X1",
                        "name": "已经测试",
                        "status": "testing",
                        "owner": "alice",
                        "modified": "2026-08-14 10:00:00",
                    },
                    {
                        "id": "X2",
                        "name": "别人的需求",
                        "status": "planning",
                        "owner": "bob",
                        "modified": "2026-08-14 09:00:00",
                    },
                ],
                2: [
                    {
                        "id": "S3",
                        "name": "第三条",
                        "status": "planning",
                        "owner": "alice",
                        "modified": "2026-08-14 12:00:00",
                    }
                ],
            }
            return SimpleNamespace(
                status="ok",
                data={"items": pages.get(page, [])},
                page={"cursor": "2" if page == 1 else None, "has_more": page == 1},
                evidence={"fetched_at": f"2026-08-14T05:2{page}:00Z"},
                error=None,
            )
        return super().read(operation, payload)


class SessionQueryCapability(PagedQueryCapability):
    def __init__(self, accessible_workspace="W1") -> None:
        super().__init__()
        self.accessible_workspace = accessible_workspace

    def read(self, operation, payload):
        if operation == "workspace.list_accessible":
            self.calls.append((operation, dict(payload)))
            return SimpleNamespace(
                status="ok",
                data={
                    "items": [
                        {
                            "id": self.accessible_workspace,
                            "name": "DeepTutor",
                            "category": "project",
                        }
                    ]
                },
                page={"cursor": None, "has_more": False},
                evidence={"fetched_at": "2026-08-14T05:19:00Z"},
                error=None,
            )
        return super().read(operation, payload)


class SemanticQueryCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._previous = os.environ.get(store.ENV_HOME)
        os.environ[store.ENV_HOME] = self._tmp.name
        semantics._PENDING_REVIEW.clear()

    def tearDown(self):
        if self._previous is None:
            os.environ.pop(store.ENV_HOME, None)
        else:
            os.environ[store.ENV_HOME] = self._previous
        self._tmp.cleanup()

    def save_baseline(self):
        return store.save(
            "W1",
            {
                "version": "1.1",
                "workspace_id": "W1",
                "workspace_name": "DeepTutor",
                "fetched_at": "2026-08-14T05:00:00Z",
                "domains": {},
            },
            TOKEN,
            confirmed_at="2026-08-14T05:00:00Z",
        )

    def confirm_unstarted_testing(self):
        capability = WorkflowCapability()
        semantics.semantic_review(
            capability,
            "W1",
            "未开始测试",
            token=TOKEN,
            values=["规划中"],
        )
        return semantics.semantic_confirm("W1", "未开始测试", token=TOKEN)

    def save_profile(self, workspace_id="W1"):
        return user_context.save_context(
            store.fingerprint(TOKEN),
            [{"id": workspace_id, "name": "DeepTutor", "category": "project"}],
            default_project="DeepTutor",
            tapd_identity="alice",
            business_role="测试负责人",
            projects_fetched_at="2026-08-14T05:00:00Z",
        )

    def test_missing_baseline_refuses_business_query_before_any_tapd_read(self):
        capability = NeverReadCapability()

        result = semantics.business_query(
            capability,
            "W1",
            "最近分配给我且未开始测试的需求",
            20,
            token=TOKEN,
        )

        self.assertEqual("needs_review", result["status"])
        self.assertFalse(result["可答"])
        self.assertEqual([], result["条目"])
        self.assertEqual("BASELINE_UNAVAILABLE", result["error"]["code"])
        self.assertIn("tapd_baseline_review", result["下一步"])
        self.assertEqual([], capability.calls)

    def test_unconfirmed_semantic_refuses_instead_of_falling_back_to_raw_list(self):
        self.save_baseline()
        capability = NeverReadCapability()

        result = semantics.business_query(
            capability,
            "W1",
            "最近分配给我且未开始测试的需求",
            20,
            token=TOKEN,
        )

        self.assertEqual("needs_review", result["status"])
        self.assertEqual("SEMANTIC_UNCONFIRMED", result["error"]["code"])
        self.assertIn("tapd_semantic_review", result["下一步"])
        self.assertEqual([], result["条目"])
        self.assertEqual([], capability.calls)

    def test_review_without_explicit_values_lists_exact_business_statuses_and_does_not_guess(
        self,
    ):
        self.save_baseline()
        capability = WorkflowCapability()

        result = semantics.semantic_review(
            capability,
            "W1",
            "未开始测试",
            token=TOKEN,
        )

        self.assertEqual("needs_input", result["status"])
        self.assertEqual(["测试中", "规划中"], result["可选状态"])
        self.assertEqual([], result["候选映射"])
        self.assertNotIn("planning", repr(result))
        self.assertEqual(
            [("workflow.get", {"workspace_id": "W1", "entity_type": "story"})],
            capability.calls,
        )

    def test_review_rejects_unknown_status_without_approximate_matching(self):
        self.save_baseline()
        capability = WorkflowCapability()

        result = semantics.semantic_review(
            capability,
            "W1",
            "未开始测试",
            token=TOKEN,
            values=["待测试"],
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("SEMANTIC_VALUE_UNKNOWN", result["error"]["code"])
        self.assertEqual([], result["条目"])
        self.assertEqual(1, len(capability.calls))

    def test_review_with_exact_status_builds_a_human_gate_card_from_real_items(self):
        self.save_baseline()
        capability = WorkflowCapability()

        result = semantics.semantic_review(
            capability,
            "W1",
            "未开始测试",
            token=TOKEN,
            values=["规划中"],
        )

        self.assertEqual("needs_confirmation", result["status"])
        self.assertEqual(
            [
                {
                    "工作项": "需求",
                    "栏位": "状态",
                    "取值": ["规划中"],
                    "来源": "工作流状态图",
                }
            ],
            result["候选映射"],
        )
        self.assertEqual("新增测试旅程", result["证据条目"][0]["标题"])
        self.assertEqual("规划中", result["证据条目"][0]["状态"])
        self.assertEqual("修复上传", result["对照条目"][0]["标题"])
        rendered = repr(result)
        self.assertNotIn("planning", rendered)
        self.assertNotIn("testing", rendered)

    def test_confirm_without_same_credential_review_is_refused(self):
        self.save_baseline()

        result = semantics.semantic_confirm("W1", "未开始测试", token=TOKEN)

        self.assertEqual("refused", result["status"])
        self.assertEqual("REVIEW_REQUIRED", result["error"]["code"])
        loaded = store.load("W1", TOKEN)
        self.assertNotIn("semantics", loaded.baseline["context"])

    def test_confirm_persists_version_1_2_semantics_in_the_existing_baseline(self):
        self.save_baseline()
        result = self.confirm_unstarted_testing()

        self.assertEqual("ok", result["status"])
        self.assertEqual("workspace-write", result["effect"])
        self.assertFalse(result["tapd_write"])
        loaded = store.load("W1", TOKEN)
        context = loaded.baseline["context"]
        self.assertEqual("1.2", context["version"])
        mapping = context["semantics"]["未开始测试"]
        self.assertEqual(["规划中"], mapping["display_values"])
        self.assertTrue(mapping["confirmed_at"])
        self.assertEqual(
            "human_reviewed_live_items", mapping["confirmation_basis"]["kind"]
        )
        self.assertEqual(["S1"], mapping["confirmation_basis"]["evidence_entity_ids"])
        self.assertTrue(mapping["confirmation_proof"])
        self.assertEqual("2026-08-14T05:00:00Z", loaded.baseline["confirmed_at"])
        self.assertNotIn("planning", repr(result))

    def test_query_requires_a_user_confirmed_profile_identity_before_tapd_reads(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        capability = NeverReadCapability()

        result = semantics.business_query(
            capability,
            "W1",
            "最近分配给我且未开始测试的需求",
            20,
            token=TOKEN,
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("USER_PROFILE_REQUIRED", result["error"]["code"])
        self.assertEqual([], result["条目"])
        self.assertEqual([], capability.calls)

    def test_query_blocks_when_the_confirmed_status_map_snapshot_has_drifted(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        self.save_profile()
        capability = ChangedWorkflowCapability()

        result = semantics.business_query(
            capability,
            "W1",
            "最近分配给我且未开始测试的需求",
            20,
            token=TOKEN,
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("STATUS_WORKFLOW_DRIFT", result["error"]["code"])
        self.assertEqual([], result["条目"])
        self.assertEqual(["workflow.get"], [call[0] for call in capability.calls])

    def test_query_blocks_an_unknown_workflow_shape_instead_of_reusing_old_semantics(
        self,
    ):
        self.save_baseline()
        self.confirm_unstarted_testing()
        self.save_profile()

        class UnknownWorkflowCapability(WorkflowCapability):
            def read(inner_self, operation, payload):
                if operation == "workflow.get":
                    inner_self.calls.append((operation, dict(payload)))
                    return SimpleNamespace(
                        status="ok",
                        data={"record": {"statuses": []}},
                        page={"cursor": None, "has_more": False},
                        evidence={"fetched_at": "2026-08-14T05:30:00Z"},
                        error=None,
                    )
                return super(UnknownWorkflowCapability, inner_self).read(
                    operation, payload
                )

        result = semantics.business_query(
            UnknownWorkflowCapability(),
            "W1",
            "最近分配给我且未开始测试的需求",
            20,
            token=TOKEN,
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("WORKFLOW_SCHEMA_UNSUPPORTED", result["error"]["code"])
        self.assertEqual([], result["条目"])

    def test_query_rejects_a_semantic_record_without_confirmation_proof_before_tapd_reads(
        self,
    ):
        self.save_baseline()
        self.confirm_unstarted_testing()
        self.save_profile()
        loaded = store.load("W1", TOKEN)
        mapping = loaded.baseline["context"]["semantics"]["未开始测试"]
        mapping.pop("confirmation_proof")
        store.save_semantic("W1", "未开始测试", mapping, TOKEN)
        capability = NeverReadCapability()

        result = semantics.business_query(
            capability,
            "W1",
            "最近分配给我且未开始测试的需求",
            20,
            token=TOKEN,
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("SEMANTIC_RECORD_INVALID", result["error"]["code"])
        self.assertEqual([], capability.calls)

    def test_query_rejects_signed_raw_display_mapping_inconsistent_with_workflow(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        self.save_profile()
        loaded = store.load("W1", TOKEN)
        mapping = loaded.baseline["context"]["semantics"]["未开始测试"]
        mapping["raw_values"] = ["testing"]
        mapping["confirmation_proof"] = semantics._confirmation_proof(mapping, TOKEN)
        store.save_semantic("W1", "未开始测试", mapping, TOKEN)
        capability = WorkflowCapability()

        result = semantics.business_query(
            capability,
            "W1",
            "最近分配给我且未开始测试的需求",
            20,
            token=TOKEN,
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("SEMANTIC_RECORD_INVALID", result["error"]["code"])
        self.assertEqual(["workflow.get"], [call[0] for call in capability.calls])

    def test_query_uses_confirmed_identity_and_semantics_then_reports_true_truncation(
        self,
    ):
        self.save_baseline()
        self.confirm_unstarted_testing()
        self.save_profile()
        capability = PagedQueryCapability()

        result = semantics.business_query(
            capability,
            "W1",
            "最近分配给我且未开始测试的需求",
            2,
            token=TOKEN,
        )

        self.assertEqual("ok", result["status"])
        self.assertTrue(result["可答"])
        self.assertEqual({"符合": 3, "返回": 2}, result["计数"])
        self.assertTrue(result["是否截断"])
        self.assertEqual(
            ["第三条", "第二条"], [item["标题"] for item in result["条目"]]
        )
        self.assertEqual(["规划中"], result["口径"][1]["取值"])
        self.assertIn("最近更新时间", result["口径"][2]["解释"])
        rendered = repr(result)
        self.assertNotIn("planning", rendered)
        self.assertNotIn("raw_values", rendered)
        self.assertNotIn("custom_field", rendered)
        list_calls = [
            payload for op, payload in capability.calls if op == "entity.list"
        ]
        self.assertEqual([1, 2], [payload["page"] for payload in list_calls])
        self.assertNotIn("owner", list_calls[0])
        self.assertNotIn("status", list_calls[0])
        self.assertIn("persistent_profile", repr(result))

    def test_session_context_answers_without_a_persistent_profile(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        claim = user_context.issue_session_context(
            TOKEN,
            workspace_id="W1",
            tapd_identity="alice",
            business_role="QA",
        )["session_context"]
        capability = SessionQueryCapability()

        result = semantics.business_query(
            capability,
            "W1",
            semantics.SUPPORTED_QUESTION,
            2,
            token=TOKEN,
            session_context=claim,
        )

        self.assertEqual("ok", result["status"])
        self.assertIn("session_context", repr(result))
        self.assertEqual(
            ["workspace.list_accessible", "workflow.get", "entity.list", "entity.list"],
            [operation for operation, _payload in capability.calls],
        )
        self.assertFalse(user_context.profile_path(store.fingerprint(TOKEN)).exists())

    def test_semantic_status_accepts_session_context_without_a_profile(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        claim = user_context.issue_session_context(
            TOKEN,
            workspace_id="W1",
            tapd_identity="alice",
            business_role="QA",
        )["session_context"]
        capability = SessionQueryCapability()

        result = semantics.semantic_status(
            capability,
            "W1",
            token=TOKEN,
            session_context=claim,
        )

        self.assertEqual("ok", result["status"])
        self.assertIn("session_context", repr(result))
        self.assertEqual(
            ["workspace.list_accessible", "workflow.get"],
            [operation for operation, _payload in capability.calls],
        )

    def test_invalid_session_contexts_are_rejected_before_any_tapd_read(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        current = user_context.issue_session_context(
            TOKEN,
            workspace_id="W1",
            tapd_identity="alice",
            business_role="QA",
        )["session_context"]
        expired = user_context.issue_session_context(
            TOKEN,
            workspace_id="W1",
            tapd_identity="alice",
            business_role="QA",
            now=0,
        )["session_context"]
        other_workspace = user_context.issue_session_context(
            TOKEN,
            workspace_id="W2",
            tapd_identity="alice",
            business_role="QA",
        )["session_context"]
        other_token = user_context.issue_session_context(
            "rotated-token",
            workspace_id="W1",
            tapd_identity="alice",
            business_role="QA",
        )["session_context"]
        cases = [
            (
                f"{current[:-1]}{'A' if current[-1] != 'A' else 'B'}",
                "SESSION_CONTEXT_INVALID",
            ),
            (expired, "SESSION_CONTEXT_EXPIRED"),
            (other_workspace, "SESSION_CONTEXT_WORKSPACE_MISMATCH"),
            (other_token, "SESSION_CONTEXT_INVALID"),
        ]
        for claim, expected in cases:
            with self.subTest(expected=expected):
                capability = NeverReadCapability()
                result = semantics.business_query(
                    capability,
                    "W1",
                    semantics.SUPPORTED_QUESTION,
                    20,
                    token=TOKEN,
                    session_context=claim,
                )
                self.assertEqual("blocked", result["status"])
                self.assertEqual(expected, result["error"]["code"])
                self.assertEqual([], capability.calls)

    def test_revoked_session_stops_after_project_scope_recheck(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        claim = user_context.issue_session_context(
            TOKEN,
            workspace_id="W1",
            tapd_identity="alice",
            business_role="QA",
        )["session_context"]
        capability = SessionQueryCapability(accessible_workspace="W2")

        result = semantics.business_query(
            capability,
            "W1",
            semantics.SUPPORTED_QUESTION,
            20,
            token=TOKEN,
            session_context=claim,
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("SESSION_CONTEXT_REVOKED", result["error"]["code"])
        self.assertEqual(
            ["workspace.list_accessible"],
            [operation for operation, _payload in capability.calls],
        )

    def test_query_refuses_when_pagination_metadata_is_missing(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        self.save_profile()

        class MissingPaginationCapability(PagedQueryCapability):
            def read(inner_self, operation, payload):
                result = super(MissingPaginationCapability, inner_self).read(
                    operation, payload
                )
                if operation == "entity.list":
                    result.page = {}
                return result

        result = semantics.business_query(
            MissingPaginationCapability(),
            "W1",
            "最近分配给我且未开始测试的需求",
            20,
            token=TOKEN,
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("PAGINATION_UNVERIFIED", result["error"]["code"])
        self.assertEqual([], result["条目"])

    def test_query_refuses_when_a_matching_modified_time_is_unparseable(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        self.save_profile()

        class InvalidModifiedCapability(WorkflowCapability):
            def read(inner_self, operation, payload):
                if operation == "entity.list":
                    inner_self.calls.append((operation, dict(payload)))
                    return SimpleNamespace(
                        status="ok",
                        data={
                            "items": [
                                {
                                    "id": "S1",
                                    "name": "无法排序",
                                    "status": "planning",
                                    "owner": "alice",
                                    "modified": "not-a-time",
                                }
                            ]
                        },
                        page={"cursor": None, "has_more": False},
                        evidence={"fetched_at": "2026-08-14T05:20:00Z"},
                        error=None,
                    )
                return super(InvalidModifiedCapability, inner_self).read(
                    operation, payload
                )

        result = semantics.business_query(
            InvalidModifiedCapability(),
            "W1",
            "最近分配给我且未开始测试的需求",
            20,
            token=TOKEN,
        )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("MODIFIED_TIME_UNVERIFIED", result["error"]["code"])
        self.assertEqual([], result["条目"])

    def test_semantic_status_reports_only_currently_answerable_business_questions(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        self.save_profile()
        capability = WorkflowCapability()

        result = semantics.semantic_status(capability, "W1", token=TOKEN)

        self.assertEqual("ok", result["status"])
        self.assertEqual(["最近分配给我且未开始测试的需求"], result["现在能答"])
        self.assertEqual([], result["现在不能答"])
        self.assertEqual("未开始测试", result["已确认谓词"][0]["谓词"])
        self.assertEqual(["规划中"], result["已确认谓词"][0]["取值"])
        rendered = repr(result)
        self.assertNotIn("planning", rendered)
        self.assertNotIn("credential", rendered)

    def test_semantic_status_blocks_a_malformed_confirmed_mapping(self):
        self.save_baseline()
        self.confirm_unstarted_testing()
        self.save_profile()
        loaded = store.load("W1", TOKEN)
        mapping = loaded.baseline["context"]["semantics"]["未开始测试"]
        mapping["display_values"] = "规划中"
        store.save_semantic("W1", "未开始测试", mapping, TOKEN)

        result = semantics.semantic_status(WorkflowCapability(), "W1", token=TOKEN)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("SEMANTIC_RECORD_INVALID", result["error"]["code"])
        self.assertEqual([], result["现在能答"])

    def test_mcp_adapter_exposes_four_business_tools_but_keeps_tapd_list_internal(self):
        tree = ast.parse(
            (_ROOT / "adapters" / "mcp_server.py").read_text(encoding="utf-8")
        )
        functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        public_tools = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
        }

        self.assertTrue(
            {
                "tapd_semantic_status",
                "tapd_semantic_review",
                "tapd_semantic_confirm",
                "tapd_business_query",
            }.issubset(functions)
        )
        self.assertTrue(
            {
                "tapd_semantic_status",
                "tapd_semantic_review",
                "tapd_semantic_confirm",
                "tapd_business_query",
            }.issubset(public_tools)
        )
        self.assertIn("tapd_list", functions)
        self.assertNotIn("tapd_list", public_tools)


if __name__ == "__main__":
    unittest.main()
