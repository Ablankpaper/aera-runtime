"""Tests for the explicit AgentEra Runtime Seed file inventory."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from scripts.agentera_runtime_dist.inventory import (
    InventoryError,
    InventoryEntry,
    assert_seed_allowlist,
    build_inventory,
)


@pytest.fixture()
def seed_root(tmp_path: Path) -> Path:
    root = tmp_path / "agentera-runtime"
    (root / "python" / "bin").mkdir(parents=True)
    (root / "python" / "lib").mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "THIRD_PARTY_LICENSES").mkdir()

    python = root / "python" / "bin" / "python3"
    python.write_bytes(b"portable-python")
    python.chmod(0o751)
    (root / "python" / "lib" / "runtime.txt").write_text(
        "locked dependency\n", encoding="utf-8"
    )
    hermes = root / "runtime" / "hermes"
    hermes.write_text("#!/bin/sh\nexec python3 -m hermes_cli.main\n", encoding="utf-8")
    hermes.chmod(0o700)
    (root / "THIRD_PARTY_LICENSES" / "LICENSE.txt").write_text(
        "dependency license\n", encoding="utf-8"
    )
    (root / "seed-info.json").write_text("{}\n", encoding="utf-8")
    return root


def _entry(entries: list[InventoryEntry], path: str) -> InventoryEntry:
    return next(entry for entry in entries if entry.path == path)


def test_build_inventory_is_sorted_hashed_and_mode_normalized(seed_root: Path):
    entries = build_inventory(seed_root)

    assert [entry.path for entry in entries] == sorted(entry.path for entry in entries)
    python = _entry(entries, "python/bin/python3")
    data = _entry(entries, "python/lib/runtime.txt")
    directory = _entry(entries, "python/bin")
    assert python.kind == "file"
    assert python.mode == 0o755
    assert python.size == len(b"portable-python")
    assert python.sha256 == hashlib.sha256(b"portable-python").hexdigest()
    assert data.mode == 0o644
    assert directory == InventoryEntry(
        path="python/bin",
        kind="directory",
        size=0,
        sha256=None,
        mode=0o755,
        link_target=None,
    )


def test_relative_symlink_that_resolves_inside_seed_is_preserved(seed_root: Path):
    link = seed_root / "runtime" / "python-link"
    try:
        link.symlink_to("../python/bin/python3")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    entries = build_inventory(seed_root)
    assert_seed_allowlist(seed_root, entries)

    assert _entry(entries, "runtime/python-link") == InventoryEntry(
        path="runtime/python-link",
        kind="symlink",
        size=0,
        sha256=None,
        mode=0o777,
        link_target="../python/bin/python3",
    )


@pytest.mark.parametrize("target", ["/tmp/outside", "../../outside"])
def test_absolute_or_out_of_root_symlink_is_rejected(seed_root: Path, target: str):
    link = seed_root / "runtime" / "escape"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(InventoryError):
        build_inventory(seed_root)


def test_symlink_chain_that_eventually_escapes_is_rejected(seed_root: Path):
    outside = seed_root.parent / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        (seed_root / "python" / "outside-link").symlink_to(outside)
        (seed_root / "runtime" / "chained-link").symlink_to("../python/outside-link")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(InventoryError):
        build_inventory(seed_root)


@pytest.mark.parametrize(
    "relative_path",
    [
        "python/.git/config",
        "python/.env",
        "python/.env.production",
        "python/auth.json",
        "python/MEMORY.md",
        "python/USER.md",
        "python/state.db",
        "python/sessions/session.json",
        "python/logs/runtime.log",
        "python/__pycache__/module.pyc",
        "python/.pytest_cache/state",
        "python/.venv/bin/python",
        "python/tests/test_runtime.py",
        "python/cache/download.bin.tmp",
        "python/pip-cache/wheel.tmp",
        "python/chromium/chrome",
        "python/.local-browsers/chromium-123/chrome-mac/Chromium",
        "python/models/model.gguf",
        "python/models/model.safetensors",
        "python/share/pytorch_model.bin",
        "python/secrets/release.pem",
        "python/secrets/release.key",
        "python/secrets/release.p12",
    ],
)
def test_allowlist_rejects_user_state_cache_heavy_or_secret_content(
    seed_root: Path, relative_path: str
):
    prohibited = seed_root / relative_path
    prohibited.parent.mkdir(parents=True, exist_ok=True)
    if prohibited.suffix.casefold() == ".pem":
        prohibited.write_bytes(b"-----BEGIN PRIVATE KEY-----\nmust-not-ship\n")
    else:
        prohibited.write_bytes(b"must-not-ship")

    entries = build_inventory(seed_root)
    with pytest.raises(InventoryError, match="prohibited"):
        assert_seed_allowlist(seed_root, entries)


def test_private_key_marker_is_detected_beyond_the_pem_prefix(seed_root: Path):
    private_key = seed_root / "python" / "share" / "certificate-chain.pem"
    private_key.parent.mkdir(parents=True)
    private_key.write_bytes(
        b"# certificate comments\n"
        + (b"x" * 5000)
        + b"\n-----BEGIN PRIVATE KEY-----\nsecret\n"
    )

    with pytest.raises(InventoryError, match="private key"):
        assert_seed_allowlist(seed_root, build_inventory(seed_root))


def test_public_certificate_pem_is_allowed(seed_root: Path):
    certificate = seed_root / "python" / "share" / "cacert.pem"
    certificate.parent.mkdir(parents=True)
    certificate.write_bytes(
        b"-----BEGIN CERTIFICATE-----\npublic-ca-data\n-----END CERTIFICATE-----\n"
    )

    assert_seed_allowlist(seed_root, build_inventory(seed_root))


def test_allowlist_rejects_unexpected_top_level_content(seed_root: Path):
    (seed_root / "README.md").write_text("unexpected", encoding="utf-8")

    with pytest.raises(InventoryError, match="top-level"):
        assert_seed_allowlist(seed_root, build_inventory(seed_root))


def test_allowlist_requires_the_declared_seed_layout(seed_root: Path):
    (seed_root / "seed-info.json").unlink()

    with pytest.raises(InventoryError, match="required"):
        assert_seed_allowlist(seed_root, build_inventory(seed_root))


def test_allowlist_rejects_an_inventory_that_omits_real_files(seed_root: Path):
    entries = build_inventory(seed_root)

    with pytest.raises(InventoryError, match="does not match"):
        assert_seed_allowlist(seed_root, entries[:-1])


def test_build_inventory_rejects_non_directory_or_symlink_root(tmp_path: Path):
    file_root = tmp_path / "file"
    file_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(InventoryError):
        build_inventory(file_root)

    if os.name != "nt":
        target = tmp_path / "target"
        target.mkdir()
        link_root = tmp_path / "link-root"
        link_root.symlink_to(target, target_is_directory=True)
        with pytest.raises(InventoryError):
            build_inventory(link_root)
