"""Static and adapter-level contract tests for the HTTPS staging package.

These tests never use a real TAPD token and never call TAPD.  They deliberately
pin the parts most likely to be weakened during deployment edits: loopback-only
publishing, no credential environment fallback, read-only/non-root runtime, and
an unauthenticated health route that exposes no configuration.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "deploy" / "compose.local.yml"
NGINX = ROOT / "deploy" / "nginx.conf.template"
ROLLBACK = ROOT / "deploy" / "rollback.local.ps1"
DEPLOY = ROOT / "deploy" / "deploy_local.py"
ADAPTER = ROOT / "adapters" / "mcp_server.py"

FIXTURE_TOKEN = "fixture-token-never-send-to-tapd"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _mcp_available() -> bool:
    try:
        return (
            importlib.util.find_spec("mcp") is not None
            and importlib.util.find_spec("starlette") is not None
        )
    except ModuleNotFoundError:
        return False


def _load_http_adapter():
    spec = importlib.util.spec_from_file_location("staging_adapter_under_test", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    original = sys.argv
    sys.argv = ["mcp_server.py", "--transport", "http"]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = original
    return module


def _load_deploy_orchestrator():
    spec = importlib.util.spec_from_file_location("deploy_lock_under_test", DEPLOY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeploymentFileContractTests(unittest.TestCase):
    def test_expected_package_files_exist(self):
        for path in (DOCKERFILE, DOCKERIGNORE, COMPOSE, NGINX, ROLLBACK, DEPLOY):
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), str(path))

    def test_runtime_image_is_pinned_nonroot_and_minimal(self):
        text = _read(DOCKERFILE)
        self.assertRegex(
            text, r"FROM python:3\.12\.\d+-slim-bookworm@sha256:[0-9a-f]{64}"
        )
        self.assertIn("TAPD_CAPABILITY_HOME=/data", text)
        self.assertRegex(text, r"(?m)^USER 10001:10001$")
        self.assertIn("--transport", text)
        self.assertIn("--host", text)
        self.assertIn("0.0.0.0", text)
        self.assertNotRegex(text, r"(?m)^COPY\s+\.\s+")

    def test_source_revision_has_no_default_and_is_propagated_to_image_metadata(self):
        dockerfile = _read(DOCKERFILE)
        compose = _read(COMPOSE)
        self.assertRegex(dockerfile, r"(?m)^ARG SOURCE_REVISION$")
        self.assertNotRegex(dockerfile, r"(?m)^ARG SOURCE_REVISION=")
        self.assertIn(
            'org.opencontainers.image.revision="${SOURCE_REVISION}"', dockerfile
        )
        self.assertIn("TAPD_CAPABILITY_SOURCE_REVISION=${SOURCE_REVISION}", dockerfile)
        self.assertIn("invalid SOURCE_REVISION", dockerfile)
        self.assertIn(
            "${TAPD_CAPABILITY_SOURCE_REVISION:?TAPD_CAPABILITY_SOURCE_REVISION is required}",
            compose,
        )
        self.assertNotIn("db5782536be39d9c824c190826e2d46c4d49500d", compose)

    def test_docker_test_stage_receives_every_deployment_fixture(self):
        dockerfile = _read(DOCKERFILE)
        ignored = {
            line.strip()
            for line in _read(DOCKERIGNORE).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("COPY Dockerfile .dockerignore Jenkinsfile /app/", dockerfile)
        self.assertIn("COPY README.md LICENSE THIRD_PARTY_NOTICES.md /app/", dockerfile)
        self.assertIn("COPY deploy/ /app/deploy/", dockerfile)
        self.assertNotIn("deploy", ignored)
        self.assertNotIn("README.md", ignored)
        test_run = dockerfile.index("RUN python -X utf8 -m unittest discover")
        self.assertLess(
            dockerfile.index("COPY Dockerfile .dockerignore Jenkinsfile /app/"),
            test_run,
        )
        self.assertLess(dockerfile.index("COPY deploy/ /app/deploy/"), test_run)

    def test_compose_only_publishes_loopback_and_has_runtime_hardening(self):
        text = _read(COMPOSE)
        self.assertIn("127.0.0.1:3796:3796", text)
        self.assertNotIn("8080", text)
        self.assertNotIn("8081", text)
        self.assertRegex(text, r"(?m)^\s+read_only:\s*true$")
        self.assertIn("/data", text)
        self.assertIn("/tmp", text)
        self.assertIn("no-new-privileges:true", text)
        self.assertRegex(text, r"(?ms)cap_drop:\s*\n\s*- ALL")
        self.assertIn("pids_limit:", text)
        self.assertIn("mem_limit:", text)
        self.assertIn("cpus:", text)

        forbidden = ("TAPD_ACCESS_TOKEN", "env_file:", "credentials/")
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)

    def test_nginx_has_exact_tls_routes_no_redirect_and_safe_logs(self):
        text = _read(NGINX)
        self.assertIn("listen 443 ssl", text)
        self.assertIn("server_name __STAGING_FQDN__", text)
        self.assertIn("ssl_certificate __TLS_CERTIFICATE_FILE__", text)
        self.assertIn("ssl_certificate_key __TLS_PRIVATE_KEY_FILE__", text)
        self.assertIn("location = /mcp", text)
        self.assertIn("location = /healthz", text)
        self.assertNotRegex(text, r"(?i)\breturn\s+30[12378]\b")
        self.assertNotIn("listen 80", text)
        self.assertNotIn("8080", text)
        self.assertNotIn("8081", text)
        self.assertIn("proxy_set_header Authorization $http_authorization", text)
        self.assertIn('proxy_set_header Authorization ""', text)
        self.assertIn("client_max_body_size", text)
        self.assertIn("limit_req", text)
        self.assertIn("proxy_read_timeout", text)
        self.assertIn("add_header X-Content-Type-Options", text)

        log_format = re.search(r"log_format\s+tapd_safe\s+(.+?);", text, re.DOTALL)
        self.assertIsNotNone(log_format)
        rendered = log_format.group(1).lower()
        for forbidden in ("authorization", "http_", "request_body", "$request "):
            with self.subTest(log_marker=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_rollback_requires_an_immutable_image_digest(self):
        text = _read(ROLLBACK)
        self.assertIn("PreviousImage", text)
        self.assertIn("PreviousSourceRevision", text)
        self.assertIn("@sha256:", text)
        self.assertIn("TAPD_CAPABILITY_SOURCE_REVISION", text)
        self.assertIn("--no-build", text)
        self.assertNotIn("down -v", text)

    def test_rollback_accepts_only_the_canonical_reviewed_compose(self):
        text = _read(ROLLBACK)
        reviewed_hash = (
            "c97a4b9abf17c295d4d0613dcef1cfdadea847637975a2bc88a5d0d1b08f9cb2"
        )
        self.assertIn("$canonicalCompose", text)
        self.assertIn(f"$reviewedComposeSha256 = '{reviewed_hash}'", text)
        self.assertIn("[StringComparer]::OrdinalIgnoreCase.Equals", text)
        self.assertIn("Get-FileHash -LiteralPath $resolvedCompose", text)
        self.assertLess(
            text.index("compose file must be the canonical reviewed template"),
            text.index("docker image inspect"),
        )
        self.assertLess(
            text.index("canonical compose file does not match its reviewed hash"),
            text.index("docker image inspect"),
        )

    def test_deploy_and_manual_rollback_share_one_host_scoped_lock_contract(self):
        deploy = _read(DEPLOY)
        rollback = _read(ROLLBACK)
        for marker in (
            "tapd-capability",
            "locks",
            "staging.lock",
            "/var/lock/tapd-capability",
            "deployment busy: another tapd-capability operation is active",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, deploy)
                self.assertIn(marker, rollback)
        self.assertIn("os.O_CREAT | os.O_EXCL | os.O_WRONLY", deploy)
        self.assertIn("with DeploymentOperationLock(self.lock_file)", deploy)
        self.assertIn("[IO.FileMode]::CreateNew", rollback)
        self.assertIn("deployment lock release failed", deploy)
        self.assertIn("deployment lock release failed", rollback)
        self.assertIn("SHGetKnownFolderPath", deploy)
        self.assertNotIn('os.environ.get("PROGRAMDATA")', deploy)

    @unittest.skipUnless(
        os.name == "nt" and (shutil.which("pwsh") or shutil.which("powershell")),
        "Windows PowerShell is required",
    )
    def test_windows_default_lock_ignores_poisoned_programdata_and_matches_known_folder(
        self,
    ):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        known_folder = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout.strip()
        poisoned = str(Path(tempfile.gettempdir()) / "attacker-programdata")
        deploy = _load_deploy_orchestrator()

        with patch.dict(os.environ, {"PROGRAMDATA": poisoned}):
            actual = deploy.default_operation_lock_file()

        expected = Path(known_folder) / "tapd-capability" / "locks" / "staging.lock"
        self.assertEqual(expected, actual)
        self.assertNotEqual(
            Path(poisoned) / "tapd-capability" / "locks" / "staging.lock",
            actual,
        )

    @unittest.skipUnless(os.name == "nt", "Windows Known Folder API is required")
    def test_windows_known_folder_failure_is_redacted(self):
        deploy = _load_deploy_orchestrator()
        with (
            patch(
                "ctypes.WinDLL",
                side_effect=OSError("fixture-sensitive-system-path"),
            ),
            self.assertRaises(deploy.DeploymentLockUnavailableError) as caught,
        ):
            deploy.default_operation_lock_file()

        self.assertEqual("deployment lock unavailable", str(caught.exception))
        self.assertNotIn("fixture-sensitive-system-path", str(caught.exception))

    @unittest.skipUnless(
        shutil.which("pwsh") or shutil.which("powershell"),
        "PowerShell is not installed",
    )
    def test_rollback_rejects_an_identical_compose_copy_before_docker(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "compose.local.yml"
            copied.write_bytes(COMPOSE.read_bytes())
            completed = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(ROLLBACK),
                    "-PreviousImage",
                    "registry.invalid/tapd-capability@sha256:" + "1" * 64,
                    "-PreviousSourceRevision",
                    "a" * 40,
                    "-ComposeFile",
                    str(copied),
                    "-WhatIf",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

        rendered = completed.stdout + completed.stderr
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("compose file must be the canonical reviewed template", rendered)
        self.assertNotIn("Previous immutable image is not available locally", rendered)

    @unittest.skipUnless(
        shutil.which("pwsh") or shutil.which("powershell"),
        "PowerShell is not installed",
    )
    def test_python_deploy_lock_makes_manual_rollback_fail_busy_without_leaks(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        deploy = _load_deploy_orchestrator()
        poisoned = str(Path(tempfile.gettempdir()) / "attacker-programdata")
        with patch.dict(os.environ, {"PROGRAMDATA": poisoned}):
            lock_file = deploy.default_operation_lock_file()
            with deploy.DeploymentOperationLock():
                completed = subprocess.run(
                    [
                        shell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(ROLLBACK),
                        "-PreviousImage",
                        "registry.invalid/tapd-capability@sha256:" + "1" * 64,
                        "-PreviousSourceRevision",
                        "a" * 40,
                        "-WhatIf",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )

        rendered = completed.stdout + completed.stderr
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("deployment busy", rendered)
        self.assertNotIn(str(lock_file), rendered)
        self.assertNotIn("fixture-credential", rendered)
        self.assertNotIn("Previous immutable image is not available locally", rendered)
        self.assertFalse(lock_file.exists())

    @unittest.skipUnless(
        shutil.which("pwsh") or shutil.which("powershell"),
        "PowerShell is not installed",
    )
    def test_manual_rollback_releases_lock_when_image_validation_raises(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        deploy = _load_deploy_orchestrator()
        lock_file = deploy.default_operation_lock_file()
        self.assertFalse(lock_file.exists())
        completed = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(ROLLBACK),
                "-PreviousImage",
                "registry.invalid/tapd-capability@sha256:" + "1" * 64,
                "-PreviousSourceRevision",
                "a" * 40,
                "-WhatIf",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        rendered = completed.stdout + completed.stderr
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("Previous immutable image is not available locally", rendered)
        self.assertNotIn(str(lock_file), rendered)
        self.assertFalse(lock_file.exists())


@unittest.skipUnless(_mcp_available(), "mcp package not installed")
class HealthAndBearerGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = _load_http_adapter()

    def test_health_is_constant_and_does_not_resolve_a_credential(self):
        original = self.adapter._credential
        self.adapter._credential = lambda: (_ for _ in ()).throw(
            AssertionError("health must not resolve a credential")
        )
        try:
            response = asyncio.run(
                self.adapter.healthz(
                    SimpleNamespace(
                        headers={"authorization": f"Bearer {FIXTURE_TOKEN}"}
                    )
                )
            )
        finally:
            self.adapter._credential = original

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, json.loads(response.body))
        rendered = repr((response.headers, response.body))
        self.assertNotIn(FIXTURE_TOKEN, rendered)
        self.assertEqual("no-store", response.headers["cache-control"])

    def test_mcp_gate_refuses_missing_or_malformed_bearer_without_echo(self):
        for headers in ({}, {"authorization": FIXTURE_TOKEN}):
            with self.subTest(headers=headers):
                error = self.adapter._mcp_authorization_error(headers)
                self.assertIsNotNone(error)
                response = error
                self.assertEqual(401, response.status_code)
                self.assertNotIn(FIXTURE_TOKEN, repr((response.headers, response.body)))

    def test_mcp_gate_accepts_a_well_formed_fixture_bearer(self):
        self.assertIsNone(
            self.adapter._mcp_authorization_error(
                {"authorization": f"Bearer {FIXTURE_TOKEN}"}
            )
        )


if __name__ == "__main__":
    unittest.main()
