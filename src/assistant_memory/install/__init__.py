# SPDX-License-Identifier: Apache-2.0
"""B.13 Part A — the install state machine.

Spec: docs/design/2026-08-27_public_repo_install_process_spec.md. The state is computed
from server-checkable facts (A-1), the gate refuses substantive requests while stages
1-4 are unclosed (A-2), the install surface stays served (A-3), stage 5 closes only on
the server's own observation of the canonical probe (A-4), and lost facts reopen the
gate through a durable regression event (A-5).
"""

from .state import (  # noqa: F401
    InstallState,
    Stage,
    compute_state,
    config_identity,
    executing_commit,
    get_setting,
    observe_regression,
    reconcile_release,
    set_setting,
)
