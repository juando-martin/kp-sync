"""Endpoint interfaces and file, command, and cloud adapters."""

from kp_sync.endpoints.base import (
    Endpoint,
    EndpointChanged,
    EndpointError,
    EndpointFingerprint,
    EndpointUnavailable,
)
from kp_sync.endpoints.command import CommandEndpoint
from kp_sync.endpoints.file import FileEndpoint
from kp_sync.endpoints.rclone import RcloneEndpoint

__all__ = [
    "CommandEndpoint",
    "Endpoint",
    "EndpointChanged",
    "EndpointError",
    "EndpointFingerprint",
    "EndpointUnavailable",
    "FileEndpoint",
    "RcloneEndpoint",
]
