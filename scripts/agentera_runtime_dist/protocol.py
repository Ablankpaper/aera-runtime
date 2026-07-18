"""Canonical manifest and Ed25519 signature protocol for Runtime artifacts."""

from __future__ import annotations

import base64
import binascii
import json
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Literal, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from packaging.version import InvalidVersion, Version

MANIFEST_SCHEMA_VERSION = 1
SIGNATURE_SCHEMA_VERSION = 1
SIGNATURE_ALGORITHM = "Ed25519"
SUPPORTED_CHANNELS = frozenset({"candidate", "stable"})
SUPPORTED_TARGETS = frozenset({("darwin", "arm64"), ("windows", "x64")})

_MANIFEST_FIELDS = frozenset({
    "schema_version",
    "key_id",
    "runtime_version",
    "source_repository",
    "source_commit",
    "channel",
    "platform",
    "arch",
    "archive_name",
    "archive_size",
    "archive_sha256",
    "python_version",
    "entrypoints",
    "minimum_desktop_version",
    "compatibility_gate_revision",
    "created_at",
    "files",
})
_SIGNATURE_FIELDS = frozenset({
    "schema_version",
    "key_id",
    "algorithm",
    "signature_base64",
})
_ENTRYPOINT_FIELDS = frozenset({"python", "hermes", "module"})
_INVENTORY_FIELDS = frozenset({"path", "kind", "size", "sha256", "mode", "link_target"})
_KEY_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_VERSION_LABEL_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}\Z")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PYTHON_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_ARCHIVE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")


class ProtocolError(ValueError):
    """The manifest or signature envelope violates the distribution protocol."""


class SignatureVerificationError(ProtocolError):
    """An Ed25519 signature or its key binding is invalid."""


class UnknownSigningKeyError(SignatureVerificationError):
    """The signature names a key that is not in the consumer trust set."""


@dataclass(frozen=True)
class RuntimeTarget:
    platform: Literal["darwin", "windows"]
    arch: Literal["arm64", "x64"]

    def __post_init__(self) -> None:
        if (self.platform, self.arch) not in SUPPORTED_TARGETS:
            raise ProtocolError(
                f"unsupported Runtime target: {self.platform}-{self.arch}"
            )


@dataclass(frozen=True)
class ManifestValidationContext:
    repository: str
    target: RuntimeTarget
    desktop_version: str
    allowed_channels: frozenset[str]

    def __post_init__(self) -> None:
        if not _REPOSITORY_RE.fullmatch(self.repository):
            raise ProtocolError("validation repository must be owner/name")
        _parse_version(self.desktop_version, "desktop_version")
        if not self.allowed_channels:
            raise ProtocolError("allowed_channels must not be empty")
        if not self.allowed_channels <= SUPPORTED_CHANNELS:
            raise ProtocolError("allowed_channels contains an unknown channel")


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    """Serialize a JSON object to the one accepted canonical representation."""

    if not isinstance(value, Mapping):
        raise ProtocolError("canonical JSON root must be an object")
    _assert_ascii_object_keys(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolError("value is not canonical JSON data") from exc
    return text.encode("utf-8")


def parse_manifest(raw: bytes) -> dict[str, object]:
    """Parse canonical manifest bytes and validate their context-free schema."""

    value = _parse_canonical_object(raw, "manifest")
    _validate_manifest_shape(value)
    return value


def validate_manifest(
    manifest: Mapping[str, object], context: ManifestValidationContext
) -> None:
    """Validate a manifest against the expected repository and desktop target."""

    _validate_manifest_shape(manifest)
    if manifest["source_repository"] != context.repository:
        raise ProtocolError("manifest source repository does not match")
    if (manifest["platform"], manifest["arch"]) != (
        context.target.platform,
        context.target.arch,
    ):
        raise ProtocolError("manifest target does not match")
    if manifest["channel"] not in context.allowed_channels:
        raise ProtocolError("manifest channel is not allowed")

    desktop_version = _parse_version(context.desktop_version, "desktop_version")
    minimum_version = _parse_version(
        _require_string(manifest, "minimum_desktop_version"),
        "minimum_desktop_version",
    )
    if desktop_version < minimum_version:
        raise ProtocolError("desktop version is below the manifest minimum")


def sign_bytes(raw: bytes, private_key_pem: bytes) -> bytes:
    """Sign exact bytes with a PEM-encoded Ed25519 private key."""

    if not isinstance(raw, bytes) or not isinstance(private_key_pem, bytes):
        raise SignatureVerificationError("signature inputs must be bytes")
    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
    except (TypeError, ValueError) as exc:
        raise SignatureVerificationError("invalid private key PEM") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureVerificationError("private key is not Ed25519")
    return key.sign(raw)


def verify_bytes(raw: bytes, signature: bytes, public_key_pem: bytes) -> None:
    """Verify an Ed25519 signature over exact raw bytes."""

    if not all(isinstance(value, bytes) for value in (raw, signature, public_key_pem)):
        raise SignatureVerificationError("verification inputs must be bytes")
    try:
        key = serialization.load_pem_public_key(public_key_pem)
    except (TypeError, ValueError) as exc:
        raise SignatureVerificationError("invalid public key PEM") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureVerificationError("public key is not Ed25519")
    try:
        key.verify(signature, raw)
    except (InvalidSignature, ValueError) as exc:
        raise SignatureVerificationError("manifest signature is invalid") from exc


def create_signature_envelope(key_id: str, signature: bytes) -> bytes:
    """Create canonical bytes for a detached Ed25519 signature envelope."""

    validate_key_id(key_id)
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ProtocolError("Ed25519 signature must be exactly 64 bytes")
    return canonical_json_bytes({
        "schema_version": SIGNATURE_SCHEMA_VERSION,
        "key_id": key_id,
        "algorithm": SIGNATURE_ALGORITHM,
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    })


def parse_signature_envelope(raw: bytes) -> dict[str, object]:
    """Parse and strictly validate canonical detached-signature metadata."""

    value = _parse_canonical_object(raw, "signature envelope")
    _require_exact_fields(value, _SIGNATURE_FIELDS, "signature envelope")
    if _require_integer(value, "schema_version", minimum=1) != SIGNATURE_SCHEMA_VERSION:
        raise ProtocolError("unsupported signature schema_version")
    key_id = _require_string(value, "key_id")
    validate_key_id(key_id)
    if value["algorithm"] != SIGNATURE_ALGORITHM:
        raise ProtocolError("unsupported signature algorithm")
    signature_text = _require_string(value, "signature_base64")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError("signature_base64 is invalid") from exc
    if len(signature) != 64:
        raise ProtocolError("Ed25519 signature must be exactly 64 bytes")
    if base64.b64encode(signature).decode("ascii") != signature_text:
        raise ProtocolError("signature_base64 is not canonical")
    return value


def verify_manifest_signature(
    manifest_raw: bytes,
    signature_raw: bytes,
    trusted_public_keys: Mapping[str, bytes],
) -> dict[str, object]:
    """Verify key binding and signature, returning the parsed manifest."""

    manifest = parse_manifest(manifest_raw)
    envelope = parse_signature_envelope(signature_raw)
    key_id = _require_string(envelope, "key_id")
    public_key = trusted_public_keys.get(key_id)
    if public_key is None:
        raise UnknownSigningKeyError(f"unknown Runtime signing key: {key_id}")
    if manifest["key_id"] != key_id:
        raise SignatureVerificationError("manifest and signature key ids differ")
    signature = base64.b64decode(
        _require_string(envelope, "signature_base64"), validate=True
    )
    verify_bytes(manifest_raw, signature, public_key)
    return manifest


def validate_key_id(key_id: str) -> None:
    """Validate the stable identifier used to select a trusted public key."""

    if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
        raise ProtocolError("invalid Runtime signing key id")


def _parse_canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise ProtocolError(f"{label} must be bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"{label} is not valid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except (json.JSONDecodeError, ProtocolError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} root must be an object")
    if canonical_json_bytes(value) != raw:
        raise ProtocolError(f"{label} bytes are not canonical JSON")
    return value


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _assert_ascii_object_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError("JSON object keys must be strings")
            if not key.isascii():
                raise ProtocolError("JSON object keys must be ASCII")
            _assert_ascii_object_keys(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _assert_ascii_object_keys(item)


def _validate_manifest_shape(manifest: Mapping[str, object]) -> None:
    _require_exact_fields(manifest, _MANIFEST_FIELDS, "manifest")
    if (
        _require_integer(manifest, "schema_version", minimum=1)
        != MANIFEST_SCHEMA_VERSION
    ):
        raise ProtocolError("unsupported manifest schema_version")

    key_id = _require_string(manifest, "key_id")
    validate_key_id(key_id)
    runtime_version = _require_string(manifest, "runtime_version")
    if not _VERSION_LABEL_RE.fullmatch(runtime_version):
        raise ProtocolError("invalid runtime_version")
    repository = _require_string(manifest, "source_repository")
    if not _REPOSITORY_RE.fullmatch(repository):
        raise ProtocolError("source_repository must be owner/name")
    source_commit = _require_string(manifest, "source_commit")
    if not _SHA1_RE.fullmatch(source_commit):
        raise ProtocolError("source_commit must be a full lowercase Git SHA")

    channel = _require_string(manifest, "channel")
    if channel not in SUPPORTED_CHANNELS:
        raise ProtocolError("unknown Runtime release channel")
    platform = _require_string(manifest, "platform")
    arch = _require_string(manifest, "arch")
    if (platform, arch) not in SUPPORTED_TARGETS:
        raise ProtocolError("unsupported Runtime platform/architecture")

    archive_name = _require_string(manifest, "archive_name")
    _validate_archive_name(archive_name, platform, arch)
    _require_integer(manifest, "archive_size", minimum=1)
    _require_sha256(manifest, "archive_sha256")
    python_version = _require_string(manifest, "python_version")
    if not _PYTHON_VERSION_RE.fullmatch(python_version):
        raise ProtocolError("python_version must contain major.minor.patch")
    _parse_version(
        _require_string(manifest, "minimum_desktop_version"),
        "minimum_desktop_version",
    )
    _require_integer(manifest, "compatibility_gate_revision", minimum=1)
    _validate_created_at(_require_string(manifest, "created_at"))

    raw_entrypoints = manifest["entrypoints"]
    if not isinstance(raw_entrypoints, Mapping):
        raise ProtocolError("entrypoints must be an object")
    entrypoints = cast(Mapping[str, object], raw_entrypoints)
    _require_exact_fields(entrypoints, _ENTRYPOINT_FIELDS, "entrypoints")
    python_entry = _require_string(entrypoints, "python")
    hermes_entry = _require_string(entrypoints, "hermes")
    _validate_relative_posix_path(python_entry, "entrypoints.python")
    _validate_relative_posix_path(hermes_entry, "entrypoints.hermes")
    if entrypoints["module"] != "hermes_cli.main":
        raise ProtocolError("entrypoints.module must be hermes_cli.main")

    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise ProtocolError("files must be a non-empty array")
    paths: list[str] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, Mapping):
            raise ProtocolError(f"files[{index}] must be an object")
        paths.append(_validate_inventory_entry(entry, index))
    if paths != sorted(paths):
        raise ProtocolError("files inventory must be sorted by path")
    comparable_paths = (
        [path.casefold() for path in paths] if platform == "windows" else paths
    )
    if len(set(comparable_paths)) != len(comparable_paths):
        raise ProtocolError("files inventory contains duplicate paths")
    if python_entry not in paths or hermes_entry not in paths:
        raise ProtocolError("entrypoints must exist in the files inventory")


def _validate_inventory_entry(entry: Mapping[str, object], index: int) -> str:
    label = f"files[{index}]"
    _require_exact_fields(entry, _INVENTORY_FIELDS, label)
    path = _require_string(entry, "path")
    _validate_relative_posix_path(path, f"{label}.path")
    kind = _require_string(entry, "kind")
    if kind not in {"file", "directory", "symlink"}:
        raise ProtocolError(f"{label}.kind is invalid")
    size = _require_integer(entry, "size", minimum=0)
    mode = _require_integer(entry, "mode", minimum=0)
    if mode > 0o7777:
        raise ProtocolError(f"{label}.mode is out of range")

    sha256 = entry["sha256"]
    link_target = entry["link_target"]
    if kind == "file":
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise ProtocolError(f"{label}.sha256 is invalid")
        if link_target is not None:
            raise ProtocolError(f"{label}.link_target must be null for a file")
    elif kind == "directory":
        if size != 0 or sha256 is not None or link_target is not None:
            raise ProtocolError(f"{label} has invalid directory metadata")
    else:
        if sha256 is not None or not isinstance(link_target, str) or not link_target:
            raise ProtocolError(f"{label} has invalid symlink metadata")
        _validate_symlink_target(path, link_target, f"{label}.link_target")
    return path


def _validate_archive_name(name: str, platform: str, arch: str) -> None:
    if not _ARCHIVE_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ProtocolError("archive_name must be a plain file name")
    if not name.isascii() or not name.startswith("agentera-runtime-"):
        raise ProtocolError("archive_name has an invalid prefix")
    expected_suffix = ".tar.zst" if platform == "darwin" else ".zip"
    if not name.endswith(f"-{platform}-{arch}{expected_suffix}"):
        raise ProtocolError("archive_name does not match the Runtime target")


def _validate_relative_posix_path(value: str, label: str) -> None:
    if (
        not value
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or "\x00" in value
        or "//" in value
    ):
        raise ProtocolError(f"{label} must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProtocolError(f"{label} escapes the Runtime root")
    if str(path) != value:
        raise ProtocolError(f"{label} is not normalized")


def _validate_symlink_target(path: str, target: str, label: str) -> None:
    if target.startswith("/") or "\\" in target or "\x00" in target:
        raise ProtocolError(f"{label} must be a relative POSIX path")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        raise ProtocolError(f"{label} escapes the Runtime root")


def _validate_created_at(value: str) -> None:
    if not value.endswith("Z"):
        raise ProtocolError("created_at must use the UTC Z suffix")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProtocolError("created_at must be RFC3339") from exc
    if parsed.tzinfo != timezone.utc:
        raise ProtocolError("created_at must be UTC")


def _require_exact_fields(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProtocolError(f"{label} fields differ: missing={missing}, extra={extra}")


def _require_string(value: Mapping[str, object], field: str) -> str:
    item = value[field]
    if not isinstance(item, str) or not item:
        raise ProtocolError(f"{field} must be a non-empty string")
    return item


def _require_integer(value: Mapping[str, object], field: str, *, minimum: int) -> int:
    item = value[field]
    if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
        raise ProtocolError(f"{field} must be an integer >= {minimum}")
    return item


def _require_sha256(value: Mapping[str, object], field: str) -> str:
    item = _require_string(value, field)
    if not _SHA256_RE.fullmatch(item):
        raise ProtocolError(f"{field} must be a lowercase SHA-256")
    return item


def _parse_version(value: str, label: str) -> Version:
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise ProtocolError(f"{label} is not a valid version") from exc
