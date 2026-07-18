"""Tests for assembling relocatable AgentEra Runtime Seeds."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from scripts.agentera_runtime_dist.builder import (
    BuildConfig,
    BuildError,
    CommandResult,
    assemble_runtime_seed,
)
from scripts.agentera_runtime_dist.protocol import RuntimeTarget

_SOURCE_COMMIT = "a" * 40


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
    ) -> None:
        self.source_tree = source_tree
        self.managed_python = managed_python
        self.dirty = dirty
        self.head = head
        self.python_version = python_version
        self.system = system
        self.machine = machine
        self.wheel_has_main = wheel_has_main
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del env
        command = tuple(str(arg) for arg in args)
        self.calls.append((command, cwd))
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
