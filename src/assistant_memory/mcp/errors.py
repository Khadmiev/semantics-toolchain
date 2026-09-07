# SPDX-License-Identifier: Apache-2.0
class ToolError(Exception):
    """Base class for tool-layer failures (the transport maps these to MCP errors)."""

    code = "tool_error"


class ToolNotAllowed(ToolError):
    """The credential's allowed_tools does not include this tool."""

    code = "tool_not_allowed"


class PermissionDenied(ToolError):
    """The principal may not perform this operation (access or write policy)."""

    code = "permission_denied"


class NotFound(ToolError):
    """Target node/edge is not visible to the principal (or does not exist)."""

    code = "not_found"


class Conflict(ToolError):
    """Optimistic-concurrency / containment conflict from the repository layer."""

    code = "conflict"


class InvalidArgument(ToolError):
    """A tool argument was missing or malformed."""

    code = "invalid_argument"


class NotConfigured(ToolError):
    """The deployment has not configured something this tool needs (an unfinished install)."""

    code = "not_configured"
