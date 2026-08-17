"""Non-sensitive TAPD user profile and deterministic project resolution."""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import store
import user_context

SECRET = "tenant-a-token-that-must-never-be-stored"
OTHER_SECRET = "tenant-b-token-that-must-never-be-stored"


def project(workspace_id: str, name: str, category: str = "project") -> dict:
    return {"id": workspace_id, "name": name, "category": category}


class FakeCapability:
    def __init__(self, items=None, *, status="ok"):
        self.items = [] if items is None else items
        self.status = status
        self.calls = []

    def read(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        if self.status != "ok":
            return SimpleNamespace(
                status="failed",
                data={},
                evidence={},
                error={"code": "AUTH_FAILED", "message": "credential rejected"},
            )
        return SimpleNamespace(
            status="ok",
            data={"items": self.items},
            evidence={"fetched_at": "2026-08-14T05:00:00Z", "source": "fixture"},
            error=None,
        )


class ContextCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._previous = os.environ.get(store.ENV_HOME)
        os.environ[store.ENV_HOME] = self._tmp.name
        self.fingerprint = store.fingerprint(SECRET)
        self.other_fingerprint = store.fingerprint(OTHER_SECRET)

    def tearDown(self):
        if self._previous is None:
            os.environ.pop(store.ENV_HOME, None)
        else:
            os.environ[store.ENV_HOME] = self._previous
        self._tmp.cleanup()

    @staticmethod
    def projects():
        return [project("W1", "DeepTutor"), project("W2", "萌伴 Web")]

    def save_default(self, default="DeepTutor"):
        return user_context.save_context(
            self.fingerprint,
            self.projects(),
            default_project=default,
            tapd_identity="测试负责人",
            business_role="QA 负责人",
            projects_fetched_at="2026-08-14T05:00:00Z",
        )


class ProjectDiscoveryTests(ContextCase):
    def test_reuses_the_existing_accessible_project_operation_only(self):
        capability = FakeCapability(
            [project("ORG", "公司", "organization"), project("W1", "DeepTutor")]
        )
        result, projects = user_context.fetch_accessible_projects(capability)
        self.assertEqual("ok", result.status)
        self.assertEqual(
            [("workspace.list_accessible", {"user": ""})], capability.calls
        )
        self.assertEqual(
            [{"id": "W1", "name": "DeepTutor", "category": "project"}], projects
        )

    def test_malformed_project_scope_is_rejected_instead_of_partially_guessed(self):
        capability = FakeCapability([{"id": "W1", "name": ""}])
        with self.assertRaises(user_context.ContextValidationError):
            user_context.fetch_accessible_projects(capability)

    def test_discovery_failure_is_returned_without_fabricating_projects(self):
        capability = FakeCapability(status="failed")
        result, projects = user_context.fetch_accessible_projects(capability)
        self.assertEqual("failed", result.status)
        self.assertEqual([], projects)
        self.assertEqual(
            [("workspace.list_accessible", {"user": ""})], capability.calls
        )


class ProfilePersistenceTests(ContextCase):
    def test_profile_contains_required_business_fields_and_verification_metadata(self):
        result = self.save_default()
        self.assertEqual("ok", result["status"])
        self.assertEqual("workspace-write", result["effect"])
        self.assertFalse(result["tapd_write"])
        self.assertEqual("DeepTutor", result["saved"]["default_project_name"])
        self.assertEqual("测试负责人", result["saved"]["tapd_identity"])
        self.assertEqual("QA 负责人", result["saved"]["business_role"])
        self.assertEqual(2, result["verification"]["accessible_project_count"])
        self.assertEqual(
            "workspace.list_accessible",
            result["verification"]["project_scope_source"],
        )
        self.assertEqual("user_confirmed", result["verification"]["identity_basis"])
        self.assertTrue(result["verification"]["verified_at"])

    def test_profile_is_outside_package_and_never_contains_token_or_returns_fingerprint(
        self,
    ):
        result = self.save_default()
        target = user_context.profile_path(self.fingerprint).resolve()
        self.assertFalse(str(target).startswith(str(_ROOT.resolve())))
        serialized = target.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn(self.fingerprint, json.dumps(result, ensure_ascii=False))
        self.assertNotIn(str(target), json.dumps(result, ensure_ascii=False))

    def test_another_token_fingerprint_cannot_read_the_profile(self):
        self.save_default()
        own = user_context.load_profile(self.fingerprint)
        foreign = user_context.load_profile(self.other_fingerprint)
        self.assertEqual("ok", own.status)
        self.assertEqual("missing", foreign.status)

    def test_inaccessible_default_is_refused_without_creating_a_profile(self):
        result = user_context.save_context(
            self.fingerprint,
            self.projects(),
            default_project="不存在的项目",
            tapd_identity="测试负责人",
            business_role="QA 负责人",
            projects_fetched_at="2026-08-14T05:00:00Z",
        )
        self.assertEqual("failed", result["status"])
        self.assertEqual("PROJECT_NOT_ACCESSIBLE", result["error"]["code"])
        self.assertFalse(user_context.profile_path(self.fingerprint).exists())

    def test_invalid_business_input_is_rejected_without_writing(self):
        invalid_rows = [
            {
                "default_project": "",
                "tapd_identity": "测试负责人",
                "business_role": "QA",
            },
            {
                "default_project": "DeepTutor",
                "tapd_identity": "",
                "business_role": "QA",
            },
            {
                "default_project": "DeepTutor",
                "tapd_identity": "测试\n负责人",
                "business_role": "QA",
            },
            {
                "default_project": "DeepTutor",
                "tapd_identity": "测试负责人",
                "business_role": " ",
            },
            {
                "default_project": "DeepTutor",
                "tapd_identity": "测试负责人",
                "business_role": "x" * 121,
            },
        ]
        for row in invalid_rows:
            with self.subTest(row=row):
                result = user_context.save_context(
                    self.fingerprint,
                    self.projects(),
                    projects_fetched_at="2026-08-14T05:00:00Z",
                    **row,
                )
                self.assertEqual("VALIDATION_FAILED", result["error"]["code"])
                self.assertFalse(user_context.profile_path(self.fingerprint).exists())

    def test_local_write_failure_is_normalized_without_path_or_fingerprint(self):
        sentinel = f"C:/private/{self.fingerprint}/.profile.sentinel.tmp"
        original = user_context._atomic_save
        user_context._atomic_save = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(sentinel)
        )
        try:
            result = self.save_default()
        finally:
            user_context._atomic_save = original
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertEqual("failed", result["status"])
        self.assertEqual("PROFILE_WRITE_FAILED", result["error"]["code"])
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn(self.fingerprint, rendered)


class ResolutionTests(ContextCase):
    def test_explicit_project_overrides_saved_default(self):
        self.save_default("萌伴 Web")
        result = user_context.resolve_context(
            self.fingerprint, self.projects(), project_hint="DeepTutor"
        )
        self.assertEqual("W1", result["resolution"]["workspace_id"])
        self.assertEqual("explicit", result["resolution"]["source"])

    def test_saved_default_precedes_the_unique_fallback(self):
        self.save_default("DeepTutor")
        result = user_context.resolve_context(self.fingerprint, self.projects())
        self.assertEqual("W1", result["resolution"]["workspace_id"])
        self.assertEqual("saved_default", result["resolution"]["source"])

    def test_unique_accessible_project_is_selected_without_a_profile(self):
        result = user_context.resolve_context(
            self.fingerprint, [project("W1", "DeepTutor")]
        )
        self.assertEqual("W1", result["resolution"]["workspace_id"])
        self.assertEqual("unique_accessible", result["resolution"]["source"])

    def test_multiple_projects_return_business_names_for_confirmation(self):
        result = user_context.resolve_context(self.fingerprint, self.projects())
        self.assertEqual("needs_confirmation", result["status"])
        self.assertEqual(["DeepTutor", "萌伴 Web"], result["project_options"])
        self.assertNotIn("W1", json.dumps(result, ensure_ascii=False))

    def test_no_accessible_project_fails_closed(self):
        result = user_context.resolve_context(self.fingerprint, [])
        self.assertEqual("blocked", result["status"])
        self.assertEqual("NO_ACCESSIBLE_PROJECTS", result["error"]["code"])

    def test_unknown_explicit_project_does_not_fall_back(self):
        self.save_default("DeepTutor")
        result = user_context.resolve_context(
            self.fingerprint, self.projects(), project_hint="另一个项目"
        )
        self.assertEqual("needs_confirmation", result["status"])
        self.assertEqual("PROJECT_NOT_ACCESSIBLE", result["error"]["code"])
        self.assertNotIn("resolution", result)

    def test_revoked_saved_default_is_blocked_not_replaced_by_the_remaining_project(
        self,
    ):
        self.save_default("DeepTutor")
        result = user_context.resolve_context(
            self.fingerprint, [project("W2", "萌伴 Web")]
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("SAVED_DEFAULT_OUT_OF_SCOPE", result["error"]["code"])
        self.assertNotIn("resolution", result)

    def test_same_business_name_in_two_projects_requires_confirmation(self):
        result = user_context.resolve_context(
            self.fingerprint,
            [project("W1", "DeepTutor"), project("W2", "DeepTutor")],
            project_hint="DeepTutor",
        )
        self.assertEqual("needs_confirmation", result["status"])
        self.assertEqual("PROJECT_AMBIGUOUS", result["error"]["code"])

    def test_corrupt_profile_fails_closed(self):
        target = user_context.profile_path(self.fingerprint)
        target.parent.mkdir(parents=True)
        target.write_text("not-json", encoding="utf-8")
        result = user_context.resolve_context(
            self.fingerprint, [project("W1", "DeepTutor")]
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("PROFILE_UNREADABLE", result["error"]["code"])


class SessionContextTests(ContextCase):
    def test_only_this_time_issues_a_verified_claim_without_profile_write(self):
        result = user_context.resolve_context(
            self.fingerprint,
            self.projects(),
            project_hint="DeepTutor",
            tapd_identity="alice",
            business_role="QA",
            credential_token=SECRET,
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual("W1", result["resolution"]["workspace_id"])
        self.assertEqual("user_confirmed", result["session_context_source"])
        self.assertEqual(900, result["session_context_ttl_seconds"])
        claim = user_context.verify_session_context(
            SECRET,
            result["session_context"],
            expected_workspace="W1",
        )
        self.assertEqual("alice", claim["tapd_identity"])
        self.assertEqual("QA", claim["business_role"])
        self.assertFalse(user_context.profile_path(self.fingerprint).exists())

    def test_temporary_project_override_does_not_replace_saved_default(self):
        self.save_default("DeepTutor")
        result = user_context.resolve_context(
            self.fingerprint,
            self.projects(),
            project_hint="W2",
            tapd_identity="alice",
            business_role="QA",
            credential_token=SECRET,
        )

        self.assertEqual("W2", result["resolution"]["workspace_id"])
        claim = user_context.verify_session_context(
            SECRET,
            result["session_context"],
            expected_workspace="W2",
        )
        self.assertEqual("W2", claim["workspace_id"])
        saved = user_context.load_profile(self.fingerprint).profile or {}
        self.assertEqual("W1", saved["default_project"]["id"])

    def test_claim_requires_identity_role_token_and_matching_fingerprint(self):
        cases = [
            {"tapd_identity": "alice", "business_role": "", "credential_token": SECRET},
            {"tapd_identity": "", "business_role": "QA", "credential_token": SECRET},
            {"tapd_identity": "alice", "business_role": "QA", "credential_token": ""},
            {
                "tapd_identity": "alice",
                "business_role": "QA",
                "credential_token": OTHER_SECRET,
            },
        ]
        for inputs in cases:
            with self.subTest(inputs=inputs):
                result = user_context.resolve_context(
                    self.fingerprint,
                    self.projects(),
                    project_hint="W1",
                    **inputs,
                )
                self.assertEqual("blocked", result["status"])
                self.assertEqual(
                    "SESSION_CONTEXT_INPUT_INVALID", result["error"]["code"]
                )
                self.assertFalse(user_context.profile_path(self.fingerprint).exists())

    def test_tamper_token_rotation_expiry_and_workspace_mismatch_fail_closed(self):
        issued = user_context.issue_session_context(
            SECRET,
            workspace_id="W1",
            tapd_identity="alice",
            business_role="QA",
            now=100,
        )["session_context"]
        cases = [
            (
                SECRET,
                f"{issued[:-1]}{'A' if issued[-1] != 'A' else 'B'}",
                "W1",
                101,
                "SESSION_CONTEXT_INVALID",
            ),
            (OTHER_SECRET, issued, "W1", 101, "SESSION_CONTEXT_INVALID"),
            (SECRET, issued, "W1", 1000, "SESSION_CONTEXT_EXPIRED"),
            (SECRET, issued, "W2", 101, "SESSION_CONTEXT_WORKSPACE_MISMATCH"),
        ]
        for token, claim, workspace, now, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(user_context.SessionContextError) as caught:
                    user_context.verify_session_context(
                        token,
                        claim,
                        expected_workspace=workspace,
                        now=now,
                    )
                self.assertEqual(expected, caught.exception.code)


class StatusTests(ContextCase):
    def test_missing_status_suggests_business_options_without_internal_ids(self):
        result = user_context.context_status(self.fingerprint, self.projects())
        self.assertEqual("needs_input", result["status"])
        self.assertEqual("missing", result["profile_state"])
        self.assertEqual(["DeepTutor", "萌伴 Web"], result["accessible_project_names"])
        self.assertNotIn("W1", json.dumps(result, ensure_ascii=False))

    def test_missing_status_with_no_accessible_project_is_blocked(self):
        result = user_context.context_status(self.fingerprint, [])
        self.assertEqual("blocked", result["status"])
        self.assertEqual("NO_ACCESSIBLE_PROJECTS", result["error"]["code"])

    def test_ready_status_returns_only_non_sensitive_profile_fields(self):
        self.save_default()
        result = user_context.context_status(self.fingerprint, self.projects())
        self.assertEqual("ok", result["status"])
        self.assertEqual("ready", result["profile_state"])
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(self.fingerprint, rendered)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("credential_fingerprint", rendered)

    def test_status_marks_a_revoked_default_stale(self):
        self.save_default()
        result = user_context.context_status(
            self.fingerprint, [project("W2", "萌伴 Web")]
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("stale", result["profile_state"])
        self.assertEqual("SAVED_DEFAULT_OUT_OF_SCOPE", result["error"]["code"])


class AdapterContractStructureTests(unittest.TestCase):
    def test_all_three_context_functions_are_public_mcp_tools(self):
        source = (_ROOT / "adapters" / "mcp_server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        decorated = set()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "mcp"
                    and decorator.func.attr == "tool"
                ):
                    decorated.add(node.name)
        self.assertTrue(
            {"tapd_context_status", "tapd_context_save", "tapd_context_resolve"}
            <= decorated
        )


class CiContractTests(unittest.TestCase):
    def test_ci_installs_and_import_checks_pinned_mcp_before_the_suite(self):
        workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        requirements = (_ROOT / "adapters" / "requirements.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("--requirement adapters/requirements.txt", workflow)
        self.assertIn("from mcp.server.fastmcp import FastMCP", workflow)
        self.assertRegex(requirements, r"(?m)^mcp==\d+\.\d+\.\d+$")

    def test_readme_matches_runtime_host_secret_resolver_and_dynamic_suite(self):
        readme = (_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("默认 `127.0.0.1:3796`", readme)
        self.assertNotIn("默认 `0.0.0.0:3796`", readme)
        self.assertIn(
            '"Authorization": "${secret:tapd-capability/header.Authorization}"',
            readme,
        )
        self.assertIn("`${secret:tapd-capability/token}`", readme)
        self.assertNotIn('"Authorization": "Bearer ${secret:', readme)
        self.assertIn('value_template: "Bearer {value}"', readme)
        self.assertNotRegex(readme, r"tests`（\d+ 离线测试")


if __name__ == "__main__":
    unittest.main()
