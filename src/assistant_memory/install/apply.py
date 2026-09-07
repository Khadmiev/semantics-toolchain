# SPDX-License-Identifier: Apache-2.0
"""The RECORD step of the release capture/apply operation (B.13 A-4), as a CLI.

One shared operation serves the initial install and every update. Its steps:
(1) inject — the rebuild command passes the checked-out commit into the build
(GIT_COMMIT, a required input); (2) RECORD — this module: persist the target release
(tag where one exists, the commit always) into the install-settings store as the
PENDING TARGET, before restart; (3) compare / (4) promote / (5) refuse by name — the
server does those itself at startup (install/state.reconcile_release).

The updater invokes this before rebuild+restart:

    docker compose run --rm app python -m assistant_memory.install.apply \
        --commit $(git rev-parse HEAD) [--tag v3]

The initial bring-up needs no invocation: with no pending and no active release the
server records the identity of the code it brings up as the first pending target and
promotes it through the same comparison (A-4: the initial install is not a special
case). That synthesized identity is FULL: the build carries the tag beside the commit
(GIT_TAG next to GIT_COMMIT, both derived from the checkout by the runbook's build
command), so a tag-deployed install records its tag as the base for later tag-to-tag
updates instead of a tagless target (review f46a31d2). What is SHARED between the two
paths is the record/compare/promote contract INCLUDING the commit form rule
(state.commit_form_problem): the synthesis refuses a malformed identity by name, the
same way this CLI refuses a malformed --commit — the initial path differs only in who
supplies the value, never in what is accepted.
"""

import argparse
import asyncio
import sys

from ..db import SessionLocal
from ..models.install import SETTING_PENDING_RELEASE
from .state import commit_form_problem, get_setting, set_setting


async def _record(tag: str | None, commit: str) -> int:
    async with SessionLocal() as session:
        previous = await get_setting(session, SETTING_PENDING_RELEASE)
        await set_setting(
            session, SETTING_PENDING_RELEASE, {"tag": tag, "commit": commit}
        )
        await session.commit()
    if previous is not None:
        print(
            f"note: replaced an unapplied pending target {previous} — the previous "
            "apply never promoted (its restart did not run this commit)"
        )
    print(
        f"pending target recorded: tag={tag or '(none)'} commit={commit}\n"
        "next: rebuild with GIT_COMMIT=<this commit>, restart, and the server "
        "compares its executing identity and promotes on match; the status then "
        "reads VALIDATING until the canonical `conventions` probe closes the loop."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="record the pending target release (the RECORD step of A-4)"
    )
    parser.add_argument(
        "--commit", required=True,
        help="the target's full 40-hex commit id (git rev-parse <ref>)",
    )
    parser.add_argument("--tag", default=None, help="the release tag, where one exists")
    args = parser.parse_args(argv)
    problem = commit_form_problem(args.commit)
    if problem is not None:
        parser.error(f"--commit refused: {problem}")
    return asyncio.run(_record(args.tag, args.commit))


if __name__ == "__main__":
    sys.exit(main())
