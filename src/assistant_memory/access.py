# SPDX-License-Identifier: Apache-2.0
"""Access / visibility resolver (app-layer enforcement).

Visibility of an account = nodes referenced by spaces it is a member of, plus
the edges between them (induced), with sensitive edges hidden unless explicitly
included via edge_spaces. Effective write permission on a node = the max
permission across the member spaces that reference it.
"""

import uuid

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models.graph import Edge, EdgeSpace, Node, NodeSpace
from .models.identity import Membership

_PERMISSION_ORDER = {"read": 0, "write": 1, "admin": 2}


async def containment_subtree(session: AsyncSession, anchor_id: uuid.UUID) -> set[uuid.UUID]:
    """Anchor + all descendants in the containment forest (follow active contained_in)."""
    sql = text(
        """
        WITH RECURSIVE sub AS (
            SELECT CAST(:anchor AS uuid) AS id
            UNION
            SELECT e.src_node
            FROM edges e
            JOIN sub ON e.dst_node = sub.id
            WHERE e.type = 'contained_in' AND e.valid_to IS NULL
        )
        SELECT id FROM sub
        """
    )
    rows = await session.execute(sql, {"anchor": str(anchor_id)})
    return {row[0] for row in rows}


class Access:
    """Account-scoped read/visibility helper. One per (session, account).

    ``scopes`` optionally narrows visibility to a subset of the account's member
    spaces (a credential's least-privilege scope, B3/B5). ``None`` means the full
    membership; otherwise the effective set is ``membership ∩ scopes``.
    """

    def __init__(
        self,
        session: AsyncSession,
        account_id: uuid.UUID,
        *,
        scopes: set[uuid.UUID] | None = None,
    ) -> None:
        self.session = session
        self.account_id = account_id
        self.scopes = scopes
        self._space_ids: set[uuid.UUID] | None = None

    async def space_ids(self) -> set[uuid.UUID]:
        if self._space_ids is None:
            rows = await self.session.scalars(
                select(Membership.space_id).where(Membership.account_id == self.account_id)
            )
            member = set(rows)
            self._space_ids = member if self.scopes is None else member & self.scopes
        return self._space_ids

    async def get_node(self, node_id: uuid.UUID) -> Node | None:
        """Return the node only if visible to this account (and not deleted)."""
        space_ids = await self.space_ids()
        if not space_ids:
            return None
        ref = await self.session.scalar(
            select(NodeSpace.node_id)
            .where(NodeSpace.node_id == node_id, NodeSpace.space_id.in_(space_ids))
            .limit(1)
        )
        if ref is None:
            return None
        node = await self.session.get(Node, node_id)
        if node is None or node.deleted_at is not None:
            return None
        return node

    async def is_node_visible(self, node_id: uuid.UUID) -> bool:
        return (await self.get_node(node_id)) is not None

    async def list_nodes(self, *, type: str | None = None, limit: int | None = None) -> list[Node]:
        space_ids = await self.space_ids()
        if not space_ids:
            return []
        stmt = (
            select(Node)
            .distinct()
            .join(NodeSpace, NodeSpace.node_id == Node.id)
            .where(NodeSpace.space_id.in_(space_ids), Node.deleted_at.is_(None))
        )
        if type is not None:
            stmt = stmt.where(Node.type == type)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(await self.session.scalars(stmt))

    async def effective_permission(self, node_id: uuid.UUID) -> str | None:
        """Max permission across the SCOPED member spaces that reference the node.

        Restricted to ``space_ids()`` (membership ∩ scopes) — a least-privilege
        credential must not inherit write/admin from a space outside its scope
        just because the node is also referenced there (operator-profile review
        c6c4e346 F015; the same hardening applies to every write tool).
        """
        if await self.get_node(node_id) is None:
            return None
        space_ids = await self.space_ids()
        perms = set(
            await self.session.scalars(
                select(Membership.permission)
                .distinct()
                .join(NodeSpace, NodeSpace.space_id == Membership.space_id)
                .where(
                    Membership.account_id == self.account_id,
                    Membership.space_id.in_(space_ids),
                    NodeSpace.node_id == node_id,
                )
            )
        )
        if not perms:
            return None
        return max(perms, key=lambda p: _PERMISSION_ORDER[p])

    async def neighbors(
        self,
        node_id: uuid.UUID,
        *,
        direction: str = "both",
        edge_types: list[str] | None = None,
    ) -> list[tuple[Edge, Node]]:
        """Visible neighbors via active edges; sensitive edges gated by edge_spaces."""
        if await self.get_node(node_id) is None:
            return []
        space_ids = await self.space_ids()

        stmt = select(Edge).where(Edge.valid_to.is_(None))
        if direction == "out":
            stmt = stmt.where(Edge.src_node == node_id)
        elif direction == "in":
            stmt = stmt.where(Edge.dst_node == node_id)
        else:
            stmt = stmt.where(or_(Edge.src_node == node_id, Edge.dst_node == node_id))
        if edge_types is not None:
            stmt = stmt.where(Edge.type.in_(edge_types))
        edges = list(await self.session.scalars(stmt))
        if not edges:
            return []

        other_ids = {e.dst_node if e.src_node == node_id else e.src_node for e in edges}
        visible_ids = set(
            await self.session.scalars(
                select(NodeSpace.node_id)
                .distinct()
                .join(Node, Node.id == NodeSpace.node_id)
                .where(
                    NodeSpace.node_id.in_(other_ids),
                    NodeSpace.space_id.in_(space_ids),
                    Node.deleted_at.is_(None),
                )
            )
        )

        sensitive_ids = {e.id for e in edges if e.sensitive}
        shared_sensitive: set[uuid.UUID] = set()
        if sensitive_ids:
            shared_sensitive = set(
                await self.session.scalars(
                    select(EdgeSpace.edge_id)
                    .distinct()
                    .where(EdgeSpace.edge_id.in_(sensitive_ids), EdgeSpace.space_id.in_(space_ids))
                )
            )

        result: list[tuple[Edge, Node]] = []
        for edge in edges:
            other = edge.dst_node if edge.src_node == node_id else edge.src_node
            if other not in visible_ids:
                continue
            if edge.sensitive and edge.id not in shared_sensitive:
                continue
            neighbor = await self.session.get(Node, other)
            if neighbor is not None:
                result.append((edge, neighbor))
        return result
