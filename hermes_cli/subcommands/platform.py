"""Parser for the opt-in ``hermes platform`` control-plane commands."""

from __future__ import annotations

from typing import Any


def build_platform_parser(subparsers: Any, *, cmd_platform: Any) -> None:
    platform_parser = subparsers.add_parser(
        "platform",
        help="Enroll or disable the outbound Aera Admin control connection",
        description=(
            "Manage the explicitly enrolled outbound control connection. "
            "Only the read-only health_check capability is supported."
        ),
    )
    actions = platform_parser.add_subparsers(dest="platform_action")
    enroll_parser = actions.add_parser(
        "enroll",
        help="Redeem a one-time enrollment code from Aera Admin",
    )
    enroll_parser.add_argument("--url", required=True, help="Aera Admin base URL")
    enroll_parser.add_argument("--code", required=True, help="One-time enrollment code")
    actions.add_parser("status", help="Show redacted enrollment status")
    actions.add_parser("disable", help="Disable control polling and remove the device identity")
    platform_parser.set_defaults(func=cmd_platform)
