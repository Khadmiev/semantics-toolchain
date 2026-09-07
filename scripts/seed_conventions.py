# SPDX-License-Identifier: Apache-2.0
"""Project the Assistant-Memory conventions into the graph, idempotently (CLI).

The content and the seeding logic live in ``assistant_memory.conventions`` (shared
with the startup bootstrap). This script is the manual entry point: it resolves a
target space + author account, runs the seeder with the REAL embedder (full
semantic index), and reports. Idempotent — safe to re-run after editing the spec.

There is no default target. It publishes into the CONFIGURED conventions home space, or
into a space named explicitly on the command line; with neither, it refuses. The silent
default it used to carry (`--space-name personal`) is the proximate cause of the duplicate
projection created on 2026-08-10: a name is not unique, the projection had moved out of
that space, and the seeder's per-space dedup could not see the copy it was making.

Usage (repo root, DB up):
    uv run --extra embeddings python scripts/seed_conventions.py            # configured home
    uv run --extra embeddings python scripts/seed_conventions.py --space-id <uuid>
    uv run python scripts/seed_conventions.py --dry-run                     # report, no commit
"""

import argparse
import asyncio

from sqlalchemy import select

from assistant_memory.config import settings
from assistant_memory.conventions import (
    DOC_LABEL,
    SECTIONS,
    SOURCE_PATH,
    SPEC_VERSION,
    ProjectionElsewhere,
    seed_conventions,
)
from assistant_memory.db import SessionLocal
from assistant_memory.models.identity import Account, Space
from assistant_memory.search import get_embedder


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed the Assistant-Memory conventions into the graph.")
    p.add_argument(
        "--space-id",
        default=None,
        help="Target space id; defaults to the configured conventions home space.",
    )
    p.add_argument("--account-id", default=None, help="Author account id (default: earliest).")
    p.add_argument("--dry-run", action="store_true", help="Do the work but roll back (no commit).")
    return p.parse_args()


async def _resolve_space(session, args) -> Space:
    """Explicit argument, else the configured home space. Never a guess."""
    target = args.space_id or settings.conventions_space_id
    if target is None:
        raise SystemExit(
            "no target space: set AM_CONVENTIONS_SPACE_ID to the conventions home space, or "
            "pass --space-id. Refusing to pick one."
        )
    sp = await session.get(Space, target)
    if sp is None:
        raise SystemExit(f"space {target} not found")
    return sp


async def _resolve_account_id(session, args):
    if args.account_id:
        acc = await session.get(Account, args.account_id)
        if acc is None:
            raise SystemExit(f"account {args.account_id} not found")
        return acc.id
    account_id = await session.scalar(select(Account.id).order_by(Account.created_at).limit(1))
    if account_id is None:
        raise SystemExit("no account found — is the owner bootstrapped?")
    return account_id


async def main() -> None:
    args = _parse_args()
    async with SessionLocal() as session:
        space = await _resolve_space(session, args)
        space_id, space_name = space.id, space.name
        account_id = await _resolve_account_id(session, args)

        try:
            counts = await seed_conventions(
                session, space_id=space_id, account_id=account_id, embedder=get_embedder()
            )
        except ProjectionElsewhere as exc:
            await session.rollback()
            raise SystemExit(str(exc)) from exc

        if args.dry_run:
            await session.rollback()
        else:
            await session.commit()

    verb = "would seed (dry-run)" if args.dry_run else "seeded"
    print(f"\n=== conventions {verb} into space '{space_name}' ({space_id}) ===")
    print(f"Document : {DOC_LABEL}")
    print(f"children : {len(SECTIONS)} atomic notes + 1 tag, contained_in the Document")
    print(f"nodes    : created={counts['created']} updated={counts['updated']} "
          f"unchanged={counts['unchanged']} skipped={counts['skipped']}")
    print(f"source   : {SOURCE_PATH} ({SPEC_VERSION})")


if __name__ == "__main__":
    asyncio.run(main())
