from __future__ import annotations

import argparse
import asyncio
import json
import stat
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_cli.platform_control import (
    PlatformControlAuthError,
    PlatformControlClient,
    PlatformIdentity,
    enroll,
    identity_path,
    load_identity,
    save_identity,
    start_platform_control_if_enabled,
    stop_platform_control,
)
from hermes_cli.platform_control_summary import (
    build_health_check_result,
    build_platform_control_summary,
)
from hermes_cli.subcommands.platform import build_platform_parser


ResponseFactory = Callable[[str, dict[str, Any], int], tuple[int, dict[str, Any]]]


class _ControlServer:
    def __init__(self, responder: ResponseFactory):
        self.requests: list[dict[str, Any]] = []
        self._responder = responder
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(
                    {
                        "body": payload,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "path": self.path,
                    }
                )
                status_code, response = owner._responder(self.path, payload, len(owner.requests))
                body = json.dumps(response).encode("utf-8")
                self.send_response(status_code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "_ControlServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def _empty_summary() -> dict[str, Any]:
    return build_platform_control_summary(config={}, resources={})


def test_platform_control_defaults_to_disabled() -> None:
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["platform_control"] == {
        "enabled": False,
        "endpoint": "",
        "heartbeat_seconds": 60,
    }


@pytest.mark.asyncio
async def test_enroll_uses_real_http_and_stores_only_device_identity(tmp_path: Path) -> None:
    def responder(path: str, body: dict[str, Any], _count: int) -> tuple[int, dict[str, Any]]:
        assert path == "/api/control/v1/enroll"
        assert body == {
            "arch": body["arch"],
            "capabilities": ["diagnostics.health.read"],
            "deviceId": "runtime-test-device",
            "enrollmentCode": "one-time-enrollment-code",
            "instanceType": "runtime",
            "os": body["os"],
            "version": body["version"],
        }
        return 200, {
            "data": {
                "deviceSecret": "device-secret-only-once",
                "heartbeatSeconds": 60,
                "instanceId": "41",
            },
            "meta": {},
            "requestId": "enroll-request",
        }

    with _ControlServer(responder) as server:
        result = await enroll(
            server.url,
            "one-time-enrollment-code",
            device_id="runtime-test-device",
            hermes_home=tmp_path,
        )

    assert result == PlatformIdentity(instance_id="41", device_secret="device-secret-only-once")
    stored = identity_path(tmp_path)
    assert load_identity(tmp_path) == result
    assert "one-time-enrollment-code" not in stored.read_text(encoding="utf-8")
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600


def test_summary_is_bounded_and_strictly_redacted() -> None:
    resources = {
        "tasks": [
            {
                "cost": 0.01,
                "file": f"/private/task-{index}.txt",
                "id": f"task-{index}",
                "model": "gpt-5",
                "prompt": f"private prompt {index}",
                "source": "kanban",
                "status": "running",
                "tokens": index,
            }
            for index in range(105)
        ],
        "codingAgents": [
            {
                "changeCount": 2,
                "durationMs": 30,
                "id": "coding-1",
                "status": "running",
                "type": "codex",
                "workspacePath": "/Users/private/project",
            }
        ],
        "cron": [],
        "devices": [],
        "workflows": [],
    }
    summary = build_platform_control_summary(
        config={"telegram": {"token": "private-token"}},
        resources=resources,
        channels=[{"configured": True, "healthy": True, "type": "telegram", "token": "private"}],
        uptime_seconds=12,
    )

    serialized = json.dumps(summary, sort_keys=True)
    assert len(summary["resources"]["tasks"]) == 100
    assert summary["resources"]["tasks"][0] == {
        "cost": 0.01,
        "id": "task-0",
        "model": "gpt-5",
        "source": "kanban",
        "status": "running",
        "tokens": 0,
    }
    assert summary["resources"]["codingAgents"][0]["workspaceHash"]
    assert summary["channels"] == [{"configured": True, "healthy": True, "type": "telegram"}]
    for forbidden in ("private prompt", "/Users/private", "private-token", '"file"', '"prompt"', '"token"'):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_heartbeat_claims_and_completes_only_health_check(tmp_path: Path) -> None:
    def responder(path: str, _body: dict[str, Any], _count: int) -> tuple[int, dict[str, Any]]:
        if path == "/api/control/v1/heartbeat":
            return 200, {
                "data": {
                    "acceptedAt": "2026-08-03T00:00:00Z",
                    "command": {
                        "claimedAt": "2026-08-03T00:00:00Z",
                        "id": "7",
                        "type": "health_check",
                    },
                    "nextHeartbeatSeconds": 60,
                },
                "meta": {},
                "requestId": "heartbeat-request",
            }
        if path == "/api/control/v1/commands/7/result":
            return 200, {"data": {"command": {"id": "7", "state": "succeeded"}}}
        raise AssertionError(path)

    save_identity(PlatformIdentity("41", "runtime-device-secret"), tmp_path)
    with _ControlServer(responder) as server:
        client = PlatformControlClient(
            endpoint=server.url,
            hermes_home=tmp_path,
            summary_builder=_empty_summary,
        )
        await client.heartbeat_once()

    assert [request["path"] for request in server.requests] == [
        "/api/control/v1/heartbeat",
        "/api/control/v1/commands/7/result",
    ]
    for request in server.requests:
        assert request["headers"]["authorization"] == "Bearer runtime-device-secret"
        assert request["headers"]["x-agentera-instance-id"] == "41"
    result = server.requests[1]["body"]
    assert result["state"] == "succeeded"
    assert result["code"] == "HEALTHY"
    assert result["summary"] == build_health_check_result()
    assert not any(word in json.dumps(result).lower() for word in ("prompt", "conversation", "secret", "log"))


@pytest.mark.asyncio
async def test_unsupported_remote_commands_fail_closed(tmp_path: Path) -> None:
    save_identity(PlatformIdentity("41", "runtime-device-secret"), tmp_path)
    client = PlatformControlClient(endpoint="http://127.0.0.1:1", hermes_home=tmp_path)

    for command_type in ("gateway_restart", "runtime_upgrade", "runtime_rollback"):
        result = await client.execute({"id": "9", "type": command_type})
        assert result == {
            "code": "UNSUPPORTED_COMMAND",
            "state": "failed",
            "summary": {"reason": "unsupported_command", "status": "rejected"},
        }


@pytest.mark.asyncio
async def test_unauthorized_heartbeat_removes_identity_and_stops_requests(tmp_path: Path) -> None:
    def responder(_path: str, _body: dict[str, Any], _count: int) -> tuple[int, dict[str, Any]]:
        return 401, {"error": {"code": "INVALID_DEVICE", "message": "re-enroll"}}

    save_identity(PlatformIdentity("41", "revoked-secret"), tmp_path)
    with _ControlServer(responder) as server:
        client = PlatformControlClient(
            endpoint=server.url,
            hermes_home=tmp_path,
            summary_builder=_empty_summary,
        )
        with pytest.raises(PlatformControlAuthError):
            await client.heartbeat_once()
        with pytest.raises(PlatformControlAuthError):
            await client.heartbeat_once()

    assert len(server.requests) == 1
    assert load_identity(tmp_path) is None


@pytest.mark.asyncio
async def test_run_retries_transient_http_failures_with_a_bound(tmp_path: Path) -> None:
    def responder(_path: str, _body: dict[str, Any], count: int) -> tuple[int, dict[str, Any]]:
        if count < 3:
            return 503, {"error": {"code": "TEMPORARY", "message": "retry"}}
        return 200, {
            "data": {"acceptedAt": "now", "command": None, "nextHeartbeatSeconds": 60},
            "meta": {},
            "requestId": "ok",
        }

    save_identity(PlatformIdentity("41", "runtime-device-secret"), tmp_path)
    with _ControlServer(responder) as server:
        client = PlatformControlClient(
            endpoint=server.url,
            heartbeat_seconds=0.01,
            hermes_home=tmp_path,
            max_retry_seconds=0.02,
            retry_base_seconds=0.005,
            summary_builder=_empty_summary,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        deadline = time.monotonic() + 2
        while len(server.requests) < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=0.5)

    assert len(server.requests) == 3


@pytest.mark.asyncio
async def test_disabled_startup_makes_no_request_and_enabled_startup_stops_promptly(tmp_path: Path) -> None:
    def responder(_path: str, _body: dict[str, Any], _count: int) -> tuple[int, dict[str, Any]]:
        return 200, {
            "data": {"acceptedAt": "now", "command": None, "nextHeartbeatSeconds": 60},
            "meta": {},
            "requestId": "ok",
        }

    save_identity(PlatformIdentity("41", "runtime-device-secret"), tmp_path)
    with _ControlServer(responder) as server:
        stop = asyncio.Event()
        disabled_task = start_platform_control_if_enabled(
            stop,
            config={"platform_control": {"enabled": False, "endpoint": server.url}},
            hermes_home=tmp_path,
            summary_builder=_empty_summary,
        )
        assert disabled_task is None
        await asyncio.sleep(0.03)
        assert server.requests == []

        enabled_task = start_platform_control_if_enabled(
            stop,
            config={
                "platform_control": {
                    "enabled": True,
                    "endpoint": server.url,
                    "heartbeat_seconds": 60,
                }
            },
            hermes_home=tmp_path,
            summary_builder=_empty_summary,
        )
        assert enabled_task is not None
        deadline = time.monotonic() + 1
        while not server.requests and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        await stop_platform_control(enabled_task, stop, timeout=0.5)

    assert len(server.requests) == 1


def test_platform_cli_exposes_enroll_status_and_disable_only() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    handler = object()
    build_platform_parser(subparsers, cmd_platform=handler)

    args = parser.parse_args(
        ["platform", "enroll", "--url", "https://admin.example", "--code", "one-time"]
    )
    assert args.platform_action == "enroll"
    assert args.url == "https://admin.example"
    assert args.code == "one-time"
    assert args.func is handler
    for action in ("status", "disable"):
        assert parser.parse_args(["platform", action]).platform_action == action
    with pytest.raises(SystemExit):
        parser.parse_args(["platform", "upgrade"])
