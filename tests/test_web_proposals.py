# SPDX-License-Identifier: Apache-2.0
import re

from sqlalchemy import select

from assistant_memory.auth import issue_credential
from assistant_memory.models.graph import Node
from assistant_memory.models.identity import Account, Membership, Space
from assistant_memory.models.policy import Proposal


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no CSRF token in page"
    return match.group(1)


async def _staged_create_node(session, owner, *, label="staged"):
    acc = await session.scalar(select(Account).where(Account.user_id == owner.id))
    sp = Space(name="propspace", template="personal", created_by=acc.id)
    session.add(sp)
    await session.flush()
    session.add(Membership(account_id=acc.id, space_id=sp.id, permission="admin"))
    await session.flush()
    issued = await issue_credential(session, account_id=acc.id, trust="limited")
    proposal = Proposal(
        proposer_client=issued.credential.id,
        op="create_node",
        target_ref={"space": str(sp.id), "type": "Note"},
        payload={"label": label, "properties": {}, "sensitivity": None},
        sensitivity="normal",
        mode="confirm",
    )
    session.add(proposal)
    await session.flush()
    return proposal


async def test_proposals_list(owner_client, session):
    client, owner = owner_client
    await _staged_create_node(session, owner)
    page = await client.get("/proposals")
    assert page.status_code == 200
    assert "create_node" in page.text


async def test_approve_applies_and_marks_approved(owner_client, session):
    client, owner = owner_client
    proposal = await _staged_create_node(session, owner, label="from-proposal")

    csrf = _csrf_from((await client.get("/proposals")).text)
    resp = await client.post(
        f"/proposals/{proposal.id}/approve", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    node = await session.scalar(select(Node).where(Node.label == "from-proposal"))
    assert node is not None  # mutation applied
    await session.refresh(proposal)
    assert proposal.status == "approved"
    assert proposal.decided_at is not None


async def test_reject_marks_rejected_without_applying(owner_client, session):
    client, owner = owner_client
    proposal = await _staged_create_node(session, owner, label="never-applied")

    csrf = _csrf_from((await client.get("/proposals")).text)
    resp = await client.post(
        f"/proposals/{proposal.id}/reject", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    assert await session.scalar(select(Node).where(Node.label == "never-applied")) is None
    await session.refresh(proposal)
    assert proposal.status == "rejected"


async def test_cannot_decide_twice(owner_client, session):
    client, owner = owner_client
    proposal = await _staged_create_node(session, owner)
    csrf = _csrf_from((await client.get("/proposals")).text)
    await client.post(f"/proposals/{proposal.id}/reject", data={"csrf": csrf})

    resp = await client.post(
        f"/proposals/{proposal.id}/approve", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 409
