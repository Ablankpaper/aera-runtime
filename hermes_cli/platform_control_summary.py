"""Bounded, content-free summaries for the opt-in Aera Admin control plane."""

from __future__ import annotations

import hashlib
import platform
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from hermes_cli import __version__


_PROCESS_STARTED = time.monotonic()
_MAX_ITEMS = 100
_RESOURCE_FIELDS: dict[str, tuple[str, ...]] = {
    "codingAgents": ("id", "type", "status", "durationMs", "changeCount", "workspaceHash"),
    "cron": ("id", "status", "nextRunAt", "lastResult"),
    "devices": ("id", "type", "status", "lastSeenAt"),
    "tasks": ("id", "status", "source", "model", "tokens", "cost", "errorCode"),
    "workflows": ("id", "status", "version", "nodeCount", "failedNode"),
}


def _short_text(value: Any, maximum: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text and len(text) <= maximum else None


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _workspace_hash(value: Any) -> str | None:
    text = _short_text(value, 4096)
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _sanitize_resource(kind: str, item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    resource_id = _short_text(item.get("id"))
    status = _short_text(item.get("status"), 80)
    if resource_id is None or status is None:
        return None

    result: dict[str, Any] = {"id": resource_id, "status": status}
    for field in _RESOURCE_FIELDS[kind]:
        if field in result or field not in item:
            continue
        value = item[field]
        if field in {"durationMs", "changeCount", "tokens", "cost", "nodeCount"}:
            number = _finite_number(value)
            if number is not None:
                result[field] = number
        else:
            text = _short_text(value)
            if text is not None:
                result[field] = text

    if kind == "codingAgents" and "workspaceHash" not in result:
        hashed = _workspace_hash(item.get("workspacePath") or item.get("workspace"))
        if hashed is not None:
            result["workspaceHash"] = hashed
    return result


def _sanitize_resources(resources: Mapping[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    source = resources if isinstance(resources, Mapping) else {}
    sanitized: dict[str, list[dict[str, Any]]] = {}
    for kind in _RESOURCE_FIELDS:
        values = source.get(kind)
        rows: list[dict[str, Any]] = []
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            for value in values:
                row = _sanitize_resource(kind, value)
                if row is not None:
                    rows.append(row)
                if len(rows) >= _MAX_ITEMS:
                    break
        sanitized[kind] = rows
    return sanitized


def _collect_tasks() -> list[dict[str, Any]]:
    try:
        from hermes_cli.kanban_db import connect_closing, list_tasks

        with connect_closing() as connection:
            tasks = list_tasks(connection, limit=_MAX_ITEMS, order_by="updated")
        return [
            {
                "id": task.id,
                "model": task.model_override,
                "source": "kanban",
                "status": task.status,
            }
            for task in tasks
        ]
    except Exception:
        return []


def _collect_cron() -> list[dict[str, Any]]:
    try:
        from cron.jobs import list_jobs

        rows: list[dict[str, Any]] = []
        for job in list_jobs(include_disabled=True)[:_MAX_ITEMS]:
            rows.append(
                {
                    "id": job.get("id"),
                    "lastResult": job.get("last_status"),
                    "nextRunAt": job.get("next_run_at"),
                    "status": job.get("state") or ("scheduled" if job.get("enabled", True) else "paused"),
                }
            )
        return rows
    except Exception:
        return []


def _collect_local_resources() -> dict[str, list[dict[str, Any]]]:
    host_fingerprint = hashlib.sha256(
        f"{platform.node()}:{platform.machine()}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "codingAgents": [],
        "cron": _collect_cron(),
        "devices": [
            {
                "id": f"runtime-{host_fingerprint}",
                "lastSeenAt": datetime.now(timezone.utc).isoformat(),
                "status": "online",
                "type": "runtime",
            }
        ],
        "tasks": _collect_tasks(),
        "workflows": [],
    }


def _sanitize_channels(channels: Sequence[Any] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if channels is None:
        return result
    for channel in channels:
        if not isinstance(channel, Mapping):
            continue
        channel_type = _short_text(channel.get("type"), 80)
        configured = channel.get("configured")
        if channel_type is None or not isinstance(configured, bool):
            continue
        clean: dict[str, Any] = {"configured": configured, "type": channel_type}
        if isinstance(channel.get("healthy"), bool):
            clean["healthy"] = channel["healthy"]
        error_code = _short_text(channel.get("errorCode"), 100)
        if error_code is not None:
            clean["errorCode"] = error_code
        result.append(clean)
        if len(result) >= _MAX_ITEMS:
            break
    return result


def build_platform_control_summary(
    *,
    config: Mapping[str, Any] | None = None,
    resources: Mapping[str, Any] | None = None,
    channels: Sequence[Any] | None = None,
    uptime_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Return the exact allowlisted heartbeat body accepted by Aera Admin.

    ``config`` is accepted so callers can use one stable interface, but no raw
    configuration values are copied into the result. When ``resources`` is
    omitted, only bounded metadata is read from local Runtime stores.
    """

    del config
    source_resources = _collect_local_resources() if resources is None else resources
    clean_resources = _sanitize_resources(source_resources)
    clean_channels = _sanitize_channels(channels)
    if uptime_seconds is None:
        uptime_seconds = max(0, int(time.monotonic() - _PROCESS_STARTED))
    uptime = _finite_number(uptime_seconds)
    if uptime is None or uptime < 0:
        uptime = 0
    metrics = {
        "channelCount": sum(1 for channel in clean_channels if channel["configured"]),
        "resourceCount": sum(len(rows) for rows in clean_resources.values()),
    }
    return {
        "arch": platform.machine() or "unknown",
        "capabilities": ["diagnostics.health.read"],
        "channels": clean_channels,
        "metrics": metrics,
        "os": platform.system().lower() or "unknown",
        "resources": clean_resources,
        "uptimeSeconds": uptime,
        "version": __version__,
    }


def build_health_check_result(
    *,
    gateway_state: str = "available",
    resources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a sanitized result for the sole supported remote command."""

    state = _short_text(gateway_state, 80) or "unknown"
    clean_resources = _sanitize_resources(
        _collect_local_resources() if resources is None else resources
    )
    return {
        "gateway": state,
        "resourceCounts": {kind: len(rows) for kind, rows in clean_resources.items()},
        "version": __version__,
    }
