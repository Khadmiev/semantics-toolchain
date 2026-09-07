# SPDX-License-Identifier: Apache-2.0
"""Write-policy engine: a max over the mode lattice (allow < confirm < deny).

Factors (each contributes a mode; the result is their max, so any factor can only
tighten):
  1. base[sensitivity][operation]          (sensitivity_mode table)
  2. space floor                           (spaces.write_floor / template default)
  3. client_trust floor                    (trust_floor table)
  4. share audience                        (sensitive share into a multi-person space)

`decision.factor` names the binding factor, for `explain`. Enforcement venue
(apply / client_confirm / server_proposal / deny) is a separate axis keyed on trust.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models.graph import Node, NodeType
from .models.identity import Membership, Space
from .models.policy import SensitivityMode, TemplateFloor, TrustFloor

MODE_ORDER = {"allow": 0, "confirm": 1, "deny": 2}
_HIGH = ("high", "critical")
_SHARE_OPS = ("share",)


@dataclass(frozen=True)
class Decision:
    mode: str
    factor: str


def _strictest(*pairs: tuple[str, str]) -> tuple[str, str]:
    return max(pairs, key=lambda pair: MODE_ORDER[pair[0]])


async def effective_node_sensitivity(session: AsyncSession, node: Node) -> str:
    if node.sensitivity:
        return node.sensitivity
    node_type = await session.get(NodeType, node.type)
    return node_type.default_sensitivity if node_type else "normal"


async def is_multi_person(session: AsyncSession, space_id: uuid.UUID) -> bool:
    count = await session.scalar(
        select(func.count(func.distinct(Membership.account_id))).where(
            Membership.space_id == space_id
        )
    )
    return (count or 0) > 1


async def _base_mode(session: AsyncSession, sensitivity: str, operation: str) -> str:
    mode = await session.scalar(
        select(SensitivityMode.mode).where(
            SensitivityMode.sensitivity == sensitivity,
            SensitivityMode.operation == operation,
        )
    )
    return mode or "allow"


async def _space_floor(session: AsyncSession, space_id: uuid.UUID | None) -> str:
    if space_id is None:
        return "allow"
    space = await session.get(Space, space_id)
    if space is None:
        return "allow"
    if space.write_floor:
        return space.write_floor
    template_floor = await session.scalar(
        select(TemplateFloor.min_mode).where(TemplateFloor.space_template == space.template)
    )
    return template_floor or "allow"


async def _trust_floor(session: AsyncSession, client_trust: str) -> str:
    floor = await session.scalar(
        select(TrustFloor.min_mode).where(TrustFloor.client_trust == client_trust)
    )
    return floor or "allow"


async def evaluate(
    session: AsyncSession,
    *,
    operation: str,
    effective_sensitivity: str,
    target_space_id: uuid.UUID | None = None,
    client_trust: str = "trusted",
) -> Decision:
    """Compute the write-policy mode and its binding factor."""
    factors = [
        (await _base_mode(session, effective_sensitivity, operation), "base"),
        (await _space_floor(session, target_space_id), "space_floor"),
        (await _trust_floor(session, client_trust), "trust_floor"),
    ]
    if (
        operation in _SHARE_OPS
        and effective_sensitivity in _HIGH
        and target_space_id is not None
        and await is_multi_person(session, target_space_id)
    ):
        factors.append(("confirm", "share_audience"))

    mode, factor = _strictest(*factors)
    return Decision(mode=mode, factor=factor)


def enforcement(decision: Decision, client_trust: str) -> str:
    """Where/how a decision is enforced: apply | deny | client_confirm | server_proposal."""
    if decision.mode == "allow":
        return "apply"
    if decision.mode == "deny":
        return "deny"
    # confirm: trusted clients confirm in-chat; others are staged server-side.
    return "client_confirm" if client_trust == "trusted" else "server_proposal"
