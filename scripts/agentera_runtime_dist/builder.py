"""Assemble deterministic, relocatable AgentEra Runtime Seed artifacts."""

from __future__ import annotations

import base64
import csv
import configparser
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scripts.agentera_runtime_dist.archive import (
    write_deterministic_tar_zst,
    write_deterministic_zip,
)
from scripts.agentera_runtime_dist.inventory import (
    InventoryEntry,
    InventoryError,
    assert_seed_allowlist,
    build_inventory,
)
from scripts.agentera_runtime_dist.protocol import (
    RuntimeTarget,
    canonical_json_bytes,
)
from scripts.agentera_runtime_dist.smoke import (
    SeedSmokeRunner,
    extract_runtime_archive,
    run_seed_smoke,
)

EXPECTED_PYTHON_VERSION = "3.11.15"
DEFAULT_SOURCE_REPOSITORY = "bignormal/aera-runtime"
_RUNTIME_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,127}\Z")
_SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_STRIP_DIRECTORY_NAMES = frozenset({"__pycache__", ".pytest_cache", "tests", "test"})
_POSIX_RELATIVE_PYTHON_HEADER = (
    b"#!/bin/sh\n'''exec' \"$(dirname \"$0\")/python3\" \"$0\" \"$@\"\n' '''\n"
)
_NON_RUNTIME_INSTALL_METADATA = frozenset({"direct_url.json", "uv_cache.json"})
_WINDOWS_ENTRYPOINT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PYTHON_OBJECT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


class _EntryPointConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


class BuildError(RuntimeError):
    """A reviewed Runtime Seed cannot be assembled from the supplied inputs."""


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


CommandRunner = Callable[..., CommandResult]


@dataclass(frozen=True)
class BuildConfig:
    repo_root: Path
    output_dir: Path
    runtime_version: str
    source_commit: str
    python_executable: Path
    target: RuntimeTarget
    source_repository: str = DEFAULT_SOURCE_REPOSITORY
    minimum_desktop_version: str = "0.1.0"
    compatibility_gate_revision: int = 1


@dataclass(frozen=True)
class BuildResult:
    archive_path: Path
    build_metadata_path: Path
    archive_sha256: str
    inventory: tuple[InventoryEntry, ...]


@dataclass(frozen=True)
class _PythonProbe:
    version: str
    system: str
    machine: str
    prefix: Path
    executable: Path


def assemble_runtime_seed(
    config: BuildConfig,
    *,
    runner: CommandRunner | None = None,
    smoke_runner: SeedSmokeRunner = run_seed_smoke,
) -> BuildResult:
    """Build, inventory, archive, extract, and smoke one native Runtime Seed."""

    command_runner = runner or _run_command
    repo_root = Path(config.repo_root).resolve(strict=True)
    output_dir = Path(config.output_dir).resolve(strict=False)
    _validate_config(config)
    _validate_reviewed_source(repo_root, config.source_commit, command_runner)
    probe = _probe_python(config.python_executable, repo_root, command_runner)
    _validate_python(probe, config.target, repo_root, command_runner)

    _build_frontend_assets(repo_root, command_runner)
    _require_frontend_assets(repo_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="agentera-runtime-build-") as temporary:
        work = Path(temporary)
        wheel = _build_wheel(repo_root, work / "wheelhouse", command_runner)
        _assert_wheel_entrypoint(wheel)
        requirements = _export_locked_requirements(
            repo_root, work / "requirements.txt", command_runner
        )
        seed_root = work / "seed" / "agentera-runtime"
        _copy_python(probe.prefix, seed_root / "python")
        copied_python = _copied_python(seed_root, config.target)
        _install_locked_runtime(
            repo_root,
            copied_python,
            requirements,
            wheel,
            command_runner,
        )
        _copy_bundled_data(repo_root, seed_root / "python")
        _write_launchers(seed_root)
        _collect_licenses(repo_root, probe.prefix, seed_root)
        _strip_build_only_content(seed_root)
        normalize_installed_runtime(seed_root / "python")
        entrypoints = _entrypoints(config.target)
        _write_seed_info(config, seed_root, entrypoints)
        try:
            inventory = build_inventory(seed_root)
            assert_seed_allowlist(seed_root, inventory)
        except InventoryError as exc:
            raise BuildError(str(exc)) from exc

        archive_path = output_dir / _archive_name(config)
        if config.target.platform == "darwin":
            write_deterministic_tar_zst(seed_root, archive_path)
        else:
            write_deterministic_zip(seed_root, archive_path)

        extracted_dir = work / "extracted"
        extracted = extract_runtime_archive(archive_path, extracted_dir)
        try:
            smoke_runner(extracted)
        except Exception as exc:
            raise BuildError("extracted Runtime Seed failed its smoke test") from exc

        archive_sha256 = _sha256_file(archive_path)
        metadata_path = output_dir / f"{archive_path.name}.build.json"
        metadata_path.write_bytes(
            canonical_json_bytes(
                _unsigned_build_metadata(
                    config,
                    archive_path,
                    archive_sha256,
                    inventory,
                    entrypoints,
                )
            )
        )
        return BuildResult(
            archive_path=archive_path,
            build_metadata_path=metadata_path,
            archive_sha256=archive_sha256,
            inventory=tuple(inventory),
        )


def detect_host_target() -> RuntimeTarget:
    """Map the current native host to a supported release target."""

    return _target_from_host(platform.system(), platform.machine())


def _validate_config(config: BuildConfig) -> None:
    if not _RUNTIME_VERSION_RE.fullmatch(config.runtime_version):
        raise BuildError("runtime version contains unsupported characters")
    if not _SOURCE_COMMIT_RE.fullmatch(config.source_commit):
        raise BuildError("source commit must be a full lowercase Git SHA")
    if config.source_repository != DEFAULT_SOURCE_REPOSITORY:
        raise BuildError("Runtime Seed source repository is not approved")
    if config.compatibility_gate_revision < 1:
        raise BuildError("compatibility gate revision must be positive")


def _validate_reviewed_source(
    repo_root: Path, source_commit: str, runner: CommandRunner
) -> None:
    status = runner(
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        cwd=repo_root,
    ).stdout
    if status.strip():
        raise BuildError("Runtime source tree must be clean before Seed assembly")
    head = runner(("git", "rev-parse", "HEAD"), cwd=repo_root).stdout.strip()
    if head != source_commit:
        raise BuildError("source commit must exactly match the current HEAD")


def _probe_python(
    python_executable: Path, repo_root: Path, runner: CommandRunner
) -> _PythonProbe:
    executable = Path(python_executable).resolve(strict=True)
    script = (
        "import json,platform,sys;"
        "print(json.dumps({'version':platform.python_version(),"
        "'system':platform.system(),'machine':platform.machine(),"
        "'prefix':sys.prefix,'executable':sys.executable},sort_keys=True))"
    )
    raw = runner((str(executable), "-c", script), cwd=repo_root).stdout.strip()
    try:
        value = json.loads(raw)
        return _PythonProbe(
            version=str(value["version"]),
            system=str(value["system"]),
            machine=str(value["machine"]),
            prefix=Path(value["prefix"]).resolve(strict=True),
            executable=Path(value["executable"]).resolve(strict=True),
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BuildError("cannot identify the supplied Python interpreter") from exc


def _validate_python(
    probe: _PythonProbe,
    target: RuntimeTarget,
    repo_root: Path,
    runner: CommandRunner,
) -> None:
    if probe.version != EXPECTED_PYTHON_VERSION:
        raise BuildError(f"Runtime Seed requires Python {EXPECTED_PYTHON_VERSION}")
    if _target_from_host(probe.system, probe.machine) != target:
        raise BuildError("Python host does not match the requested native target")
    uv_python_dir_raw = runner(("uv", "python", "dir"), cwd=repo_root).stdout.strip()
    try:
        uv_python_dir = Path(uv_python_dir_raw).resolve(strict=True)
        probe.prefix.relative_to(uv_python_dir)
        probe.executable.relative_to(probe.prefix)
    except (OSError, ValueError) as exc:
        raise BuildError(
            "Python interpreter is not from the uv-managed Python root"
        ) from exc


def _target_from_host(system: str, machine: str) -> RuntimeTarget:
    normalized_system = system.casefold()
    normalized_machine = machine.casefold()
    if normalized_system == "darwin" and normalized_machine in {"arm64", "aarch64"}:
        return RuntimeTarget("darwin", "arm64")
    if normalized_system == "windows" and normalized_machine in {
        "amd64",
        "x86_64",
        "x64",
    }:
        return RuntimeTarget("windows", "x64")
    raise BuildError(f"unsupported native Runtime target: {system}-{machine}")


def _build_frontend_assets(repo_root: Path, runner: CommandRunner) -> None:
    for directory in (repo_root / "web", repo_root / "ui-tui"):
        runner(("npm", "ci"), cwd=directory)
        runner(("npm", "run", "build"), cwd=directory)
    tui_output = repo_root / "ui-tui" / "dist" / "entry.js"
    if tui_output.is_file():
        destination = repo_root / "hermes_cli" / "tui_dist" / "entry.js"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tui_output, destination)


def _require_frontend_assets(repo_root: Path) -> None:
    required = (
        repo_root / "hermes_cli" / "web_dist" / "index.html",
        repo_root / "hermes_cli" / "tui_dist" / "entry.js",
    )
    for path in required:
        if not path.is_file() or path.stat().st_size == 0:
            relative = path.relative_to(repo_root)
            raise BuildError(f"required frontend asset is missing: {relative}")


def _build_wheel(repo_root: Path, wheelhouse: Path, runner: CommandRunner) -> Path:
    wheelhouse.mkdir(parents=True)
    generated_build = repo_root / "build"
    if generated_build.exists() or generated_build.is_symlink():
        raise BuildError("source build directory must not exist before wheel assembly")
    try:
        runner(
            ("uv", "build", "--wheel", "--out-dir", str(wheelhouse)),
            cwd=repo_root,
            env={"AGENTERA_RUNTIME_RELEASE_BUILD": "1"},
        )
    finally:
        if generated_build.exists() and not generated_build.is_symlink():
            shutil.rmtree(generated_build)
    wheels = sorted(wheelhouse.glob("*.whl"))
    if len(wheels) != 1:
        raise BuildError("Runtime build must produce exactly one Hermes wheel")
    return wheels[0]


def _assert_wheel_entrypoint(wheel: Path) -> None:
    try:
        with zipfile.ZipFile(wheel, mode="r") as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise BuildError("Hermes wheel is invalid") from exc
    if "hermes_cli/main.py" not in names:
        raise BuildError("Hermes wheel does not contain hermes_cli.main")
    required_frontend_assets = {
        "hermes_cli/web_dist/index.html",
        "hermes_cli/tui_dist/entry.js",
    }
    missing_frontend_assets = sorted(required_frontend_assets - names)
    if missing_frontend_assets:
        raise BuildError(
            "Hermes wheel does not contain built frontend assets: "
            + ", ".join(missing_frontend_assets)
        )


def _export_locked_requirements(
    repo_root: Path, destination: Path, runner: CommandRunner
) -> Path:
    runner(
        (
            "uv",
            "export",
            "--locked",
            "--extra",
            "all",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements.txt",
            "--output-file",
            str(destination),
        ),
        cwd=repo_root,
    )
    try:
        contents = destination.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError("locked requirements export was not created") from exc
    if "--hash=sha256:" not in contents:
        raise BuildError("locked requirements export must contain package hashes")
    return destination


def _copy_python(source_prefix: Path, destination: Path) -> None:
    try:
        shutil.copytree(source_prefix, destination, symlinks=True)
    except OSError as exc:
        raise BuildError("cannot copy the uv-managed Python tree") from exc


def _copied_python(seed_root: Path, target: RuntimeTarget) -> Path:
    relative = (
        Path("python.exe") if target.platform == "windows" else Path("bin/python3")
    )
    executable = seed_root / "python" / relative
    if not executable.is_file():
        raise BuildError(f"copied Python is missing its entrypoint: python/{relative}")
    return executable


def _install_locked_runtime(
    repo_root: Path,
    copied_python: Path,
    requirements: Path,
    wheel: Path,
    runner: CommandRunner,
) -> None:
    common = (
        "--python",
        str(copied_python),
        "--no-python-downloads",
        "--break-system-packages",
        "--link-mode",
        "copy",
        "--no-cache",
    )
    runner(
        (
            "uv",
            "pip",
            "install",
            *common,
            "--requirements",
            str(requirements),
            "--require-hashes",
            "--no-deps",
        ),
        cwd=repo_root,
    )
    runner(
        (
            "uv",
            "pip",
            "install",
            *common,
            "--no-deps",
            str(wheel),
        ),
        cwd=repo_root,
    )


def _copy_bundled_data(repo_root: Path, python_root: Path) -> None:
    for name in ("skills", "optional-skills", "optional-mcps"):
        source = repo_root / name
        if not source.is_dir():
            raise BuildError(f"required bundled data tree is missing: {name}")
        destination = python_root / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, symlinks=True)


def _write_launchers(seed_root: Path) -> None:
    runtime = seed_root / "runtime"
    runtime.mkdir()
    posix = runtime / "hermes"
    posix.write_text(
        '#!/bin/sh\nHERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'export HERMES_BUNDLED_SKILLS="$HERE/../python/skills"\n'
        'export HERMES_OPTIONAL_SKILLS="$HERE/../python/optional-skills"\n'
        'export HERMES_OPTIONAL_MCPS="$HERE/../python/optional-mcps"\n'
        'exec "$HERE/../python/bin/python3" -m hermes_cli.main "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    posix.chmod(0o755)
    (runtime / "hermes.cmd").write_text(
        "@echo off\r\n"
        'set "RUNTIME_DIR=%~dp0"\r\n'
        'set "HERMES_BUNDLED_SKILLS=%RUNTIME_DIR%..\\python\\skills"\r\n'
        'set "HERMES_OPTIONAL_SKILLS=%RUNTIME_DIR%..\\python\\optional-skills"\r\n'
        'set "HERMES_OPTIONAL_MCPS=%RUNTIME_DIR%..\\python\\optional-mcps"\r\n'
        '"%RUNTIME_DIR%..\\python\\python.exe" -m hermes_cli.main %*\r\n',
        encoding="utf-8",
        newline="",
    )


def _collect_licenses(repo_root: Path, python_prefix: Path, seed_root: Path) -> None:
    destination = seed_root / "THIRD_PARTY_LICENSES"
    destination.mkdir()
    shutil.copy2(repo_root / "LICENSE", destination / "hermes-agent-LICENSE.txt")
    python_license = python_prefix / "LICENSE"
    if python_license.is_file():
        shutil.copy2(python_license, destination / "python-LICENSE.txt")
    site_packages = _site_packages(seed_root / "python")
    packages_destination = destination / "python-packages"
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        candidates = [
            path
            for path in dist_info.rglob("*")
            if path.is_file()
            and any(
                part.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
                for part in path.relative_to(dist_info).parts
            )
        ]
        for index, source in enumerate(candidates):
            package = re.sub(r"[^A-Za-z0-9._-]", "_", dist_info.stem)
            target = packages_destination / package / f"{index:03d}-{source.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _site_packages(python_root: Path) -> Path:
    windows = python_root / "Lib" / "site-packages"
    if windows.is_dir():
        return windows
    candidates = sorted((python_root / "lib").glob("python3.11/site-packages"))
    return (
        candidates[0]
        if candidates
        else python_root / "lib" / "python3.11" / "site-packages"
    )


def normalize_installed_runtime(python_root: Path) -> None:
    """Remove install-host state and make generated scripts relocatable."""

    python_root = Path(python_root).resolve(strict=True)
    site_packages = _site_packages(python_root)
    additional_record_paths: dict[Path, tuple[str, ...]] = {}
    windows_scripts = python_root / "Scripts"
    if windows_scripts.is_dir():
        additional_record_paths = _normalize_windows_console_scripts(
            windows_scripts, site_packages
        )
    else:
        _normalize_posix_python_scripts(python_root / "bin")
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        for name in _NON_RUNTIME_INSTALL_METADATA:
            metadata = dist_info / name
            if metadata.is_file() or metadata.is_symlink():
                metadata.unlink()
        record = dist_info / "RECORD"
        if record.is_file():
            _regenerate_record(
                record,
                site_packages,
                python_root,
                additional_record_paths.get(record, ()),
            )


def _normalize_posix_python_scripts(scripts_dir: Path) -> None:
    if not scripts_dir.is_dir():
        return
    for script in sorted(scripts_dir.iterdir()):
        if script.is_symlink() or not script.is_file():
            continue
        try:
            contents = script.read_bytes()
        except OSError as exc:
            raise BuildError(f"cannot inspect installed script: {script.name}") from exc
        first_line, separator, body = contents.partition(b"\n")
        if (
            separator
            and first_line.startswith(b"#!")
            and b"python" in first_line.lower()
        ):
            try:
                script.write_bytes(_POSIX_RELATIVE_PYTHON_HEADER + body)
            except OSError as exc:
                raise BuildError(
                    f"cannot normalize installed script: {script.name}"
                ) from exc


def _normalize_windows_console_scripts(
    scripts_dir: Path, site_packages: Path
) -> dict[Path, tuple[str, ...]]:
    owners: dict[str, str] = {}
    record_paths: dict[Path, list[str]] = {}
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        entry_points_path = dist_info / "entry_points.txt"
        if not entry_points_path.is_file():
            continue
        parser = _EntryPointConfigParser(interpolation=None, delimiters=("=",))
        try:
            with entry_points_path.open(encoding="utf-8") as handle:
                parser.read_file(handle)
        except (OSError, UnicodeError, configparser.Error) as exc:
            raise BuildError(
                f"cannot read installed entry points: {dist_info.name}"
            ) from exc
        for group in ("console_scripts", "gui_scripts"):
            if not parser.has_section(group):
                continue
            for raw_name, raw_value in parser.items(group):
                name = raw_name.strip()
                value = raw_value.split("[", 1)[0].strip()
                module, separator, attribute = value.partition(":")
                module = module.strip()
                attribute = attribute.strip()
                if (
                    not _WINDOWS_ENTRYPOINT_NAME_RE.fullmatch(name)
                    or not separator
                    or not _PYTHON_OBJECT_RE.fullmatch(module)
                    or not _PYTHON_OBJECT_RE.fullmatch(attribute)
                ):
                    raise BuildError(
                        f"unsupported installed entry point: {dist_info.name}:{name}"
                    )
                key = name.casefold()
                previous = owners.get(key)
                if previous is not None:
                    raise BuildError(
                        f"duplicate installed entry point: {name} ({previous}, "
                        f"{dist_info.name})"
                    )
                owners[key] = dist_info.name
                generated = _write_windows_entrypoint(
                    scripts_dir, name=name, module=module, attribute=attribute
                )
                record = dist_info / "RECORD"
                record_paths.setdefault(record, []).extend(
                    _relative_record_path(site_packages, path) for path in generated
                )
    return {record: tuple(sorted(paths)) for record, paths in record_paths.items()}


def _write_windows_entrypoint(
    scripts_dir: Path, *, name: str, module: str, attribute: str
) -> tuple[Path, Path]:
    removable = {
        f"{name}.cmd",
        f"{name}.exe",
        f"{name}.exe.manifest",
        f"{name}-script.py",
    }
    removable_casefold = {item.casefold() for item in removable}
    for candidate in scripts_dir.iterdir():
        if candidate.name.casefold() not in removable_casefold:
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            raise BuildError(f"installed entry point is a directory: {candidate.name}")
        candidate.unlink()

    python_script = scripts_dir / f"{name}-script.py"
    python_script.write_text(
        "from importlib import import_module\n"
        "\n"
        f"target = import_module({module!r})\n"
        f"for part in {attribute!r}.split('.'):\n"
        "    target = getattr(target, part)\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(target())\n",
        encoding="utf-8",
        newline="\n",
    )
    command_script = scripts_dir / f"{name}.cmd"
    command_script.write_text(
        f'@echo off\r\n"%~dp0..\\python.exe" "%~dp0{name}-script.py" %*\r\n',
        encoding="utf-8",
        newline="",
    )
    return command_script, python_script


def _relative_record_path(site_packages: Path, target: Path) -> str:
    return Path(os.path.relpath(target, start=site_packages)).as_posix()


def _regenerate_record(
    record: Path,
    site_packages: Path,
    python_root: Path,
    additional_paths: Sequence[str] = (),
) -> None:
    try:
        with record.open(encoding="utf-8", newline="") as handle:
            original_rows = list(csv.reader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise BuildError(f"cannot read installed RECORD: {record.parent.name}") from exc

    record_resolved = record.resolve(strict=True)
    rows_by_path: dict[str, list[str]] = {}
    rows = [*original_rows, *([path, "", ""] for path in additional_paths)]
    for row in rows:
        if len(row) != 3 or not row[0]:
            raise BuildError(f"invalid installed RECORD: {record.parent.name}")
        relative = PurePosixPath(row[0])
        if relative.is_absolute():
            raise BuildError(f"installed RECORD path is absolute: {row[0]}")
        target = site_packages.joinpath(*relative.parts).resolve(strict=False)
        try:
            target.relative_to(python_root)
        except ValueError as exc:
            raise BuildError(f"installed RECORD escapes Python root: {row[0]}") from exc
        if target == record_resolved or not target.is_file():
            continue
        try:
            contents = target.read_bytes()
        except OSError as exc:
            raise BuildError(f"cannot hash installed RECORD path: {row[0]}") from exc
        digest = base64.urlsafe_b64encode(hashlib.sha256(contents).digest())
        encoded = digest.decode("ascii").rstrip("=")
        rows_by_path[row[0]] = [
            row[0],
            f"sha256={encoded}",
            str(len(contents)),
        ]

    record_relative = record.relative_to(site_packages).as_posix()
    rows_by_path[record_relative] = [record_relative, "", ""]
    try:
        with record.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerows(rows_by_path[path] for path in sorted(rows_by_path))
    except OSError as exc:
        raise BuildError(
            f"cannot write installed RECORD: {record.parent.name}"
        ) from exc


def _strip_build_only_content(seed_root: Path) -> None:
    directories = sorted(
        (
            path
            for path in seed_root.rglob("*")
            if path.is_dir() and not path.is_symlink()
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in directories:
        if path.name.casefold() in _STRIP_DIRECTORY_NAMES:
            shutil.rmtree(path)


def _entrypoints(target: RuntimeTarget) -> dict[str, str]:
    if target.platform == "darwin":
        python = "python/bin/python3"
        hermes = "runtime/hermes"
    else:
        python = "python/python.exe"
        hermes = "runtime/hermes.cmd"
    return {"python": python, "hermes": hermes, "module": "hermes_cli.main"}


def _write_seed_info(
    config: BuildConfig,
    seed_root: Path,
    entrypoints: Mapping[str, str],
) -> None:
    info: dict[str, object] = {
        "schema_version": 1,
        "runtime_version": config.runtime_version,
        "source_repository": config.source_repository,
        "source_commit": config.source_commit,
        "python_version": EXPECTED_PYTHON_VERSION,
        "platform": config.target.platform,
        "arch": config.target.arch,
        "entrypoints": dict(entrypoints),
        "compatibility_gate_revision": config.compatibility_gate_revision,
    }
    (seed_root / "seed-info.json").write_bytes(canonical_json_bytes(info))


def _archive_name(config: BuildConfig) -> str:
    suffix = ".tar.zst" if config.target.platform == "darwin" else ".zip"
    return (
        f"agentera-runtime-{config.runtime_version}-"
        f"{config.target.platform}-{config.target.arch}{suffix}"
    )


def _unsigned_build_metadata(
    config: BuildConfig,
    archive_path: Path,
    archive_sha256: str,
    inventory: Sequence[InventoryEntry],
    entrypoints: Mapping[str, str],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_version": config.runtime_version,
        "source_repository": config.source_repository,
        "source_commit": config.source_commit,
        "platform": config.target.platform,
        "arch": config.target.arch,
        "archive_name": archive_path.name,
        "archive_size": archive_path.stat().st_size,
        "archive_sha256": archive_sha256,
        "python_version": EXPECTED_PYTHON_VERSION,
        "entrypoints": dict(entrypoints),
        "minimum_desktop_version": config.minimum_desktop_version,
        "compatibility_gate_revision": config.compatibility_gate_revision,
        "files": [entry.__dict__ for entry in inventory],
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _run_command(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    command = list(args)
    resolved_executable = shutil.which(command[0], path=command_env.get("PATH"))
    if resolved_executable is not None:
        command[0] = resolved_executable
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=command_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise BuildError(f"build command failed: {args[0]}: {stderr[-2000:]}") from exc
    return CommandResult(completed.stdout, completed.stderr)
