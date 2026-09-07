# SPDX-License-Identifier: Apache-2.0
from assistant_memory import policy
from assistant_memory.models.identity import Account, Membership, Space, User
from assistant_memory.policy import Decision, enforcement


async def _account(session, label="x"):
    user = User(label=label)
    session.add(user)
    await session.flush()
    acc = Account(user_id=user.id, label=label)
    session.add(acc)
    await session.flush()
    return acc


async def _space(session, owner, *, name="s", template="personal", write_floor=None):
    sp = Space(name=name, template=template, write_floor=write_floor, created_by=owner.id)
    session.add(sp)
    await session.flush()
    return sp


async def _member(session, account, space, permission="write"):
    session.add(Membership(account_id=account.id, space_id=space.id, permission=permission))
    await session.flush()


async def test_base_allow_for_normal(session):
    decision = await policy.evaluate(session, operation="create", effective_sensitivity="normal")
    assert decision == Decision(mode="allow", factor="base")


async def test_base_deny_for_critical(session):
    decision = await policy.evaluate(session, operation="create", effective_sensitivity="critical")
    assert decision.mode == "deny"
    assert decision.factor == "base"


async def test_critical_unshare_stays_allow(session):
    decision = await policy.evaluate(session, operation="unshare", effective_sensitivity="critical")
    assert decision.mode == "allow"


async def test_trust_floor_limited_confirms(session):
    decision = await policy.evaluate(
        session, operation="create", effective_sensitivity="normal", client_trust="limited"
    )
    assert decision == Decision(mode="confirm", factor="trust_floor")


async def test_trust_floor_untrusted_denies(session):
    decision = await policy.evaluate(
        session, operation="create", effective_sensitivity="normal", client_trust="untrusted"
    )
    assert decision.mode == "deny"


async def test_space_write_floor_confirms(session, account):
    space = await _space(session, account, write_floor="confirm")
    decision = await policy.evaluate(
        session, operation="create", effective_sensitivity="normal", target_space_id=space.id
    )
    assert decision == Decision(mode="confirm", factor="space_floor")


async def test_audience_solo_share_allows(session, account):
    space = await _space(session, account)
    await _member(session, account, space)  # single member -> solo
    decision = await policy.evaluate(
        session, operation="share", effective_sensitivity="high", target_space_id=space.id
    )
    assert decision.mode == "allow"


async def test_audience_multiperson_sensitive_share_confirms(session, account):
    space = await _space(session, account, template="shared")
    other = await _account(session, "wife")
    await _member(session, account, space)
    await _member(session, other, space)  # two members -> multi-person
    decision = await policy.evaluate(
        session, operation="share", effective_sensitivity="high", target_space_id=space.id
    )
    assert decision == Decision(mode="confirm", factor="share_audience")


async def test_audience_multiperson_nonsensitive_share_allows(session, account):
    space = await _space(session, account, template="shared")
    other = await _account(session, "wife")
    await _member(session, account, space)
    await _member(session, other, space)
    decision = await policy.evaluate(
        session, operation="share", effective_sensitivity="normal", target_space_id=space.id
    )
    assert decision.mode == "allow"


def test_enforcement_venue_mapping():
    assert enforcement(Decision("allow", "base"), "trusted") == "apply"
    assert enforcement(Decision("deny", "base"), "trusted") == "deny"
    assert enforcement(Decision("confirm", "share_audience"), "trusted") == "client_confirm"
    assert enforcement(Decision("confirm", "trust_floor"), "limited") == "server_proposal"
