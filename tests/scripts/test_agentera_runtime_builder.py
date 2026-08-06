"""Tests for assembling relocatable AgentEra Runtime Seeds."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from scripts.agentera_runtime_dist import builder as builder_module
from scripts.agentera_runtime_dist.builder import (
    BuildConfig,
    BuildError,
    CommandResult,
    assemble_runtime_seed,
    normalize_installed_runtime,
)
from scripts.agentera_runtime_dist.protocol import RuntimeTarget
from scripts.agentera_runtime_dist.smoke import extract_runtime_archive

_SOURCE_COMMIT = "a" * 40


def test_command_runner_resolves_windows_command_shim_to_full_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    resolved_npm = r"C:\Program Files\nodejs\npm.cmd"
    observed: dict[str, object] = {}

    def fake_which(command: str, *, path: str | None = None) -> str:
        observed["which"] = (command, path)
        return resolved_npm

    def fake_run(args: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["args"] = list(args)
        observed["shell"] = kwargs.get("shell", False)
        return subprocess.CompletedProcess(args, 0, "built\n", "")

    monkeypatch.setattr(builder_module.shutil, "which", fake_which)
    monkeypatch.setattr(builder_module.subprocess, "run", fake_run)

    result = builder_module._run_command(
        ("npm", "ci"), cwd=tmp_path, env={"PATH": "windows-path"}
    )

    assert observed["which"] == ("npm", "windows-path")
    assert observed["args"] == [resolved_npm, "ci"]
    assert observed["shell"] is False
    assert result == CommandResult("built\n", "")


@pytest.fixture()
def source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "hermes_cli" / "web_dist").mkdir(parents=True)
    (root / "hermes_cli" / "tui_dist").mkdir(parents=True)
    (root / "hermes_cli" / "web_dist" / "index.html").write_text(
        "<html></html>", encoding="utf-8"
    )
    (root / "hermes_cli" / "tui_dist" / "entry.js").write_text(
        "console.log('tui')", encoding="utf-8"
    )
    for name in ("skills", "optional-skills", "optional-mcps"):
        (root / name).mkdir()
        (root / name / "README.md").write_text(name, encoding="utf-8")
    (root / "web").mkdir()
    (root / "ui-tui").mkdir()
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "hermes-agent"\nversion = "0.18.2"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return root


@pytest.fixture()
def managed_python(tmp_path: Path) -> Path:
    root = tmp_path / "uv" / "python" / "cpython-3.11.15-macos-aarch64-none"
    (root / "bin").mkdir(parents=True)
    executable = root / "bin" / "python3"
    executable.write_text("python", encoding="utf-8")
    executable.chmod(0o755)
    (root / "LICENSE").write_text("Python license\n", encoding="utf-8")
    return executable


class FakeRunner:
    def __init__(
        self,
        source_tree: Path,
        managed_python: Path,
        *,
        dirty: bool = False,
        head: str = _SOURCE_COMMIT,
        python_version: str = "3.11.15",
        system: str = "Darwin",
        machine: str = "arm64",
        wheel_has_main: bool = True,
        wheel_has_frontend: bool = True,
    ) -> None:
        self.source_tree = source_tree
        self.managed_python = managed_python
        self.dirty = dirty
        self.head = head
        self.python_version = python_version
        self.system = system
        self.machine = machine
        self.wheel_has_main = wheel_has_main
        self.wheel_has_frontend = wheel_has_frontend
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.environments: list[
            tuple[tuple[str, ...], Mapping[str, str] | None]
        ] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        command = tuple(str(arg) for arg in args)
        self.calls.append((command, cwd))
        self.environments.append((command, env))
        if command[:3] == ("git", "status", "--porcelain=v1"):
            return CommandResult("?? local.txt\n" if self.dirty else "", "")
        if command == ("git", "rev-parse", "HEAD"):
            return CommandResult(f"{self.head}\n", "")
        if command == ("uv", "python", "dir"):
            uv_python_dir = self.managed_python.parents[2]
            return CommandResult(f"{uv_python_dir}\n", "")
        if command[0] == str(self.managed_python) and command[1] == "-c":
            return CommandResult(
                json.dumps({
                    "version": self.python_version,
                    "system": self.system,
                    "machine": self.machine,
                    "prefix": str(self.managed_python.parent.parent),
                    "executable": str(self.managed_python),
                })
                + "\n",
                "",
            )
        if command[:3] == ("uv", "build", "--wheel"):
            generated = self.source_tree / "build" / "generated.txt"
            generated.parent.mkdir()
            generated.write_text("setuptools build output", encoding="utf-8")
            output_dir = Path(command[command.index("--out-dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            wheel = output_dir / "hermes_agent-0.18.2-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                member = "hermes_cli/main.py" if self.wheel_has_main else "other.py"
                archive.writestr(member, "def main(): pass\n")
                if self.wheel_has_frontend:
                    archive.writestr(
                        "hermes_cli/web_dist/index.html", "<html></html>"
                    )
                    archive.writestr(
                        "hermes_cli/tui_dist/entry.js", "console.log('tui')"
                    )
            return CommandResult("", "")
        if command[:2] == ("uv", "export"):
            output_file = Path(command[command.index("--output-file") + 1])
            output_file.write_text(
                "certifi==2026.5.20 \\\n+    --hash=sha256:" + ("b" * 64) + "\n",
                encoding="utf-8",
            )
            return CommandResult("", "")
        return CommandResult("", "")


def _config(source_tree: Path, managed_python: Path, tmp_path: Path) -> BuildConfig:
    return BuildConfig(
        repo_root=source_tree,
        output_dir=tmp_path / "output",
        runtime_version="0.18.2-agentera.1",
        source_commit=_SOURCE_COMMIT,
        python_executable=managed_python,
        target=RuntimeTarget("darwin", "arm64"),
    )


def test_builder_uses_locked_native_flow_and_emits_smoked_archive(
    source_tree: Path, managed_python: Path, tmp_path: Path
):
    runner = FakeRunner(source_tree, managed_python)
    smoked: list[Path] = []

    result = assemble_runtime_seed(
        _config(source_tree, managed_python, tmp_path),
        runner=runner,
        smoke_runner=smoked.append,
    )

    assert result.archive_path.name.endswith("-darwin-arm64.tar.zst")
    assert result.archive_path.is_file()
    assert result.build_metadata_path.is_file()
    assert not (source_tree / "build").exists()
    assert len(smoked) == 1
    assert smoked[0].name == "agentera-runtime"
    extracted = extract_runtime_archive(result.archive_path, tmp_path / "extracted")
    posix_launcher = (extracted / "runtime" / "hermes").read_text(encoding="utf-8")
    windows_launcher = (extracted / "runtime" / "hermes.cmd").read_text(encoding="utf-8")
    for name in ("HERMES_BUNDLED_SKILLS", "HERMES_OPTIONAL_SKILLS", "HERMES_OPTIONAL_MCPS"):
        assert f'export {name}="$HERE/../python/' in posix_launcher
        assert f'set "{name}=%RUNTIME_DIR%..\\python\\' in windows_launcher
    commands = [command for command, _cwd in runner.calls]
    export = next(command for command in commands if command[:2] == ("uv", "export"))
    install = next(
        command
        for command in commands
        if command[:3] == ("uv", "pip", "install") and "--requirements" in command
    )
    assert "--locked" in export
    assert export[export.index("--extra") + 1] == "all"
    assert "--all-extras" not in export
    assert "--no-dev" in export
    assert "--require-hashes" in install
    assert "--no-deps" in install
    assert "--break-system-packages" in install
    wheel_environment = next(
        environment
        for command, environment in runner.environments
        if command[:3] == ("uv", "build", "--wheel")
    )
    assert wheel_environment == {"AGENTERA_RUNTIME_RELEASE_BUILD": "1"}


@pytest.mark.parametrize(
    ("dirty", "head", "python_version", "machine", "message"),
    [
        (True, _SOURCE_COMMIT, "3.11.15", "arm64", "clean"),
        (False, "b" * 40, "3.11.15", "arm64", "HEAD"),
        (False, _SOURCE_COMMIT, "3.11.14", "arm64", "3.11.15"),
        (False, _SOURCE_COMMIT, "3.11.15", "x86_64", "target"),
    ],
)
def test_builder_rejects_unreviewed_or_wrong_native_inputs(
    source_tree: Path,
    managed_python: Path,
    tmp_path: Path,
    dirty: bool,
    head: str,
    python_version: str,
    machine: str,
    message: str,
):
    runner = FakeRunner(
        source_tree,
        managed_python,
        dirty=dirty,
        head=head,
        python_version=python_version,
        machine=machine,
    )
    config = _config(source_tree, managed_python, tmp_path)

    with pytest.raises(BuildError, match=message):
        assemble_runtime_seed(config, runner=runner, smoke_runner=lambda _path: None)


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("hermes_cli/web_dist/index.html", "web_dist"),
        ("hermes_cli/tui_dist/entry.js", "tui_dist"),
    ],
)
def test_builder_rejects_missing_frontend_assets(
    source_tree: Path,
    managed_python: Path,
    tmp_path: Path,
    missing: str,
    message: str,
):
    (source_tree / missing).unlink()
    runner = FakeRunner(source_tree, managed_python)

    with pytest.raises(BuildError, match=message):
        assemble_runtime_seed(
            _config(source_tree, managed_python, tmp_path),
            runner=runner,
            smoke_runner=lambda _path: None,
        )


def test_builder_rejects_wheel_without_hermes_main(
    source_tree: Path, managed_python: Path, tmp_path: Path
):
    runner = FakeRunner(source_tree, managed_python, wheel_has_main=False)

    with pytest.raises(BuildError, match="hermes_cli.main"):
        assemble_runtime_seed(
            _config(source_tree, managed_python, tmp_path),
            runner=runner,
            smoke_runner=lambda _path: None,
        )


def test_builder_rejects_wheel_without_built_frontend_assets(
    source_tree: Path, managed_python: Path, tmp_path: Path
):
    runner = FakeRunner(
        source_tree,
        managed_python,
        wheel_has_frontend=False,
    )

    with pytest.raises(BuildError, match="frontend assets"):
        assemble_runtime_seed(
            _config(source_tree, managed_python, tmp_path),
            runner=runner,
            smoke_runner=lambda _path: None,
        )


def test_builder_rejects_prohibited_content_copied_from_python(
    source_tree: Path, managed_python: Path, tmp_path: Path
):
    prohibited = managed_python.parent.parent / "pip-cache" / "wheel.tmp"
    prohibited.parent.mkdir()
    prohibited.write_text("cache", encoding="utf-8")
    runner = FakeRunner(source_tree, managed_python)

    with pytest.raises(BuildError, match="prohibited"):
        assemble_runtime_seed(
            _config(source_tree, managed_python, tmp_path),
            runner=runner,
            smoke_runner=lambda _path: None,
        )


def _write_nondeterministic_install_metadata(
    python_root: Path, *, temporary_prefix: Path, installed_at: str
) -> tuple[Path, Path]:
    scripts = python_root / "bin"
    scripts.mkdir(parents=True)
    (scripts / "python3").symlink_to(sys.executable)
    hermes = scripts / "hermes"
    hermes.write_text(
        f"#!{temporary_prefix}/python/bin/python3\n"
        "import sys\n"
        "print(f'normalized:{sys.argv[1]}')\n",
        encoding="utf-8",
        newline="\n",
    )
    hermes.chmod(0o755)

    site_packages = python_root / "lib" / "python3.11" / "site-packages"
    dist_info = site_packages / "hermes_agent-0.18.2.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "direct_url.json").write_text(
        json.dumps({"url": temporary_prefix.as_uri()}), encoding="utf-8"
    )
    (dist_info / "uv_cache.json").write_text(
        json.dumps({"timestamp": installed_at}), encoding="utf-8"
    )
    record = dist_info / "RECORD"
    record.write_text(
        "../../../bin/hermes,sha256=stale,1\n"
        "hermes_agent-0.18.2.dist-info/direct_url.json,sha256=stale,1\n"
        "hermes_agent-0.18.2.dist-info/uv_cache.json,sha256=stale,1\n"
        "hermes_agent-0.18.2.dist-info/RECORD,,\n",
        encoding="utf-8",
        newline="\n",
    )
    return hermes, record


def test_installed_runtime_normalization_is_relocatable_and_deterministic(
    tmp_path: Path,
):
    first_root = tmp_path / "first" / "python"
    second_root = tmp_path / "second" / "python"
    first_script, first_record = _write_nondeterministic_install_metadata(
        first_root,
        temporary_prefix=tmp_path / "random-build-a",
        installed_at="2026-07-18T01:02:03Z",
    )
    second_script, second_record = _write_nondeterministic_install_metadata(
        second_root,
        temporary_prefix=tmp_path / "random-build-b",
        installed_at="2027-08-19T04:05:06Z",
    )

    normalize_installed_runtime(first_root)
    normalize_installed_runtime(second_root)

    assert first_script.read_bytes() == second_script.read_bytes()
    assert first_record.read_bytes() == second_record.read_bytes()
    assert str(tmp_path).encode() not in first_script.read_bytes()
    assert not (first_record.parent / "direct_url.json").exists()
    assert not (first_record.parent / "uv_cache.json").exists()

    completed = subprocess.run(
        [str(first_script), "ok"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "normalized:ok\n"

    with first_record.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert [row[0] for row in rows] == [
        "../../../bin/hermes",
        "hermes_agent-0.18.2.dist-info/RECORD",
    ]
    expected_hash = (
        base64
        .urlsafe_b64encode(hashlib.sha256(first_script.read_bytes()).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert rows[0] == [
        "../../../bin/hermes",
        f"sha256={expected_hash}",
        str(first_script.stat().st_size),
    ]
    assert rows[1] == ["hermes_agent-0.18.2.dist-info/RECORD", "", ""]


def _write_nondeterministic_windows_entrypoint(
    python_root: Path, *, temporary_prefix: Path, installed_at: str
) -> Path:
    scripts = python_root / "Scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "hermes.exe"
    launcher.write_bytes(b"MZ" + str(temporary_prefix).encode() + b"\0python.exe")

    site_packages = python_root / "Lib" / "site-packages"
    dist_info = site_packages / "hermes_agent-0.18.2.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "entry_points.txt").write_text(
        "[console_scripts]\nhermes = hermes_cli.main:main\n",
        encoding="utf-8",
        newline="\n",
    )
    (dist_info / "direct_url.json").write_text(
        json.dumps({"url": temporary_prefix.as_uri()}), encoding="utf-8"
    )
    (dist_info / "uv_cache.json").write_text(
        json.dumps({"timestamp": installed_at}), encoding="utf-8"
    )
    (dist_info / "RECORD").write_text(
        "../../Scripts/hermes.exe,sha256=stale,1\n"
        "hermes_agent-0.18.2.dist-info/direct_url.json,sha256=stale,1\n"
        "hermes_agent-0.18.2.dist-info/entry_points.txt,sha256=stale,1\n"
        "hermes_agent-0.18.2.dist-info/uv_cache.json,sha256=stale,1\n"
        "hermes_agent-0.18.2.dist-info/RECORD,,\n",
        encoding="utf-8",
        newline="\n",
    )
    return dist_info


def test_windows_console_scripts_are_rebuilt_without_install_paths(tmp_path: Path):
    first_root = tmp_path / "first-windows" / "python"
    second_root = tmp_path / "second-windows" / "python"
    first_dist = _write_nondeterministic_windows_entrypoint(
        first_root,
        temporary_prefix=tmp_path / "windows-build-a",
        installed_at="2026-07-18T01:02:03Z",
    )
    second_dist = _write_nondeterministic_windows_entrypoint(
        second_root,
        temporary_prefix=tmp_path / "windows-build-b",
        installed_at="2027-08-19T04:05:06Z",
    )

    normalize_installed_runtime(first_root)
    normalize_installed_runtime(second_root)

    for relative in ("hermes.cmd", "hermes-script.py"):
        first = first_root / "Scripts" / relative
        second = second_root / "Scripts" / relative
        assert first.read_bytes() == second.read_bytes()
        assert str(tmp_path).encode() not in first.read_bytes()
    assert not (first_root / "Scripts" / "hermes.exe").exists()

    record = (first_dist / "RECORD").read_text(encoding="utf-8")
    assert "../../Scripts/hermes.exe" not in record
    assert "../../Scripts/hermes.cmd" in record
    assert "../../Scripts/hermes-script.py" in record
    assert not (first_dist / "direct_url.json").exists()
    assert not (first_dist / "uv_cache.json").exists()
    assert (first_dist / "RECORD").read_bytes() == (second_dist / "RECORD").read_bytes()
