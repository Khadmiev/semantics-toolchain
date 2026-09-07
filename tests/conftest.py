# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import assistant_memory.db as _db
from assistant_memory.config import settings
from assistant_memory.conventions import DOC_LABEL
from assistant_memory.models.graph import Node
from assistant_memory.models.identity import Account, Space, User
from assistant_memory.search import embedder as _embedder


def _test_database_url() -> str:
    """The DEDICATED test database, named explicitly — never inherited from the app's config.

    The suite used to build its engine from ``settings.database_url``, i.e. whatever database
    the app happened to be pointed at. That is not merely untidy: several tests asserted
    absolute counts ("exactly one profile entry", "version == 1") and were green only because
    the database they landed in happened to have those tables empty. Refreshing that database
    from production turned four of them red without a line of code changing — they had been
    verifying the fixture, not the behaviour.

    So the target is a separate setting and there is NO fallback. Falling back to the app's
    database is exactly the silent wrong-target this project keeps paying for; an unset value
    stops the run and says what to do instead.

    It is read through ``settings`` rather than ``os.environ`` because that is the only place
    `.env` reaches — an environment lookup finds nothing when the value lives in the file.
    """
    url = (settings.test_database_url or "").strip()
    if not url:
        raise RuntimeError(
            "AM_TEST_DATABASE_URL is not set. The suite needs its OWN database — it must not "
            "run against the one the app uses, because tests that read rows they did not "
            "create are green by luck. Create one and point at it:\n"
            "  docker exec assistant_memory_db createdb -U am assistant_memory_test\n"
            "  AM_DATABASE_URL=<that url> uv run alembic upgrade head\n"
            "  .env:  AM_TEST_DATABASE_URL=postgresql+asyncpg://am:am@localhost:5433/"
            "assistant_memory_test\n"
            "Re-run migrations against it whenever new ones land."
        )
    return url


# NullPool: a fresh connection per test, bound to that test's event loop
# (avoids asyncpg "attached to a different loop" with function-scoped loops).
_engine = create_async_engine(_test_database_url(), poolclass=NullPool)

# The app's module-global engine needs the same treatment: every TestClient (and
# every pytest-asyncio test) runs its own event loop, so a pooled asyncpg
# connection checked in under one loop blows up (AttributeError deep in asyncpg)
# when a later test's loop checks it out — e.g. test_app_boots_with_mcp_mounted
# runs the lifespan (bootstrap writes through SessionLocal), then
# test_invalid_invite_landing hits the real get_session in a fresh loop.
# Rebinding here works because conftest imports before any test module imports
# assistant_memory.main (which does `from .db import SessionLocal, engine`).
_db.engine = _engine
_db.SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _deterministic_embeddings(monkeypatch):
    """Pin the fast deterministic embedder for tests, regardless of .env (which may
    select the heavy bge-m3 for the running app)."""
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    _embedder.get_embedder.cache_clear()
    yield
    _embedder.get_embedder.cache_clear()


@pytest_asyncio.fixture
async def session():
    """A session joined to an outer transaction, rolled back after the test.

    ``join_transaction_mode="create_savepoint"`` (SQLAlchemy 2.0) makes the session
    open a SAVEPOINT on the already-open connection transaction, so route code that
    calls ``commit()`` only releases the savepoint — the outer transaction is
    discarded at teardown, keeping every test isolated even across commits.
    """
    conn = await _engine.connect()
    trans = await conn.begin()
    db = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )()
    try:
        yield db
    finally:
        await db.close()
        await trans.rollback()
        await conn.close()


@pytest.fixture(autouse=True)
def _installed_admission(monkeypatch):
    """Run the suite as an INSTALLED instance: the A-2 gate admits everything.

    The suite exercises the capabilities of an installed instance; making every one
    of its ~2000 tests seed a full install first would test the fixture, not the
    behaviour. The gate's own tests (test_install_gate.py) point `admission_check`
    back at `_real_admission` and exercise the real path against explicit states.
    """
    from assistant_memory.install import gate as install_gate

    async def _open(_session):
        return None

    monkeypatch.setattr(install_gate, "admission_check", _open)
    # The completion boundary rides the same bypass: the suite runs as a FULLY
    # installed instance (stage 5 closed too); the guard's own tests point it back.
    monkeypatch.setattr(install_gate, "completion_check", _open)


@pytest.fixture(autouse=True)
def _neutral_space_routing(monkeypatch):
    """Start every test from an UNCONFIGURED deployment, whatever .env says.

    The two space ids route real writes and name spaces in the operator's installation,
    which the test database has no reason to contain. A test that cares configures what it
    needs; nothing here silently inherits a deployment's routing.
    """
    monkeypatch.setattr(settings, "conventions_space_id", None)
    monkeypatch.setattr(settings, "feedback_space_id", None)


@pytest.fixture
def feedback_destination(monkeypatch, space):
    """Point the server-side feedback destination at the test's own space.

    `feedback` takes no `space` argument by design — the destination is configuration, so
    a test configures it the same way a deployment does.
    """
    monkeypatch.setattr(settings, "feedback_space_id", space.id)
    return space


@pytest_asyncio.fixture
async def unseeded_deployment(session):
    """Make the deployment look like it has never been seeded, for this test only.

    The seeder's cross-space refusal and the startup seed condition are deployment-wide by
    design — they ask "does a projection exist ANYWHERE" — so a test of one is at the mercy
    of every conventions Document in the database. On a dedicated test database that is
    normally none; this fixture makes it none REGARDLESS, so the same test also passes
    against a database restored from production instead of being refused by data it did not
    put there. Soft-deleting inside the test transaction (rolled back at teardown, like
    every other write here) is what a fresh install looks like from those guards' side.

    B.13: the install-settings store joins the same rule. A lifespan-boot test COMMITS
    what the bootstrap now creates (the conventions home and its recorded id), so a
    later "fresh install" test would resolve a home this test never recorded; clearing
    the store in-transaction restores the never-installed baseline for this test only.
    """
    from sqlalchemy import delete as _delete

    from assistant_memory.models.install import InstallSetting

    await session.execute(_delete(InstallSetting))
    await session.execute(
        update(Node)
        .where(Node.type == "Document", Node.label == DOC_LABEL, Node.deleted_at.is_(None))
        .values(deleted_at=datetime.now(UTC))
    )
    await session.flush()
    return None


@pytest_asyncio.fixture
async def account(session):
    user = User(label="tester")
    session.add(user)
    await session.flush()
    acc = Account(user_id=user.id, label="personal")
    session.add(acc)
    await session.flush()
    return acc


@pytest_asyncio.fixture
async def space(session, account):
    sp = Space(name="personal", template="personal", created_by=account.id)
    session.add(sp)
    await session.flush()
    return sp


@pytest_asyncio.fixture
async def space2(session, account):
    sp = Space(name="household", template="shared", created_by=account.id)
    session.add(sp)
    await session.flush()
    return sp


# --- web integration harness --------------------------------------------


@pytest_asyncio.fixture
async def owner_client(session):
    """An httpx client driving the real app in-loop, authenticated as an owner.

    ``get_session`` is overridden to the test session (so route writes land in the
    rolled-back transaction) and ``require_user`` to a freshly-made owner User.
    CSRF still flows for real — read the token from a rendered page and post it.
    """
    import httpx

    from assistant_memory.db import get_session
    from assistant_memory.main import app
    from assistant_memory.web.routes import require_user

    user = User(label="owner", is_owner=True)
    session.add(user)
    await session.flush()
    session.add(Account(user_id=user.id, label="personal"))
    await session.flush()

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[require_user] = lambda: user
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, user
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(require_user, None)
