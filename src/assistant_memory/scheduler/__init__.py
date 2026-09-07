# SPDX-License-Identifier: Apache-2.0
"""Actionable-layer scheduler plugin.

The resident runner reads ScheduledJob nodes (definitions live in the graph, D3) and
fires due jobs. Slice 1 is the foundation: the ScheduledJob node type, a due-jobs query,
a D23 claim/finish run-record, and a rescan loop. The execute seam (GATHER→REASON→ACT),
LISTEN/NOTIFY precision, the trigger predicate, and cron/event kinds land in later slices.
"""
