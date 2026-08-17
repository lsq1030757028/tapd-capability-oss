"""Run Ruff check and format gates for every Python file changed from a base."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def changed_python_files(repo: Path, base: str, head: str = "HEAD") -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
            f"{base}...{head}",
            "--",
            "*.py",
        ],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return [os.fsdecode(path) for path in completed.stdout.split(b"\0") if path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ruff", default="ruff")
    args = parser.parse_args(argv)

    files = changed_python_files(args.repo, args.base, args.head)
    if not files:
        print("No changed Python files.")
        return 0

    print(f"Checking {len(files)} changed Python files.")
    subprocess.run([args.ruff, "check", "--", *files], cwd=args.repo, check=True)
    subprocess.run(
        [args.ruff, "format", "--check", "--", *files],
        cwd=args.repo,
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
