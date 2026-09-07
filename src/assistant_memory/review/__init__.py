# SPDX-License-Identifier: Apache-2.0
"""Review orchestration — automates the dev<->critic review loop (spec
docs/design/2026-07-07_review_orchestration_spec.md). A FastAPI module sibling of
the memory app: message bus + per-review isolation tokens + the server-enforced
convergence guard. Transient state only; the durable outcome folds into the graph
on finalize (INV-7).
"""
