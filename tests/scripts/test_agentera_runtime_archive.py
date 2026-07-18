"""Tests for deterministic native AgentEra Runtime Seed archives."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest
import zstandard

from scripts.agentera_runtime_dist.archive import (
    ArchiveError,
    inspect_archive,
    write_deterministic_tar_zst,
    write_deterministic_zip,
)
from scripts.agentera_runtime_dist.inventory import build_inventory


@pytest.fixture()
def seed_root(tmp_path: Path) -> Path:
    root = tmp_path / "agentera-runtime"
    (root / "python" / "bin").mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "THIRD_PARTY_LICENSES").mkdir()
    python = root / "python" / "bin" / "python3"
    python.write_bytes(b"portable-python")
    python.chmod(0o755)
    hermes = root / "runtime" / "hermes"
    hermes.write_text("#!/bin/sh\n", encoding="utf-8")
    hermes.chmod(0o755)
    (root / "THIRD_PARTY_LICENSES" / "LICENSE.txt").write_text(
        "license\n", encoding="utf-8"
    )
    (root / "seed-info.json").write_text("{}\n", encoding="utf-8")
    return root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tar_members(path: Path) -> list[tarfile.TarInfo]:
    decompressed = zstandard.ZstdDecompressor().decompress(
        path.read_bytes(), max_output_size=8 * 1024 * 1024
    )
    with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:") as archive:
        return archive.getmembers()


def _normalized_zip_info(
    name: str, *, kind: str = "file", mode: int = 0o644
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    if kind == "directory":
        info.external_attr = ((stat.S_IFDIR | mode) << 16) | 0x10
    elif kind == "symlink":
        info.external_attr = (stat.S_IFLNK | mode) << 16
    else:
        info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def test_tar_zst_repeated_builds_are_byte_identical_and_inspectable(
    seed_root: Path, tmp_path: Path
):
    first = tmp_path / "first.tar.zst"
    second = tmp_path / "second.tar.zst"

    write_deterministic_tar_zst(seed_root, first)
    os.utime(seed_root / "runtime" / "hermes", (1_900_000_000, 1_900_000_000))
    write_deterministic_tar_zst(seed_root, second)

    assert _sha256(first) == _sha256(second)
    assert first.read_bytes() == second.read_bytes()
    assert inspect_archive(first) == build_inventory(seed_root)


def test_tar_metadata_is_normalized_and_executable_bits_survive(
    seed_root: Path, tmp_path: Path
):
    destination = tmp_path / "runtime.tar.zst"

    write_deterministic_tar_zst(seed_root, destination)
    members = _tar_members(destination)

    assert members[0].name == "agentera-runtime"
    assert [member.name for member in members] == sorted(
        member.name for member in members
    )
    assert all(member.mtime == 0 for member in members)
    assert all(member.uid == 0 and member.gid == 0 for member in members)
    assert all(member.uname == "" and member.gname == "" for member in members)
    hermes = next(
        member for member in members if member.name == "agentera-runtime/runtime/hermes"
    )
    assert stat.S_IMODE(hermes.mode) == 0o755


def test_zip_repeated_builds_are_byte_identical_and_use_posix_names(
    seed_root: Path, tmp_path: Path
):
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    write_deterministic_zip(seed_root, first)
    os.utime(seed_root / "python" / "bin" / "python3", (1_800_000_000, 1_800_000_000))
    write_deterministic_zip(seed_root, second)

    assert first.read_bytes() == second.read_bytes()
    assert inspect_archive(first) == build_inventory(seed_root)
    with zipfile.ZipFile(first) as archive:
        infos = archive.infolist()
    assert [info.filename for info in infos] == sorted(info.filename for info in infos)
    assert all("\\" not in info.filename for info in infos)
    assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
    hermes = next(
        info for info in infos if info.filename == "agentera-runtime/runtime/hermes"
    )
    assert stat.S_IMODE(hermes.external_attr >> 16) == 0o755


def test_relative_symlink_metadata_survives_both_archive_formats(
    seed_root: Path, tmp_path: Path
):
    link = seed_root / "runtime" / "python-link"
    try:
        link.symlink_to("../python/bin/python3")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    tar_path = tmp_path / "runtime.tar.zst"
    zip_path = tmp_path / "runtime.zip"
    write_deterministic_tar_zst(seed_root, tar_path)
    write_deterministic_zip(seed_root, zip_path)

    expected = build_inventory(seed_root)
    assert inspect_archive(tar_path) == expected
    assert inspect_archive(zip_path) == expected
    tar_link = next(
        member
        for member in _tar_members(tar_path)
        if member.name == "agentera-runtime/runtime/python-link"
    )
    assert tar_link.issym()
    assert tar_link.linkname == "../python/bin/python3"


def test_archive_writer_rejects_seed_with_prohibited_content(
    seed_root: Path, tmp_path: Path
):
    (seed_root / "python" / "MEMORY.md").write_text(
        "private learning", encoding="utf-8"
    )

    with pytest.raises(ArchiveError):
        write_deterministic_tar_zst(seed_root, tmp_path / "runtime.tar.zst")
    with pytest.raises(ArchiveError):
        write_deterministic_zip(seed_root, tmp_path / "runtime.zip")


def test_inspector_rejects_unsupported_or_path_traversing_archives(tmp_path: Path):
    unsupported = tmp_path / "runtime.bin"
    unsupported.write_bytes(b"not an archive")
    with pytest.raises(ArchiveError):
        inspect_archive(unsupported)

    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            _normalized_zip_info("agentera-runtime/", kind="directory", mode=0o755),
            b"",
        )
        archive.writestr(
            _normalized_zip_info("agentera-runtime/../escape.txt"), b"escape"
        )
    with pytest.raises(ArchiveError, match="archive member"):
        inspect_archive(malicious)


def test_inspector_rejects_broken_internal_symlink(tmp_path: Path):
    malicious = tmp_path / "broken-link.zip"
    with zipfile.ZipFile(malicious, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            _normalized_zip_info("agentera-runtime/", kind="directory", mode=0o755),
            b"",
        )
        archive.writestr(
            _normalized_zip_info(
                "agentera-runtime/runtime/link", kind="symlink", mode=0o777
            ),
            b"../python/missing",
        )

    with pytest.raises(ArchiveError, match="missing target"):
        inspect_archive(malicious)


def test_inspector_rejects_internal_symlink_cycle(tmp_path: Path):
    malicious = tmp_path / "link-cycle.zip"
    with zipfile.ZipFile(malicious, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            _normalized_zip_info("agentera-runtime/", kind="directory", mode=0o755),
            b"",
        )
        archive.writestr(
            _normalized_zip_info(
                "agentera-runtime/runtime/first", kind="symlink", mode=0o777
            ),
            b"second",
        )
        archive.writestr(
            _normalized_zip_info(
                "agentera-runtime/runtime/second", kind="symlink", mode=0o777
            ),
            b"first",
        )

    with pytest.raises(ArchiveError, match="cycle"):
        inspect_archive(malicious)
