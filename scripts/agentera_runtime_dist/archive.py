"""Deterministic TAR/Zstandard and ZIP archives for AgentEra Runtime Seeds."""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import IO

import zstandard

from scripts.agentera_runtime_dist.inventory import (
    InventoryEntry,
    InventoryError,
    assert_seed_allowlist,
    build_inventory,
    validate_relative_path,
    validate_symlink_target,
)

ARCHIVE_ROOT = "agentera-runtime"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class ArchiveError(ValueError):
    """A Runtime archive cannot be safely created or inspected."""


def write_deterministic_tar_zst(root: Path, destination: Path) -> None:
    """Write a deterministic single-threaded Zstandard-compressed TAR."""

    root, destination, entries = _prepare_write(root, destination, ".tar.zst")
    temporary = _temporary_path(destination, suffix=".tar.zst")
    try:
        with temporary.open("wb") as raw_output:
            compressor = zstandard.ZstdCompressor(
                level=19,
                threads=0,
                write_checksum=True,
                write_content_size=False,
                write_dict_id=False,
            )
            with compressor.stream_writer(raw_output, closefd=False) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT
                ) as archive:
                    archive.addfile(_tar_directory_info(ARCHIVE_ROOT, 0o755))
                    for entry in entries:
                        _write_tar_entry(archive, root, entry)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        if inspect_archive(temporary) != entries:
            raise ArchiveError("written TAR inventory differs from the Seed")
        _publish_temporary(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def write_deterministic_zip(root: Path, destination: Path) -> None:
    """Write a deterministic ZIP with normalized POSIX member names."""

    root, destination, entries = _prepare_write(root, destination, ".zip")
    temporary = _temporary_path(destination, suffix=".zip")
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            archive.writestr(_zip_info(f"{ARCHIVE_ROOT}/", "directory", 0o755), b"")
            for entry in sorted(entries, key=_zip_archive_name):
                _write_zip_entry(archive, root, entry)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        if inspect_archive(temporary) != entries:
            raise ArchiveError("written ZIP inventory differs from the Seed")
        _publish_temporary(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def inspect_archive(destination: Path) -> list[InventoryEntry]:
    """Read a supported archive into the same normalized inventory model."""

    destination = Path(destination)
    try:
        if destination.name.endswith(".tar.zst"):
            return _inspect_tar_zst(destination)
        if destination.name.endswith(".zip"):
            return _inspect_zip(destination)
    except ArchiveError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        tarfile.TarError,
        zipfile.BadZipFile,
        zstandard.ZstdError,
    ) as exc:
        raise ArchiveError(f"cannot inspect Runtime archive: {destination}") from exc
    raise ArchiveError(f"unsupported Runtime archive format: {destination.name}")


def _prepare_write(
    root: Path, destination: Path, expected_suffix: str
) -> tuple[Path, Path, list[InventoryEntry]]:
    root = Path(root)
    destination = Path(destination)
    if not destination.name.endswith(expected_suffix):
        raise ArchiveError(f"destination must end with {expected_suffix}")
    try:
        resolved_root = root.resolve(strict=True)
        resolved_destination = destination.resolve(strict=False)
        resolved_destination.relative_to(resolved_root)
    except FileNotFoundError as exc:
        raise ArchiveError("Seed root does not exist") from exc
    except ValueError:
        pass
    else:
        raise ArchiveError("archive destination must be outside the Seed root")

    try:
        entries = build_inventory(root)
        assert_seed_allowlist(root, entries)
    except InventoryError as exc:
        raise ArchiveError(str(exc)) from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    return root, destination, entries


def _temporary_path(destination: Path, *, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=suffix,
    )
    os.close(descriptor)
    return Path(name)


def _publish_temporary(temporary: Path, destination: Path) -> None:
    temporary.chmod(0o644)
    os.replace(temporary, destination)
    try:
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _normalized_tar_info(name: str, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = mode
    info.pax_headers = {}
    return info


def _tar_directory_info(name: str, mode: int) -> tarfile.TarInfo:
    info = _normalized_tar_info(name, mode)
    info.type = tarfile.DIRTYPE
    info.size = 0
    return info


def _write_tar_entry(
    archive: tarfile.TarFile, root: Path, entry: InventoryEntry
) -> None:
    archive_name = f"{ARCHIVE_ROOT}/{entry.path}"
    if entry.kind == "directory":
        archive.addfile(_tar_directory_info(archive_name, entry.mode))
        return
    info = _normalized_tar_info(archive_name, entry.mode)
    if entry.kind == "symlink":
        info.type = tarfile.SYMTYPE
        info.size = 0
        info.linkname = entry.link_target or ""
        archive.addfile(info)
        return
    info.type = tarfile.REGTYPE
    info.size = entry.size
    with (root / entry.path).open("rb") as source:
        archive.addfile(info, source)


def _zip_info(name: str, kind: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.comment = b""
    info.extra = b""
    if kind == "directory":
        info.external_attr = ((stat.S_IFDIR | mode) << 16) | 0x10
    elif kind == "symlink":
        info.external_attr = (stat.S_IFLNK | mode) << 16
    else:
        info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def _write_zip_entry(
    archive: zipfile.ZipFile, root: Path, entry: InventoryEntry
) -> None:
    archive_name = _zip_archive_name(entry)
    if entry.kind == "directory":
        archive.writestr(_zip_info(archive_name, entry.kind, entry.mode), b"")
    elif entry.kind == "symlink":
        archive.writestr(
            _zip_info(archive_name, entry.kind, entry.mode),
            (entry.link_target or "").encode("utf-8"),
        )
    else:
        info = _zip_info(archive_name, entry.kind, entry.mode)
        with archive.open(info, mode="w", force_zip64=True) as target:
            with (root / entry.path).open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)


def _zip_archive_name(entry: InventoryEntry) -> str:
    archive_name = f"{ARCHIVE_ROOT}/{entry.path}"
    return f"{archive_name}/" if entry.kind == "directory" else archive_name


def _inspect_tar_zst(destination: Path) -> list[InventoryEntry]:
    entries: list[InventoryEntry] = []
    names: list[str] = []
    root_seen = False
    with destination.open("rb") as compressed:
        with zstandard.ZstdDecompressor().stream_reader(compressed) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    names.append(member.name)
                    _assert_tar_metadata(member)
                    relative = _archive_relative_path(
                        member.name, is_directory=member.isdir()
                    )
                    if relative is None:
                        if root_seen or not member.isdir():
                            raise ArchiveError("archive root entry is invalid")
                        root_seen = True
                        continue
                    if member.isdir():
                        entry = InventoryEntry(
                            path=relative,
                            kind="directory",
                            size=0,
                            sha256=None,
                            mode=stat.S_IMODE(member.mode),
                        )
                    elif member.issym():
                        try:
                            validate_symlink_target(relative, member.linkname)
                        except InventoryError as exc:
                            raise ArchiveError(str(exc)) from exc
                        entry = InventoryEntry(
                            path=relative,
                            kind="symlink",
                            size=0,
                            sha256=None,
                            mode=stat.S_IMODE(member.mode),
                            link_target=member.linkname,
                        )
                    elif member.isfile():
                        source = archive.extractfile(member)
                        if source is None:
                            raise ArchiveError(f"cannot read TAR member: {member.name}")
                        digest, size = _hash_stream(source)
                        if size != member.size:
                            raise ArchiveError(
                                f"TAR member size differs: {member.name}"
                            )
                        entry = InventoryEntry(
                            path=relative,
                            kind="file",
                            size=size,
                            sha256=digest,
                            mode=stat.S_IMODE(member.mode),
                        )
                    else:
                        raise ArchiveError(f"unsupported TAR member: {member.name}")
                    entries.append(entry)
    _assert_archive_order_and_root(names, root_seen)
    _assert_unique_inventory(entries)
    _assert_symlink_targets_exist(entries)
    return entries


def _inspect_zip(destination: Path) -> list[InventoryEntry]:
    entries: list[InventoryEntry] = []
    names: list[str] = []
    root_seen = False
    with zipfile.ZipFile(destination, mode="r") as archive:
        if archive.comment:
            raise ArchiveError("ZIP archive comment must be empty")
        for info in archive.infolist():
            names.append(info.filename)
            _assert_zip_metadata(info)
            unix_mode = info.external_attr >> 16
            is_directory = info.is_dir() or stat.S_ISDIR(unix_mode)
            relative = _archive_relative_path(info.filename, is_directory=is_directory)
            if relative is None:
                if root_seen or not is_directory:
                    raise ArchiveError("archive root entry is invalid")
                root_seen = True
                continue
            if is_directory:
                entry = InventoryEntry(
                    path=relative,
                    kind="directory",
                    size=0,
                    sha256=None,
                    mode=stat.S_IMODE(unix_mode),
                )
            elif stat.S_ISLNK(unix_mode):
                target = archive.read(info).decode("utf-8")
                try:
                    validate_symlink_target(relative, target)
                except InventoryError as exc:
                    raise ArchiveError(str(exc)) from exc
                entry = InventoryEntry(
                    path=relative,
                    kind="symlink",
                    size=0,
                    sha256=None,
                    mode=stat.S_IMODE(unix_mode),
                    link_target=target,
                )
            elif stat.S_IFMT(unix_mode) in {0, stat.S_IFREG}:
                with archive.open(info, mode="r") as source:
                    digest, size = _hash_stream(source)
                if size != info.file_size:
                    raise ArchiveError(f"ZIP member size differs: {info.filename}")
                entry = InventoryEntry(
                    path=relative,
                    kind="file",
                    size=size,
                    sha256=digest,
                    mode=stat.S_IMODE(unix_mode),
                )
            else:
                raise ArchiveError(f"unsupported ZIP member: {info.filename}")
            entries.append(entry)
    _assert_archive_order_and_root(names, root_seen)
    _assert_unique_inventory(entries)
    _assert_symlink_targets_exist(entries)
    entries.sort(key=lambda entry: entry.path)
    return entries


def _archive_relative_path(name: str, *, is_directory: bool) -> str | None:
    if "\\" in name or "\x00" in name or name.startswith("/"):
        raise ArchiveError(f"unsafe archive member path: {name}")
    normalized_name = name[:-1] if is_directory and name.endswith("/") else name
    if normalized_name == ARCHIVE_ROOT:
        return None
    prefix = f"{ARCHIVE_ROOT}/"
    if not normalized_name.startswith(prefix):
        raise ArchiveError(f"archive member is outside {ARCHIVE_ROOT}: {name}")
    relative = normalized_name[len(prefix) :]
    try:
        validate_relative_path(relative, label="archive member")
    except InventoryError as exc:
        raise ArchiveError(str(exc)) from exc
    return relative


def _assert_tar_metadata(member: tarfile.TarInfo) -> None:
    if member.mtime != 0 or member.uid != 0 or member.gid != 0:
        raise ArchiveError(f"TAR metadata is not normalized: {member.name}")
    if member.uname or member.gname:
        raise ArchiveError(f"TAR owner names are not normalized: {member.name}")
    mode = stat.S_IMODE(member.mode)
    expected_modes = (
        {0o755} if member.isdir() else {0o777} if member.issym() else {0o644, 0o755}
    )
    if mode not in expected_modes:
        raise ArchiveError(f"TAR mode is not normalized: {member.name}")


def _assert_zip_metadata(info: zipfile.ZipInfo) -> None:
    if info.date_time != _ZIP_TIMESTAMP:
        raise ArchiveError(f"ZIP timestamp is not normalized: {info.filename}")
    if info.create_system != 3 or info.comment or info.extra:
        raise ArchiveError(f"ZIP metadata is not normalized: {info.filename}")
    unix_mode = info.external_attr >> 16
    if info.is_dir() or stat.S_ISDIR(unix_mode):
        expected_modes = {0o755}
    elif stat.S_ISLNK(unix_mode):
        expected_modes = {0o777}
    else:
        expected_modes = {0o644, 0o755}
    if stat.S_IMODE(unix_mode) not in expected_modes:
        raise ArchiveError(f"ZIP mode is not normalized: {info.filename}")


def _assert_archive_order_and_root(names: list[str], root_seen: bool) -> None:
    if not root_seen:
        raise ArchiveError(f"archive is missing its {ARCHIVE_ROOT} root")
    if names != sorted(names):
        raise ArchiveError("archive members are not sorted")


def _assert_unique_inventory(entries: list[InventoryEntry]) -> None:
    paths = [entry.path for entry in entries]
    if len(paths) != len(set(path.casefold() for path in paths)):
        raise ArchiveError("archive contains duplicate member paths")


def _assert_symlink_targets_exist(entries: list[InventoryEntry]) -> None:
    by_path = {entry.path: entry for entry in entries}
    for initial in entries:
        if initial.kind != "symlink":
            continue
        current = initial
        visited: set[str] = set()
        while current.kind == "symlink":
            if current.path in visited:
                raise ArchiveError(f"archive symlink cycle: {initial.path}")
            visited.add(current.path)
            target_path = posixpath.normpath(
                posixpath.join(
                    posixpath.dirname(current.path), current.link_target or ""
                )
            )
            target = by_path.get(target_path)
            if target is None:
                raise ArchiveError(
                    f"archive symlink has a missing target: {initial.path}"
                )
            current = target


def _hash_stream(source: IO[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size
