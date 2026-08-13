"""Contract tests for signed AgentEra Runtime distribution manifests."""

from __future__ import annotations

import base64
import json
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.agentera_runtime_dist.protocol import (
    ManifestValidationContext,
    ProtocolError,
    RuntimeTarget,
    SignatureVerificationError,
    UnknownSigningKeyError,
    canonical_json_bytes,
    create_signature_envelope,
    parse_manifest,
    parse_signature_envelope,
    sign_bytes,
    validate_manifest,
    verify_bytes,
    verify_manifest_signature,
)
from scripts.generate_agentera_runtime_key import generate_key_pair


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _private_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_pem(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _manifest(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "key_id": "agentera-runtime-2026-01",
        "runtime_version": "0.18.2-agentera.1",
        "source_repository": "Ablankpaper/aera-runtime",
        "source_commit": "a" * 40,
        "channel": "candidate",
        "platform": "darwin",
        "arch": "arm64",
        "archive_name": ("agentera-runtime-0.18.2-agentera.1-darwin-arm64.tar.zst"),
        "archive_size": 2048,
        "archive_sha256": "b" * 64,
        "python_version": "3.11.15",
        "entrypoints": {
            "python": "python/bin/python3",
            "hermes": "runtime/hermes",
            "module": "hermes_cli.main",
        },
        "minimum_desktop_version": "0.7.3",
        "compatibility_gate_revision": 1,
        "created_at": "2026-07-18T04:05:06Z",
        "files": [
            {
                "path": "python/bin/python3",
                "kind": "file",
                "size": 1024,
                "sha256": "c" * 64,
                "mode": 0o755,
                "link_target": None,
            },
            {
                "path": "runtime/hermes",
                "kind": "file",
                "size": 512,
                "sha256": "d" * 64,
                "mode": 0o755,
                "link_target": None,
            },
        ],
    }
    value.update(overrides)
    return value


def _context(**overrides: object) -> ManifestValidationContext:
    values: dict[str, object] = {
        "repository": "Ablankpaper/aera-runtime",
        "target": RuntimeTarget(platform="darwin", arch="arm64"),
        "desktop_version": "0.7.3",
        "allowed_channels": frozenset({"candidate", "stable"}),
    }
    values.update(overrides)
    return ManifestValidationContext(**values)  # type: ignore[arg-type]


def test_canonical_json_bytes_sorts_keys_without_insignificant_whitespace():
    assert canonical_json_bytes({"z": 1, "a": {"d": 4, "b": 2}}) == (
        b'{"a":{"b":2,"d":4},"z":1}'
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1, "key_id":"x"}',
        b'{"key_id":"x","schema_version":1}\n',
        b'{"schema_version":1,"schema_version":1}',
        '{"sch\u00e9ma_version":1}'.encode(),
    ],
)
def test_parse_manifest_rejects_noncanonical_duplicate_or_non_ascii_keys(raw: bytes):
    with pytest.raises(ProtocolError):
        parse_manifest(raw)


def test_ed25519_signatures_cover_the_exact_raw_manifest_bytes():
    private_key = _private_key()
    raw = canonical_json_bytes(_manifest())
    signature = sign_bytes(raw, _private_pem(private_key))

    verify_bytes(raw, signature, _public_pem(private_key))

    with pytest.raises(SignatureVerificationError):
        verify_bytes(raw + b"\n", signature, _public_pem(private_key))

    with pytest.raises(SignatureVerificationError):
        verify_bytes(raw, signature, _public_pem(_private_key()))


def test_signature_envelope_is_canonical_and_has_an_exact_schema():
    signature = b"s" * 64
    raw = create_signature_envelope("agentera-runtime-2026-01", signature)

    assert parse_signature_envelope(raw) == {
        "algorithm": "Ed25519",
        "key_id": "agentera-runtime-2026-01",
        "schema_version": 1,
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    }

    envelope = json.loads(raw)
    envelope["unexpected"] = True
    with pytest.raises(ProtocolError):
        parse_signature_envelope(canonical_json_bytes(envelope))


def test_key_rotation_selects_the_key_named_by_the_signed_envelope():
    old_key = _private_key()
    new_key = _private_key()
    manifest = _manifest(key_id="agentera-runtime-2026-02")
    raw = canonical_json_bytes(manifest)
    envelope = create_signature_envelope(
        "agentera-runtime-2026-02", sign_bytes(raw, _private_pem(new_key))
    )

    parsed = verify_manifest_signature(
        raw,
        envelope,
        {
            "agentera-runtime-2026-01": _public_pem(old_key),
            "agentera-runtime-2026-02": _public_pem(new_key),
        },
    )

    assert parsed["key_id"] == "agentera-runtime-2026-02"


def test_unknown_key_id_is_rejected_before_signature_verification():
    key = _private_key()
    raw = canonical_json_bytes(_manifest(key_id="agentera-runtime-2026-99"))
    envelope = create_signature_envelope(
        "agentera-runtime-2026-99", sign_bytes(raw, _private_pem(key))
    )

    with pytest.raises(UnknownSigningKeyError):
        verify_manifest_signature(raw, envelope, {})


def test_manifest_and_signature_envelope_must_name_the_same_key():
    key = _private_key()
    raw = canonical_json_bytes(_manifest(key_id="agentera-runtime-2026-01"))
    envelope = create_signature_envelope(
        "agentera-runtime-2026-02", sign_bytes(raw, _private_pem(key))
    )

    with pytest.raises(SignatureVerificationError):
        verify_manifest_signature(
            raw,
            envelope,
            {"agentera-runtime-2026-02": _public_pem(key)},
        )


def test_valid_manifest_matches_the_expected_consumer_context():
    manifest = parse_manifest(canonical_json_bytes(_manifest()))

    validate_manifest(manifest, _context())


@pytest.mark.parametrize(
    ("overrides", "context"),
    [
        ({"schema_version": 2}, _context()),
        ({"source_repository": "NousResearch/hermes-agent"}, _context()),
        ({"source_commit": "a" * 39}, _context()),
        ({"source_commit": "G" * 40}, _context()),
        ({"channel": "nightly"}, _context()),
        ({"platform": "windows", "arch": "x64"}, _context()),
        ({"archive_name": "../runtime.tar.zst"}, _context()),
        ({"archive_name": "/tmp/runtime.tar.zst"}, _context()),
        ({"archive_name": "runtime\\seed.tar.zst"}, _context()),
        (
            {"archive_name": ("agentera-runtime-evil\n-darwin-arm64.tar.zst")},
            _context(),
        ),
        (
            {"archive_name": "agentera-runtime-C:-darwin-arm64.tar.zst"},
            _context(),
        ),
        ({"archive_sha256": "b" * 63}, _context()),
        ({"minimum_desktop_version": "0.7.4"}, _context()),
        ({"created_at": "2026-07-18T12:05:06+08:00"}, _context()),
    ],
)
def test_manifest_rejects_unknown_or_incompatible_values(
    overrides: dict[str, object], context: ManifestValidationContext
):
    manifest = _manifest(**overrides)

    with pytest.raises(ProtocolError):
        validate_manifest(manifest, context)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("archive_size", True),
        ("archive_size", -1),
        ("compatibility_gate_revision", False),
        ("compatibility_gate_revision", 0),
    ],
)
def test_boolean_or_out_of_range_integer_fields_are_rejected(field: str, value: object):
    with pytest.raises(ProtocolError):
        validate_manifest(_manifest(**{field: value}), _context())


def test_manifest_rejects_extra_top_level_fields():
    manifest = _manifest()
    manifest["download_url"] = "https://untrusted.invalid/runtime"

    with pytest.raises(ProtocolError):
        validate_manifest(manifest, _context())


def test_inventory_paths_must_be_sorted_and_unique():
    files = cast(list[dict[str, object]], deepcopy(_manifest()["files"]))
    files.reverse()
    with pytest.raises(ProtocolError):
        validate_manifest(_manifest(files=files), _context())

    duplicate = cast(list[dict[str, object]], deepcopy(_manifest()["files"]))
    duplicate.append(deepcopy(duplicate[-1]))
    with pytest.raises(ProtocolError):
        validate_manifest(_manifest(files=duplicate), _context())


@pytest.mark.parametrize(
    "path",
    ["../python/bin/python3", "/python/bin/python3", "python\\bin\\python3"],
)
def test_inventory_rejects_escaping_or_non_posix_paths(path: str):
    files = cast(list[dict[str, object]], deepcopy(_manifest()["files"]))
    files[0]["path"] = path

    with pytest.raises(ProtocolError):
        validate_manifest(_manifest(files=files), _context())


def test_runtime_target_rejects_unsupported_platform_arch_pairs():
    with pytest.raises(ProtocolError):
        RuntimeTarget(platform="darwin", arch="x64")

    with pytest.raises(ProtocolError):
        RuntimeTarget(platform=cast(Any, "linux"), arch="x64")


def test_generate_key_pair_writes_private_key_0600_and_prints_no_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    private_path = tmp_path / "private" / "runtime.pem"
    public_path = tmp_path / "public" / "runtime.public.pem"

    fingerprint = generate_key_pair(
        key_id="agentera-runtime-2026-01",
        private_out=private_path,
        public_out=public_path,
    )

    captured = capsys.readouterr().out
    assert private_path.exists() and public_path.exists()
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert "PRIVATE KEY" in private_path.read_text(encoding="ascii")
    assert "PUBLIC KEY" in public_path.read_text(encoding="ascii")
    assert "PRIVATE KEY" not in captured
    assert private_path.read_text(encoding="ascii") not in captured
    assert fingerprint in captured
    assert "agentera-runtime-2026-01" in captured


def test_key_generator_script_is_directly_executable_from_the_repository():
    repository = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "generate_agentera_runtime_key.py"),
            "--help",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--private-out" in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("existing", ["private", "public"])
def test_generate_key_pair_refuses_to_overwrite_either_output(
    tmp_path: Path, existing: str
):
    private_path = tmp_path / "runtime.pem"
    public_path = tmp_path / "runtime.public.pem"
    target = private_path if existing == "private" else public_path
    target.write_text("keep-me", encoding="utf-8")

    with pytest.raises(FileExistsError):
        generate_key_pair(
            key_id="agentera-runtime-2026-01",
            private_out=private_path,
            public_out=public_path,
        )

    assert target.read_text(encoding="utf-8") == "keep-me"
    other = public_path if existing == "private" else private_path
    assert not other.exists()
