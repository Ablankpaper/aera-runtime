"""Build and enforce the explicit file inventory for a Runtime Seed."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

_ALLOWED_TOP_LEVEL = frozenset({
    "python",
    "runtime",
    "THIRD_PARTY_LICENSES",
    "seed-info.json",
})
_REQUIRED_LAYOUT = {
    "python": "directory",
    "runtime": "directory",
    "THIRD_PARTY_LICENSES": "directory",
    "seed-info.json": "file",
}
_PROHIBITED_SEGMENTS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".cache",
    ".local-browsers",
    "cache",
    "caches",
    "tests",
    "test",
    "sessions",
    "logs",
    "log",
    "chromium",
    "chrome",
    "firefox",
    "webkit",
    "ms-playwright",
    "playwright-browsers",
    "models",
    "model-weights",
    "checkpoints",
})
_PROHIBITED_FILENAMES = frozenset({
    ".env",
    "auth.json",
    "memory.md",
    "user.md",
    "state.db",
})
_PROHIBITED_PRIVATE_KEY_SUFFIXES = frozenset({".key", ".p12", ".pfx", ".ppk"})
_PROHIBITED_WEIGHT_SUFFIXES = frozenset({
    ".gguf",
    ".safetensors",
    ".onnx",
    ".ckpt",
    ".pt",
})
_CONTEXTUAL_WEIGHT_SUFFIXES = frozenset({".bin", ".pth"})
_WEIGHT_NAME_MARKERS = ("model", "weight", "checkpoint")
_BROWSER_SEGMENT_PREFIXES = ("chromium-", "chrome-", "firefox-", "webkit-")
_PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
)


class InventoryError(ValueError):
    """The Seed tree is unsafe or outside the declared distribution layout."""


@dataclass(frozen=True)
class InventoryEntry:
    path: str
    kind: Literal["file", "directory", "symlink"]
    size: int
    sha256: str | None
    mode: int
    link_target: str | None = None


def build_inventory(root: Path) -> list[InventoryEntry]:
    """Return a sorted, normalized inventory without following symlinks."""

    root = Path(root)
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise InventoryError(f"cannot inspect Seed root: {root}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise InventoryError("Seed root must be a real directory, not a symlink")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise InventoryError("cannot resolve Seed root") from exc

    entries: list[InventoryEntry] = []
    _walk_directory(root, PurePosixPath(), resolved_root, entries)
    entries.sort(key=lambda entry: entry.path)
    return entries


def assert_seed_allowlist(root: Path, entries: Sequence[InventoryEntry]) -> None:
    """Reject omitted, unexpected, private, cached, user, or heavyweight content."""

    actual_entries = build_inventory(root)
    if list(entries) != actual_entries:
        raise InventoryError("provided inventory does not match the real Seed tree")
    by_path = {entry.path: entry for entry in actual_entries}
    for path, required_kind in _REQUIRED_LAYOUT.items():
        entry = by_path.get(path)
        if entry is None or entry.kind != required_kind:
            raise InventoryError(
                f"required Seed path is missing or has the wrong kind: {path}"
            )

    for entry in actual_entries:
        parts = PurePosixPath(entry.path).parts
        if not parts or parts[0] not in _ALLOWED_TOP_LEVEL:
            raise InventoryError(f"unexpected top-level Seed content: {entry.path}")
        _assert_entry_is_allowed(Path(root) / entry.path, entry, parts)


def validate_relative_path(path: str, *, label: str = "path") -> None:
    """Require a normalized relative POSIX path rooted inside the Seed."""

    if (
        not path
        or path.startswith("/")
        or path.endswith("/")
        or "\\" in path
        or "\x00" in path
        or "//" in path
    ):
        raise InventoryError(f"{label} is not a normalized relative POSIX path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise InventoryError(f"{label} escapes the Seed root")
    if str(pure) != path:
        raise InventoryError(f"{label} is not normalized")


def validate_symlink_target(path: str, target: str) -> None:
    """Lexically require a relative link target that stays inside the Seed."""

    if not target or target.startswith("/") or "\\" in target or "\x00" in target:
        raise InventoryError("symlink target must be a relative POSIX path")
    combined = PurePosixPath(path).parent.joinpath(PurePosixPath(target))
    normalized = os.path.normpath(str(combined)).replace(os.sep, "/")
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise InventoryError("symlink target escapes the Seed root")


def _walk_directory(
    directory: Path,
    relative_directory: PurePosixPath,
    resolved_root: Path,
    entries: list[InventoryEntry],
) -> None:
    try:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
    except OSError as exc:
        raise InventoryError(f"cannot read Seed directory: {directory}") from exc

    for child in children:
        relative = relative_directory / child.name
        relative_text = relative.as_posix()
        validate_relative_path(relative_text, label="inventory path")
        path = Path(child.path)
        try:
            child_stat = child.stat(follow_symlinks=False)
        except OSError as exc:
            raise InventoryError(f"cannot inspect Seed entry: {relative_text}") from exc

        if stat.S_ISLNK(child_stat.st_mode):
            entries.append(_symlink_entry(path, relative_text, resolved_root))
        elif stat.S_ISDIR(child_stat.st_mode):
            entries.append(
                InventoryEntry(
                    path=relative_text,
                    kind="directory",
                    size=0,
                    sha256=None,
                    mode=0o755,
                )
            )
            _walk_directory(path, relative, resolved_root, entries)
        elif stat.S_ISREG(child_stat.st_mode):
            entries.append(_file_entry(path, relative_text, child_stat.st_mode))
        else:
            raise InventoryError(f"unsupported special file in Seed: {relative_text}")


def _file_entry(path: Path, relative_path: str, raw_mode: int) -> InventoryEntry:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise InventoryError(f"cannot read Seed file: {relative_path}") from exc
    mode = 0o755 if stat.S_IMODE(raw_mode) & 0o111 else 0o644
    return InventoryEntry(
        path=relative_path,
        kind="file",
        size=size,
        sha256=digest.hexdigest(),
        mode=mode,
    )


def _symlink_entry(
    path: Path, relative_path: str, resolved_root: Path
) -> InventoryEntry:
    try:
        target = os.readlink(path)
    except OSError as exc:
        raise InventoryError(f"cannot read Seed symlink: {relative_path}") from exc
    validate_symlink_target(relative_path, target)
    try:
        resolved_target = path.resolve(strict=True)
        resolved_target.relative_to(resolved_root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise InventoryError(
            f"Seed symlink is broken or escapes the root: {relative_path}"
        ) from exc
    return InventoryEntry(
        path=relative_path,
        kind="symlink",
        size=0,
        sha256=None,
        mode=0o777,
        link_target=target,
    )


def _assert_entry_is_allowed(
    physical_path: Path, entry: InventoryEntry, parts: tuple[str, ...]
) -> None:
    folded_parts = tuple(part.casefold() for part in parts)
    if any(_is_prohibited_segment(part) for part in folded_parts):
        raise InventoryError(f"prohibited directory in Seed: {entry.path}")
    filename = folded_parts[-1]
    if filename in _PROHIBITED_FILENAMES or filename.startswith(".env."):
        raise InventoryError(f"prohibited user-state file in Seed: {entry.path}")
    suffix = PurePosixPath(filename).suffix
    if suffix in _PROHIBITED_PRIVATE_KEY_SUFFIXES:
        raise InventoryError(f"prohibited private-key file in Seed: {entry.path}")
    if suffix in _PROHIBITED_WEIGHT_SUFFIXES:
        raise InventoryError(f"prohibited model-weight file in Seed: {entry.path}")
    if suffix in _CONTEXTUAL_WEIGHT_SUFFIXES and any(
        marker in filename for marker in _WEIGHT_NAME_MARKERS
    ):
        raise InventoryError(f"prohibited model-weight file in Seed: {entry.path}")
    if entry.kind == "file" and suffix == ".pem":
        try:
            contains_private_key = _contains_private_key_marker(physical_path)
        except OSError as exc:
            raise InventoryError(f"cannot inspect PEM file: {entry.path}") from exc
        if contains_private_key:
            raise InventoryError(f"prohibited private key in Seed: {entry.path}")


def _is_prohibited_segment(segment: str) -> bool:
    return (
        segment in _PROHIBITED_SEGMENTS
        or segment.endswith(("-cache", "_cache", ".cache"))
        or segment.startswith(_BROWSER_SEGMENT_PREFIXES)
    )


def _contains_private_key_marker(path: Path) -> bool:
    longest_marker = max(len(marker) for marker in _PRIVATE_KEY_MARKERS)
    carry = b""
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            window = carry + chunk
            if any(marker in window for marker in _PRIVATE_KEY_MARKERS):
                return True
            carry = window[-(longest_marker - 1) :]
    return False
