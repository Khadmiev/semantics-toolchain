# SPDX-License-Identifier: Apache-2.0
"""Review-orchestration repository errors.

Mirrors repository/errors.py: a small typed hierarchy the HTTP layer maps to
status codes. `ConvergenceRefusedError` is the load-bearing one — it is what the
server raises to REFUSE an inconsistent `converged` declaration (spec §10, INV-2/
INV-4); the review stays open (fail-safe).
"""


class ReviewError(Exception):
    """Base class for review-orchestration errors."""


class ReviewNotFoundError(ReviewError):
    """No review with that id."""


class InvalidReviewTokenError(ReviewError):
    """Presented per-review token does not resolve to a row."""


class RevokedReviewTokenError(ReviewError):
    """Per-review token exists but was revoked (review finalized/abandoned)."""


class InvalidStateTransitionError(ReviewError):
    """A requested state change is not a legal successor of the current state (spec §5)."""


class InvalidMessagePayloadError(ReviewError):
    """A message payload violates its kind's wire contract (e.g. a disposition without
    a terminal ``outcome``).

    Refused at POST time so the defect surfaces to its AUTHOR immediately: convergence
    counts only ``outcome in (fixed|waived)``, so a mis-keyed disposition would silently
    never satisfy the guard and surface iterations later as a 409 to the OTHER role
    (the hpo-spec review incident, feedback 4a83064b).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InstrumentValidationError(ReviewError):
    """A profile / model-list / default-instrument MANAGEMENT request violates its
    contract (B.9 Parts C/D): non-absolute paths in a profile version, a sandbox form
    that is not pinned read-only (D-6), a default version missing the operator quote or
    with a non-resolving Decision ref (D-5), an override key unknown to the genre
    registry. Carries the concrete reason; HTTP maps it to 422."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReviewCreationRefusedError(ReviewError):
    """Review creation refused at the instrument gate (B.9 C-5 / D-1 / D-3 / D-4).

    The refusal is a ROUTE, not a dead end: ``reason`` names what is missing (the
    host × engine pair with no proven profile, the model entry to add or verify, the
    identity field to fill, the missing self-check waiver) and points at the preparation
    path, so a first run on a new machine lands in setup, not in a mystery.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ConvergenceRefusedError(ReviewError):
    """A `converged` declaration is MALFORMED (spec §10, Part K) — the critic's own error.

    Raised ONLY for a malformed declaration: wrong state (no critic pickup), a stale
    artifact version, or a missing materialized clean pass. Carries the concrete failing
    check; the review is left open — refusing is fail-safe. A declaration blocked merely by
    an unsettled LEDGER (undisposed findings, open operator items, ungrafted external
    defects) is NOT raised here — it is recorded and routed to a wakeable state, then
    self-completed when the blocker clears.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
