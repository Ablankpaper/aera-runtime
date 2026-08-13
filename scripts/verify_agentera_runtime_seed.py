#!/usr/bin/env python3
"""Verify and smoke a signed AgentEra Runtime Seed using public keys only."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.agentera_runtime_dist.builder import detect_host_target
from scripts.agentera_runtime_dist.protocol import (
    ManifestValidationContext,
    RuntimeTarget,
    validate_key_id,
)
from scripts.agentera_runtime_dist.smoke import verify_signed_runtime_seed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify signature, archive bytes, target, inventory, and Runtime smoke."
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--signature", required=True, type=Path)
    parser.add_argument(
        "--public-key",
        action="append",
        required=True,
        metavar="KEY_ID=PUBLIC_PEM",
        help="Trusted Ed25519 public key; may be repeated for rotation.",
    )
    parser.add_argument("--repository", default="Ablankpaper/aera-runtime")
    parser.add_argument("--desktop-version", required=True)
    parser.add_argument(
        "--channel", action="append", choices=("candidate", "stable"), default=None
    )
    parser.add_argument("--platform", choices=("darwin", "windows"))
    parser.add_argument("--arch", choices=("arm64", "x64"))
    parser.add_argument("--extract-dir", type=Path)
    return parser


def _load_public_keys(specifications: Sequence[str]) -> dict[str, bytes]:
    trusted: dict[str, bytes] = {}
    for specification in specifications:
        key_id, separator, raw_path = specification.partition("=")
        if not separator or not raw_path:
            raise ValueError("--public-key must use KEY_ID=PUBLIC_PEM")
        validate_key_id(key_id)
        if key_id in trusted:
            raise ValueError(f"duplicate Runtime public key id: {key_id}")
        pem = Path(raw_path).read_bytes()
        try:
            key = serialization.load_pem_public_key(pem)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid Runtime public key: {key_id}") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError(f"Runtime key is not an Ed25519 public key: {key_id}")
        trusted[key_id] = pem
    return trusted


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if bool(args.platform) != bool(args.arch):
        raise SystemExit("--platform and --arch must be supplied together")
    target = (
        RuntimeTarget(args.platform, args.arch)
        if args.platform and args.arch
        else detect_host_target()
    )
    context = ManifestValidationContext(
        repository=args.repository,
        target=target,
        desktop_version=args.desktop_version,
        allowed_channels=frozenset(args.channel or ("candidate", "stable")),
    )
    trusted = _load_public_keys(args.public_key)
    if args.extract_dir is not None:
        extracted = verify_signed_runtime_seed(
            archive_path=args.archive,
            manifest_path=args.manifest,
            signature_path=args.signature,
            trusted_public_keys=trusted,
            context=context,
            extraction_dir=args.extract_dir,
        )
        print(f"verified_runtime={extracted}")
    else:
        with tempfile.TemporaryDirectory(
            prefix="agentera-runtime-verified-"
        ) as temporary:
            extracted = verify_signed_runtime_seed(
                archive_path=args.archive,
                manifest_path=args.manifest,
                signature_path=args.signature,
                trusted_public_keys=trusted,
                context=context,
                extraction_dir=Path(temporary) / "extracted",
            )
            print(f"verified_runtime={extracted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
