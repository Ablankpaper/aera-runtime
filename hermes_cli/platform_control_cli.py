"""User-facing commands for explicit Runtime enrollment and disablement."""

from __future__ import annotations

import asyncio
import json
from typing import Any


def _save_settings(*, enabled: bool, endpoint: str | None = None) -> None:
    from hermes_cli.config import load_config, save_config

    config = load_config()
    current = config.get("platform_control")
    section = dict(current) if isinstance(current, dict) else {}
    section["enabled"] = enabled
    if endpoint is not None:
        section["endpoint"] = endpoint
    section.setdefault("heartbeat_seconds", 60)
    config["platform_control"] = section
    save_config(
        config,
        preserve_keys={
            ("platform_control", "enabled"),
            ("platform_control", "endpoint"),
            ("platform_control", "heartbeat_seconds"),
        },
    )


def platform_command(args: Any) -> int:
    from hermes_cli.platform_control import enroll, load_identity, remove_identity

    action = getattr(args, "platform_action", None)
    if action == "enroll":
        identity = asyncio.run(enroll(args.url, args.code))
        _save_settings(enabled=True, endpoint=args.url.rstrip("/"))
        print(f"Runtime enrolled as instance {identity.instance_id}.")
        print("Outbound health polling will start with the next gateway run.")
        return 0
    if action == "status":
        from hermes_cli.config import load_config

        config = load_config()
        section = config.get("platform_control")
        settings = section if isinstance(section, dict) else {}
        identity = load_identity()
        print(
            json.dumps(
                {
                    "enabled": settings.get("enabled") is True,
                    "endpoint": settings.get("endpoint") or "",
                    "enrolled": identity is not None,
                    "instanceId": identity.instance_id if identity is not None else None,
                    "supportedCommands": ["health_check"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if action == "disable":
        _save_settings(enabled=False)
        remove_identity()
        print("Platform control disabled; the local device identity was removed.")
        return 0
    print("Choose one of: enroll, status, disable")
    return 2
