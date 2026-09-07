# SPDX-License-Identifier: Apache-2.0
from sqlalchemy.orm import DeclarativeBase

# Allowed values for the text+check "enums" (kept as text for easy evolution).
SENSITIVITIES = ("low", "normal", "high", "critical")
# Epistemic validity axis (kind x status, §6): orthogonal to the node type.
NODE_STATUSES = ("current", "provisional", "superseded", "disputed", "rejected")
PERMISSIONS = ("read", "write", "admin")
MODES = ("allow", "confirm", "deny")
TRUSTS = ("trusted", "limited", "untrusted")
EDGE_CATEGORIES = ("containment", "associative")
PROPOSAL_STATUSES = ("pending", "approved", "rejected", "expired")


def _check_values(column: str, values: tuple[str, ...]) -> str:
    """SQL fragment: column IN (...) for a CheckConstraint."""
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
