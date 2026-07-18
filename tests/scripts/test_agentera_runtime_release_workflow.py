"""Contract tests for the signed native AgentEra Runtime release workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "agentera-runtime-release.yml"
COMPATIBILITY_SCRIPT = ROOT / "scripts" / "run_agentera_compatibility.sh"
RUNBOOK_PATH = ROOT / "docs" / "agentera-runtime-release.md"
FULL_SHA_ACTION = re.compile(r"^[^./][^@]+@[0-9a-f]{40}$")


def _workflow() -> dict[str, Any]:
    value = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML follows YAML 1.1 and parses an unquoted `on` key as boolean true.
    value = workflow.get("on", workflow.get(True))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    if isinstance(value, str):
        return {value}
    assert isinstance(value, list)
    return {str(item) for item in value}


def _job_text(job: dict[str, Any]) -> str:
    return json.dumps(job, sort_keys=True)


def test_dispatch_inputs_are_narrow_and_version_is_computed_from_source():
    workflow = _workflow()
    dispatch = _triggers(workflow)["workflow_dispatch"]
    inputs = dispatch["inputs"]

    assert set(inputs) == {
        "agentera_revision",
        "channel",
        "candidate_number",
        "publish",
    }
    assert inputs["agentera_revision"]["type"] == "number"
    assert inputs["agentera_revision"]["default"] == 1
    assert inputs["channel"]["type"] == "choice"
    assert inputs["channel"]["options"] == ["candidate", "stable"]
    assert inputs["candidate_number"]["type"] == "number"
    assert inputs["candidate_number"]["default"] == 1
    assert inputs["publish"]["type"] == "boolean"
    assert inputs["publish"]["default"] is False

    compatibility = workflow["jobs"]["compatibility"]
    text = _job_text(compatibility)
    assert "pyproject.toml" in text
    assert "github.sha" in text or "GITHUB_SHA" in text
    assert "runtime_version" in text


def test_same_repository_pr_label_runs_a_candidate_rehearsal_from_head_sha():
    workflow = _workflow()
    triggers = _triggers(workflow)

    assert triggers["pull_request"]["types"] == ["labeled"]

    compatibility = workflow["jobs"]["compatibility"]
    compatibility_text = _job_text(compatibility)
    guard = str(compatibility["if"])
    assert "github.event.label.name == 'runtime-dry-run'" in guard
    assert (
        "github.event.pull_request.head.repo.full_name == github.repository" in guard
    )
    assert "github.event.pull_request.head.sha" in compatibility_text
    assert "github.event_name == 'pull_request'" in compatibility_text
    assert "'candidate'" in compatibility_text

    publish_text = _job_text(workflow["jobs"]["publish"])
    assert "github.event_name == 'workflow_dispatch'" in publish_text
    assert "inputs.publish" in publish_text

    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    assert "runtime-dry-run" in runbook
    assert "same-repository" in runbook


def test_release_graph_uses_native_targets_and_exact_toolchains():
    jobs = _workflow()["jobs"]
    macos = jobs["build_macos_arm64"]
    windows = jobs["build_windows_x64"]
    sign = jobs["sign"]
    publish = jobs["publish"]

    assert _needs(macos) == {"compatibility"}
    assert _needs(windows) == {"compatibility"}
    assert _needs(sign) == {
        "compatibility",
        "build_macos_arm64",
        "build_windows_x64",
    }
    assert _needs(publish) == {
        "compatibility",
        "build_macos_arm64",
        "build_windows_x64",
        "sign",
    }

    assert macos["runs-on"] == "macos-14"
    assert "arm64" in _job_text(macos)
    assert windows["runs-on"] == "windows-2022"
    assert "AMD64" in _job_text(windows) or "x64" in _job_text(windows)
    for native_job in (macos, windows):
        text = _job_text(native_job)
        assert "3.11.15" in text
        assert '"node-version": "22"' in text
        assert "build_agentera_runtime_seed.py" in text
        assert (
            "uv python find --no-project --managed-python --resolve-links 3.11.15"
            in text
        )
        setup_node = next(
            step for step in native_job["steps"] if step.get("name") == "Install Node.js 22"
        )
        assert setup_node["with"]["cache-dependency-path"] == "package-lock.json"


def test_signing_secret_is_confined_and_all_external_actions_are_sha_pinned():
    workflow = _workflow()
    jobs = workflow["jobs"]
    secret_reference = "secrets.AGENTERA_RUNTIME_SIGNING_KEY_PEM_B64"

    assert secret_reference in _job_text(jobs["sign"])
    for name, job in jobs.items():
        if name != "sign":
            assert secret_reference not in _job_text(job)

    for job in jobs.values():
        for step in job.get("steps", []):
            action = step.get("uses")
            if action and not action.startswith("./"):
                assert FULL_SHA_ACTION.fullmatch(action), action

    sign_text = _job_text(jobs["sign"])
    assert "runtime-production" in sign_text
    assert "verify_signed_runtime_seed" in sign_text
    assert "upload-artifact" in sign_text
    assert "inputs.publish" not in sign_text


def test_distribution_gate_and_runbook_preserve_the_hermes_boundary():
    gate = COMPATIBILITY_SCRIPT.read_text(encoding="utf-8")
    for invariant in (
        "test_system_prompt_restore.py",
        "test_memory_tool.py",
        "test_profile_isolation_runtime.py",
        "test_skill_manager_tool.py",
        "test_curator.py",
        "test_curator_backup.py",
    ):
        assert invariant in gate
    assert "AGENTERA_RUNTIME_RELEASE_BUILD" in gate
    assert "test_agentera_runtime_smoke.py" in gate

    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    for required in (
        "AGENTERA_RUNTIME_SIGNING_KEY_PEM_B64",
        "runtime-production",
        "candidate",
        "stable",
        "key rotation",
        "reproduc",
        "recovery",
        "HERMES_HOME",
    ):
        assert required.casefold() in runbook.casefold()
