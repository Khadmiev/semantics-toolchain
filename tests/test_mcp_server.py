# SPDX-License-Identifier: Apache-2.0
"""Thin checks on the transport binding (no full MCP handshake — by design).

The substance is tested in test_mcp_tools.py. Here we only confirm the server
boots with the MCP mount + lifespan, that the endpoint is wired, and that the
catalog/schema/dispatch tables stay in sync.
"""

import inspect

from fastapi.testclient import TestClient

from assistant_memory.main import app
from assistant_memory.mcp import catalog
from assistant_memory.mcp.server import INSTRUCTIONS, SCHEMAS, _annotations, server
from assistant_memory.mcp.tools import HANDLERS


def test_catalog_schema_dispatch_in_sync() -> None:
    names = set(catalog.BY_NAME)
    assert set(SCHEMAS) == names
    assert set(HANDLERS) == names


def test_schema_properties_match_handler_signatures() -> None:
    """The public MCP schema must not drift from the handler contract (F8, review dc694faf):
    every keyword-only handler param appears in the schema and vice versa."""
    for name, schema in SCHEMAS.items():
        params = {
            p.name
            for p in inspect.signature(HANDLERS[name]).parameters.values()
            if p.kind == p.KEYWORD_ONLY
        }
        props = set(schema.get("properties", {}))
        assert params == props, (
            f"{name}: schema/handler drift (handler-only: {params - props}, "
            f"schema-only: {props - params})"
        )


def test_server_advertises_instructions() -> None:
    # initialize returns these to guide the client LLM
    assert server.instructions == INSTRUCTIONS
    assert "confirm_required" in INSTRUCTIONS  # the response protocol is documented


def test_tool_annotations_reflect_kind() -> None:
    assert _annotations(catalog.BY_NAME["get"]).readOnlyHint is True
    assert _annotations(catalog.BY_NAME["create_node"]).readOnlyHint is False
    assert _annotations(catalog.BY_NAME["delete_node"]).destructiveHint is True


def test_app_boots_with_mcp_mounted() -> None:
    # Entering the client runs the lifespan (session_manager.run()).
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        # /mcp is mounted (a bare GET is rejected by the transport, but not 404).
        assert client.get("/mcp").status_code != 404
