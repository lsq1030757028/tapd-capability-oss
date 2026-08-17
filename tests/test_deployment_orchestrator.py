"""Fault-injection tests for the local deployment/automatic rollback state machine."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import multiprocessing
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy" / "deploy_local.py"
CANONICAL_COMPOSE = ROOT / "deploy" / "compose.local.yml"
CANONICAL_COMPOSE_SHA256 = (
    "c97a4b9abf17c295d4d0613dcef1cfdadea847637975a2bc88a5d0d1b08f9cb2"
)
PARENT_REVISION = "9c9b5d14b91a253e8a98ca268bcc813c411877ed"
FOLLOWUP_REVISION = "a24b6c8d0e1f23456789abcdef0123456789abcd"
PREVIOUS_IMAGE = "registry.invalid/tapd-capability@sha256:" + "1" * 64
CANDIDATE_IMAGE = "registry.invalid/tapd-capability@sha256:" + "2" * 64


def _hold_deployment_lock(lock_file: str, ready, release) -> None:
    module = _load_module()
    with module.DeploymentOperationLock(Path(lock_file)):
        ready.set()
        release.wait(10)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "deploy_local_under_test", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    def __init__(
        self,
        start_results,
        health_results,
        before_first_start=None,
        verify_results=(True, True),
    ):
        self.start_results = iter(start_results)
        self.health_results = iter(health_results)
        self.verify_results = iter(verify_results)
        self.before_first_start = before_first_start
        self.events = []
        self.started = False

    def verify_image(self, *, image, source_revision):
        self.events.append(("verify", image, source_revision))
        return next(self.verify_results)

    def start(self, *, compose_file, image, source_revision, service):
        if not self.started and self.before_first_start is not None:
            self.before_first_start()
        self.started = True
        self.events.append(
            ("start", image, source_revision, service, str(compose_file))
        )
        return next(self.start_results)

    def health(self, url):
        self.events.append(("health", url))
        return next(self.health_results)


class SourceRevisionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_missing_or_non_commit_source_revision_fails_closed(self):
        for value in ("", "9c9b5d1", "g" * 40, "a" * 39, "a" * 41):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.module.validate_source_revision(value)

    def test_parent_and_followup_revisions_drive_safe_runtime_metadata(self):
        for revision in (PARENT_REVISION, FOLLOWUP_REVISION):
            with self.subTest(revision=revision):
                self.assertEqual(
                    revision, self.module.validate_source_revision(revision)
                )
                self.assertEqual(
                    {
                        "org.opencontainers.image.revision": revision,
                        "TAPD_CAPABILITY_SOURCE_REVISION": revision,
                    },
                    self.module.image_metadata(revision),
                )


class DeploymentStateMachineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.compose = CANONICAL_COMPOSE
        self.state_dir = self.root / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def _config(self):
        return self.module.DeploymentConfig(
            candidate_image=CANDIDATE_IMAGE,
            candidate_revision=FOLLOWUP_REVISION,
            previous_image=PREVIOUS_IMAGE,
            previous_revision=PARENT_REVISION,
            compose_file=self.compose,
            state_dir=self.state_dir,
            health_url="http://127.0.0.1:3796/healthz",
        )

    def test_snapshot_is_persisted_before_candidate_start_and_contains_no_credentials(
        self,
    ):
        def assert_snapshot_exists():
            snapshots = list(self.state_dir.glob("deployment-*.json"))
            self.assertEqual(1, len(snapshots))
            rendered = snapshots[0].read_text(encoding="utf-8").lower()
            self.assertNotIn("authorization", rendered)
            self.assertNotIn("access_token", rendered)
            self.assertNotIn("secret", rendered)

        runner = FakeRunner([True], [True], before_first_start=assert_snapshot_exists)
        result = self.module.DeploymentOrchestrator(self._config(), runner).run()
        self.assertEqual(0, result.exit_code)
        self.assertEqual("candidate_healthy", result.state)
        snapshot = json.loads(result.snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(PREVIOUS_IMAGE, snapshot["previous"]["image"])
        self.assertEqual(FOLLOWUP_REVISION, snapshot["candidate"]["source_revision"])
        self.assertEqual(
            {
                "schema": "tapd-capability-compose/v1",
                "compose_identity": "deploy/compose.local.yml",
                "compose_sha256": CANONICAL_COMPOSE_SHA256,
                "service": "tapd-capability",
                "host_bind": "127.0.0.1:3796",
                "container_port": 3796,
                "persistent_volume": {
                    "name": "tapd-capability-data",
                    "target": "/data",
                },
                "health_url": "http://127.0.0.1:3796/healthz",
                "rootfs_read_only": True,
            },
            snapshot["configuration"],
        )
        self.assertEqual(
            [result.snapshot_path],
            list(self.state_dir.iterdir()),
            "the snapshot directory must not contain a raw Compose backup",
        )

    def test_candidate_health_failure_automatically_restores_previous_and_rechecks(
        self,
    ):
        runner = FakeRunner([True, True], [False, True])
        result = self.module.DeploymentOrchestrator(self._config(), runner).run()
        self.assertEqual(2, result.exit_code)
        self.assertEqual("rolled_back_healthy", result.state)
        self.assertEqual(
            [
                ("start", CANDIDATE_IMAGE, FOLLOWUP_REVISION),
                ("health", "http://127.0.0.1:3796/healthz"),
                ("start", PREVIOUS_IMAGE, PARENT_REVISION),
                ("health", "http://127.0.0.1:3796/healthz"),
            ],
            [
                event[:3] if event[0] == "start" else event
                for event in runner.events
                if event[0] in {"start", "health"}
            ],
        )
        rendered = result.snapshot_path.read_text(encoding="utf-8")
        self.assertNotIn("down", rendered)
        self.assertNotIn("delete", rendered)

    def test_failed_restore_or_failed_restore_health_is_nonzero(self):
        cases = (
            (FakeRunner([True, False], [False]), 3, "rollback_start_failed"),
            (FakeRunner([True, True], [False, False]), 4, "rollback_health_failed"),
        )
        for runner, expected_code, expected_state in cases:
            with self.subTest(expected_state=expected_state):
                result = self.module.DeploymentOrchestrator(
                    self._config(), runner
                ).run()
                self.assertEqual(expected_code, result.exit_code)
                self.assertEqual(expected_state, result.state)

    def test_only_the_exact_canonical_compose_is_accepted_before_state_or_runner(self):
        marker = "fixture-credential-must-never-be-copied"
        malicious_composes = {
            "tapd header key": (
                "services:\n  tapd-capability:\n    environment:\n"
                f"      X-TAPD-Access-Token: {marker}\n"
            ),
            "case variant": (
                "services:\n  tapd-capability:\n    environment:\n"
                f"      x-tapd-access-token: {marker}\n"
            ),
            "hyphen and space variant": (
                "services:\n  tapd-capability:\n    environment:\n"
                f'      "X TAPD-ACCESS TOKEN": {marker}\n'
            ),
            "generic token": (
                "services:\n  tapd-capability:\n    environment:\n"
                f"      SERVICE_TOKEN: {marker}\n"
            ),
            "password": (
                "services:\n  tapd-capability:\n    environment:\n"
                f"      password: {marker}\n"
            ),
            "api key": (
                "services:\n  tapd-capability:\n    environment:\n"
                f'      "api key": {marker}\n'
            ),
            "authorization": (
                "services:\n  tapd-capability:\n    environment:\n"
                f"      Authorization: {marker}\n"
            ),
            "env file": (
                f"services:\n  tapd-capability:\n    env_file: ./{marker}.env\n"
            ),
            "docker secret": (
                "services:\n  tapd-capability:\n    secrets:\n"
                f"      - {marker}\nsecrets:\n  {marker}:\n    external: true\n"
            ),
            "docker config": (
                "services:\n  tapd-capability:\n    configs:\n"
                f"      - {marker}\nconfigs:\n  {marker}:\n    external: true\n"
            ),
            "label injection": (
                "services:\n  tapd-capability:\n    labels:\n"
                f"      fixture.review: {marker}\n"
            ),
            "build argument": (
                "services:\n  tapd-capability:\n    build:\n      args:\n"
                f"        FIXTURE_VALUE: {marker}\n"
            ),
            "credential volume": (
                "services:\n  tapd-capability:\n    volumes:\n"
                f"      - ./{marker}:/run/credentials:ro\n"
            ),
            "variable interpolation": (
                "services:\n  tapd-capability:\n    environment:\n"
                f"      SAFE_NAME: ${{{marker}}}\n"
            ),
            "unknown field": (
                f"services:\n  tapd-capability:\n    x-unknown-injection: {marker}\n"
            ),
        }

        for name, body in malicious_composes.items():
            with self.subTest(name=name):
                compose = self.root / f"{name.replace(' ', '-')}.yml"
                compose.write_text(body, encoding="utf-8")
                state_dir = self.root / f"state-{name.replace(' ', '-')}"
                config = self._config()
                config.compose_file = compose
                config.state_dir = state_dir
                runner = FakeRunner([True], [True])

                with self.assertRaises(ValueError):
                    self.module.DeploymentOrchestrator(config, runner).run()

                self.assertEqual([], runner.events)
                self.assertFalse(state_dir.exists())

    def test_copy_of_canonical_compose_fails_before_state_or_runner(self):
        copied = self.root / "compose.local.yml"
        copied.write_bytes(CANONICAL_COMPOSE.read_bytes())
        runner = FakeRunner([True], [True])
        config = self._config()
        config.compose_file = copied

        with self.assertRaises(ValueError):
            self.module.DeploymentOrchestrator(config, runner).run()

        self.assertEqual([], runner.events)
        self.assertFalse(self.state_dir.exists())

    def test_canonical_compose_hash_is_a_reviewed_literal(self):
        self.assertEqual(
            CANONICAL_COMPOSE_SHA256,
            hashlib.sha256(CANONICAL_COMPOSE.read_bytes()).hexdigest(),
        )

    def test_second_process_fails_busy_before_runner_or_state_side_effects(self):
        lock_file = self.root / "host-operation.lock"
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        holder = context.Process(
            target=_hold_deployment_lock,
            args=(str(lock_file), ready, release),
        )
        holder.start()
        self.assertTrue(ready.wait(5), "first process did not acquire fixture lock")
        lock_token = lock_file.read_text(encoding="ascii")
        runner = FakeRunner([True], [True])
        started = time.monotonic()
        try:
            with self.assertRaises(self.module.DeploymentBusyError) as caught:
                self.module.run_locked_deployment(
                    self._config(), runner, lock_file=lock_file
                )
        finally:
            release.set()
            holder.join(5)
            if holder.is_alive():
                holder.terminate()
                holder.join(5)

        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual([], runner.events)
        self.assertFalse(self.state_dir.exists())
        rendered = str(caught.exception)
        self.assertIn("deployment busy", rendered)
        self.assertNotIn(str(lock_file), rendered)
        self.assertNotIn(lock_token, rendered)

    def test_lock_is_released_after_an_exception_without_deadlock(self):
        lock_file = self.root / "host-operation.lock"
        with (
            self.assertRaises(RuntimeError),
            self.module.DeploymentOperationLock(lock_file),
        ):
            raise RuntimeError("fixture failure")

        with self.module.DeploymentOperationLock(lock_file):
            self.assertTrue(lock_file.is_file())
        self.assertFalse(lock_file.exists())

    def test_cli_reports_safe_busy_exit_without_snapshot_or_lock_path(self):
        lock_file = self.module.default_operation_lock_file()
        error = io.StringIO()
        with self.module.DeploymentOperationLock(), redirect_stderr(error):
            exit_code = self.module.main(
                [
                    "--candidate-image",
                    CANDIDATE_IMAGE,
                    "--candidate-revision",
                    FOLLOWUP_REVISION,
                    "--previous-image",
                    PREVIOUS_IMAGE,
                    "--previous-revision",
                    PARENT_REVISION,
                    "--state-dir",
                    str(self.state_dir),
                    "--dry-run",
                ]
            )

        self.assertEqual(self.module.EXIT_DEPLOYMENT_BUSY, exit_code)
        self.assertEqual(
            "deployment busy: another tapd-capability operation is active",
            error.getvalue().strip(),
        )
        self.assertNotIn(str(lock_file), error.getvalue())
        self.assertFalse(self.state_dir.exists())


if __name__ == "__main__":
    unittest.main()
