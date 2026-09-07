# SPDX-License-Identifier: Apache-2.0
# Import model modules so their tables register on Base.metadata (used by Alembic).
from . import graph, identity, install, policy, profile, review  # noqa: E402, F401
from .base import Base

__all__ = ["Base", "graph", "identity", "install", "policy", "profile", "review"]
