# SPDX-License-Identifier: Apache-2.0
"""MCP data plane: narrow, separate tools an LLM client calls over a credential.

Layered:
- ``tools`` — the dispatch core: ``(session, principal, **args) -> dict``. Each
  tool glues auth (B3) -> policy (B4) -> access (B2) -> repository (B1). Pure and
  unit-testable; no transport.
- ``catalog`` — the tool registry + the ``allowed_tools`` visibility gate.
- ``server`` — the thin streamable-HTTP MCP binding mounted into FastAPI.

Control-plane operations (identity, memberships, spaces, credentials, policy)
are deliberately absent (decisions §8) — there is nothing here to call.
"""
