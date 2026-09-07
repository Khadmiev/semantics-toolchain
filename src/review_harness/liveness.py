# SPDX-License-Identifier: Apache-2.0
"""Liveness: the window in which death is recognised, and the time to notify.

The thresholds are sequential, not competing (the operator's decision of
2026-09-01):

* the MACHINE profile owns the recognition window — silence longer than the
  window means real death; the window is grounded in observed facts of the
  environment (validated in machine_profile);
* the OPERATOR profile owns the notification time — once death is recognised,
  the operator learns of it no later than that;
* the whole time until the operator knows is the sum of the two, and that is a
  property of the environment rather than a defect; the window in force is named
  at the start of a run.
"""

from __future__ import annotations

import re

from .errors import HarnessError

# A marker in the operator profile cache. It lives in the profile (graph →
# cache), not in the code: another operator has another time. The line format:
#   notify-within: 300s
_NOTIFY_RE = re.compile(r"notify-within:\s*(\d+)\s*s", re.IGNORECASE)


def operator_notify_within_sec(profile_cache_text: str) -> int:
    """Take the notification time out of the operator profile cache.

    A missing parameter is a loud refusal, not a quiet default: the thresholds are
    parameters of the profiles, and no numbers are baked into the code (the
    liveness section of the spec).
    """
    m = _NOTIFY_RE.search(profile_cache_text)
    if not m:
        raise HarnessError(
            "the operator profile carries no notification time (notify-within)",
            cause="the notification time is a profile parameter; there is no default baked in",
            next_action=(
                "add the preference to the graph (remember_preference, domain liveness, "
                "a line of the form «notify-within: 300s») and refresh the profile cache"
            ),
        )
    return int(m.group(1))


def announce_window(detection_window_sec: int, notify_within_sec: int, say) -> None:
    """Name the thresholds in force at the start of a run.

    So that "within a minute" on this machine is not a surprise: the whole time
    from death to the operator's knowledge is the window plus the notification
    time.
    """
    say(
        f"[liveness] This machine's death-recognition window: {detection_window_sec}s; "
        f"notification after recognition: no later than {notify_within_sec}s. "
        f"Whole time until the operator knows: up to {detection_window_sec + notify_within_sec}s."
    )
