#!/usr/bin/env python3
"""Build one native, unsigned AgentEra Runtime Seed candidate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.agentera_runtime_dist.builder import (
    BuildConfig,
    assemble_runtime_seed,
    detect_host_target,
)
from scripts.agentera_runtime_dist.protocol import RuntimeTarget


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and smoke an unsigned native AgentEra Runtime Seed."
    )
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument(
        "--source-repository", default="Ablankpaper/aera-runtime", metavar="OWNER/NAME"
    )
    parser.add_argument("--minimum-desktop-version", default="0.1.0")
    parser.add_argument("--compatibility-gate-revision", type=int, default=1)
    parser.add_argument("--platform", choices=("darwin", "windows"))
    parser.add_argument("--arch", choices=("arm64", "x64"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if bool(args.platform) != bool(args.arch):
        raise SystemExit("--platform and --arch must be supplied together")
    target = (
        RuntimeTarget(args.platform, args.arch)
        if args.platform and args.arch
        else detect_host_target()
    )
    result = assemble_runtime_seed(
        BuildConfig(
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            runtime_version=args.runtime_version,
            source_commit=args.source_commit,
            python_executable=args.python,
            target=target,
            source_repository=args.source_repository,
            minimum_desktop_version=args.minimum_desktop_version,
            compatibility_gate_revision=args.compatibility_gate_revision,
        )
    )
    print(f"archive={result.archive_path}")
    print(f"archive_sha256={result.archive_sha256}")
    print(f"build_metadata={result.build_metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
