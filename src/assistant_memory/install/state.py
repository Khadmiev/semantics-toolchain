# SPDX-License-Identifier: Apache-2.0
"""The install state machine: facts in, state out (B.13 A-1/A-4/A-5).

The state is COMPUTED from server-checkable predicates on every ask — startup, request
time, and each resident unit boundary — with no memory of past answers (A-1). What is
persisted is never the state itself but the facts that exist nowhere else: the
install-settings store (routing id, release identities, generation counter), the
completion records, and the regression events (models/install.py).

Layout:

- ``REQUIRED_SETTINGS`` — the ONE list that both the config-stage predicate and the
  configuration identity are computed from, so a value cannot be required for validity
  yet invisible to the comparison (A-4).
- stage predicates 1-5, each answerable against the database and configuration.
- ``compute_state`` — the five stages, the first unclosed one, and the reported status.
- ``reconcile_release`` — A-4's compare/promote step, run at startup.
- ``observe_regression`` — A-5's durable invalidation event, run at every
  re-evaluation point after the facts are computed.
"""

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import PLACEHOLDER_SESSION_SECRETS, settings
from ..conventions import DOC_LABEL
from ..models.graph import Node, NodeSpace, NodeVersion
from ..models.identity import Account, Membership, Space, User
from ..models.install import (
    SETTING_ACTIVE_RELEASE,
    SETTING_CONVENTIONS_SPACE_ID,
    SETTING_INSTALLATION_GENERATION,
    SETTING_PENDING_RELEASE,
    InstallCompletionRecord,
    InstallRegressionEvent,
    InstallSetting,
)

logger = logging.getLogger(__name__)

#: The default space's name — ONE definition, consumed by the bootstrap (which ensures
#: the space) and by the seed-stage predicate (which checks it), so the two cannot
#: drift apart. Lives here rather than in bootstrap.py because bootstrap imports this
#: module already (the reverse import would be circular).
DEFAULT_SPACE_NAME = "personal"

# --- the required-settings list (A-1 stage 2 == A-4 identity, one source) ----------


def _required_settings() -> dict[str, str | None]:
    """The config stage's required settings, name -> live value (None = missing).

    A placeholder value is reported as missing: the stage's question is "does a real
    value exist", and a shipped stand-in is not one. The placeholder vocabulary is
    ``config.PLACEHOLDER_SESSION_SECRETS`` — one definition shared with the startup
    guard, so no surface can ship a placeholder the predicates do not recognize. This
    list is the SINGLE source for both the stage-2 predicate and the configuration
    identity (A-4's one-to-one join) — extend it in one place or not at all.
    """
    return {
        "base_url": settings.base_url or None,
        "google_client_id": settings.google_client_id or None,
        "google_client_secret": settings.google_client_secret or None,
        "owner_email": settings.owner_email or None,
        "session_secret": (
            None
            if not settings.session_secret
            or settings.session_secret in PLACEHOLDER_SESSION_SECRETS
            else settings.session_secret
        ),
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def config_identity() -> str | None:
    """The configuration identity the loop closes under (A-4), or None while the
    config stage is unclosed.

    A digest over exactly the required-settings list: the base URL and the OAuth
    client id and owner email participate as values (they are not secrets), the
    Google client secret and the session secret participate as non-reversible
    digests. One missing value -> no identity: an identity computed over a partial
    configuration would compare equal across genuinely different installs.
    """
    values = _required_settings()
    if any(v is None for v in values.values()):
        return None
    material = "\n".join(
        (
            f"base_url={values['base_url']}",
            f"google_client_id={values['google_client_id']}",
            f"google_client_secret_sha256={_digest(values['google_client_secret'])}",
            f"owner_email={values['owner_email']}",
            f"session_secret_sha256={_digest(values['session_secret'])}",
        )
    )
    return _digest(material)


def executing_commit() -> str | None:
    """The server's OWN build-injected identity (A-4): the commit baked into the
    executing artifact at build time. Empty/unset -> None ("unknown"), which A-4
    treats as a named refusal below INSTALLED, never a silent pass."""
    return os.environ.get("AM_GIT_COMMIT") or None


_FULL_COMMIT_FORM = re.compile(r"[0-9a-f]{40}")


def commit_form_problem(commit: str) -> str | None:
    """A-4's ONE form contract over the commit half of a release identity, shared by
    every path that records or certifies one: the update-path record operation
    (install/apply.py) and the first-bring-up target synthesis + comparison here.
    Returns None on a full 40-hex lowercase object id, else the refusal by name.

    The realistic failure this catches is not a mistyped hash — nobody types one: the
    value rides a build-command substitution, and a shell-dialect mismatch (cmd leaves
    ``$(git rev-parse HEAD)`` literal) or a non-git checkout (a zip download) hands the
    build a non-hash string. Validating on one path only let a first bring-up certify
    an identity the update path would refuse (review f46a31d2, reopened finding
    initial-release-shared-capture-still-bypassed).
    """
    # fullmatch, not a $-anchored match: Python's $ also matches before a FINAL
    # newline, which would pass a 41-character substitution artifact ("<40 hex>\n")
    # through the declared exact-length contract.
    if not _FULL_COMMIT_FORM.fullmatch(commit):
        return (
            f"commit {commit!r} is not a full 40-character lowercase hex object id "
            "(rebuild with GIT_COMMIT=$(git rev-parse HEAD) resolved by an expanding "
            "shell, from a real git checkout)"
        )
    return None


def executing_tag() -> str | None:
    """The release TAG half of the build-injected identity (A-4: "tag where one
    exists, the commit always"), riding the same inject step as the commit. Optional
    by design — empty when the checkout was not at a tag; consumed by the
    first-bring-up target synthesis so a tag-deployed install keeps its tag (review
    f46a31d2, finding initial-release-bypasses-capture-and-loses-tag)."""
    return os.environ.get("AM_GIT_TAG") or None


def release_identity_problem(target: dict | None) -> str | None:
    """A-4's ONE full-identity comparison, shared by every consumer: promotion,
    refusal, and the stage-5 proof all ask the same question of a target —
    does the EXECUTING identity (commit AND tag) match it? Returns None on a full
    match, else the failed half by name. Comparing only the commit let a
    same-commit/different-tag target promote and reuse an old completion proof
    (review f46a31d2, finding release-tag-not-compared-as-identity)."""
    if target is None:
        return "no target recorded"
    executing = executing_commit()
    if executing is None:
        return "executing identity missing (image built without GIT_COMMIT)"
    form = commit_form_problem(executing)
    if form is not None:
        # The same form rule the update-path record operation enforces (A-4: one
        # shared contract) — a malformed executing identity is its own named refusal,
        # never certified by a comparison it would trivially win against itself.
        return f"executing identity malformed: {form}"
    if target.get("commit") != executing:
        return "tree updated, old code executing"
    if target.get("tag") != executing_tag():
        return (
            f"release tag mismatch (target {target.get('tag')!r}, executing "
            f"{executing_tag()!r} — rebuild with GIT_TAG matching the target)"
        )
    return None


# --- install-settings accessors ----------------------------------------------------


async def get_setting(session: AsyncSession, key: str):
    row = await session.get(InstallSetting, key)
    return None if row is None else row.value


async def set_setting(session: AsyncSession, key: str, value) -> None:
    await session.execute(
        pg_insert(InstallSetting)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )


async def get_generation(session: AsyncSession) -> int:
    """The persisted monotonic installation generation (A-4). Starts at one."""
    value = await get_setting(session, SETTING_INSTALLATION_GENERATION)
    return int(value) if value is not None else 1


# --- stage predicates (A-1) --------------------------------------------------------


@lru_cache(maxsize=1)
def _expected_schema_revision() -> str | None:
    """The migration head shipped with this code, from the alembic script directory.

    None when the scripts are not locatable (an unusual layout); the schema predicate
    then degrades to "a single applied revision exists" and says so in its detail.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        for base in (Path.cwd(), *Path(__file__).resolve().parents):
            ini = base / "alembic.ini"
            if ini.is_file():
                cfg = Config(str(ini))
                script_location = cfg.get_main_option("script_location") or "migrations"
                if not Path(script_location).is_absolute():
                    cfg.set_main_option("script_location", str(base / script_location))
                heads = ScriptDirectory.from_config(cfg).get_heads()
                return heads[0] if len(heads) == 1 else None
    except Exception:  # noqa: BLE001 - a broken lookup degrades the check, never the boot
        logger.exception("could not resolve the expected alembic head")
    return None


async def _applied_schema_revision(session: AsyncSession) -> str | None:
    """Read the applied alembic revision WITHOUT poisoning the caller's transaction.

    Under PostgreSQL a failed statement aborts the current transaction: swallowing the
    exception and carrying on would make every later stage-predicate query in the same
    session raise InFailedSQLTransaction instead of computing (the promised behaviour
    is a computed `schema` failure). The probe therefore runs inside a SAVEPOINT — a
    failure rolls back the savepoint alone and the session stays usable.
    """
    try:
        async with session.begin_nested():
            return await session.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:  # noqa: BLE001 - no table / no schema at all
        return None


@dataclass
class Stage:
    """One stage's computed answer."""

    name: str
    ok: bool
    closes_with: str
    detail: dict = field(default_factory=dict)


_CLOSES = {
    "schema": (
        "Apply the database migrations (the container runs `alembic upgrade head` "
        "on start; a failure here is a pre-serve state — read the container logs)."
    ),
    "config": (
        "Fill the required settings in the instance configuration and restart; "
        "the unfilled ones are listed in this stage's detail."
    ),
    "seed": (
        "The structural bootstrap runs on process start once schema and config hold; "
        "if this stage stays red, read the service logs for the bootstrap failure."
    ),
    "owner": (
        "Open the install surface in a browser and complete the owner Google login."
    ),
    "validated": (
        "Connect an MCP client under the owner credential and make one successful "
        "authenticated `conventions` tool call — the runbook's finishing probe."
    ),
}


async def _stage_schema(session: AsyncSession) -> Stage:
    applied = await _applied_schema_revision(session)
    expected = _expected_schema_revision()
    if applied is None:
        return Stage("schema", False, _CLOSES["schema"], {"applied": None})
    if expected is None:
        return Stage(
            "schema", True, _CLOSES["schema"],
            {"applied": applied, "expected": "unknown (migration scripts not locatable)"},
        )
    return Stage(
        "schema", applied == expected, _CLOSES["schema"],
        {"applied": applied, "expected": expected},
    )


def _stage_config() -> Stage:
    values = _required_settings()
    missing = sorted(name for name, v in values.items() if v is None)
    return Stage("config", not missing, _CLOSES["config"], {"missing": missing})


async def _owner_user(session: AsyncSession) -> User | None:
    return await session.scalar(
        select(User).where(User.is_owner.is_(True)).order_by(User.created_at).limit(1)
    )


async def _owner_account_id(session: AsyncSession) -> uuid.UUID | None:
    return await session.scalar(
        select(Account.id)
        .join(User, User.id == Account.user_id)
        .where(User.is_owner.is_(True))
        .order_by(Account.created_at)
        .limit(1)
    )


async def routing_home_problem(
    session: AsyncSession, space_id: uuid.UUID, owner_account_id: uuid.UUID | None
) -> str | None:
    """A-1's routing-home contract, SHARED by every consumption site: the space exists
    AND the owner account holds a membership on it. Returns None when the contract
    holds, else the failed half by name.

    One contract, four callers — the recorded-id branch and the environment-adoption
    branch of bootstrap.resolve_conventions_home, the set-conventions-home repair
    endpoint, and the seed-stage routing predicate — so an id cannot be adopted,
    recorded, or certified against a weaker check than the one the spec states
    ("existence-and-ownership verification"). Existence-only acceptance let a mistaken
    pre-provisioned id seed conventions into a space the owner cannot see (review
    f46a31d2, finding conventions-home-ownership-not-enforced).
    """
    if await session.get(Space, space_id) is None:
        return "the space does not exist"
    if owner_account_id is None:
        return "no owner account exists to hold the membership"
    member = await session.scalar(
        select(Membership.permission).where(
            Membership.space_id == space_id,
            Membership.account_id == owner_account_id,
        )
    )
    if member is None:
        return "the owner account has no membership in the space"
    return None


async def qualifying_projection_space(
    session: AsyncSession, owner_account_id: uuid.UUID | None, *, space_id: uuid.UUID | None = None
) -> uuid.UUID | None:
    """THE one definition of 'this space holds the serving-grade conventions
    projection': a current, non-deleted Document with the canonical label whose
    CURRENT version carries its full-text index row, in a space the OWNER account is
    a member of. Returns the holding space's id (or None); pass ``space_id`` to ask
    about one specific space.

    Shared by the seed-stage predicate and by BOTH repair-route queries — the round-4
    finding routing-home-projection-qualification-not-shared caught the repair using
    a weaker hand-written copy (no FTS, no owner visibility), so the two could
    disagree about the same space."""
    if owner_account_id is None:
        return None
    query = (
        select(NodeSpace.space_id)
        .join(Node, Node.id == NodeSpace.node_id)
        .join(NodeVersion, Node.current_version_id == NodeVersion.id)
        .join(
            Membership,
            and_(
                Membership.space_id == NodeSpace.space_id,
                Membership.account_id == owner_account_id,
            ),
        )
        .where(
            Node.type == "Document",
            Node.label == DOC_LABEL,
            Node.deleted_at.is_(None),
            Node.status == "current",
            NodeVersion.search_tsv.isnot(None),
        )
        .limit(1)
    )
    if space_id is not None:
        query = query.where(NodeSpace.space_id == space_id)
    return await session.scalar(query)


async def _stage_seed(session: AsyncSession) -> Stage:
    detail: dict = {}
    owner_account = await _owner_account_id(session)
    detail["owner_account"] = owner_account is not None

    # A-1 stage 3 names the default space and the owner's membership on it as seed
    # facts in their own right (finding seed-stage-omits-default-space-facts): the
    # SAME selection rule the bootstrap's _ensure_personal_space uses — the owner's
    # membership row joined to a space named "personal" — so the predicate goes false
    # when either the space or the membership is gone.
    default_space_ok = False
    if owner_account is not None:
        default_space_ok = (
            await session.scalar(
                select(Space.id)
                .join(Membership, Membership.space_id == Space.id)
                .where(
                    Membership.account_id == owner_account,
                    Space.name == DEFAULT_SPACE_NAME,
                )
                .limit(1)
            )
        ) is not None
    detail["default_space"] = (
        "exists with owner membership"
        if default_space_ok
        else "no default space with an owner membership"
    )
    # The routing prerequisite (A-1 stage 3): the recorded conventions-home id
    # resolves UNDER THE SHARED CONTRACT — existence and the owner's membership
    # (routing_home_problem), not bare existence. Identity is the recorded id,
    # never a name. Resolved FIRST because the document predicate below is scoped
    # to it.
    home_raw = await get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    routing_ok = False
    home_id: uuid.UUID | None = None
    if home_raw is not None:
        try:
            home_id = uuid.UUID(str(home_raw))
        except ValueError:
            detail["conventions_home"] = f"recorded id is not a uuid: {home_raw!r}"
        else:
            problem = await routing_home_problem(session, home_id, owner_account)
            routing_ok = problem is None
            detail["conventions_home"] = (
                "resolves" if routing_ok else f"recorded id {home_id}: {problem}"
            )
    else:
        detail["conventions_home"] = "no recorded id"

    # The conventions Document IN THE RECORDED HOME: the SHARED qualifying-projection
    # predicate (qualifying_projection_space), scoped to the home space — an
    # owner-visible projection in some OTHER space used to satisfy this predicate
    # while the recorded home stood empty, unjoining the two facts stage 3 binds
    # together (finding routing-home-not-joined-to-conventions-projection).
    conventions_ok = False
    if owner_account is not None and routing_ok and home_id is not None:
        conventions_ok = (
            await qualifying_projection_space(session, owner_account, space_id=home_id)
        ) is not None
    detail["conventions_document"] = (
        conventions_ok
        if conventions_ok
        else "no current owner-visible conventions projection IN the recorded home"
    )

    return Stage(
        "seed",
        bool(owner_account) and default_space_ok and conventions_ok and routing_ok,
        _CLOSES["seed"],
        detail,
    )


async def _stage_owner(session: AsyncSession) -> Stage:
    owner = await _owner_user(session)
    bound = owner is not None and owner.google_sub is not None
    return Stage("owner", bound, _CLOSES["owner"], {"bound": bound})


async def _record_exists(session: AsyncSession, *conditions) -> bool:
    return (
        await session.scalar(
            select(InstallCompletionRecord.id).where(*conditions).limit(1)
        )
    ) is not None


async def _any_completion_record(session: AsyncSession) -> bool:
    return (
        await session.scalar(select(func.count()).select_from(InstallCompletionRecord))
    ) > 0


async def _stage_validated(session: AsyncSession, generation: int) -> Stage:
    """Stage 5: a completion record exists that proves the CURRENT generation, the
    LIVE configuration identity, and the release that is actually executing — which
    must also be the active installed release (A-4). ANY matching record is a true
    proof; "the latest" would be a lottery between same-timestamp rows."""
    detail: dict = {}
    if not await _record_exists(
        session, InstallCompletionRecord.generation == generation
    ):
        return Stage(
            "validated", False, _CLOSES["validated"],
            {"record": "none for the current generation"},
        )

    live_identity = config_identity()
    identity_ok = live_identity is not None and await _record_exists(
        session,
        InstallCompletionRecord.generation == generation,
        InstallCompletionRecord.config_identity == live_identity,
    )
    detail["config_identity"] = (
        "a record matches" if identity_ok else "no record matches the live identity"
    )

    active = await get_setting(session, SETTING_ACTIVE_RELEASE)
    pending = await get_setting(session, SETTING_PENDING_RELEASE)
    executing = executing_commit()
    if executing is None:
        release_ok = False
        detail["release"] = "executing identity missing (image built without GIT_COMMIT)"
    elif (form := commit_form_problem(executing)) is not None:
        # The same shared form contract, carried into the REPORTING actor: with the
        # first-bring-up synthesis refusing to record a malformed identity, both
        # release settings stay empty — without this branch the status would say
        # only "no active installed release" and never name the actual problem.
        release_ok = False
        detail["release"] = f"executing identity malformed: {form}"
    elif pending is not None and release_identity_problem(pending) is not None:
        # A-4: a pending target the executing identity does not FULLY match (commit
        # AND tag) is a named refusal HOLDING THE STATE BELOW INSTALLED — not a
        # report beside a certificate (findings pending-release-mismatch-still-
        # installed and release-tag-not-compared-as-identity).
        release_ok = False
        detail["release"] = (
            f"pending target not promoted: {release_identity_problem(pending)}"
        )
    elif active is None:
        release_ok = False
        detail["release"] = "no active installed release recorded"
    elif (active_problem := release_identity_problem(active)) is not None:
        release_ok = False
        detail["release"] = active_problem
    else:
        # The record proves the FULL executing identity: commit and tag both.
        release_ok = live_identity is not None and await _record_exists(
            session,
            InstallCompletionRecord.generation == generation,
            InstallCompletionRecord.config_identity == live_identity,
            InstallCompletionRecord.release_commit == executing,
            InstallCompletionRecord.release_tag.is_(None)
            if executing_tag() is None
            else InstallCompletionRecord.release_tag == executing_tag(),
        )
        detail["release"] = (
            "a record proves the executing release"
            if release_ok
            else "no record proves the executing release (commit AND tag)"
        )

    return Stage("validated", identity_ok and release_ok, _CLOSES["validated"], detail)


# --- the computed state ------------------------------------------------------------


@dataclass
class InstallState:
    stages: list[Stage]
    generation: int
    active_release: dict | None
    pending_release: dict | None
    executing: str | None
    release_refusal: str | None
    was_installed: bool  # any completion record exists, ever (degradation reporting)

    @property
    def first_unclosed(self) -> Stage | None:
        return next((s for s in self.stages if not s.ok), None)

    @property
    def serving_open(self) -> bool:
        """A-2's admission condition: stages 1-4 hold (stage 5 never gates admission)."""
        return all(s.ok for s in self.stages[:4])

    @property
    def status(self) -> str:
        if all(s.ok for s in self.stages):
            return "installed"
        if self.serving_open:
            return "validating"
        return "degraded" if self.was_installed else "installing"


async def compute_state(session: AsyncSession) -> InstallState:
    """Recompute the whole state from facts. No caching, no memory (A-1)."""
    generation = await get_generation(session)
    stages = [
        await _stage_schema(session),
        _stage_config(),
        await _stage_seed(session),
        await _stage_owner(session),
        await _stage_validated(session, generation),
    ]
    active = await get_setting(session, SETTING_ACTIVE_RELEASE)
    pending = await get_setting(session, SETTING_PENDING_RELEASE)
    executing = executing_commit()
    refusal: str | None = None
    if executing is None:
        refusal = (
            "missing executing identity: the image was built without the GIT_COMMIT "
            "build argument, which is a required input of the release capture/apply "
            "operation"
        )
    elif (form := commit_form_problem(executing)) is not None:
        refusal = f"executing identity malformed: {form}"
    elif pending is not None and (pending_problem := release_identity_problem(pending)):
        refusal = pending_problem
    return InstallState(
        stages=stages,
        generation=generation,
        active_release=active,
        pending_release=pending,
        executing=executing,
        release_refusal=refusal,
        was_installed=await _any_completion_record(session),
    )


# --- A-4: the release compare/promote step (startup) -------------------------------


async def reconcile_release(session: AsyncSession) -> None:
    """Compare the executing identity against the pending target; promote on match.

    Promotion happens on this comparison and nowhere else (A-4): on a match the
    pending target BECOMES the active installed release and the pending slot is
    cleared. On a mismatch or a missing executing identity nothing promotes — the
    active release stays what it was and the pending target stays recorded, so the
    status can report both. The initial install is not a special case: with no
    pending target and no active release, the identity of the code being brought up
    is recorded as the first pending target and promoted by the same comparison.
    """
    executing = executing_commit()
    pending = await get_setting(session, SETTING_PENDING_RELEASE)
    active = await get_setting(session, SETTING_ACTIVE_RELEASE)

    if pending is None and active is None and executing is not None:
        form = commit_form_problem(executing)
        if form is not None:
            # The synthesized first target rides the SAME form contract the update
            # path's record operation enforces — a malformed identity is refused by
            # name here instead of being recorded and certified by a comparison
            # against itself (review f46a31d2).
            logger.warning(
                "release reconcile: first bring-up refused — executing identity "
                "malformed: %s — nothing recorded, nothing promotes", form,
            )
            return
        # First bring-up: the first pending target is the identity of the code the
        # installer brings up (A-4) — the FULL identity: the build-injected tag when
        # the checkout sat on one, so a tag-deployed install does not lose the base
        # its later tag-to-tag updates calculate from.
        pending = {"tag": executing_tag(), "commit": executing}
        await set_setting(session, SETTING_PENDING_RELEASE, pending)

    if executing is None:
        logger.warning(
            "release reconcile: executing identity missing (AM_GIT_COMMIT unset) — "
            "nothing promotes; the status reports the named refusal"
        )
        return
    if pending is None:
        return
    problem = release_identity_problem(pending)
    if problem is None:
        # Promotion demands the FULL identity match — commit AND tag (A-4; finding
        # release-tag-not-compared-as-identity).
        await set_setting(session, SETTING_ACTIVE_RELEASE, pending)
        await set_setting(session, SETTING_PENDING_RELEASE, None)
        logger.info(
            "release reconcile: promoted pending target %s to active installed release",
            pending,
        )
    else:
        logger.warning(
            "release reconcile: %s (pending %s, executing %s/%s) — the active "
            "release stays %s",
            problem, pending, executing, executing_tag(), active,
        )


# --- A-5: the durable regression event ---------------------------------------------


async def observe_regression(session: AsyncSession, state: InstallState) -> bool:
    """Record a regression event and advance the generation, when one is observed.

    A regression exists when a completion record for the CURRENT generation exists
    (the instance was INSTALLED in this generation) and either a structural stage
    (1-4) stops holding, or the live configuration identity differs from the
    record's persisted one (A-5: a valid-but-different configuration demands a new
    loop-close). Returns True when an event was recorded.

    Advancing keys on the current generation row under a re-check, so two
    concurrent observers of the same episode record one event: the second one
    re-reads the generation, finds no record for it, and stands down.
    """
    generation = await get_generation(session)
    if not await _record_exists(
        session, InstallCompletionRecord.generation == generation
    ):
        return False  # nothing certified in this generation -> nothing to retire

    structural_failed = next((s for s in state.stages[:4] if not s.ok), None)
    live_identity = config_identity()
    # The identity comparison is against the generation's RECORDS: a change is a
    # regression only while no record proves the live identity — a fresh loop-close
    # under the new configuration is current proof, not a stale one.
    identity_changed = (
        structural_failed is None
        and live_identity is not None
        and not await _record_exists(
            session,
            InstallCompletionRecord.generation == generation,
            InstallCompletionRecord.config_identity == live_identity,
        )
    )
    if structural_failed is None and not identity_changed:
        return False

    # Serialize the advance on the generation row.
    await session.execute(
        pg_insert(InstallSetting)
        .values(key=SETTING_INSTALLATION_GENERATION, value=1)
        .on_conflict_do_nothing(index_elements=["key"])
    )
    locked = await session.execute(
        select(InstallSetting)
        .where(InstallSetting.key == SETTING_INSTALLATION_GENERATION)
        .with_for_update()
    )
    row = locked.scalar_one()
    current = int(row.value)
    if current != generation or not await _record_exists(
        session, InstallCompletionRecord.generation == current
    ):
        return False  # another observer already advanced for this episode
    row.value = current + 1
    failed_stage = structural_failed.name if structural_failed else "config_identity"
    detail = (
        dict(structural_failed.detail)
        if structural_failed
        else {"config_identity": "live configuration differs from the record's identity"}
    )
    session.add(
        InstallRegressionEvent(
            failed_stage=failed_stage,
            detail=detail,
            generation_before=current,
            generation_after=current + 1,
        )
    )
    logger.warning(
        "install regression observed (%s): generation %d -> %d — every earlier "
        "completion record is now historical; the status reports "
        "installed-then-degraded until a new loop-close",
        failed_stage, current, current + 1,
    )
    return True


# --- A-4: the completion record write (called by the canonical-probe hook) ---------


async def write_completion_record(
    session: AsyncSession, *, account_id: uuid.UUID, observation: dict
) -> InstallCompletionRecord | None:
    """Write the completion record for an observed canonical probe (A-4).

    Refused (returns None, with the reason logged) when the executing identity is
    missing or malformed — the record's release column is the proof's subject and
    cannot be honest without a real one — or when the configuration identity cannot
    be computed (config stage unclosed; the gate should have refused the call first).
    """
    executing = executing_commit()
    if executing is None:
        logger.warning(
            "completion record refused: executing identity missing (AM_GIT_COMMIT "
            "unset) — a record cannot name the release it proves"
        )
        return None
    form = commit_form_problem(executing)
    if form is not None:
        # The CERTIFYING actor rides the same shared form contract as the recording
        # ones: a record naming a malformed release would be a durable false proof.
        logger.warning("completion record refused: executing identity malformed: %s", form)
        return None
    identity = config_identity()
    if identity is None:
        logger.warning(
            "completion record refused: configuration identity not computable "
            "(required settings missing)"
        )
        return None
    # The record's subject is the EXECUTING identity — both halves read from the
    # build injection, never copied from the active pointer (finding
    # release-tag-not-compared-as-identity: the copy let a record certify a tag the
    # executing image never carried).
    record = InstallCompletionRecord(
        account_id=account_id,
        observation=observation,
        generation=await get_generation(session),
        config_identity=identity,
        release_tag=executing_tag(),
        release_commit=executing,
    )
    session.add(record)
    await session.flush()
    logger.info(
        "completion record written: generation=%d release=%s account=%s",
        record.generation, executing, account_id,
    )
    return record
