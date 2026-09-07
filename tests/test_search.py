# SPDX-License-Identifier: Apache-2.0
import uuid

from assistant_memory.auth import issue_credential, resolve_credential
from assistant_memory.mcp import tools
from assistant_memory.models.identity import Membership


async def _grant(session, account, space, permission="write"):
    session.add(Membership(account_id=account.id, space_id=space.id, permission=permission))
    await session.flush()


async def _principal(session, account, *, scopes=None):
    issued = await issue_credential(session, account_id=account.id, scopes=scopes)
    return await resolve_credential(session, issued.token)


async def _ids(result):
    return [r["id"] for r in result["results"]]


async def test_search_ranks_token_overlap_first(session, account, space):
    await _grant(session, account, space)
    principal = await _principal(session, account)
    a = await tools.create_node(
        session, principal, type="Note", space=space.id, label="grocery shopping list"
    )
    await tools.create_node(
        session, principal, type="Note", space=space.id, label="vacation travel plan"
    )

    res = await tools.search(session, principal, query="grocery shopping")
    assert (await _ids(res))[0] == a["node"]["id"]


async def test_search_finds_fulltext_by_word(session, account, space):
    await _grant(session, account, space)
    principal = await _principal(session, account)
    node = await tools.create_node(
        session, principal, type="Note", space=space.id, label="weekly shopping list"
    )
    await tools.create_node(
        session, principal, type="Note", space=space.id, label="holiday plans"
    )

    res = await tools.search(session, principal, query="shopping")
    assert node["node"]["id"] in await _ids(res)


async def test_search_is_access_filtered(session, account, space, space2):
    # node lives only in space2; credential scoped to space -> invisible
    await _grant(session, account, space)
    await _grant(session, account, space2)
    principal_full = await _principal(session, account)
    node = await tools.create_node(
        session, principal_full, type="Note", space=space2.id, label="secret recipe"
    )

    scoped = await _principal(session, account, scopes=[str(space.id)])
    res = await tools.search(session, scoped, query="secret recipe")
    assert node["node"]["id"] not in await _ids(res)
    # but visible to the unscoped credential
    res_full = await tools.search(session, principal_full, query="secret recipe")
    assert node["node"]["id"] in await _ids(res_full)


async def test_search_excludes_deleted(session, account, space):
    await _grant(session, account, space)
    principal = await _principal(session, account)
    created = await tools.create_node(
        session, principal, type="Note", space=space.id, label="ephemeral note"
    )
    await tools.delete_node(
        session,
        principal,
        node_id=created["node"]["id"],
        expected_version=created["node"]["current_version_id"],
    )

    res = await tools.search(session, principal, query="ephemeral")
    assert await _ids(res) == []


async def test_search_only_current_version(session, account, space):
    await _grant(session, account, space)
    principal = await _principal(session, account)
    created = await tools.create_node(
        session, principal, type="Note", space=space.id, label="alpha"
    )
    keeper = await tools.create_node(
        session, principal, type="Note", space=space.id, label="alpha keeper"
    )
    await tools.update_node(
        session,
        principal,
        node_id=created["node"]["id"],
        expected_version=created["node"]["current_version_id"],
        label="omega",
    )

    # reindex took effect: the new text is found
    omega_hits = await _ids(await tools.search(session, principal, query="omega"))
    assert created["node"]["id"] in omega_hits
    # the superseded "alpha" text no longer matches FTS, so the genuine "alpha"
    # node outranks the now-"omega" node (which only lingers as a weak vector NN)
    alpha_hits = await _ids(await tools.search(session, principal, query="alpha"))
    assert alpha_hits[0] == keeper["node"]["id"]


async def test_search_empty_query_returns_recent(session, account, space):
    await _grant(session, account, space)
    principal = await _principal(session, account)
    await tools.create_node(session, principal, type="Note", space=space.id, label="one")
    await tools.create_node(session, principal, type="Note", space=space.id, label="two")

    res = await tools.search(session, principal, query=None)
    assert len(res["results"]) == 2


async def test_search_filter_by_type(session, account, space):
    await _grant(session, account, space)
    principal = await _principal(session, account)
    note = await tools.create_node(
        session, principal, type="Note", space=space.id, label="quarterly review"
    )
    await tools.create_node(
        session, principal, type="Project", space=space.id, label="quarterly review"
    )

    res = await tools.search(
        session, principal, query="quarterly review", filters={"type": "Note"}
    )
    ids = await _ids(res)
    assert note["node"]["id"] in ids
    assert all(r["type"] == "Note" for r in res["results"])


async def test_search_filter_by_status(session, account, space):
    await _grant(session, account, space)
    principal = await _principal(session, account)
    current = await tools.create_node(
        session, principal, type="Note", space=space.id, label="latency budget"
    )
    prov = await tools.create_node(
        session, principal, type="Note", space=space.id, label="latency budget",
        status="provisional",
    )

    res = await tools.search(
        session, principal, query="latency budget", filters={"status": "provisional"}
    )
    ids = await _ids(res)
    assert prov["node"]["id"] in ids
    assert current["node"]["id"] not in ids
    assert all(r["status"] == "provisional" for r in res["results"])

    # a list of statuses matches any of them
    both = await tools.search(
        session, principal, query="latency budget",
        filters={"status": ["current", "provisional"]},
    )
    both_ids = await _ids(both)
    assert current["node"]["id"] in both_ids
    assert prov["node"]["id"] in both_ids


async def test_search_no_membership_is_empty(session, account, space):
    # principal with no membership sees nothing
    principal = await _principal(session, account)
    assert (await tools.search(session, principal, query="anything"))["results"] == []
    assert uuid.UUID(principal.credential_id.hex)  # sanity: real credential
