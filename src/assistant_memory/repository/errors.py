# SPDX-License-Identifier: Apache-2.0
class RepositoryError(Exception):
    """Base class for repository-layer errors."""


class NotFoundError(RepositoryError):
    """Target node/edge does not exist (or is deleted)."""


class ConflictError(RepositoryError):
    """Optimistic-concurrency (CAS) failure: expected_version is stale."""


class ContainerConflictError(RepositoryError):
    """A node already has an active container (single-parent invariant)."""
