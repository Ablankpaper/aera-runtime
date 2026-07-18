#!/usr/bin/env python3
"""Generate an AgentEra Runtime Ed25519 signing key pair without overwrite."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.agentera_runtime_dist.protocol import validate_key_id


def generate_key_pair(*, key_id: str, private_out: Path, public_out: Path) -> str:
    """Create a new key pair and return its non-secret SHA-256 fingerprint."""

    validate_key_id(key_id)
    private_out = Path(private_out).expanduser()
    public_out = Path(public_out).expanduser()
    if private_out.absolute() == public_out.absolute():
        raise ValueError("private and public output paths must differ")
    for output in (private_out, public_out):
        if output.exists() or output.is_symlink():
            raise FileExistsError(
                f"refusing to overwrite existing key output: {output}"
            )

    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = f"sha256:{hashlib.sha256(public_der).hexdigest()}"

    private_out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    public_out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_created = False
    try:
        _write_exclusive(private_out, private_pem, mode=0o600)
        private_created = True
        _write_exclusive(public_out, public_pem, mode=0o644)
    except Exception:
        if private_created:
            private_out.unlink(missing_ok=True)
        raise

    print(f"key_id={key_id}")
    print(f"public_fingerprint={fingerprint}")
    print(f"private_output={private_out}")
    print(f"public_output={public_out}")
    return fingerprint


def _write_exclusive(path: Path, data: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an Ed25519 key pair for AgentEra Runtime releases."
    )
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--private-out", required=True, type=Path)
    parser.add_argument("--public-out", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    generate_key_pair(
        key_id=args.key_id,
        private_out=args.private_out,
        public_out=args.public_out,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
