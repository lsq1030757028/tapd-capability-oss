"""Static contracts for the historical CODING gate and the public hosted Actions gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
JENKINSFILE = ROOT / "Jenkinsfile"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
METADATA_WRITER = ROOT / "ci" / "write_image_metadata.py"
CHANGED_PYTHON_CHECK = ROOT / "ci" / "check_changed_python.py"
CHECKOUT_SHA = "11d5960a326750d5838078e36cf38b85af677262"
UPLOAD_ARTIFACT_SHA = "ea165f8d65b6e75b540449e92b4886f43607fa02"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_metadata_writer():
    spec = importlib.util.spec_from_file_location(
        "ci_metadata_under_test", METADATA_WRITER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_changed_python_check():
    spec = importlib.util.spec_from_file_location(
        "changed_python_check_under_test", CHANGED_PYTHON_CHECK
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JenkinsfileContractTests(unittest.TestCase):
    def test_private_checkout_is_bound_to_a_safe_ref_and_exact_full_sha(self):
        text = _read(JENKINSFILE)
        self.assertIn("git@github.com:lsq1030757028/tapd-capability.git", text)
        self.assertIn("GITHUB_REF must be a safe refs/heads branch ref", text)
        self.assertIn("GITHUB_COMMIT must be a full lowercase 40-character SHA", text)
        self.assertIn("branches: [[name: env.SOURCE_REVISION]]", text)
        self.assertIn(
            'refspec: "+${env.SOURCE_REF}:refs/remotes/origin/reviewed"', text
        )
        self.assertIn('test "$SOURCE_REVISION" = "$GITHUB_COMMIT"', text)
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_COMMIT"', text)
        self.assertIn("git merge-base --is-ancestor", text)

    def test_job_controlled_credential_id_is_required_without_a_fake_default(self):
        text = _read(JENKINSFILE)
        self.assertIn("env.CODING_GITHUB_SSH_CREDENTIAL_ID ?: ''", text)
        self.assertIn("CODING_GITHUB_SSH_CREDENTIAL_ID is required", text)
        self.assertIn("credentialsId: env.GITHUB_CHECKOUT_CREDENTIAL_ID", text)
        self.assertNotIn("REPLACE_WITH", text)
        self.assertNotIn("echo $CODING_GITHUB_SSH_CREDENTIAL_ID", text)

    def test_pipeline_builds_both_existing_docker_targets_and_archives_identity(self):
        text = _read(JENKINSFILE)
        self.assertIn("docker build --pull --target test", text)
        self.assertIn("docker build --pull --target runtime", text)
        self.assertIn('--build-arg "SOURCE_REVISION=$GITHUB_COMMIT"', text)
        self.assertIn("pytest==8.4.1", text)
        self.assertIn("--junitxml=ci-artifacts/mcp-full-suite.xml", text)
        self.assertIn("ci/write_image_metadata.py", text)
        self.assertIn("archiveArtifacts artifacts: 'ci-artifacts/*'", text)

    def test_pipeline_has_no_registry_or_deployment_side_effect(self):
        text = (_read(JENKINSFILE) + "\n" + _read(WORKFLOW)).lower()
        forbidden = (
            "docker push",
            "docker login",
            "docker compose",
            "deploy_local.py",
            "kubectl ",
            "helm ",
            "rsync ",
            "scp ",
            "ssh ",
        )
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)

    def test_public_github_actions_use_required_hosted_runner_contract(self):
        workflow = _read(WORKFLOW)
        trigger = workflow.split("permissions:", maxsplit=1)[0]
        self.assertIn("pull_request:", trigger)
        self.assertIn("push:", trigger)
        self.assertIn("branches:\n      - main", trigger)
        self.assertIn("workflow_dispatch:", trigger)
        self.assertNotIn("pull_request_target:", trigger)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertNotIn("self-hosted", workflow)
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' }}", workflow
        )
        self.assertNotIn("secrets.", workflow)

    def test_github_actions_checkout_is_event_bound_and_actions_are_pinned(self):
        workflow = _read(WORKFLOW)
        self.assertIn("github.event.pull_request.head.sha", workflow)
        self.assertIn("github.sha", workflow)
        self.assertIn("github.head_ref", workflow)
        self.assertIn("^[0-9a-f]{40}$", workflow)
        self.assertIn('test "$(git rev-parse HEAD)" = "$SOURCE_SHA"', workflow)
        self.assertIn(f"actions/checkout@{CHECKOUT_SHA}", workflow)
        self.assertNotIn("actions/checkout@v", workflow)
        self.assertIn(f"actions/upload-artifact@{UPLOAD_ARTIFACT_SHA}", workflow)
        self.assertNotIn("actions/upload-artifact@v", workflow)
        self.assertIn("persist-credentials: false", workflow)

    def test_github_actions_preserve_build_only_candidate_surface(self):
        workflow = _read(WORKFLOW)
        self.assertIn("Credential scan", workflow)
        self.assertIn("docker build --pull --target test", workflow)
        self.assertIn("pytest==8.4.1", workflow)
        self.assertIn(
            "python -m pytest tests/ -q --ignore=tests/acceptance_live.py", workflow
        )
        self.assertIn("docker build --pull --target runtime", workflow)
        self.assertIn('--build-arg "SOURCE_REVISION=$SOURCE_SHA"', workflow)
        self.assertIn("org.opencontainers.image.revision", workflow)
        self.assertIn("ci/write_image_metadata.py", workflow)
        self.assertIn("ci-artifacts/image-metadata.json", workflow)


class ImageMetadataContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.writer = _load_metadata_writer()

    def _args(self, output: Path) -> argparse.Namespace:
        revision = "a" * 40
        return argparse.Namespace(
            output=output,
            repository="git@github.com:lsq1030757028/tapd-capability.git",
            source_ref="refs/heads/feat/candidate",
            source_revision=revision,
            test_image_id="sha256:" + "1" * 64,
            runtime_image_id="sha256:" + "2" * 64,
            runtime_label_revision=revision,
        )

    def test_receipt_is_explicitly_build_only_and_does_not_invent_registry_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "image-metadata.json"
            args = self._args(output)
            receipt = self.writer.build_receipt(args)

        self.assertEqual(args.source_revision, receipt["source"]["revision"])
        self.assertEqual(
            args.runtime_image_id,
            receipt["images"]["runtime"]["local_image_id"],
        )
        self.assertIsNone(receipt["images"]["runtime"]["registry_digest"])
        self.assertEqual(
            {"registry_push": False, "deployment": False}, receipt["effects"]
        )
        self.assertNotIn("credential", json.dumps(receipt).lower())

    def test_revision_label_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self._args(Path(temporary) / "image-metadata.json")
            args.runtime_label_revision = "b" * 40
            with self.assertRaises(ValueError):
                self.writer.build_receipt(args)


class ChangedPythonGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gate = _load_changed_python_check()

    def test_discovers_all_added_copied_modified_and_renamed_python_files(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"src/a.py\0tests/test_a.py\0"
        )
        with patch.object(self.gate.subprocess, "run", return_value=completed) as run:
            files = self.gate.changed_python_files(ROOT, "origin/main", "HEAD")

        self.assertEqual(["src/a.py", "tests/test_a.py"], files)
        command = run.call_args.args[0]
        self.assertIn("--diff-filter=ACMR", command)
        self.assertIn("origin/main...HEAD", command)
        self.assertIn("*.py", command)

    def test_gate_persists_both_ruff_check_and_format_check(self):
        text = _read(CHANGED_PYTHON_CHECK)
        self.assertIn('[args.ruff, "check", "--", *files]', text)
        self.assertIn('[args.ruff, "format", "--check", "--", *files]', text)


if __name__ == "__main__":
    unittest.main()
