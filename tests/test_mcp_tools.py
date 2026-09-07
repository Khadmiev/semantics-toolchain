# SPDX-License-Identifier: Apache-2.0
import uuid

import pytest
from sqlalchemy import select

from assistant_memory.auth import issue_credential, resolve_credential
from assistant_memory.conventions import FEEDBACK_HUB_LABEL
from assistant_memory.mcp import catalog, tools
from assistant_memory.mcp.errors import Conflict, InvalidArgument, NotFound, PermissionDenied
from assistant_memory.models.graph import Edge, Node
from assistant_memory.models.identity import Account, Membership, User
from assistant_memory.repository import graph

# --- fixtures / helpers --------------------------------------------------


async def _grant(session, account, space, permission="admin"):
    session.add(Membership(account_id=account.id, space_id=space.id, permission=permission))
    await session.flush()


async def _other_account(session, label="other"):
    user = User(label=label)
    session.add(user)
    await session.flush()
    acc = Account(user_id=user.id, label=label)
    session.add(acc)
    await session.flush()
    return acc


async def _principal(session, account, *, trust="trusted", scopes=None, allowed_tools=None):
    """A real credential (so proposer_client FK holds) resolved to a Principal."""
    issued = await issue_credential(
        session, account_id=account.id, trust=trust, scopes=scopes, allowed_tools=allowed_tools
    )
    return await resolve_credential(session, issued.token)


# --- read ----------------------------------------------------------------


async def test_get_visible_and_invisible(session, account, space):
    await _grant(session, account, space)
    node = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    principal = await _principal(session, account)

    got = await tools.get(session, principal, node_id=node.id)
    assert got["id"] == str(node.id)
    assert got["type"] == "Note"

    with pytest.raises(NotFound):
        await tools.get(session, principal, node_id=uuid.uuid4())


async def test_scope_narrows_visibility(session, account, space, space2):
    await _grant(session, account, space2)
    node = await graph.create_node(session, type="Note", space_id=space2.id, account_id=account.id)
    # credential scoped to `space` only -> space2 node invisible despite membership
    principal = await _principal(session, account, scopes=[str(space.id)])

    with pytest.raises(NotFound):
        await tools.get(session, principal, node_id=node.id)


async def test_traverse_and_explain(session, account, space):
    await _grant(session, account, space)
    a = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    b = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    await graph.link(
        session, type="relates_to", src_node=a.id, dst_node=b.id, account_id=account.id
    )
    principal = await _principal(session, account)

    walk = await tools.traverse(session, principal, start=a.id, depth=1)
    assert str(b.id) in {n["id"] for n in walk["nodes"]}
    assert len(walk["edges"]) == 1

    prov = await tools.explain(session, principal, node_id=a.id)
    assert len(prov["versions"]) == 1


async def test_conventions_returns_document_and_sections(session, account, space):
    await _grant(session, account, space)
    doc = await graph.create_node(
        session, type="Document", space_id=space.id, account_id=account.id,
        label=tools.CONVENTIONS_DOC_LABEL,
        properties={"text": "anchor", "path": "docs/x.md", "version": "v1 (2026-07-02)"},
    )
    for label, text in [("§1 Two stores", "graph vs files"), ("§2 Self-containment", "substance")]:
        child = await graph.create_node(
            session, type="Note", space_id=space.id, account_id=account.id,
            label=label, properties={"text": text},
        )
        await graph.link(
            session, type="contained_in", src_node=child.id, dst_node=doc.id,
            account_id=account.id,
        )
    principal = await _principal(session, account)

    res = await tools.conventions(session, principal)
    assert res["found"] is True
    assert res["version"] == "v1 (2026-07-02)"
    assert res["document"]["label"] == tools.CONVENTIONS_DOC_LABEL
    labels = {s["label"] for s in res["sections"]}
    assert labels == {"§1 Two stores", "§2 Self-containment"}
    assert all(s["text"] and s["status"] == "current" for s in res["sections"])


async def test_conventions_absent_returns_not_found(session, account, space):
    await _grant(session, account, space)
    principal = await _principal(session, account)
    res = await tools.conventions(session, principal)
    assert res["found"] is False


async def test_feedback_lands_provisional_in_hub(
    session, account, space, feedback_destination
):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    res = await tools.feedback(
        session, principal,
        text="§9 conflates decision-recording with the semantic-map clock",
        kind="ambiguity", section="§9",
    )
    assert res["status"] == "ok"
    node = res["node"]
    assert node["type"] == "Feedback"
    assert node["status"] == "provisional"
    assert node["properties"]["kind"] == "ambiguity"
    assert node["properties"]["section"] == "§9"
    assert node["properties"]["reporter_account"] == str(account.id)

    # It is contained_in the reserved feedback hub.
    parent = await session.scalar(
        select(Node)
        .join(Edge, Edge.dst_node == Node.id)
        .where(
            Edge.src_node == uuid.UUID(node["id"]),
            Edge.type == "contained_in",
            Edge.valid_to.is_(None),
        )
    )
    assert parent is not None and parent.label == FEEDBACK_HUB_LABEL


async def test_feedback_rejects_empty_text(session, account, space, feedback_destination):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    with pytest.raises(InvalidArgument):
        await tools.feedback(session, principal, text="   ")


async def test_feedback_rejects_bad_kind(session, account, space, feedback_destination):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    with pytest.raises(InvalidArgument):
        await tools.feedback(session, principal, text="x", kind="bogus")


# --- write ---------------------------------------------------------------


async def test_create_node_apply(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)

    res = await tools.create_node(
        session, principal, type="Note", space=space.id, label="hi", properties={"k": 1}
    )
    assert res["status"] == "ok"
    assert res["node"]["label"] == "hi"
    # round-trips through access
    assert await tools.get(session, principal, node_id=res["node"]["id"])


async def test_create_node_requires_write_permission(session, account, space):
    await _grant(session, account, space, "read")
    principal = await _principal(session, account)
    with pytest.raises(PermissionDenied):
        await tools.create_node(session, principal, type="Note", space=space.id)


async def test_create_node_out_of_scope(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account, scopes=[str(uuid.uuid4())])
    with pytest.raises(PermissionDenied):
        await tools.create_node(session, principal, type="Note", space=space.id)


async def test_create_node_limited_trust_stages_proposal(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account, trust="limited")

    res = await tools.create_node(session, principal, type="Note", space=space.id)
    assert res["status"] == "pending"
    assert res["factor"] == "trust_floor"
    assert uuid.UUID(res["proposal_id"])  # a Proposal row was staged


async def test_create_node_untrusted_denied(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account, trust="untrusted")
    with pytest.raises(PermissionDenied):
        await tools.create_node(session, principal, type="Note", space=space.id)


async def test_create_node_space_floor_confirm_roundtrip(session, account, space):
    space.write_floor = "confirm"
    await session.flush()
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)  # trusted

    first = await tools.create_node(session, principal, type="Note", space=space.id)
    assert first["status"] == "confirm_required"
    assert first["factor"] == "space_floor"

    confirmed = await tools.create_node(
        session, principal, type="Note", space=space.id, confirm=True
    )
    assert confirmed["status"] == "ok"


async def test_update_node_cas(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    created = await tools.create_node(session, principal, type="Note", space=space.id, label="v1")
    node_id = created["node"]["id"]
    version = created["node"]["current_version_id"]

    updated = await tools.update_node(
        session, principal, node_id=node_id, expected_version=version, patch={"x": 2}
    )
    assert updated["node"]["properties"]["x"] == 2

    with pytest.raises(Conflict):
        await tools.update_node(
            session, principal, node_id=node_id, expected_version=version, patch={"x": 3}
        )


async def test_create_node_status_default_and_set(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)

    default = await tools.create_node(session, principal, type="Note", space=space.id)
    assert default["node"]["status"] == "current"

    prov = await tools.create_node(
        session, principal, type="Hypothesis", space=space.id, status="provisional"
    )
    assert prov["node"]["status"] == "provisional"


async def test_create_node_rejects_bad_status(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    with pytest.raises(InvalidArgument):
        await tools.create_node(session, principal, type="Note", space=space.id, status="banana")


async def test_update_node_changes_status(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    created = await tools.create_node(
        session, principal, type="Hypothesis", space=space.id, status="provisional"
    )
    node_id = created["node"]["id"]

    updated = await tools.update_node(
        session, principal, node_id=node_id,
        expected_version=created["node"]["current_version_id"], status="rejected",
    )
    assert updated["node"]["status"] == "rejected"

    with pytest.raises(InvalidArgument):
        await tools.update_node(
            session, principal, node_id=node_id,
            expected_version=updated["node"]["current_version_id"], status="nope",
        )


async def test_delete_node(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    created = await tools.create_node(session, principal, type="Note", space=space.id)
    node_id, version = created["node"]["id"], created["node"]["current_version_id"]

    res = await tools.delete_node(
        session, principal, node_id=node_id, expected_version=version
    )
    assert res["status"] == "ok"
    with pytest.raises(NotFound):
        await tools.get(session, principal, node_id=node_id)


async def test_link_and_unlink(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    a = await tools.create_node(session, principal, type="Note", space=space.id)
    b = await tools.create_node(session, principal, type="Note", space=space.id)

    linked = await tools.link(
        session, principal, type="relates_to", src=a["node"]["id"], dst=b["node"]["id"]
    )
    assert linked["status"] == "ok"
    edge_id = linked["edge"]["id"]

    unlinked = await tools.unlink(session, principal, edge_id=edge_id)
    assert unlinked["removed"] is True


# --- semantic memory (remember_* / timeline) -----------------------------


async def test_remember_fact_creates_note(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)

    res = await tools.remember_fact(
        session, principal, text="Paris is the capital of France", space=space.id
    )
    assert res["status"] == "ok"
    assert res["node"]["type"] == "Note"
    assert res["node"]["properties"]["text"] == "Paris is the capital of France"
    # label auto-derived from the text
    assert res["node"]["label"] == "Paris is the capital of France"


async def test_remember_fact_dedupes(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)

    first = await tools.remember_fact(session, principal, text="I have a cat", space=space.id)
    second = await tools.remember_fact(session, principal, text="i have a CAT ", space=space.id)
    assert second["status"] == "exists"
    assert second["node"]["id"] == first["node"]["id"]


async def test_remember_preference_tags(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)

    res = await tools.remember_preference(
        session, principal, text="prefers dark mode", space=space.id, tags=["ui"]
    )
    assert res["status"] == "ok"
    assert res["node"]["type"] == "Note"
    labels = {t["label"] for t in res["tags"]}
    assert labels == {"preference", "ui"}

    # the tag is a real Tag node reachable via a tagged_with edge
    walk = await tools.traverse(
        session, principal, start=res["node"]["id"], edges=["tagged_with"], depth=1
    )
    tag_types = {n["type"] for n in walk["nodes"] if n["id"] != res["node"]["id"]}
    assert tag_types == {"Tag"}


async def test_remember_preference_reuses_tag_node(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)

    a = await tools.remember_preference(session, principal, text="likes tea", space=space.id)
    b = await tools.remember_preference(session, principal, text="likes coffee", space=space.id)
    tag_a = next(t["id"] for t in a["tags"] if t["label"] == "preference")
    tag_b = next(t["id"] for t in b["tags"] if t["label"] == "preference")
    assert tag_a == tag_b  # same "preference" Tag node reused


async def test_remember_decision_type_and_policy(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)

    res = await tools.remember_decision(
        session, principal, text="Ship on Friday", space=space.id
    )
    assert res["status"] == "ok"
    assert res["node"]["type"] == "Decision"
    assert res["node"]["sensitivity"] is None  # follows the type default (high)


async def test_remember_respects_write_policy(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account, trust="limited")

    res = await tools.remember_fact(session, principal, text="staged", space=space.id)
    assert res["status"] == "pending"
    # nothing was created, so no tag/node leaked
    found = await tools.search(session, principal, query="staged")
    assert found["results"] == []


async def test_remember_requires_write_permission(session, account, space):
    await _grant(session, account, space, "read")
    principal = await _principal(session, account)
    with pytest.raises(PermissionDenied):
        await tools.remember_fact(session, principal, text="x", space=space.id)


async def test_remember_rejects_empty_text(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    with pytest.raises(InvalidArgument):
        await tools.remember_fact(session, principal, text="   ", space=space.id)


async def test_list_spaces(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    res = await tools.list_spaces(session, principal)
    by_id = {s["id"]: s for s in res["spaces"]}
    assert str(space.id) in by_id
    assert by_id[str(space.id)]["writable"] is True
    assert by_id[str(space.id)]["permission"] == "write"


async def test_list_spaces_readonly_flag(session, account, space):
    await _grant(session, account, space, "read")
    principal = await _principal(session, account)
    res = await tools.list_spaces(session, principal)
    s = next(x for x in res["spaces"] if x["id"] == str(space.id))
    assert s["writable"] is False


async def test_remember_defaults_to_sole_writable_space(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    res = await tools.remember_fact(session, principal, text="no space given")
    assert res["status"] == "ok"
    assert res["node"]["origin_space"] == str(space.id)


async def test_create_node_defaults_to_sole_writable_space(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    res = await tools.create_node(session, principal, type="Note", label="x")
    assert res["status"] == "ok"
    assert res["node"]["origin_space"] == str(space.id)


async def test_remember_ambiguous_space_requires_explicit(session, account, space, space2):
    await _grant(session, account, space, "write")
    await _grant(session, account, space2, "write")
    principal = await _principal(session, account)
    with pytest.raises(InvalidArgument):
        await tools.remember_fact(session, principal, text="ambiguous")


async def test_remember_no_writable_space(session, account, space):
    await _grant(session, account, space, "read")
    principal = await _principal(session, account)
    with pytest.raises(PermissionDenied):
        await tools.remember_fact(session, principal, text="x")


async def test_timeline_chronology_and_filters(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    a = await tools.remember_fact(session, principal, text="first", space=space.id)
    b = await tools.remember_fact(session, principal, text="second", space=space.id)
    # a second version on b
    await tools.update_node(
        session, principal, node_id=b["node"]["id"],
        expected_version=b["node"]["current_version_id"], patch={"note": "edit"},
    )

    full = await tools.timeline(session, principal)
    assert len(full["events"]) == 3  # a v1, b v1, b v2
    assert full["events"][0]["created_at"] >= full["events"][-1]["created_at"]

    scoped = await tools.timeline(session, principal, entity_id=a["node"]["id"])
    assert {e["node_id"] for e in scoped["events"]} == {a["node"]["id"]}
    assert scoped["events"][0]["is_current"] is True


async def test_timeline_access_filtered(session, account, space, space2):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    await tools.remember_fact(session, principal, text="mine", space=space.id)
    # a node in space2 the principal is not a member of
    await graph.create_node(session, type="Note", space_id=space2.id, account_id=account.id)

    events = await tools.timeline(session, principal)
    assert len(events["events"]) == 1
    assert events["events"][0]["label"] == "mine"

    with pytest.raises(NotFound):
        await tools.timeline(session, principal, entity_id=uuid.uuid4())


# --- ontology (type registry + retype) -----------------------------------


async def test_list_node_and_edge_types(session, account):
    principal = await _principal(session, account)
    nt = await tools.list_node_types(session, principal)
    assert "Note" in {t["type"] for t in nt["node_types"]}
    et = await tools.list_edge_types(session, principal)
    assert "relates_to" in {t["type"] for t in et["edge_types"]}


async def test_create_node_type_and_use(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    res = await tools.create_node_type(
        session, principal, type="Meeting", default_sensitivity="normal", description="a meeting"
    )
    assert res["status"] == "ok"
    # idempotent
    again = await tools.create_node_type(session, principal, type="Meeting")
    assert again["status"] == "exists"
    # the new type is usable by create_node
    node = await tools.create_node(session, principal, type="Meeting", space=space.id)
    assert node["node"]["type"] == "Meeting"


async def test_create_node_type_requires_trusted(session, account):
    principal = await _principal(session, account, trust="limited")
    with pytest.raises(PermissionDenied):
        await tools.create_node_type(session, principal, type="Nope")


async def test_create_node_type_bad_sensitivity(session, account):
    principal = await _principal(session, account)
    with pytest.raises(InvalidArgument):
        await tools.create_node_type(session, principal, type="Weird", default_sensitivity="secret")


async def test_create_edge_type_and_use(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    et = await tools.create_edge_type(session, principal, type="attended", category="associative")
    assert et["status"] == "ok"
    a = await tools.create_node(session, principal, type="Note", space=space.id)
    b = await tools.create_node(session, principal, type="Note", space=space.id)
    linked = await tools.link(
        session, principal, type="attended", src=a["node"]["id"], dst=b["node"]["id"]
    )
    assert linked["status"] == "ok"


async def test_create_edge_type_bad_category(session, account):
    principal = await _principal(session, account)
    with pytest.raises(InvalidArgument):
        await tools.create_edge_type(session, principal, type="weird", category="nonsense")


async def test_retype_node(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    created = await tools.create_node(session, principal, type="Note", space=space.id, label="x")
    nid = created["node"]["id"]

    res = await tools.retype_node(session, principal, node_id=nid, new_type="Idea")
    assert res["status"] == "ok"
    assert res["old_type"] == "Note" and res["new_type"] == "Idea"
    got = await tools.get(session, principal, node_id=nid)
    assert got["type"] == "Idea"


async def test_retype_node_unknown_type(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    created = await tools.create_node(session, principal, type="Note", space=space.id)
    with pytest.raises(InvalidArgument):
        await tools.retype_node(session, principal, node_id=created["node"]["id"], new_type="Bogus")


async def test_retype_edge(session, account, space):
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    a = await tools.create_node(session, principal, type="Note", space=space.id)
    b = await tools.create_node(session, principal, type="Note", space=space.id)
    edge = await tools.link(
        session, principal, type="relates_to", src=a["node"]["id"], dst=b["node"]["id"]
    )
    res = await tools.retype_edge(
        session, principal, edge_id=edge["edge"]["id"], new_type="references"
    )
    assert res["status"] == "ok"
    assert res["old_type"] == "relates_to" and res["new_type"] == "references"


# --- share ---------------------------------------------------------------


async def test_share_nodes(session, account, space, space2):
    await _grant(session, account, space, "write")
    await _grant(session, account, space2, "write")
    principal = await _principal(session, account)
    created = await tools.create_node(session, principal, type="Note", space=space.id)

    res = await tools.share_nodes(
        session, principal, target_space=space2.id, node_ids=[created["node"]["id"]]
    )
    assert res["results"][0]["status"] == "ok"
    assert res["results"][0]["added"] is True


async def test_sensitive_multiperson_share_needs_confirm(session, account, space, space2):
    # space2 has two member accounts -> multi-person
    other = await _other_account(session)
    await _grant(session, account, space, "write")
    await _grant(session, account, space2, "write")
    await _grant(session, other, space2, "read")
    principal = await _principal(session, account)
    # Project default sensitivity is high
    created = await tools.create_node(session, principal, type="Project", space=space.id)

    res = await tools.share_nodes(
        session, principal, target_space=space2.id, node_ids=[created["node"]["id"]]
    )
    row = res["results"][0]
    assert row["status"] == "confirm_required"
    assert row["factor"] == "share_audience"

    ok = await tools.share_nodes(
        session, principal, target_space=space2.id, node_ids=[created["node"]["id"]], confirm=True
    )
    assert ok["results"][0]["status"] == "ok"


async def test_share_edge_requires_endpoints_in_space(session, account, space, space2):
    await _grant(session, account, space, "write")
    await _grant(session, account, space2, "write")
    principal = await _principal(session, account)
    a = await tools.create_node(session, principal, type="Note", space=space.id)
    b = await tools.create_node(session, principal, type="Note", space=space.id)
    edge = await tools.link(
        session, principal, type="relates_to", src=a["node"]["id"], dst=b["node"]["id"]
    )

    # endpoints not in space2 yet
    with pytest.raises(PermissionDenied):
        await tools.share_edge(
            session, principal, target_space=space2.id, edge_id=edge["edge"]["id"]
        )

    await tools.share_nodes(
        session, principal, target_space=space2.id, node_ids=[a["node"]["id"], b["node"]["id"]]
    )
    res = await tools.share_edge(
        session, principal, target_space=space2.id, edge_id=edge["edge"]["id"]
    )
    assert res["status"] == "ok"


async def test_unshare(session, account, space, space2):
    await _grant(session, account, space, "write")
    await _grant(session, account, space2, "write")
    principal = await _principal(session, account)
    created = await tools.create_node(session, principal, type="Note", space=space.id)
    await tools.share_nodes(
        session, principal, target_space=space2.id, node_ids=[created["node"]["id"]]
    )

    res = await tools.unshare(session, principal, node_id=created["node"]["id"], space=space2.id)
    assert res["removed"] is True


# --- catalog / allowed_tools gate ---------------------------------------


async def test_allowed_tools_gate(session, account):
    full = await _principal(session, account)
    assert {s.name for s in catalog.visible_tools(full)} == set(catalog.BY_NAME)

    limited = await _principal(session, account, allowed_tools=["get", "search"])
    assert {s.name for s in catalog.visible_tools(limited)} == {"get", "search"}
    assert catalog.is_allowed(limited, "create_node") is False
    assert catalog.is_allowed(limited, "get") is True
