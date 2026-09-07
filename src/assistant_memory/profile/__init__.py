# SPDX-License-Identifier: Apache-2.0
"""Operator-profile plugin — server side (cycle 2 of review a6827c1a).

The profile layer turns operator interaction preferences into a first-class,
cross-agent contract: agents record preferences through validated capabilities,
the server resolves one EFFECTIVE SET per (operator, project) and compiles a
deterministic markdown profile with a content-addressed version; repo-based
agents hydrate it into a gitignored local file via the guarded bootstrap.

Spec of record: docs/design/2026-07-14_operator_profile_plugin_spec.md.
"""

from . import service  # noqa: F401
