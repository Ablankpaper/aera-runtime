"""Opt-in outbound control client for an explicitly enrolled Aera Runtime."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from hermes_cli import __version__
from hermes_constants import get_hermes_home
from utils import atomic_json_write


logger = logging.getLogger(__name__)
_IDENTITY_FILE = "platform-control.json"
_MAX_RESPONSE_BYTES = 1024 * 1024
_COMMAND_ID = re.compile(r"^[0-9]+$")


class PlatformControlError(RuntimeError):
    """Base error for the outbound control protocol."""


class PlatformControlAuthError(PlatformControlError):
    """The device identity was rejected and must be re-enrolled."""


class PlatformControlTransientError(PlatformControlError):
    """A bounded retry may recover this transport/server failure."""


class PlatformControlProtocolError(PlatformControlError):
    """The remote response did not match the fixed control contract."""


@dataclass(frozen=True)
class PlatformIdentity:
    instance_id: str
    device_secret: str


def identity_path(hermes_home: Path | str | None = None) -> Path:
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return home / _IDENTITY_FILE


def save_identity(identity: PlatformIdentity, hermes_home: Path | str | None = None) -> Path:
    path = identity_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    atomic_json_write(
        path,
        {"deviceSecret": identity.device_secret, "instanceId": identity.instance_id},
        indent=2,
        mode=0o600,
    )
    return path


def load_identity(hermes_home: Path | str | None = None) -> PlatformIdentity | None:
    path = identity_path(hermes_home)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"deviceSecret", "instanceId"}:
        return None
    instance_id = value.get("instanceId")
    device_secret = value.get("deviceSecret")
    if not isinstance(instance_id, str) or not instance_id.strip():
        return None
    if not isinstance(device_secret, str) or not device_secret.strip():
        return None
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return PlatformIdentity(instance_id=instance_id, device_secret=device_secret)


def remove_identity(hermes_home: Path | str | None = None) -> None:
    try:
        identity_path(hermes_home).unlink()
    except FileNotFoundError:
        pass


def _normalize_endpoint(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint.strip())
        port = parsed.port
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("platform control URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("platform control URL must use http(s) and include a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("platform control URL cannot contain credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("plain HTTP is allowed only for a loopback integration environment")
    authority = parsed.hostname
    if ":" in authority and not authority.startswith("["):
        authority = f"[{authority}]"
    if port is not None:
        authority = f"{authority}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, authority, path, "", ""))


def _response_error(status: int, body: bytes) -> PlatformControlError:
    message = f"platform control returned HTTP {status}"
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                message = f"{message} ({error['code']})"
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    if status == 401:
        return PlatformControlAuthError(message)
    if status >= 500 or status in {408, 429}:
        return PlatformControlTransientError(message)
    return PlatformControlProtocolError(message)


def _post_json_sync(
    endpoint: str,
    path: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    request_headers = {"content-type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        f"{endpoint}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise PlatformControlProtocolError("platform control response is too large")
    except urllib.error.HTTPError as exc:
        body = exc.read(_MAX_RESPONSE_BYTES)
        raise _response_error(exc.code, body) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PlatformControlTransientError("platform control request failed") from exc
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformControlProtocolError("platform control returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise PlatformControlProtocolError("platform control response must be an object")
    return parsed


async def enroll(
    endpoint: str,
    enrollment_code: str,
    *,
    device_id: str | None = None,
    hermes_home: Path | str | None = None,
    request_timeout: float = 10,
) -> PlatformIdentity:
    """Redeem a one-time Admin code and persist only the returned identity."""

    if not isinstance(enrollment_code, str) or not enrollment_code.strip():
        raise ValueError("enrollment code is required")
    base_url = _normalize_endpoint(endpoint)
    body = {
        "arch": platform.machine() or "unknown",
        "capabilities": ["diagnostics.health.read"],
        "deviceId": device_id or f"runtime-{uuid.uuid4()}",
        "enrollmentCode": enrollment_code.strip(),
        "instanceType": "runtime",
        "os": platform.system().lower() or "unknown",
        "version": __version__,
    }
    response = await asyncio.to_thread(
        _post_json_sync,
        base_url,
        "/api/control/v1/enroll",
        body,
        timeout=request_timeout,
    )
    data = response.get("data")
    if not isinstance(data, dict):
        raise PlatformControlProtocolError("enrollment response is missing data")
    instance_id = data.get("instanceId")
    device_secret = data.get("deviceSecret")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise PlatformControlProtocolError("enrollment response has no instance ID")
    if not isinstance(device_secret, str) or not device_secret.strip():
        raise PlatformControlProtocolError("enrollment response has no device secret")
    identity = PlatformIdentity(instance_id=instance_id, device_secret=device_secret)
    save_identity(identity, hermes_home)
    logger.info("Platform control enrollment completed for instance %s", instance_id)
    return identity


class PlatformControlClient:
    def __init__(
        self,
        *,
        endpoint: str,
        heartbeat_seconds: float = 60,
        hermes_home: Path | str | None = None,
        max_retry_seconds: float = 30,
        request_timeout: float = 10,
        retry_base_seconds: float = 1,
        summary_builder: Callable[[], dict[str, Any]] | None = None,
        health_builder: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        from hermes_cli.platform_control_summary import (
            build_health_check_result,
            build_platform_control_summary,
        )

        self.endpoint = _normalize_endpoint(endpoint)
        self.heartbeat_seconds = max(0.001, float(heartbeat_seconds))
        self.hermes_home = Path(hermes_home) if hermes_home is not None else None
        self.max_retry_seconds = max(0.001, float(max_retry_seconds))
        self.request_timeout = max(0.1, float(request_timeout))
        self.retry_base_seconds = max(0.001, float(retry_base_seconds))
        self.summary_builder = summary_builder or build_platform_control_summary
        self.health_builder = health_builder or build_health_check_result
        self._identity = load_identity(self.hermes_home)
        self._auth_disabled = False

    def _headers(self) -> dict[str, str]:
        if self._auth_disabled or self._identity is None:
            raise PlatformControlAuthError("runtime is not enrolled")
        return {
            "authorization": f"Bearer {self._identity.device_secret}",
            "x-agentera-instance-id": self._identity.instance_id,
        }

    async def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                _post_json_sync,
                self.endpoint,
                path,
                body,
                headers=self._headers(),
                timeout=self.request_timeout,
            )
        except PlatformControlAuthError:
            self._auth_disabled = True
            self._identity = None
            remove_identity(self.hermes_home)
            logger.error("Platform control identity was rejected; explicit re-enrollment is required")
            raise

    async def execute(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if command.get("type") != "health_check":
            return {
                "code": "UNSUPPORTED_COMMAND",
                "state": "failed",
                "summary": {"reason": "unsupported_command", "status": "rejected"},
            }
        return {"code": "HEALTHY", "state": "succeeded", "summary": self.health_builder()}

    async def heartbeat_once(self) -> dict[str, Any]:
        summary = self.summary_builder()
        if not isinstance(summary, dict):
            raise PlatformControlProtocolError("summary builder must return an object")
        response = await self._post("/api/control/v1/heartbeat", summary)
        data = response.get("data")
        if not isinstance(data, dict):
            raise PlatformControlProtocolError("heartbeat response is missing data")
        command = data.get("command")
        if command is not None:
            if not isinstance(command, dict):
                raise PlatformControlProtocolError("heartbeat command must be an object")
            command_id = str(command.get("id", ""))
            if not _COMMAND_ID.fullmatch(command_id):
                raise PlatformControlProtocolError("heartbeat command has an invalid ID")
            result = await self.execute(command)
            await self._post(f"/api/control/v1/commands/{command_id}/result", result)
            logger.info(
                "Platform control command %s completed with %s",
                command_id,
                result["code"],
            )
        logger.info("Platform control heartbeat accepted")
        return data

    @staticmethod
    async def _wait_or_stop(stop: asyncio.Event, delay: float) -> bool:
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.001, delay))
            return True
        except TimeoutError:
            return False

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        while not stop.is_set():
            try:
                await self.heartbeat_once()
                failures = 0
                delay = self.heartbeat_seconds
            except PlatformControlAuthError:
                return
            except PlatformControlError as exc:
                failures += 1
                delay = min(
                    self.max_retry_seconds,
                    self.retry_base_seconds * (2 ** min(failures - 1, 10)),
                )
                logger.warning("Platform control heartbeat failed; retrying in %.2fs: %s", delay, exc)
            if await self._wait_or_stop(stop, delay):
                return


def _runner_channels(runner: Any) -> list[dict[str, Any]]:
    adapters = getattr(runner, "adapters", None)
    if not isinstance(adapters, Mapping):
        return []
    channels: list[dict[str, Any]] = []
    for key in adapters:
        value = getattr(key, "value", key)
        if isinstance(value, str) and value:
            channels.append({"configured": True, "healthy": True, "type": value})
    return channels


def start_platform_control_if_enabled(
    stop: asyncio.Event,
    *,
    config: Mapping[str, Any] | None = None,
    hermes_home: Path | str | None = None,
    runner: Any = None,
    summary_builder: Callable[[], dict[str, Any]] | None = None,
) -> asyncio.Task[None] | None:
    """Start the client only after an explicit enable and saved enrollment."""

    if config is None:
        from hermes_cli.config import load_config

        config = load_config()
    section = config.get("platform_control") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping) or section.get("enabled") is not True:
        return None
    endpoint = section.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.strip() or load_identity(hermes_home) is None:
        return None
    heartbeat_seconds = section.get("heartbeat_seconds", 60)
    if isinstance(heartbeat_seconds, bool) or not isinstance(heartbeat_seconds, (int, float)):
        heartbeat_seconds = 60
    if summary_builder is None:
        from hermes_cli.platform_control_summary import build_platform_control_summary

        summary_builder = lambda: build_platform_control_summary(
            config=config,
            channels=_runner_channels(runner),
        )
    client = PlatformControlClient(
        endpoint=endpoint,
        heartbeat_seconds=heartbeat_seconds,
        hermes_home=hermes_home,
        summary_builder=summary_builder,
    )
    return asyncio.create_task(client.run(stop), name="platform-control")


async def stop_platform_control(
    task: asyncio.Task[None] | None,
    stop: asyncio.Event,
    *,
    timeout: float = 5,
) -> None:
    stop.set()
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning("Platform control task stopped after an error: %s", exc)
