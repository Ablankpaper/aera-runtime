"""Safe extraction and isolated smoke tests for AgentEra Runtime Seeds."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

import zstandard

from scripts.agentera_runtime_dist.archive import (
    ARCHIVE_ROOT,
    ArchiveError,
    inspect_archive,
)
from scripts.agentera_runtime_dist.inventory import (
    InventoryEntry,
    InventoryError,
    build_inventory,
)
from scripts.agentera_runtime_dist.protocol import (
    ManifestValidationContext,
    ProtocolError,
    validate_manifest,
    verify_manifest_signature,
)

_SMOKE_IMPORTS = (
    "hermes_cli.main",
    "tools.registry",
    "tools.memory_tool",
    "tools.skill_manager_tool",
    "agent.curator",
)
_EXPECTED_RUNTIME_INITIALIZATION = frozenset(
    {"SOUL.md", "audio_cache", "hooks", "image_cache", "memories", "pairing"}
)
_NETWORK_GUARD = """\
import socket

def _agentera_network_blocked(*_args, **_kwargs):
    raise RuntimeError("network is disabled during AgentEra Runtime smoke tests")

socket.create_connection = _agentera_network_blocked
socket.socket.connect = _agentera_network_blocked
socket.socket.connect_ex = _agentera_network_blocked
"""


class SmokeError(RuntimeError):
    """A Runtime Seed is unsafe, incompatible, or unable to pass smoke tests."""


@dataclass(frozen=True)
class SmokeCommandResult:
    stdout: str
    stderr: str


SmokeCommandRunner = Callable[..., SmokeCommandResult]
SeedSmokeRunner = Callable[[Path], None]


def snapshot_tree(root: Path) -> dict[str, str]:
    """Return a stable content-and-metadata snapshot without following links."""

    root = Path(root)
    if not root.exists():
        return {}
    try:
        entries = build_inventory(root)
    except InventoryError as exc:
        raise SmokeError(f"cannot snapshot HERMES_HOME boundary: {exc}") from exc
    snapshot: dict[str, str] = {}
    for entry in entries:
        payload = json.dumps(
            {
                "kind": entry.kind,
                "size": entry.size,
                "sha256": entry.sha256,
                "mode": entry.mode,
                "link_target": entry.link_target,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        snapshot[entry.path] = hashlib.sha256(payload).hexdigest()
    return snapshot


def run_seed_smoke(
    seed_root: Path,
    *,
    runner: SmokeCommandRunner | None = None,
    hermes_home: Path | None = None,
    timeout: float = 45.0,
) -> None:
    """Probe the extracted Seed without network access or real user state."""

    seed_root = Path(seed_root).resolve(strict=True)
    python = _seed_python(seed_root)
    required_paths = (
        seed_root / "runtime" / "hermes",
        seed_root / "runtime" / "hermes.cmd",
        seed_root / "python" / "skills",
        seed_root / "python" / "optional-skills",
        seed_root / "python" / "optional-mcps",
    )
    missing = [
        str(path.relative_to(seed_root)) for path in required_paths if not path.exists()
    ]
    if missing:
        raise SmokeError(f"Runtime Seed is missing required paths: {missing}")

    command_runner = runner or _run_smoke_command
    with tempfile.TemporaryDirectory(prefix="agentera-runtime-smoke-") as temporary:
        sandbox = Path(temporary)
        boundary = (
            Path(hermes_home) if hermes_home is not None else sandbox / "hermes-home"
        )
        _create_boundary_fixture(boundary)
        before = _boundary_data_snapshot(boundary)
        guard = sandbox / "network-guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(_NETWORK_GUARD, encoding="utf-8")
        fake_home = sandbox / "home"
        fake_home.mkdir()
        env = _smoke_environment(seed_root, boundary, guard, fake_home)
        commands = (
            (str(python), "-m", "hermes_cli.main", "--version"),
            (str(python), "-m", "hermes_cli.main", "serve", "--help"),
            (
                str(python),
                "-c",
                "import importlib; "
                f"mods={_SMOKE_IMPORTS!r}; "
                "[importlib.import_module(name) for name in mods]",
            ),
        )
        for command in commands:
            try:
                command_runner(command, cwd=seed_root, env=env, timeout=timeout)
            except SmokeError:
                raise
            except Exception as exc:
                raise SmokeError(
                    f"Runtime smoke command failed: {command[1:]}"
                ) from exc
        after = _boundary_data_snapshot(boundary)
        if after != before:
            raise SmokeError(_boundary_change_message(before, after))


def extract_runtime_archive(archive_path: Path, destination: Path) -> Path:
    """Safely reconstruct a verified archive under a fresh destination."""

    archive_path = Path(archive_path)
    destination = Path(destination)
    try:
        expected = inspect_archive(archive_path)
    except ArchiveError as exc:
        raise SmokeError(str(exc)) from exc
    if destination.exists():
        raise SmokeError("Runtime extraction destination must not already exist")
    destination.mkdir(parents=True)
    seed_root = destination / ARCHIVE_ROOT
    try:
        if archive_path.name.endswith(".tar.zst"):
            _extract_tar_zst(archive_path, seed_root, expected)
        elif archive_path.name.endswith(".zip"):
            _extract_zip(archive_path, seed_root, expected)
        else:
            raise SmokeError("unsupported Runtime archive format")
        actual = build_inventory(seed_root)
        if actual != expected:
            raise SmokeError("extracted Runtime inventory differs from the archive")
        return seed_root
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def verify_signed_runtime_seed(
    *,
    archive_path: Path,
    manifest_path: Path,
    signature_path: Path,
    trusted_public_keys: Mapping[str, bytes],
    context: ManifestValidationContext,
    extraction_dir: Path,
    smoke_runner: SeedSmokeRunner = run_seed_smoke,
) -> Path:
    """Verify signature, target, bytes, inventory, extraction, and smoke."""

    archive_path = Path(archive_path)
    try:
        manifest = verify_manifest_signature(
            Path(manifest_path).read_bytes(),
            Path(signature_path).read_bytes(),
            trusted_public_keys,
        )
        validate_manifest(manifest, context)
    except (OSError, ProtocolError) as exc:
        raise SmokeError(f"Runtime manifest verification failed: {exc}") from exc
    if archive_path.name != manifest["archive_name"]:
        raise SmokeError("Runtime archive name differs from the signed manifest")
    expected_size = cast(int, manifest["archive_size"])
    try:
        actual_size = archive_path.stat().st_size
    except OSError as exc:
        raise SmokeError("cannot inspect Runtime archive") from exc
    if actual_size != expected_size:
        raise SmokeError("Runtime archive size differs from the signed manifest")
    actual_hash = _sha256_file(archive_path)
    if actual_hash != manifest["archive_sha256"]:
        raise SmokeError("Runtime archive hash differs from the signed manifest")
    try:
        archive_inventory = inspect_archive(archive_path)
    except ArchiveError as exc:
        raise SmokeError(str(exc)) from exc
    if archive_inventory != _manifest_inventory(manifest):
        raise SmokeError("Runtime archive inventory differs from the signed manifest")
    extracted = extract_runtime_archive(archive_path, extraction_dir)
    try:
        smoke_runner(extracted)
    except SmokeError:
        shutil.rmtree(Path(extraction_dir), ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(Path(extraction_dir), ignore_errors=True)
        raise SmokeError("Runtime Seed smoke test failed") from exc
    return extracted


def _seed_python(seed_root: Path) -> Path:
    info_path = seed_root / "seed-info.json"
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeError("Runtime seed-info.json is invalid") from exc
    entrypoints = info.get("entrypoints", {}) if isinstance(info, dict) else {}
    configured = entrypoints.get("python") if isinstance(entrypoints, dict) else None
    candidates = []
    if isinstance(configured, str):
        candidates.append(seed_root.joinpath(*PurePosixPath(configured).parts))
    candidates.extend((
        seed_root / "python" / "bin" / "python3",
        seed_root / "python" / "python.exe",
    ))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SmokeError("Runtime Seed has no bundled Python entrypoint")


def _create_boundary_fixture(boundary: Path) -> None:
    boundary = Path(boundary)
    if boundary.exists() and any(boundary.iterdir()):
        raise SmokeError("smoke HERMES_HOME must be a fresh empty directory")
    boundary.mkdir(parents=True, exist_ok=True)
    fixture = {
        "MEMORY.md": "# synthetic memory\n",
        "USER.md": "# synthetic user\n",
        "sessions/session.db": "synthetic-session\n",
        "skills/learned/SKILL.md": "# synthetic learned skill\n",
        "skills/.curator/state.json": '{"last_run":null}\n',
        "profiles/default/config.yaml": "model: synthetic\n",
        "gateway/state.json": '{"enabled":false}\n',
        "cron/jobs.json": "[]\n",
        "workspace/file.txt": "synthetic workspace\n",
    }
    for relative, content in fixture.items():
        path = boundary.joinpath(*PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _boundary_change_message(
    before: Mapping[str, str], after: Mapping[str, str]
) -> str:
    before_paths = set(before)
    after_paths = set(after)
    added = sorted(after_paths - before_paths)
    removed = sorted(before_paths - after_paths)
    changed = sorted(
        path for path in before_paths & after_paths if before[path] != after[path]
    )
    details: list[str] = []
    for label, paths in (("added", added), ("removed", removed), ("changed", changed)):
        if paths:
            rendered = ",".join(paths[:12])
            if len(paths) > 12:
                rendered += f",...(+{len(paths) - 12})"
            details.append(f"{label}={rendered}")
    return (
        "Runtime smoke test changed the synthetic HERMES_HOME boundary: "
        + "; ".join(details)
    )


def _boundary_data_snapshot(boundary: Path) -> dict[str, str]:
    return {
        path: digest
        for path, digest in snapshot_tree(boundary).items()
        if path != ".update_check" and path != "logs" and not path.startswith("logs/")
        and path not in _EXPECTED_RUNTIME_INITIALIZATION
    }


def _smoke_environment(
    seed_root: Path, boundary: Path, guard: Path, fake_home: Path
) -> dict[str, str]:
    env = {
        "HOME": str(fake_home),
        "USERPROFILE": str(fake_home),
        "LOCALAPPDATA": str(fake_home / "AppData" / "Local"),
        "HERMES_HOME": str(boundary),
        "HERMES_BUNDLED_SKILLS": str(seed_root / "python" / "skills"),
        "HERMES_OPTIONAL_SKILLS": str(seed_root / "python" / "optional-skills"),
        "HERMES_OPTIONAL_MCPS": str(seed_root / "python" / "optional-mcps"),
        "PYTHONPATH": str(guard),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_NO_INDEX": "1",
        "UV_OFFLINE": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join([
            str(seed_root / "runtime"),
            str(seed_root / "python" / "bin"),
            "/usr/bin",
            "/bin",
        ]),
    }
    for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TMP", "TEMP"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _run_smoke_command(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> SmokeCommandResult:
    try:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            env=dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise SmokeError(f"Runtime smoke command failed: {stderr[-1000:]}") from exc
    return SmokeCommandResult(completed.stdout, completed.stderr)


def _extract_tar_zst(
    archive_path: Path, seed_root: Path, expected: list[InventoryEntry]
) -> None:
    by_path = {entry.path: entry for entry in expected}
    with archive_path.open("rb") as compressed:
        with zstandard.ZstdDecompressor().stream_reader(compressed) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    if member.name == ARCHIVE_ROOT:
                        seed_root.mkdir()
                        _set_mode(seed_root, 0o755)
                        continue
                    relative = member.name.removeprefix(f"{ARCHIVE_ROOT}/")
                    entry = by_path.get(relative)
                    if entry is None:
                        raise SmokeError(f"unexpected TAR member: {member.name}")
                    target = _safe_destination(seed_root, entry.path)
                    _write_extracted_entry(
                        target,
                        seed_root,
                        entry,
                        archive.extractfile(member) if entry.kind == "file" else None,
                    )


def _extract_zip(
    archive_path: Path, seed_root: Path, expected: list[InventoryEntry]
) -> None:
    by_path = {entry.path: entry for entry in expected}
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        for info in archive.infolist():
            normalized = (
                info.filename[:-1] if info.filename.endswith("/") else info.filename
            )
            if normalized == ARCHIVE_ROOT:
                seed_root.mkdir()
                _set_mode(seed_root, 0o755)
                continue
            relative = normalized.removeprefix(f"{ARCHIVE_ROOT}/")
            entry = by_path.get(relative)
            if entry is None:
                raise SmokeError(f"unexpected ZIP member: {info.filename}")
            target = _safe_destination(seed_root, entry.path)
            if entry.kind == "symlink":
                _write_extracted_entry(target, seed_root, entry, None)
            elif entry.kind == "file":
                with archive.open(info, mode="r") as source:
                    _write_extracted_entry(target, seed_root, entry, source)
            else:
                _write_extracted_entry(target, seed_root, entry, None)


def _write_extracted_entry(
    target: Path,
    seed_root: Path,
    entry: InventoryEntry,
    source: object | None,
) -> None:
    _assert_safe_parent_chain(seed_root, target.parent)
    if entry.kind == "directory":
        target.mkdir()
        _set_mode(target, entry.mode)
    elif entry.kind == "symlink":
        os.symlink(entry.link_target or "", target)
    else:
        if source is None or not hasattr(source, "read"):
            raise SmokeError(f"cannot read archive file: {entry.path}")
        with target.open("xb") as output:
            shutil.copyfileobj(source, output)  # type: ignore[arg-type]
        _set_mode(target, entry.mode)


def _safe_destination(seed_root: Path, relative: str) -> Path:
    target = seed_root.joinpath(*PurePosixPath(relative).parts)
    try:
        target.relative_to(seed_root)
    except ValueError as exc:
        raise SmokeError("archive member escapes the Runtime root") from exc
    return target


def _assert_safe_parent_chain(seed_root: Path, parent: Path) -> None:
    try:
        relative = parent.relative_to(seed_root)
    except ValueError as exc:
        raise SmokeError("archive member parent escapes the Runtime root") from exc
    current = seed_root
    if current.is_symlink() or not current.is_dir():
        raise SmokeError("Runtime extraction root is unsafe")
    for part in relative.parts:
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise SmokeError("archive member traverses a symlink or missing directory")


def _set_mode(path: Path, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)


def _manifest_inventory(manifest: Mapping[str, object]) -> list[InventoryEntry]:
    raw_files = cast(list[object], manifest["files"])
    entries: list[InventoryEntry] = []
    for raw in raw_files:
        item = cast(Mapping[str, object], raw)
        entries.append(
            InventoryEntry(
                path=cast(str, item["path"]),
                kind=cast(Literal["file", "directory", "symlink"], item["kind"]),
                size=cast(int, item["size"]),
                sha256=cast(str | None, item["sha256"]),
                mode=cast(int, item["mode"]),
                link_target=cast(str | None, item["link_target"]),
            )
        )
    return entries


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise SmokeError("cannot hash Runtime archive") from exc
    return digest.hexdigest()
