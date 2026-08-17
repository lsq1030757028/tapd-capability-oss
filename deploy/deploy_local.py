"""Fail-closed local deploy orchestration with automatic immutable-image rollback.

The runner is injected so every state transition is testable without Docker,
network access, credentials, or a TAPD request.  The real runner only starts an
already-built immutable image and checks the constant loopback health endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
SERVICE = "tapd-capability"
CANONICAL_COMPOSE = Path(__file__).resolve().with_name("compose.local.yml")
CANONICAL_COMPOSE_IDENTITY = "deploy/compose.local.yml"
CANONICAL_COMPOSE_SHA256 = (
    "c97a4b9abf17c295d4d0613dcef1cfdadea847637975a2bc88a5d0d1b08f9cb2"
)
HOST_BIND = "127.0.0.1:3796"
CONTAINER_PORT = 3796
PERSISTENT_VOLUME = {"name": "tapd-capability-data", "target": "/data"}
EXIT_CANDIDATE_FAILED_ROLLED_BACK = 2
EXIT_ROLLBACK_START_FAILED = 3
EXIT_ROLLBACK_HEALTH_FAILED = 4
EXIT_IDENTITY_FAILED = 5
EXIT_LOCK_UNAVAILABLE = 73
EXIT_DEPLOYMENT_BUSY = 75
LOCK_FILE_NAME = "staging.lock"


def default_operation_lock_file() -> Path:
    if os.name == "nt":
        common_data = _windows_common_application_data()
        return common_data / "tapd-capability" / "locks" / LOCK_FILE_NAME
    return Path("/var/lock/tapd-capability") / LOCK_FILE_NAME


def _windows_common_application_data() -> Path:
    """Read ProgramData from the OS Known Folder API, never process environment."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = (
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        )

    folder_id_program_data = GUID(
        0x62AB5D82,
        0xFDC1,
        0x4DC3,
        (ctypes.c_ubyte * 8)(0xA9, 0xDD, 0x07, 0x0D, 0x1D, 0x49, 0x5D, 0x97),
    )
    path_pointer = ctypes.c_void_p()
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_void_p),
        )
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id_program_data),
            0,
            None,
            ctypes.byref(path_pointer),
        )
        if result != 0 or not path_pointer.value:
            raise OSError
        return Path(ctypes.wstring_at(path_pointer.value))
    except (AttributeError, OSError):
        raise DeploymentLockUnavailableError("deployment lock unavailable") from None
    finally:
        if path_pointer.value:
            try:
                ole32 = ctypes.WinDLL("ole32", use_last_error=True)
                ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
                ole32.CoTaskMemFree(path_pointer)
            except (AttributeError, OSError):
                raise DeploymentLockUnavailableError(
                    "deployment lock unavailable"
                ) from None


class DeploymentBusyError(RuntimeError):
    """Another process owns the shared service operation lock."""


class DeploymentLockUnavailableError(RuntimeError):
    """The shared host lock could not be safely acquired or released."""


class DeploymentOperationLock:
    """Non-blocking package-external lock shared by deploy and manual rollback."""

    def __init__(self, lock_file: Path | None = None) -> None:
        self.lock_file = Path(lock_file or default_operation_lock_file())
        self._descriptor: int | None = None
        self._token = ""

    def __enter__(self):
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise DeploymentLockUnavailableError(
                "deployment lock unavailable"
            ) from None

        self._token = uuid.uuid4().hex
        try:
            self._descriptor = os.open(
                self.lock_file,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            raise DeploymentBusyError(
                "deployment busy: another tapd-capability operation is active"
            ) from None
        except OSError:
            raise DeploymentLockUnavailableError(
                "deployment lock unavailable"
            ) from None

        try:
            os.write(self._descriptor, self._token.encode("ascii"))
            os.fsync(self._descriptor)
        except OSError:
            self._close_descriptor()
            self._remove_owned_marker(best_effort=True)
            raise DeploymentLockUnavailableError(
                "deployment lock unavailable"
            ) from None
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._close_descriptor()
        if not self._remove_owned_marker(best_effort=False):
            raise DeploymentLockUnavailableError(
                "deployment lock release failed"
            ) from None
        return False

    def _close_descriptor(self) -> None:
        if self._descriptor is not None:
            try:
                os.close(self._descriptor)
            finally:
                self._descriptor = None

    def _remove_owned_marker(self, *, best_effort: bool) -> bool:
        try:
            if self.lock_file.read_text(encoding="ascii") != self._token:
                return False
            self.lock_file.unlink()
            return True
        except OSError:
            return best_effort


def validate_source_revision(value: str) -> str:
    revision = str(value or "").strip()
    if not _REVISION.fullmatch(revision):
        raise ValueError(
            "source revision must be the explicit 40-character lowercase commit SHA"
        )
    return revision


def validate_image(value: str) -> str:
    image = str(value or "").strip()
    if not _IMAGE.fullmatch(image):
        raise ValueError(
            "image must be an immutable repository@sha256:<64 hex> reference"
        )
    return image


def image_metadata(revision: str) -> dict[str, str]:
    checked = validate_source_revision(revision)
    return {
        "org.opencontainers.image.revision": checked,
        "TAPD_CAPABILITY_SOURCE_REVISION": checked,
    }


class DeploymentConfig:
    def __init__(
        self,
        *,
        candidate_image: str,
        candidate_revision: str,
        previous_image: str,
        previous_revision: str,
        compose_file: Path,
        state_dir: Path,
        health_url: str,
        service: str = SERVICE,
    ) -> None:
        self.candidate_image = candidate_image
        self.candidate_revision = candidate_revision
        self.previous_image = previous_image
        self.previous_revision = previous_revision
        self.compose_file = Path(compose_file)
        self.state_dir = Path(state_dir)
        self.health_url = health_url
        self.service = service

    def validate(self) -> None:
        self.candidate_image = validate_image(self.candidate_image)
        self.previous_image = validate_image(self.previous_image)
        self.candidate_revision = validate_source_revision(self.candidate_revision)
        self.previous_revision = validate_source_revision(self.previous_revision)
        if self.candidate_image == self.previous_image:
            raise ValueError("candidate and previous immutable image must differ")
        if not self.compose_file.is_file():
            raise ValueError("compose file does not exist")
        if self.compose_file.resolve() != CANONICAL_COMPOSE:
            raise ValueError("compose file must be the canonical reviewed template")
        if _sha256(self.compose_file) != CANONICAL_COMPOSE_SHA256:
            raise ValueError("canonical compose file does not match its reviewed hash")
        if self.service != SERVICE:
            raise ValueError(f"service must be {SERVICE!r}")
        if self.health_url != "http://127.0.0.1:3796/healthz":
            raise ValueError("health URL must be the fixed loopback /healthz endpoint")


class DeploymentResult:
    def __init__(self, exit_code: int, state: str, snapshot_path: Path) -> None:
        self.exit_code = exit_code
        self.state = state
        self.snapshot_path = snapshot_path


class DeploymentOrchestrator:
    def __init__(
        self, config: DeploymentConfig, runner, *, lock_file: Path | None = None
    ) -> None:
        self.config = config
        self.runner = runner
        self.lock_file = lock_file
        self.snapshot: dict = {}
        self.snapshot_path = Path()

    def run(self) -> DeploymentResult:
        with DeploymentOperationLock(self.lock_file):
            return self._run_locked()

    def _run_locked(self) -> DeploymentResult:
        self.config.validate()
        self.snapshot_path = self._prepare_snapshot()

        if not self.runner.verify_image(
            image=self.config.candidate_image,
            source_revision=self.config.candidate_revision,
        ):
            return self._finish(EXIT_IDENTITY_FAILED, "candidate_identity_failed")
        if not self.runner.verify_image(
            image=self.config.previous_image,
            source_revision=self.config.previous_revision,
        ):
            return self._finish(EXIT_IDENTITY_FAILED, "previous_identity_failed")

        started = self.runner.start(
            compose_file=self.config.compose_file,
            image=self.config.candidate_image,
            source_revision=self.config.candidate_revision,
            service=self.config.service,
        )
        self._record("candidate_started" if started else "candidate_start_failed")
        if not started:
            return self._rollback()

        if self.runner.health(self.config.health_url):
            return self._finish(0, "candidate_healthy")
        self._record("candidate_health_failed")
        return self._rollback()

    def _rollback(self) -> DeploymentResult:
        restored = self.runner.start(
            compose_file=self.config.compose_file,
            image=self.config.previous_image,
            source_revision=self.config.previous_revision,
            service=self.config.service,
        )
        if not restored:
            return self._finish(EXIT_ROLLBACK_START_FAILED, "rollback_start_failed")
        self._record("rollback_started")
        if not self.runner.health(self.config.health_url):
            return self._finish(EXIT_ROLLBACK_HEALTH_FAILED, "rollback_health_failed")
        return self._finish(
            EXIT_CANDIDATE_FAILED_ROLLED_BACK,
            "rolled_back_healthy",
        )

    def _prepare_snapshot(self) -> Path:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        now = _now()
        deployment_id = uuid.uuid4().hex
        path = (
            self.config.state_dir
            / f"deployment-{now.replace(':', '')}-{deployment_id}.json"
        )
        self.snapshot_path = path
        self.snapshot = {
            "schema": "tapd-capability-deployment/v1",
            "created_at": now,
            "candidate": {
                "image": self.config.candidate_image,
                "source_revision": self.config.candidate_revision,
            },
            "previous": {
                "image": self.config.previous_image,
                "source_revision": self.config.previous_revision,
            },
            "configuration": {
                "schema": "tapd-capability-compose/v1",
                "compose_identity": CANONICAL_COMPOSE_IDENTITY,
                "compose_sha256": CANONICAL_COMPOSE_SHA256,
                "service": self.config.service,
                "host_bind": HOST_BIND,
                "container_port": CONTAINER_PORT,
                "persistent_volume": PERSISTENT_VOLUME.copy(),
                "health_url": self.config.health_url,
                "rootfs_read_only": True,
            },
            "history": [],
        }
        self._record("prepared")
        return path

    def _record(self, state: str) -> None:
        self.snapshot["state"] = state
        self.snapshot.setdefault("history", []).append({"state": state, "at": _now()})
        if self.snapshot_path:
            _atomic_json(self.snapshot_path, self.snapshot)

    def _finish(self, exit_code: int, state: str) -> DeploymentResult:
        self._record(state)
        return DeploymentResult(exit_code, state, self.snapshot_path)


class SubprocessRunner:
    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def verify_image(self, *, image: str, source_revision: str) -> bool:
        command = [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
            image,
        ]
        if self.dry_run:
            _print_dry_run(command)
            return True
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.returncode == 0 and completed.stdout.strip() == source_revision

    def start(
        self, *, compose_file: Path, image: str, source_revision: str, service: str
    ) -> bool:
        command = [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "up",
            "-d",
            "--no-build",
            service,
        ]
        if self.dry_run:
            _print_dry_run(command, image=image, source_revision=source_revision)
            return True
        environment = os.environ.copy()
        environment.pop("TAPD_ACCESS_TOKEN", None)
        environment["TAPD_CAPABILITY_IMAGE"] = image
        environment["TAPD_CAPABILITY_SOURCE_REVISION"] = source_revision
        return subprocess.run(command, check=False, env=environment).returncode == 0

    def health(self, url: str) -> bool:
        if self.dry_run:
            _print_dry_run(["GET", url])
            return True
        try:
            with urlopen(url, timeout=5) as response:
                return response.status == 200 and json.load(response) == {
                    "status": "ok"
                }
        except Exception:  # noqa: BLE001 - health failure is a state transition
            return False


def run_locked_deployment(
    config: DeploymentConfig, runner, *, lock_file: Path | None = None
) -> DeploymentResult:
    return DeploymentOrchestrator(config, runner, lock_file=lock_file).run()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _print_dry_run(command: list[str], **metadata: str) -> None:
    print(json.dumps({"dry_run": command, **metadata}, ensure_ascii=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--candidate-revision", required=True)
    parser.add_argument("--previous-image", required=True)
    parser.add_argument("--previous-revision", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path(__file__).resolve().with_name("compose.local.yml"),
    )
    parser.add_argument(
        "--health-url",
        default="http://127.0.0.1:3796/healthz",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = DeploymentConfig(
        candidate_image=args.candidate_image,
        candidate_revision=args.candidate_revision,
        previous_image=args.previous_image,
        previous_revision=args.previous_revision,
        compose_file=args.compose_file,
        state_dir=args.state_dir,
        health_url=args.health_url,
    )
    try:
        result = run_locked_deployment(
            config,
            SubprocessRunner(dry_run=args.dry_run),
        )
    except DeploymentBusyError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_DEPLOYMENT_BUSY
    except DeploymentLockUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_LOCK_UNAVAILABLE
    except ValueError as exc:
        print(f"deployment contract rejected: {exc}", file=sys.stderr)
        return 64
    print(
        json.dumps(
            {
                "state": result.state,
                "exit_code": result.exit_code,
                "snapshot": str(result.snapshot_path),
            },
            ensure_ascii=False,
        )
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
