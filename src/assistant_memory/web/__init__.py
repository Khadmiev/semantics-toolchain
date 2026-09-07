# SPDX-License-Identifier: Apache-2.0
"""Web admin (B7): the human control plane — Google OAuth login, invites, spaces,
memberships, credentials, the change feed + undo, and the proposals queue.

Server-rendered (Jinja2 + HTMX), one process with the API. Everything here lives
behind a human login (decisions §8); the LLM data plane is in ``mcp``.
"""
