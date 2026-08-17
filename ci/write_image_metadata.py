"""Write a non-secret build-only Docker identity receipt for later release gates."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REVISION = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_REF = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--test-image-id", required=True)
    parser.add_argument("--runtime-image-id", required=True)
    parser.add_argument("--runtime-label-revision", required=True)
    return parser


def build_receipt(args: argparse.Namespace) -> dict:
    if not SOURCE_REF.fullmatch(args.source_ref):
        raise ValueError("source ref must be a refs/heads branch ref")
    if not REVISION.fullmatch(args.source_revision):
        raise ValueError("source revision must be a full lowercase SHA")
    if args.runtime_label_revision != args.source_revision:
        raise ValueError("runtime image revision label does not match source revision")
    for name in ("test_image_id", "runtime_image_id"):
        if not IMAGE_ID.fullmatch(getattr(args, name)):
            raise ValueError(f"{name} must be a sha256 Docker image ID")

    return {
        "schema": "tapd-capability-ci-image/v1",
        "pipeline_contract": "tapd-capability-coding-v1-fixed-sha-build-no-deploy",
        "repository": args.repository,
        "source": {"ref": args.source_ref, "revision": args.source_revision},
        "images": {
            "test": {"local_image_id": args.test_image_id},
            "runtime": {
                "local_image_id": args.runtime_image_id,
                "revision_label": args.runtime_label_revision,
                "registry_digest": None,
            },
        },
        "effects": {"registry_push": False, "deployment": False},
        "release_gate": (
            "A separately authorized registry push must record an immutable "
            "repository@sha256 digest before deployment."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt = build_receipt(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
