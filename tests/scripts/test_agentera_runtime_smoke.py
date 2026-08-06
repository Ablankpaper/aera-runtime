"""Tests for safe Runtime Seed extraction and isolated smoke verification."""

from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path
from typing import Mapping, Sequence

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.agentera_runtime_dist.archive import write_deterministic_tar_zst
from scripts.agentera_runtime_dist.inventory import build_inventory
from scripts.agentera_runtime_dist.protocol import (
    MANIFEST_SCHEMA_VERSION,
    ManifestValidationContext,
    RuntimeTarget,
    canonical_json_bytes,
    create_signature_envelope,
    sign_bytes,
)
from scripts.agentera_runtime_dist.smoke import (
    SmokeCommandResult,
    SmokeError,
    extract_runtime_archive,
    run_seed_smoke,
    snapshot_tree,
    verify_signed_runtime_seed,
)
from scripts.verify_agentera_runtime_seed import _load_public_keys


@pytest.fixture()
def seed_root(tmp_path: Path) -> Path:
    root = tmp_path / "seed"
    (root / "python" / "bin").mkdir(parents=True)
    (root / "python" / "skills").mkdir()
    (root / "python" / "optional-skills").mkdir()
    (root / "python" / "optional-mcps").mkdir()
    (root / "runtime").mkdir()
    (root / "THIRD_PARTY_LICENSES").mkdir()
    python = root / "python" / "bin" / "python3"
    python.write_text("python", encoding="utf-8")
    python.chmod(0o755)
    hermes = root / "runtime" / "hermes"
    hermes.write_text("#!/bin/sh\n", encoding="utf-8")
    hermes.chmod(0o755)
    (root / "runtime" / "hermes.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (root / "THIRD_PARTY_LICENSES" / "LICENSE.txt").write_text(
        "license", encoding="utf-8"
    )
    (root / "seed-info.json").write_text("{}", encoding="utf-8")
    return root


class RecordingSmokeRunner:
    def __init__(
        self,
        *,
        mutate: bool = False,
        write_operational_state: bool = False,
        write_first_launch_state: bool = False,
    ) -> None:
        self.mutate = mutate
        self.write_operational_state = write_operational_state
        self.write_first_launch_state = write_first_launch_state
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str]]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> SmokeCommandResult:
        del cwd, timeout
        self.calls.append((tuple(str(arg) for arg in args), dict(env)))
        if self.mutate:
            Path(env["HERMES_HOME"], "MEMORY.md").write_text(
                "changed", encoding="utf-8"
            )
        if self.write_operational_state:
            home = Path(env["HERMES_HOME"])
            (home / "logs").mkdir(exist_ok=True)
            (home / "logs" / "agent.log").write_text("smoke log\n", encoding="utf-8")
            (home / ".update_check").write_text("{}", encoding="utf-8")
        if self.write_first_launch_state:
            home = Path(env["HERMES_HOME"])
            for name in ("audio_cache", "hooks", "image_cache", "memories", "pairing"):
                (home / name).mkdir(exist_ok=True)
            (home / "SOUL.md").write_text("# default identity\n", encoding="utf-8")
        return SmokeCommandResult("ok\n", "")


def test_smoke_runs_required_probes_without_changing_boundary(seed_root: Path):
    runner = RecordingSmokeRunner()
    boundary = seed_root.parent / "boundary"

    run_seed_smoke(seed_root, runner=runner, hermes_home=boundary)

    commands = [command for command, _env in runner.calls]
    assert any(command[-1] == "--version" for command in commands)
    assert any(command[-2:] == ("serve", "--help") for command in commands)
    import_probe = next(command for command in commands if "-c" in command)
    assert "tools.memory_tool" in import_probe[-1]
    assert "tools.skill_manager_tool" in import_probe[-1]
    assert "agent.curator" in import_probe[-1]
    assert all(env["HERMES_HOME"] == str(boundary) for _command, env in runner.calls)
    assert snapshot_tree(boundary)


def test_smoke_uses_disposable_windows_home_environment(seed_root: Path):
    runner = RecordingSmokeRunner()

    run_seed_smoke(seed_root, runner=runner)

    env = runner.calls[0][1]
    fake_home = Path(env["HOME"])
    assert Path(env["USERPROFILE"]) == fake_home
    assert Path(env["LOCALAPPDATA"]) == fake_home / "AppData" / "Local"


def test_smoke_fails_if_hermes_boundary_changes(seed_root: Path):
    with pytest.raises(SmokeError, match=r"HERMES_HOME.*MEMORY\.md"):
        run_seed_smoke(
            seed_root,
            runner=RecordingSmokeRunner(mutate=True),
            hermes_home=seed_root.parent / "boundary",
        )


def test_smoke_allows_only_disposable_logs_and_update_cache(seed_root: Path):
    run_seed_smoke(
        seed_root,
        runner=RecordingSmokeRunner(write_operational_state=True),
        hermes_home=seed_root.parent / "boundary",
    )


def test_smoke_allows_expected_first_launch_state(seed_root: Path):
    run_seed_smoke(
        seed_root,
        runner=RecordingSmokeRunner(write_first_launch_state=True),
        hermes_home=seed_root.parent / "boundary",
    )


def test_safe_extraction_reconstructs_seed(seed_root: Path, tmp_path: Path):
    archive = tmp_path / "runtime.tar.zst"
    write_deterministic_tar_zst(seed_root, archive)

    extracted = extract_runtime_archive(archive, tmp_path / "extracted")

    assert extracted.name == "agentera-runtime"
    assert build_inventory(extracted) == build_inventory(seed_root)


def test_safe_extraction_rejects_member_below_symlink(tmp_path: Path):
    archive_path = tmp_path / "symlink-parent.zip"

    def info(name: str, kind: str, mode: int) -> zipfile.ZipInfo:
        value = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        value.create_system = 3
        value.compress_type = zipfile.ZIP_DEFLATED
        file_type = {
            "directory": stat.S_IFDIR,
            "symlink": stat.S_IFLNK,
            "file": stat.S_IFREG,
        }[kind]
        value.external_attr = (file_type | mode) << 16
        if kind == "directory":
            value.external_attr |= 0x10
        return value

    with zipfile.ZipFile(archive_path, mode="w") as archive:
        archive.writestr(info("agentera-runtime/", "directory", 0o755), b"")
        archive.writestr(info("agentera-runtime/python/", "directory", 0o755), b"")
        archive.writestr(info("agentera-runtime/runtime/", "directory", 0o755), b"")
        archive.writestr(
            info("agentera-runtime/runtime/link", "symlink", 0o777), b"../python"
        )
        archive.writestr(
            info("agentera-runtime/runtime/link/escape.txt", "file", 0o644),
            b"escape",
        )

    extraction_dir = tmp_path / "extracted"
    with pytest.raises(SmokeError, match="symlink"):
        extract_runtime_archive(archive_path, extraction_dir)
    assert not extraction_dir.exists()


def test_public_key_loader_rejects_private_material(tmp_path: Path):
    private_key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    with pytest.raises(ValueError, match="public key"):
        _load_public_keys([f"runtime-2026-01={private_path}"])


def test_signed_verifier_checks_manifest_archive_and_smokes(
    seed_root: Path, tmp_path: Path
):
    archive = tmp_path / "agentera-runtime-0.18.2-agentera.1-darwin-arm64.tar.zst"
    write_deterministic_tar_zst(seed_root, archive)
    inventory = build_inventory(seed_root)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "key_id": "runtime-2026-01",
        "runtime_version": "0.18.2-agentera.1",
        "source_repository": "bignormal/aera-runtime",
        "source_commit": "a" * 40,
        "channel": "candidate",
        "platform": "darwin",
        "arch": "arm64",
        "archive_name": archive.name,
        "archive_size": archive.stat().st_size,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "python_version": "3.11.15",
        "entrypoints": {
            "python": "python/bin/python3",
            "hermes": "runtime/hermes",
            "module": "hermes_cli.main",
        },
        "minimum_desktop_version": "0.1.0",
        "compatibility_gate_revision": 1,
        "created_at": "2026-07-18T00:00:00Z",
        "files": [entry.__dict__ for entry in inventory],
    }
    manifest_raw = canonical_json_bytes(manifest)
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signature_raw = create_signature_envelope(
        "runtime-2026-01", sign_bytes(manifest_raw, private_pem)
    )
    manifest_path = tmp_path / "manifest.json"
    signature_path = tmp_path / "manifest.sig.json"
    manifest_path.write_bytes(manifest_raw)
    signature_path.write_bytes(signature_raw)
    smoked: list[Path] = []

    extracted = verify_signed_runtime_seed(
        archive_path=archive,
        manifest_path=manifest_path,
        signature_path=signature_path,
        trusted_public_keys={"runtime-2026-01": public_pem},
        context=ManifestValidationContext(
            repository="bignormal/aera-runtime",
            target=RuntimeTarget("darwin", "arm64"),
            desktop_version="0.1.0",
            allowed_channels=frozenset({"candidate"}),
        ),
        extraction_dir=tmp_path / "verified",
        smoke_runner=smoked.append,
    )

    assert extracted == tmp_path / "verified" / "agentera-runtime"
    assert smoked == [extracted]

    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(SmokeError, match="hash|size"):
        verify_signed_runtime_seed(
            archive_path=archive,
            manifest_path=manifest_path,
            signature_path=signature_path,
            trusted_public_keys={"runtime-2026-01": public_pem},
            context=ManifestValidationContext(
                repository="bignormal/aera-runtime",
                target=RuntimeTarget("darwin", "arm64"),
                desktop_version="0.1.0",
                allowed_channels=frozenset({"candidate"}),
            ),
            extraction_dir=tmp_path / "tampered",
            smoke_runner=lambda _path: None,
        )
