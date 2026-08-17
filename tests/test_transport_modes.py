"""Transport selection and per-request credentials (stdio vs streamable HTTP).

The load-bearing assertion in this file is the *negative* one: in HTTP mode a
call that brings no credential must fail, even when the process environment
holds a perfectly good token. Serving it would be lateral privilege escalation
— one tenant's request answered with another's key.

The credential rules live in ``src/credentials.py``, which imports no MCP
package, so most of this file runs anywhere. The adapter-level tests skip when
``mcp`` is not installed (CI's offline job installs pytest only).
"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import credentials
import store
import user_context
from credentials import CredentialError

SECRET = "tenant-a-token-value"
OTHER_SECRET = "tenant-b-token-value"

_ADAPTER = _ROOT / "adapters" / "mcp_server.py"

EXPECTED_TOOLS = {
    "tapd_projects",
    "tapd_workspace",
    "tapd_field_config",
    "tapd_probe",
    "tapd_baseline_status",
    "tapd_baseline_review",
    "tapd_baseline_confirm",
    "tapd_baseline_drift",
    "tapd_watchlist_recheck",
    "tapd_field_evidence",
    "tapd_context_status",
    "tapd_context_save",
    "tapd_context_resolve",
    "tapd_semantic_status",
    "tapd_semantic_review",
    "tapd_semantic_confirm",
    "tapd_business_query",
}


def _mcp_available() -> bool:
    try:
        return (
            importlib.util.find_spec("mcp") is not None
            and importlib.util.find_spec("mcp.server.fastmcp") is not None
        )
    except ModuleNotFoundError:
        return False


def _load_adapter(module_name: str, argv: list[str]):
    """Import adapters/mcp_server.py afresh with a chosen command line."""
    spec = importlib.util.spec_from_file_location(module_name, _ADAPTER)
    module = importlib.util.module_from_spec(spec)
    original = sys.argv
    sys.argv = ["mcp_server.py", *argv]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = original
    return module


class RuntimeResolutionTests(unittest.TestCase):
    """Default is stdio; nothing accidental may move it."""

    def test_nothing_configured_means_stdio(self):
        runtime = credentials.resolve_runtime([], {})
        self.assertEqual(credentials.TRANSPORT_STDIO, runtime.transport)
        self.assertFalse(runtime.is_http)

    def test_unrelated_flags_do_not_change_the_transport(self):
        runtime = credentials.resolve_runtime(["-m", "pytest", "-q", "--color=yes"], {})
        self.assertEqual(credentials.TRANSPORT_STDIO, runtime.transport)

    def test_environment_selects_http(self):
        runtime = credentials.resolve_runtime([], {credentials.ENV_TRANSPORT: "http"})
        self.assertEqual(credentials.TRANSPORT_HTTP, runtime.transport)
        self.assertTrue(runtime.is_http)

    def test_command_line_selects_http_in_both_spellings(self):
        for argv in (["--transport", "http"], ["--transport=streamable-http"]):
            with self.subTest(argv=argv):
                self.assertTrue(credentials.resolve_runtime(argv, {}).is_http)

    def test_command_line_overrides_environment(self):
        runtime = credentials.resolve_runtime(
            ["--transport", "stdio"], {credentials.ENV_TRANSPORT: "http"}
        )
        self.assertEqual(credentials.TRANSPORT_STDIO, runtime.transport)

    def test_host_and_port_defaults_and_overrides(self):
        default = credentials.resolve_runtime([], {})
        self.assertEqual(credentials.DEFAULT_HTTP_HOST, default.host)
        self.assertEqual(credentials.DEFAULT_HTTP_PORT, default.port)

        chosen = credentials.resolve_runtime(
            ["--transport", "http", "--host", "127.0.0.1", "--port", "9123"], {}
        )
        self.assertEqual("127.0.0.1", chosen.host)
        self.assertEqual(9123, chosen.port)
        self.assertEqual("http://127.0.0.1:9123/mcp", chosen.endpoint)

        from_env = credentials.resolve_runtime(
            [], {credentials.ENV_HOST: "10.0.0.4", credentials.ENV_PORT: "9124"}
        )
        self.assertEqual("10.0.0.4", from_env.host)
        self.assertEqual(9124, from_env.port)

    def test_explicitly_wrong_values_are_rejected_loudly(self):
        with self.assertRaises(ValueError):
            credentials.resolve_runtime(["--transport", "grpc"], {})
        with self.assertRaises(ValueError):
            credentials.resolve_runtime(["--port", "not-a-port"], {})
        with self.assertRaises(ValueError):
            credentials.resolve_runtime(["--port", "70000"], {})


class HeaderCredentialTests(unittest.TestCase):
    """One request, one credential — or an explicit refusal."""

    def test_bearer_header_yields_the_token(self):
        self.assertEqual(
            SECRET,
            credentials.token_from_headers({"authorization": f"Bearer {SECRET}"}),
        )

    def test_header_name_and_scheme_are_case_insensitive(self):
        self.assertEqual(
            SECRET,
            credentials.token_from_headers({"Authorization": f"bEaReR {SECRET}"}),
        )

    def test_alternate_header_is_accepted(self):
        self.assertEqual(
            SECRET, credentials.token_from_headers({"X-TAPD-Access-Token": SECRET})
        )

    def test_no_credential_header_is_refused_with_an_actionable_message(self):
        with self.assertRaises(CredentialError) as caught:
            credentials.token_from_headers({"content-type": "application/json"})
        message = str(caught.exception)
        self.assertIn("Authorization", message)
        self.assertIn(credentials.ALTERNATE_HEADER, message)

    def test_empty_headers_are_refused(self):
        for headers in ({}, None):
            with self.subTest(headers=headers), self.assertRaises(CredentialError):
                credentials.token_from_headers(headers)

    def test_wrong_scheme_is_refused(self):
        with self.assertRaises(CredentialError):
            credentials.token_from_headers({"authorization": "Basic Zm9vOmJhcg=="})

    def test_bearer_without_a_value_is_refused(self):
        with self.assertRaises(CredentialError):
            credentials.token_from_headers({"authorization": "Bearer   "})

    def test_a_malformed_header_never_echoes_its_contents(self):
        """The commonest malformation is the raw token with the scheme omitted."""
        with self.assertRaises(CredentialError) as caught:
            credentials.token_from_headers({"authorization": SECRET})
        self.assertNotIn(SECRET, str(caught.exception))


class EnvironmentCredentialTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_environment_variable_is_used(self):
        self.assertEqual(
            SECRET,
            credentials.token_from_environment(
                {credentials.ENV_TOKEN: SECRET}, self.home
            ),
        )

    def test_credential_file_is_the_fallback(self):
        target = self.home / "credentials" / "tapd_access_token"
        target.parent.mkdir(parents=True)
        target.write_text(f"{SECRET}\n", encoding="utf-8")
        self.assertEqual(SECRET, credentials.token_from_environment({}, self.home))

    def test_absence_names_both_places_to_put_it(self):
        with self.assertRaises(CredentialError) as caught:
            credentials.token_from_environment({}, self.home)
        message = str(caught.exception)
        self.assertIn(credentials.ENV_TOKEN, message)
        self.assertIn("tapd_access_token", message)


class BaselineNamespaceTests(unittest.TestCase):
    """Multi-tenant storage: same workspace, two people, two files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._previous = store.os.environ.get(store.ENV_HOME)
        store.os.environ[store.ENV_HOME] = self._tmp.name

    def tearDown(self):
        if self._previous is None:
            store.os.environ.pop(store.ENV_HOME, None)
        else:
            store.os.environ[store.ENV_HOME] = self._previous
        self._tmp.cleanup()

    def test_single_tenant_layout_is_unchanged(self):
        self.assertEqual(store.baselines_dir() / "W1.json", store.path_for("W1"))

    def test_two_tenants_do_not_share_one_file(self):
        a = store.fingerprint(SECRET)
        b = store.fingerprint(OTHER_SECRET)
        self.assertNotEqual(a, b)

        store.save(
            "W1", {"domains": {"story": "a"}}, SECRET, confirmed_at="1", namespace=a
        )
        store.save(
            "W1",
            {"domains": {"story": "b"}},
            OTHER_SECRET,
            confirmed_at="2",
            namespace=b,
        )

        self.assertNotEqual(store.path_for("W1", a), store.path_for("W1", b))
        first = store.load("W1", SECRET, a)
        second = store.load("W1", OTHER_SECRET, b)
        self.assertTrue(first.usable and second.usable)
        self.assertEqual({"story": "a"}, first.baseline["context"]["domains"])
        self.assertEqual({"story": "b"}, second.baseline["context"]["domains"])

    def test_namespaced_file_still_carries_no_token(self):
        namespace = store.fingerprint(SECRET)
        store.save("W1", {"domains": {}}, SECRET, confirmed_at="1", namespace=namespace)
        written = store.path_for("W1", namespace).read_text(encoding="utf-8")
        self.assertNotIn(SECRET, written)


@unittest.skipUnless(_mcp_available(), "mcp package not installed")
class AdapterTransportTests(unittest.TestCase):
    """Both modes expose the same tools; only the credential path differs."""

    @classmethod
    def setUpClass(cls):
        cls.stdio = _load_adapter("adapter_stdio_under_test", [])
        cls.http = _load_adapter("adapter_http_under_test", ["--transport", "http"])

    @staticmethod
    def _tool_names(module) -> set:
        return {tool.name for tool in asyncio.run(module.mcp.list_tools())}

    def test_the_tool_surface_does_not_depend_on_the_transport(self):
        self.assertEqual(EXPECTED_TOOLS, self._tool_names(self.stdio))
        self.assertEqual(self._tool_names(self.stdio), self._tool_names(self.http))

    def test_tool_schemas_are_identical_across_transports(self):
        def described(module):
            return sorted(
                (t.name, json.dumps(t.inputSchema, sort_keys=True))
                for t in asyncio.run(module.mcp.list_tools())
            )

        self.assertEqual(described(self.stdio), described(self.http))

    def test_public_session_context_contract_is_narrow_and_explicit(self):
        tools = {
            tool.name: tool.inputSchema
            for tool in asyncio.run(self.stdio.mcp.list_tools())
        }
        resolve_properties = tools["tapd_context_resolve"]["properties"]
        status_properties = tools["tapd_semantic_status"]["properties"]
        query_properties = tools["tapd_business_query"]["properties"]

        self.assertIn("tapd_identity", resolve_properties)
        self.assertIn("business_role", resolve_properties)
        self.assertIn("session_context", status_properties)
        self.assertIn("session_context", query_properties)
        self.assertNotIn("tapd_identity", query_properties)
        self.assertNotIn("business_role", query_properties)

    def test_modes_are_what_they_claim(self):
        self.assertFalse(self.stdio.RUNTIME.is_http)
        self.assertTrue(self.http.RUNTIME.is_http)

    def test_stdio_reads_the_process_environment(self):
        original = self.stdio.os.environ.get(credentials.ENV_TOKEN)
        self.stdio.os.environ[credentials.ENV_TOKEN] = SECRET
        try:
            self.assertEqual(SECRET, self.stdio._credential())
        finally:
            if original is None:
                self.stdio.os.environ.pop(credentials.ENV_TOKEN, None)
            else:
                self.stdio.os.environ[credentials.ENV_TOKEN] = original

    def test_http_takes_the_credential_from_the_live_request(self):
        request = SimpleNamespace(headers={"authorization": f"Bearer {SECRET}"})
        context = SimpleNamespace(request_context=SimpleNamespace(request=request))
        original = self.http.mcp.get_context
        self.http.mcp.get_context = lambda: context
        try:
            self.assertEqual(SECRET, self.http._credential())
        finally:
            self.http.mcp.get_context = original

    def test_http_never_falls_back_to_the_process_environment(self):
        """The whole point: no request credential means refusal, not someone else's."""
        original = self.http.os.environ.get(credentials.ENV_TOKEN)
        self.http.os.environ[credentials.ENV_TOKEN] = SECRET
        try:
            with self.assertRaises(CredentialError) as caught:
                # No request in flight, so get_context() has no HTTP request.
                self.http._credential()
            self.assertNotIn(SECRET, str(caught.exception))
        finally:
            if original is None:
                self.http.os.environ.pop(credentials.ENV_TOKEN, None)
            else:
                self.http.os.environ[credentials.ENV_TOKEN] = original

    def test_http_refuses_a_request_that_carries_no_token(self):
        request = SimpleNamespace(headers={"content-type": "application/json"})
        context = SimpleNamespace(request_context=SimpleNamespace(request=request))
        original = self.http.mcp.get_context
        self.http.mcp.get_context = lambda: context
        try:
            with self.assertRaises(CredentialError):
                self.http._credential()
        finally:
            self.http.mcp.get_context = original

    def test_probe_reports_missing_credentials_as_auth_failure_not_unreachable(self):
        outcome = self.http.tapd_probe()
        self.assertEqual("failed", outcome["status"])
        self.assertEqual("AUTH_FAILED", outcome["error"]["code"])
        self.assertFalse(outcome["reachable"])

    def test_storage_namespace_is_per_tenant_in_http_and_absent_in_stdio(self):
        self.assertEqual("", self.stdio._namespace(SECRET))
        self.assertEqual(store.fingerprint(SECRET), self.http._namespace(SECRET))
        self.assertNotEqual(
            self.http._namespace(SECRET), self.http._namespace(OTHER_SECRET)
        )

    def test_profile_namespace_is_per_credential_in_both_transports(self):
        expected = store.fingerprint(SECRET)
        self.assertEqual(expected, self.stdio._profile_namespace(SECRET))
        self.assertEqual(expected, self.http._profile_namespace(SECRET))
        self.assertNotEqual(
            self.http._profile_namespace(SECRET),
            self.http._profile_namespace(OTHER_SECRET),
        )

    def test_context_tools_save_locally_then_resolve_without_exposing_credential(self):
        temporary = tempfile.TemporaryDirectory()
        original_home = store.os.environ.get(store.ENV_HOME)
        original_credential = self.http._credential
        original_scope = self.http._context_project_scope
        projects = [
            {"id": "W1", "name": "DeepTutor", "category": "project"},
            {"id": "W2", "name": "萌伴 Web", "category": "project"},
        ]
        self.http._credential = lambda: SECRET
        self.http._context_project_scope = lambda token: (
            projects,
            {"fetched_at": "2026-08-14T05:00:00Z"},
            None,
        )
        store.os.environ[store.ENV_HOME] = temporary.name
        try:
            saved = self.http.tapd_context_save("DeepTutor", "测试负责人", "QA 负责人")
            status = self.http.tapd_context_status()
            resolved = self.http.tapd_context_resolve()
            self.assertEqual("workspace-write", saved["effect"])
            self.assertFalse(saved["tapd_write"])
            self.assertEqual("ready", status["profile_state"])
            self.assertEqual("W1", resolved["resolution"]["workspace_id"])
            rendered = json.dumps([saved, status, resolved], ensure_ascii=False)
            self.assertNotIn(SECRET, rendered)
            self.assertNotIn(store.fingerprint(SECRET), rendered)
            self.assertNotIn(temporary.name, rendered)
        finally:
            self.http._credential = original_credential
            self.http._context_project_scope = original_scope
            if original_home is None:
                store.os.environ.pop(store.ENV_HOME, None)
            else:
                store.os.environ[store.ENV_HOME] = original_home
            temporary.cleanup()

    def test_public_context_resolve_issues_session_without_profile_write(self):
        temporary = tempfile.TemporaryDirectory()
        original_home = store.os.environ.get(store.ENV_HOME)
        original_credential = self.http._credential
        original_scope = self.http._context_project_scope
        projects = [{"id": "W1", "name": "DeepTutor", "category": "project"}]
        self.http._credential = lambda: SECRET
        self.http._context_project_scope = lambda token: (
            projects,
            {"fetched_at": "2026-08-14T05:00:00Z"},
            None,
        )
        store.os.environ[store.ENV_HOME] = temporary.name
        try:
            result = self.http.tapd_context_resolve(
                "DeepTutor",
                "alice",
                "QA",
            )
            self.assertEqual("ok", result["status"])
            self.assertIn("session_context", result)
            self.assertFalse(
                user_context.profile_path(store.fingerprint(SECRET)).exists()
            )
            rendered = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(SECRET, rendered)
            self.assertNotIn(store.fingerprint(SECRET), rendered)
        finally:
            self.http._credential = original_credential
            self.http._context_project_scope = original_scope
            if original_home is None:
                store.os.environ.pop(store.ENV_HOME, None)
            else:
                store.os.environ[store.ENV_HOME] = original_home
            temporary.cleanup()

    def test_public_call_tool_normalizes_profile_write_failure_without_path_leak(self):
        temporary = tempfile.TemporaryDirectory()
        original_home = store.os.environ.get(store.ENV_HOME)
        original_credential = self.http._credential
        original_scope = self.http._context_project_scope
        original_save = self.http.user_context._atomic_save
        fingerprint = store.fingerprint(SECRET)
        sentinel = f"C:/private/{fingerprint}/.profile.sentinel.tmp"
        projects = [{"id": "W1", "name": "DeepTutor", "category": "project"}]
        self.http._credential = lambda: SECRET
        self.http._context_project_scope = lambda token: (
            projects,
            {"fetched_at": "2026-08-14T05:00:00Z"},
            None,
        )

        def fail_write(*_args, **_kwargs):
            raise OSError(sentinel)

        self.http.user_context._atomic_save = fail_write
        store.os.environ[store.ENV_HOME] = temporary.name
        try:
            outcome = asyncio.run(
                self.http.mcp.call_tool(
                    "tapd_context_save",
                    {
                        "default_project": "DeepTutor",
                        "tapd_identity": "测试负责人",
                        "business_role": "QA 负责人",
                    },
                )
            )
            rendered = repr(outcome)
            self.assertIn("PROFILE_WRITE_FAILED", rendered)
            self.assertNotIn(sentinel, rendered)
            self.assertNotIn(fingerprint, rendered)
            self.assertNotIn(temporary.name, rendered)
        finally:
            self.http._credential = original_credential
            self.http._context_project_scope = original_scope
            self.http.user_context._atomic_save = original_save
            if original_home is None:
                store.os.environ.pop(store.ENV_HOME, None)
            else:
                store.os.environ[store.ENV_HOME] = original_home
            temporary.cleanup()

    def test_one_tenants_review_cannot_be_confirmed_by_another(self):
        workspace = "W1"
        self.http._PENDING_REVIEW.clear()
        self.http._PENDING_REVIEW[self.http._pending_key(workspace, SECRET)] = object()
        self.assertNotIn(
            self.http._pending_key(workspace, OTHER_SECRET), self.http._PENDING_REVIEW
        )
        self.http._PENDING_REVIEW.clear()

    def test_no_module_level_state_holds_a_credential(self):
        for module in (self.stdio, self.http):
            with self.subTest(module=module.__name__):
                for name, value in vars(module).items():
                    if isinstance(value, str):
                        self.assertNotIn(SECRET, value, name)


if __name__ == "__main__":
    unittest.main()
