# SPDX-License-Identifier: Apache-2.0
"""The genre's convergence conditions, checked where convergence is DECLARED.

The failure this closes: the server validates its own conditions and knows nothing of a
genre's; development can compute the genre's and is the side least interested in enforcing
them. So a review could converge with the blind reading missing, the claim map full of
unconfirmed rows and the operator's declarations never made — every layer reporting itself
satisfied. A rule only the interested party can check is not enforced.

And the second half, found by this code's own review: a gate that only asked "does a blind
record EXIST" trusted the poster for everything the record itself carries. The channel
publishes records whole — outcome, canary verdict, launch numbers, fingerprints — so the
gate re-runs every creditability condition the wire can carry, and these tests feed it
hand-posted phases that a mere existence check would have waved through.
"""

import hashlib
from datetime import UTC, datetime, timedelta

from assistant_memory.audience import channel, gate
from assistant_memory.audience.claim_map import Attestation, ClaimRow, ClaimStatus, Party
from assistant_memory.audience.records import RunKind, RunOutcome, RunRecord
from assistant_memory.review import watcher

T0 = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
SEQ = 40
DIGEST = "R" * 16
ANSWER = "Нет, в этом диалоге сохранённых сведений о прежних задачах нет."
ANSWER_SHA = hashlib.sha256(ANSWER.encode("utf-8")).hexdigest()


def _canary(**over) -> RunRecord:
    base = dict(
        id="canary-1", kind=RunKind.CANARY, iteration=2, artifact_seq=SEQ,
        profile_digest="P", launch_number=7, started_at=T0,
        finished_at=T0 + timedelta(minutes=2), transcript_path="runs/canary-1.log",
        transcript_sha256="hc", tool_calls=(), model="gpt-x", model_version="2026-08",
        spend=1, outcome=RunOutcome.HAPPENED, runner_version="0.145.0",
        sandbox_mode="read-only", approval_mode="never", canary_verdict_clean=True,
        markers_message_seq=1, answer_message_seq=2, answer_sha256=ANSWER_SHA,
    )
    return RunRecord(**{**base, **over})


def _record(**over) -> RunRecord:
    base = dict(
        id="blind-1", kind=RunKind.BLIND, iteration=2, artifact_seq=SEQ, profile_digest="P",
        launch_number=8, started_at=T0 + timedelta(minutes=3),
        finished_at=T0 + timedelta(minutes=8),
        transcript_path="runs/blind-1.log", transcript_sha256="h", tool_calls=(),
        model="gpt-x", model_version="2026-08", spend=1, outcome=RunOutcome.HAPPENED,
        runner_version="0.145.0", sandbox_mode="read-only", approval_mode="never",
        canary_record_id="canary-1", prompt_digest="pr", reader_digest=DIGEST,
        contract_version=1, contract_sha256="c1", category_set_version="cs1",
        report_schema_version="ss1", section_budgets='{"пол": 250}',
        render_path="runs/blind-1_report.md", render_sha256="r1",
    )
    return RunRecord(**{**base, **over})


def _confirmed_row() -> ClaimRow:
    row = ClaimRow(id="M-1", section="S-1", quote="Выросло вдвое")
    return ClaimRow(
        **{
            **row.__dict__,
            "recorded_status": ClaimStatus.CONFIRMED,
            "status_attestation": Attestation(
                by=Party.INFORMED, basis="узел графа 1afaf640", triple=row.triple,
                # B.8 F-2: a confirmation carries typed evidence of reading
                evidence={"ground": "graph", "read_ref": "r1",
                          "node_id": "1afaf640", "version": "v3"},
            ),
        }
    )


def _findings(**over) -> dict:
    kwargs = dict(
        findings=[], run_record_id="blind-1", reader_digest=DIGEST, iteration=2,
        contract_public_items=["C-2", "C-4"],
    )
    kwargs.update(over)
    return channel.blind_findings_message(SEQ, **kwargs)


def _takeaway(**over) -> dict:
    kwargs = dict(
        iteration=2, retelling_matches=True, goal_moved=False,
        reader_retelling="Понял так-то.", intended_takeaway="Главное — механизм работает.",
        declared_at="2026-08-05",
    )
    kwargs.update(over)
    seq = kwargs.pop("artifact_seq", SEQ)
    return channel.intent_takeaway_message(seq, **kwargs)


def _complete(**over):
    """The channel of a lawful round: markers, the answer, the pair, the assessment, the
    map, the words.

    Overridable per slot; ``None`` drops the slot. The message times are the channel's own:
    the marker list precedes the canary's start — the ordering the gate must see.
    """
    slots: dict[str, tuple[dict | None, str, datetime]] = {
        "markers": (
            channel.canary_markers_message(SEQ, ["маркер"]),
            "development", T0 - timedelta(minutes=1),
        ),
        "answer": (
            channel.canary_answer_message(SEQ, ANSWER),
            "development", T0 + timedelta(minutes=2),
        ),
        "canary": (
            channel.run_record_message(_canary()), "development", T0 + timedelta(minutes=2)
        ),
        "record": (
            channel.run_record_message(_record()), "development", T0 + timedelta(minutes=8)
        ),
        "findings": (_findings(), "development", T0 + timedelta(minutes=9)),
        "claims": (
            channel.claim_map_message([_confirmed_row()], iteration=2, artifact_seq=SEQ),
            "development", T0 + timedelta(minutes=10),
        ),
        "takeaway": (_takeaway(), "operator", T0 + timedelta(minutes=11)),
    }
    for name, value in over.items():
        if value is None:
            slots.pop(name)
        else:
            old = slots[name]
            slots[name] = (value, old[1], old[2]) if isinstance(value, dict) else value
    return [
        {
            "seq": i, "role": role, "kind": "notice", "payload": payload,
            "created_at": moment.isoformat(),
        }
        for i, (payload, role, moment) in enumerate(
            (v for v in slots.values() if v[0] is not None), start=1
        )
    ]


#: The default roster for these tests: both v2 roles declared OFF — their conditions are
#: absent, and the declaration is explicit, exactly as the gate demands.
ROLES_OFF = {"strategic_reader": False, "machine_comb": False}


def _refusals(messages, dispositions=(), roles=ROLES_OFF) -> list[str]:
    created_at = {m["seq"]: datetime.fromisoformat(m["created_at"]) for m in messages}
    return gate.convergence_refusals(
        channel.read_phases(messages), artifact_seq=SEQ, created_at=created_at,
        dispositions=dispositions, roles=roles,
    )


# --- the conditions ---------------------------------------------------------------------


def test_a_complete_channel_has_no_refusals():
    assert _refusals(_complete()) == []


def test_a_missing_blind_assessment_blocks():
    assert any("нет слепой оценки" in r for r in _refusals(_complete(findings=None)))


def test_findings_naming_a_record_the_channel_does_not_hold_block():
    """A reference to evidence nobody published is a reference to nothing."""
    messages = _complete(findings=_findings(run_record_id="призрак"))
    assert any("которой нет в канале" in r for r in _refusals(messages))


def _older_pair(source_findings=(), with_source_phase=True, **findings_over):
    """A channel whose pair (answer, canary, reading) belongs to the PREVIOUS version.

    A lawful carry needs the SOURCE reading's own findings phase on the channel — that is
    what the carried list is checked against — so one rides along by default, with seq 0 so
    the fixture's seq references stay true.
    """
    slots = dict(
        answer=channel.canary_answer_message(SEQ - 1, ANSWER),
        canary=channel.run_record_message(_canary(artifact_seq=SEQ - 1)),
        record=channel.run_record_message(_record(artifact_seq=SEQ - 1)),
    )
    if findings_over:
        slots["findings"] = _findings(**findings_over)
    messages = _complete(**slots)
    if with_source_phase:
        source = channel.blind_findings_message(
            SEQ - 1, findings=list(source_findings), run_record_id="blind-1",
            reader_digest=DIGEST, iteration=2, contract_public_items=["C-2", "C-4"],
        )
        messages.insert(0, {
            "seq": 0, "role": "development", "kind": "notice",
            "payload": source, "created_at": (T0 - timedelta(hours=1)).isoformat(),
        })
    return messages


def test_a_reading_of_an_older_version_without_the_carry_mark_blocks():
    """Not "an acceptable old one" — a missing current one. The distinction is the whole
    difference between a saved run and a false one."""
    assert any("перенос не объявлен" in r for r in _refusals(_older_pair()))


def test_a_declared_carry_makes_an_older_reading_legitimate():
    messages = _older_pair(
        iteration=3, carried_from_artifact_seq=SEQ - 1, carried_from_iteration=2
    )
    # the map and the declarations must speak for the same round as the carried assessment
    for m in messages:
        if m["payload"].get("phase") in (
            channel.CLAIM_MAP_PHASE, channel.INTENT_TAKEAWAY_PHASE
        ):
            m["payload"]["iteration"] = 3
    assert _refusals(messages) == []


# --- the pair is re-checked from the published records, not believed ----------------------


def test_an_annulled_reading_blocks_even_though_its_record_exists():
    """Existence was the old bar, and it is exactly what a hand-posted phase clears."""
    messages = _complete(record=channel.run_record_message(_record(
        outcome=RunOutcome.ANNULLED, outcome_reason="вызовы в стенограмме",
    )))
    assert any("аннулированное чтение не оценка" in r for r in _refusals(messages))


def test_a_dirty_canary_blocks():
    messages = _complete(canary=channel.run_record_message(_canary(
        canary_verdict_clean=False, outcome_reason="задела маркеры",
    )))
    assert any("вердикт канарейки не чист" in r for r in _refusals(messages))


def test_a_missing_canary_record_blocks():
    messages = _complete(canary=None)
    assert any("пара без канарейки не пара" in r for r in _refusals(messages))


def test_non_adjacent_launch_numbers_block():
    """A gap in the counter is an unrecorded run between the measurement and the reading."""
    messages = _complete(record=channel.run_record_message(_record(launch_number=9)))
    assert any("номера запусков не соседние" in r for r in _refusals(messages))


def test_a_rival_reading_on_the_same_canary_blocks():
    rival = channel.run_record_message(_record(id="blind-2", launch_number=8))
    messages = _complete()
    messages.append({
        "seq": len(messages) + 1, "role": "development", "kind": "notice",
        "payload": rival, "created_at": (T0 + timedelta(minutes=12)).isoformat(),
    })
    assert any("зачлась бы двум чтениям разом" in r for r in _refusals(messages))


def test_markers_published_after_the_canary_started_block():
    """A criterion chosen once the answer is known proves nothing about the answer."""
    messages = _complete()
    messages[0]["created_at"] = (T0 + timedelta(minutes=1)).isoformat()
    assert any("не строго раньше старта канарейки" in r for r in _refusals(messages))


def test_markers_published_at_the_very_start_instant_block_too():
    """The critic's counterexample: a TIE. At equal timestamps the order of the two events
    is unproven, and an unproven order is what the condition exists to refuse."""
    messages = _complete()
    messages[0]["created_at"] = T0.isoformat()
    assert any("не строго раньше старта канарейки" in r for r in _refusals(messages))


def test_diverging_instruments_between_canary_and_reading_block():
    messages = _complete(record=channel.run_record_message(_record(model_version="2026-09")))
    assert any("версии модели" in r for r in _refusals(messages))


def test_a_fresh_phase_whose_digest_disagrees_with_its_record_blocks():
    messages = _complete(findings=_findings(reader_digest="X" * 16))
    assert any("не совпадает с отпечатком записи" in r for r in _refusals(messages))


def test_a_carry_of_a_moved_fingerprint_blocks():
    """An unmoved fingerprint IS the definition of a lawful carry."""
    messages = _complete(
        answer=channel.canary_answer_message(SEQ - 1, ANSWER),
        canary=channel.run_record_message(_canary(artifact_seq=SEQ - 1)),
        record=channel.run_record_message(
            _record(artifact_seq=SEQ - 1, reader_digest="OLD" * 6)
        ),
        findings=_findings(
            iteration=3, carried_from_artifact_seq=SEQ - 1, carried_from_iteration=2
        ),
    )
    assert any("не совпадает с отпечатком записи" in r for r in _refusals(messages))


# --- the carry is bound to the cycle on both ends ----------------------------------------


def test_a_half_declared_carry_blocks():
    """A source version without a source round (or the reverse) is a carry whose link
    cannot be checked — and an uncheckable link is exactly what a carry must not be."""
    messages = _older_pair(iteration=3, carried_from_artifact_seq=SEQ - 1)
    assert any("перенос объявлен наполовину" in r for r in _refusals(messages))


def test_a_carry_naming_a_round_its_record_was_not_read_on_blocks():
    """The verifiable counterexample from the critic: source record iteration=2, phase
    declares carried_from_iteration=1 — the carry contradicts its own evidence."""
    messages = _older_pair(
        iteration=3, carried_from_artifact_seq=SEQ - 1, carried_from_iteration=1
    )
    assert any("противоречит своей улике" in r for r in _refusals(messages))


def test_a_carry_not_forward_in_rounds_blocks():
    messages = _older_pair(
        iteration=2, carried_from_artifact_seq=SEQ - 1, carried_from_iteration=2
    )
    assert any("только из прошлого" in r for r in _refusals(messages))


def test_a_fresh_reading_declared_for_another_round_blocks():
    messages = _complete(findings=_findings(iteration=3))
    reasons = _refusals(messages)
    assert any("прогона в этом круге не было" in r for r in reasons)


def test_a_map_and_an_assessment_of_different_rounds_block():
    messages = _complete()
    for m in messages:
        if m["payload"].get("phase") == channel.CLAIM_MAP_PHASE:
            m["payload"]["iteration"] = 5
    assert any("фаз одного круга" in r for r in _refusals(messages))


# --- a record's id is an identity, not a key to overwrite --------------------------------


def test_a_second_differing_record_under_one_id_is_refused_not_last_wins():
    """The critic's counterexample: an early annulled record buried by a later clean twin.
    An immutable record cannot differ from itself — the contradiction is refused."""
    messages = _complete()
    early_dirty = channel.run_record_message(
        _canary(canary_verdict_clean=False, outcome_reason="задела маркеры")
    )
    messages.insert(2, {
        "seq": 0, "role": "development", "kind": "notice",
        "payload": early_dirty, "created_at": (T0 + timedelta(minutes=1)).isoformat(),
    })
    for i, m in enumerate(messages, start=1):
        m["seq"] = i
    assert any("опубликована дважды с разным содержимым" in r for r in _refusals(messages))


def test_an_identical_re_post_of_a_record_is_an_honest_retry():
    messages = _complete()
    twin = channel.run_record_message(_canary())
    messages.append({
        "seq": len(messages) + 1, "role": "development", "kind": "notice",
        "payload": twin, "created_at": (T0 + timedelta(minutes=12)).isoformat(),
    })
    assert _refusals(messages) == []


def test_duplicate_claim_row_ids_in_one_publication_are_refused():
    """Two internally-valid rows under one id would let a terminal twin stand beside the
    row it contradicts."""
    claims = channel.claim_map_message(
        [_confirmed_row(), _confirmed_row()], iteration=2, artifact_seq=SEQ
    )
    messages = _complete(claims=claims)
    assert any("выданы дважды в одной публикации" in r for r in _refusals(messages))


# --- the canary's verdict is bound to a published answer ---------------------------------


def test_a_missing_canary_answer_phase_blocks():
    """A hash of an unpublished text binds nothing: the record would be the only witness
    to its own answer."""
    messages = _complete(answer=None)
    assert any("фазы ответа канарейки" in r for r in _refusals(messages))


def test_a_published_answer_whose_hash_disagrees_with_the_record_blocks():
    messages = _complete(answer=channel.canary_answer_message(SEQ, ANSWER + " Ещё слово."))
    assert any("относится к другому ответу" in r for r in _refusals(messages))


def test_a_clean_verdict_over_an_answer_that_hits_a_marker_blocks():
    """The machine half of the verdict is re-run over the published body: a "clean" that
    does not survive the published markers is refused."""
    dirty = "Помню задачу про маркер и её детали."
    messages = _complete(
        answer=channel.canary_answer_message(SEQ, dirty),
        canary=channel.run_record_message(
            _canary(answer_sha256=hashlib.sha256(dirty.encode("utf-8")).hexdigest())
        ),
    )
    assert any("вердикт записан чистым" in r for r in _refusals(messages))


# --- the claim map's published rows are re-checked --------------------------------------


def test_a_non_terminal_claim_row_blocks_and_is_named():
    messages = _complete(
        claims=channel.claim_map_message(
            [ClaimRow(id="M-2", section="S-1", quote="Ещё утверждение")],
            iteration=2, artifact_seq=SEQ,
        )
    )
    reasons = _refusals(messages)
    assert any("M-2" in r and "не в терминальном статусе" in r for r in reasons)


def test_a_missing_claim_map_blocks():
    assert any("карта утверждений" in r for r in _refusals(_complete(claims=None)))


def test_a_hand_forged_confirmed_row_blocks_on_its_signer():
    """The wire can be written by hand; a signer not entitled to the status is refused."""
    claims = channel.claim_map_message([_confirmed_row()], iteration=2, artifact_seq=SEQ)
    claims["rows"][0]["status_by"] = Party.DEVELOPMENT.value
    messages = _complete(claims=claims)
    assert any("а вправе только" in r for r in _refusals(messages))


def test_a_row_whose_triple_disagrees_with_its_fields_blocks():
    claims = channel.claim_map_message([_confirmed_row()], iteration=2, artifact_seq=SEQ)
    claims["rows"][0]["quote"] = "Выросло втрое"
    messages = _complete(claims=claims)
    assert any("хэш цитаты не сходится" in r for r in _refusals(messages))


# --- the operator's two declarations -----------------------------------------------------


def test_the_operators_two_declarations_are_never_defaulted_to_fine():
    """They have no machine basis, no other side has access to them, and absence counts as
    "not met" precisely so that silence cannot pass for consent."""
    assert any("оператор не объявил" in r for r in _refusals(_complete(takeaway=None)))


def test_a_takeaway_authored_by_development_is_refused():
    """The same prompt-maintained trust level as the whole loop — but these two words belong
    to the operator, and a development-authored declaration is refused outright."""
    messages = _complete()
    messages[-1]["role"] = "development"
    assert any("вправе только оператор" in r for r in _refusals(messages))


def test_a_declared_mismatch_or_a_moved_goal_blocks():
    mismatch = _complete(takeaway=_takeaway(retelling_matches=False))
    assert any("НЕ совпал" in r for r in _refusals(mismatch))

    moved = _complete(takeaway=_takeaway(goal_moved=True))
    assert any("цель двигалась" in r for r in _refusals(moved))


def test_a_takeaway_of_another_round_blocks():
    messages = _complete(takeaway=_takeaway(iteration=5))
    assert any("фаз одного круга" in r for r in _refusals(messages))


def _with_history(current: dict) -> list[dict]:
    """A channel that already holds an earlier operator declaration.

    The history message keeps seq 0 so every reference already sitting in the fixture
    (marker seq, answer seq) stays true — the gate orders by seq, not by list position.
    """
    messages = _complete(takeaway=current)
    earlier = _takeaway(artifact_seq=SEQ - 3, iteration=1)
    messages.insert(0, {
        "seq": 0, "role": "operator", "kind": "notice",
        "payload": earlier, "created_at": (T0 - timedelta(hours=1)).isoformat(),
    })
    return messages


def test_an_unanchored_goal_signal_beside_a_history_blocks():
    """"Did not move" is relative: beside an existing declaration it must name what it did
    not move FROM, or it is unproven — and unproven reads as "moved"."""
    messages = _with_history(_takeaway())
    assert any("непривязанное" in r for r in _refusals(messages))


def test_a_goal_signal_anchored_to_the_wrong_text_blocks():
    import hashlib as _h

    wrong = _takeaway(
        previous_intent_artifact_seq=SEQ - 3,
        previous_takeaway_sha256=_h.sha256(b"other text").hexdigest(),
    )
    messages = _with_history(wrong)
    assert any("относится к другому тексту" in r for r in _refusals(messages))


def test_a_correctly_anchored_goal_signal_passes():
    import hashlib as _h

    anchored = _takeaway(
        previous_intent_artifact_seq=SEQ - 3,
        previous_takeaway_sha256=_h.sha256(
            "Главное — механизм работает.".encode()
        ).hexdigest(),
    )
    assert _refusals(_with_history(anchored)) == []


# --- the blind findings are counted against dispositions ---------------------------------


def _item(text="Термин «леджер» не объяснён", contract_sha256="c1", contract_version=1):
    from assistant_memory.audience.blind_report import finding_identity

    return {
        "id": finding_identity("непонятно", "S-1", text, contract_sha256),
        "section": "S-1",
        "category": "непонятно",
        "text": text,
        "contract_item": "C-2",
        "contract_version": contract_version,
        "contract_sha256": contract_sha256,
    }


def test_an_undisposed_blind_finding_blocks():
    """The critic's counterexample: a valid finding rides the phase, nobody disposed it,
    and the gate used to say nothing."""
    messages = _complete(findings=_findings(findings=[_item()]))
    assert any("не имеет терминальной диспозиции" in r for r in _refusals(messages))


def test_a_terminally_disposed_blind_finding_passes():
    item = _item()
    messages = _complete(findings=_findings(findings=[item]))
    dispositions = [{"finding_id": item["id"], "outcome": "fixed"}]
    assert _refusals(messages, dispositions=dispositions) == []


def test_a_contract_change_retires_the_old_disposition():
    """The critic's counterexample: same words, new contract. The identity is contract-
    bound, so the disposition given under the old premises stops covering — and an item
    stamped with a foreign contract is refused against its own record."""
    old_item = _item()  # identity under the OLD contract, disposed back then
    new_item = _item(contract_sha256="c2", contract_version=2)
    messages = _complete(findings=_findings(findings=[new_item]))
    dispositions = [{"finding_id": old_item["id"], "outcome": "waived"}]
    reasons = _refusals(messages, dispositions=dispositions)
    assert any("находка чужого контракта" in r for r in reasons)
    assert any("не имеет терминальной диспозиции" in r for r in reasons)


def test_a_forged_item_identity_blocks():
    item = _item()
    item["id"] = "B-000000000000"
    messages = _complete(findings=_findings(findings=[]))
    for m in messages:
        if m["payload"].get("phase") == channel.BLIND_FINDINGS_PHASE:
            m["payload"]["findings"] = [item]
    assert any("не выведен из его же содержимого" in r for r in _refusals(messages))


def test_a_non_object_element_is_refused_not_filtered():
    """The critic's counterexample verbatim: filtering a string element out is the quiet
    disappearance the whole condition exists to stop."""
    messages = _complete()
    for m in messages:
        if m["payload"].get("phase") == channel.BLIND_FINDINGS_PHASE:
            m["payload"]["findings"] = ["unparseable blind finding"]
    assert any("нельзя и диспозировать" in r for r in _refusals(messages))


# --- the carried list is bound to its source phase ---------------------------------------


def _carry_over(**kw):
    kw.setdefault("iteration", 3)
    kw.setdefault("carried_from_artifact_seq", SEQ - 1)
    kw.setdefault("carried_from_iteration", 2)
    return kw


def _same_round(messages):
    for m in messages:
        if m["payload"].get("phase") in (
            channel.CLAIM_MAP_PHASE, channel.INTENT_TAKEAWAY_PHASE
        ):
            m["payload"]["iteration"] = 3
    return messages


def test_a_carry_that_drops_the_source_findings_blocks():
    """The critic's second counterexample: the source phase holds a live finding, the carry
    arrives with an empty list — a new list wearing an old record."""
    item = _item()
    messages = _same_round(
        _older_pair(source_findings=[item], **_carry_over(findings=[]))
    )
    assert any("не совпадает с исходной фазой" in r for r in _refusals(messages))


def test_a_carry_without_a_source_phase_blocks():
    messages = _same_round(_older_pair(with_source_phase=False, **_carry_over()))
    assert any("не находит в канале исходной фазы" in r for r in _refusals(messages))


def test_contradicting_source_phases_block():
    item = _item()
    messages = _same_round(_older_pair(source_findings=[], **_carry_over()))
    second_source = channel.blind_findings_message(
        SEQ - 1, findings=[item], run_record_id="blind-1",
        reader_digest=DIGEST, iteration=2, contract_public_items=["C-2", "C-4"],
    )
    messages.insert(1, {
        "seq": -1, "role": "development", "kind": "notice",
        "payload": second_source, "created_at": (T0 - timedelta(hours=2)).isoformat(),
    })
    assert any("противоречат друг другу составом" in r for r in _refusals(messages))


def test_a_faithful_carry_with_disposed_findings_passes():
    item = _item()
    messages = _same_round(
        _older_pair(source_findings=[item], **_carry_over(findings=[item]))
    )
    dispositions = [{"finding_id": item["id"], "outcome": "waived"}]
    assert _refusals(messages, dispositions=dispositions) == []


# --- a later publication may not erase an earlier one's adverse evidence -----------------


def _append(messages, payload, *, role="development", minutes=20):
    messages.append({
        "seq": len(messages) + 1, "role": role, "kind": "notice",
        "payload": payload, "created_at": (T0 + timedelta(minutes=minutes)).isoformat(),
    })
    return messages


def test_a_late_empty_findings_phase_cannot_bury_a_live_finding():
    """The critic's first counterexample: a live finding, then a later empty phase for the
    same record — the last-phase rule alone would see only the empty one."""
    item = _item()
    messages = _complete(findings=_findings(findings=[item]))
    _append(messages, _findings(findings=[]))
    assert any("не вправе стереть раннюю" in r for r in _refusals(messages))


def test_a_rebound_anchor_under_a_disposed_id_blocks():
    """The critic's counterexample: swap contract_item C-2 → C-99 under the same id in a
    later phase of the same record — identity survives, the anchor does not. The builder
    refuses such a payload outright, so the forged phase is hand-patched, as an attacker
    would post it."""
    item = _item()
    rebound = {**item, "contract_item": "C-99"}
    messages = _complete(findings=_findings(findings=[item]))
    forged = dict(_findings(findings=[item]))
    forged["findings"] = [rebound]
    _append(messages, forged)
    dispositions = [{"finding_id": item["id"], "outcome": "waived"}]
    assert any(
        "не вправе стереть раннюю" in r for r in _refusals(messages, dispositions)
    )


def test_a_carry_with_a_rebound_anchor_blocks():
    item = _item()
    rebound = {**item, "contract_item": "C-99"}
    messages = _same_round(
        _older_pair(source_findings=[item], **_carry_over(findings=[item]))
    )
    for m in messages:
        payload = m["payload"]
        if (
            payload.get("phase") == channel.BLIND_FINDINGS_PHASE
            and payload.get("carried_from_artifact_seq") is not None
        ):
            payload["findings"] = [rebound]
    dispositions = [{"finding_id": item["id"], "outcome": "waived"}]
    assert any(
        "полное каноническое содержимое" in r for r in _refusals(messages, dispositions)
    )


def test_a_non_canonical_anchor_is_refused():
    item = _item()
    item["contract_item"] = "C-01"
    messages = _complete(findings=_findings(findings=[]))
    for m in messages:
        if m["payload"].get("phase") == channel.BLIND_FINDINGS_PHASE:
            m["payload"]["findings"] = [item]
    assert any("каноническим идентификатором" in r for r in _refusals(messages))


def test_an_anchor_outside_the_declared_universe_blocks():
    """The critic's counterexample: C-99 is canonical in shape and resolves to nothing —
    even under a terminal disposition."""
    item = _item()
    item["contract_item"] = "C-99"
    messages = _complete(findings=_findings(findings=[]))
    for m in messages:
        if m["payload"].get("phase") == channel.BLIND_FINDINGS_PHASE:
            m["payload"]["findings"] = [item]
    dispositions = [{"finding_id": item["id"], "outcome": "waived"}]
    assert any(
        "вне объявленного перечня публичных" in r
        for r in _refusals(messages, dispositions)
    )


def test_a_phase_without_the_anchor_universe_is_refused_at_the_schema():
    import pytest

    messages = _complete()
    for m in messages:
        if m["payload"].get("phase") == channel.BLIND_FINDINGS_PHASE:
            del m["payload"]["contract_public_items"]
    with pytest.raises(channel.ChannelError):
        _refusals(messages)


def test_a_late_takeaway_cannot_flip_the_goal_signal():
    """The second counterexample: goal_moved=true, then a later same-round phase with
    goal_moved=false — the declaration is immutable within its round."""
    messages = _complete(takeaway=_takeaway(goal_moved=True))
    _append(messages, _takeaway(goal_moved=False), role="operator")
    assert any("не вправе перевернуть ранний сигнал" in r for r in _refusals(messages))


def test_an_identical_takeaway_repost_is_tolerated():
    messages = _complete()
    _append(messages, _takeaway(), role="operator")
    assert _refusals(messages) == []


def test_a_claim_map_may_move_statuses_but_may_not_lose_rows():
    confirmed = _confirmed_row()
    pending = ClaimRow(id="M-2", section="S-1", quote="Ещё утверждение")
    first = channel.claim_map_message([confirmed, pending], iteration=2, artifact_seq=SEQ)
    messages = _complete(claims=first)

    # progression: the pending row later confirmed — legitimate, still non-terminal reasons
    # only until then; here we check the LOSS case
    second = channel.claim_map_message([confirmed], iteration=2, artifact_seq=SEQ)
    _append(messages, second)
    assert any("сама строка не исчезает" in r for r in _refusals(messages))


# --- what the watcher does with them --------------------------------------------------------


def _verdict(value="converged"):
    return {
        "findings": {"items": []},
        "status": {"value": value},
    }


def test_a_convergence_whose_genre_inputs_are_missing_is_not_posted():
    """Withheld VISIBLY, not downgraded in silence: the critic's findings are untouched and
    the claim that the work is finished becomes a question for the operator."""
    posts = watcher.result_messages(
        _verdict(), SEQ, genre_refusals=["нет слепой оценки этой версии артефакта"]
    )
    statuses = [p["payload"] for p in posts if p["kind"] == "status"]
    notices = [p["payload"] for p in posts if p["kind"] == "notice"]
    assert statuses[0]["value"] == "needs_human"
    assert any(n.get("phase") == watcher.GENRE_GATE_PHASE for n in notices)
    assert any("нет слепой оценки" in r for n in notices for r in n.get("reasons", []))
    assert any(p["kind"] == "findings" for p in posts)  # findings survive untouched


def test_a_convergence_with_the_genre_conditions_met_is_posted_unchanged():
    posts = watcher.result_messages(_verdict(), SEQ, genre_refusals=[])
    assert [p["payload"]["value"] for p in posts if p["kind"] == "status"] == ["converged"]
    assert not [
        p for p in posts
        if p["kind"] == "notice" and p["payload"].get("phase") == watcher.GENRE_GATE_PHASE
    ]


def test_an_ordinary_review_pays_nothing_for_the_genre_gate():
    """Only an audience review is measured against the genre's conditions — an ordinary one
    must not be blocked by inputs its protocol never produces."""
    assert watcher.audience_gate_refusals(_complete(), SEQ, None) == []
    assert watcher.audience_gate_refusals(_complete(), SEQ, "audience", ROLES_OFF) == []
    # and an audience gate handed NO role roster fails closed rather than assuming one
    assert any(
        "включённость ролей" in r
        for r in watcher.audience_gate_refusals(_complete(), SEQ, "audience")
    )


def test_a_genre_gate_that_cannot_run_refuses_rather_than_passes():
    """A malformed genre phase is a refusal, not an absence: a gate that cannot evaluate is
    not a gate that passed."""
    broken = [
        {"seq": 1, "role": "development", "kind": "notice",
         "payload": {"phase": channel.CLAIM_MAP_PHASE, "artifact_seq": SEQ}}
    ]
    reasons = watcher.audience_gate_refusals(broken, SEQ, "audience")
    assert any("не смог вычислиться" in r for r in reasons)


# --- garbage in the nested payloads refuses, and never crashes ---------------------------


def test_an_unknown_row_status_is_a_refusal_not_a_crash():
    """The critic's first counterexample: rows[0]['status']='unknown-status' used to end
    the gate with a ValueError — an outage in place of a "no"."""
    messages = _complete()
    for m in messages:
        if m["payload"].get("phase") == channel.CLAIM_MAP_PHASE:
            m["payload"]["rows"][0]["status"] = "unknown-status"
    reasons = _refusals(messages)
    assert any("неизвестный статус" in r for r in reasons)


def test_a_non_object_row_is_refused_at_the_channel_reader():
    """The second counterexample: rows=['not-a-row'] crashed with AttributeError. The
    channel reader now refuses the payload wholesale."""
    import pytest

    messages = _complete()
    for m in messages:
        if m["payload"].get("phase") == channel.CLAIM_MAP_PHASE:
            m["payload"]["rows"] = ["not-a-row"]
    with pytest.raises(channel.ChannelError):
        _refusals(messages)
    # and the WATCHER turns that into a genre refusal rather than an aborted pass
    reasons = watcher.audience_gate_refusals(messages, SEQ, "audience")
    assert any("не смог вычислиться" in r for r in reasons)


def test_semantically_empty_evidence_is_refused_on_read():
    """The critic's counterexample: empty markers, an empty canary answer with a matching
    hash — present in form, empty in substance. The builders always refused these; now the
    READ path holds the same line."""
    import pytest

    messages = _complete()
    empty_sha = hashlib.sha256(b"").hexdigest()
    for m in messages:
        p = m["payload"]
        if p.get("phase") == channel.CANARY_MARKERS_PHASE:
            p["markers"] = []
        if p.get("phase") == channel.CANARY_PHASE:
            p["answer"] = ""
        if p.get("phase") == channel.RUN_RECORD_PHASE and p["record"].get(
            "answer_sha256"
        ):
            p["record"]["answer_sha256"] = empty_sha
    with pytest.raises(channel.ChannelError) as refusal:
        _refusals(messages)
    assert any("содержательно пусто" in r for r in refusal.value.reasons)


def test_the_takeaway_builder_refuses_untyped_signals_instead_of_coercing():
    """bool('yes') is True: a cast in the builder would turn an untyped caller's garbage
    into exactly the decision the read path refuses."""
    import pytest

    with pytest.raises(channel.ChannelError) as refusal:
        _takeaway(retelling_matches="yes", goal_moved="")
    assert any("превратило бы мусор в решение" in r for r in refusal.value.reasons)


def test_truthy_but_untyped_decision_signals_are_refused_on_read():
    """The critic's counterexample: retelling_matches='yes', goal_moved='' — truthiness
    would read them as "matched, did not move", a decision the operator never made."""
    import pytest

    messages = _complete()
    for m in messages:
        if m["payload"].get("phase") == channel.INTENT_TAKEAWAY_PHASE:
            m["payload"]["retelling_matches"] = "yes"
            m["payload"]["goal_moved"] = ""
    with pytest.raises(channel.ChannelError) as refusal:
        _refusals(messages)
    assert any("строгим булевым" in r for r in refusal.value.reasons)


def test_a_blank_operator_declaration_is_refused_on_read():
    import pytest

    messages = _complete()
    for m in messages:
        if m["payload"].get("phase") == channel.INTENT_TAKEAWAY_PHASE:
            m["payload"]["intended_takeaway"] = "   "
            m["payload"]["declared_at"] = ""
    with pytest.raises(channel.ChannelError):
        _refusals(messages)


def test_a_garbage_run_record_is_refused_at_the_channel_reader():
    import pytest

    messages = _complete()
    replaced = False
    for m in messages:
        if m["payload"].get("phase") == channel.RUN_RECORD_PHASE and not replaced:
            m["payload"]["record"] = "garbage"
            replaced = True
    assert replaced
    with pytest.raises(channel.ChannelError):
        _refusals(messages)


# --- B.8 D-4: a broken channel message can be retracted -----------------------------


def _broken_run_record(seq, role="development"):
    """A hand-posted message under the reserved run_record phase whose record does not
    rebuild — the exact shape that once bricked a channel (incident 2c18e50e)."""
    return {
        "seq": seq, "role": role, "kind": "notice",
        "payload": {"phase": "run_record", "artifact_seq": SEQ, "record": {"id": "x"}},
        "created_at": (T0 + timedelta(minutes=20)).isoformat(),
    }


def _retraction(seq, target_seq, role="development", reason="запись не разбирается"):
    return {
        "seq": seq, "role": role, "kind": "notice",
        "payload": {"phase": "phase_retraction", "target_seq": target_seq,
                    "reason": reason},
        "created_at": (T0 + timedelta(minutes=21)).isoformat(),
    }


def test_a_broken_genre_message_still_invalidates_the_replay_without_a_retraction():
    messages = _complete()
    messages.append(_broken_run_record(len(messages) + 1))
    try:
        channel.read_phases(messages)
        raise AssertionError("a broken genre message must refuse the replay")
    except channel.ChannelError as e:
        assert any("не разбирается" in r for r in e.reasons)


def test_a_retraction_excludes_the_broken_message_from_the_replay():
    messages = _complete()
    broken_seq = len(messages) + 1
    messages.append(_broken_run_record(broken_seq))
    messages.append(_retraction(broken_seq + 1, broken_seq))
    phases = channel.read_phases(messages)  # must not raise
    assert all(p.seq != broken_seq for p in phases)


def test_retracting_a_valid_message_is_itself_a_schema_error():
    messages = _complete()
    valid_seq = messages[0]["seq"]
    messages.append(_retraction(len(messages) + 1, valid_seq))
    try:
        channel.read_phases(messages)
        raise AssertionError("retracting a valid message must refuse")
    except channel.ChannelError as e:
        assert any("проходит собственную валидацию" in r for r in e.reasons)


def test_a_retraction_with_no_target_is_refused():
    messages = _complete()
    messages.append(_retraction(len(messages) + 1, 999))
    try:
        channel.read_phases(messages)
        raise AssertionError("a dangling retraction must refuse")
    except channel.ChannelError as e:
        assert any("которого нет на канале" in r for r in e.reasons)


def test_only_the_authoring_role_may_retract():
    messages = _complete()
    broken_seq = len(messages) + 1
    messages.append(_broken_run_record(broken_seq, role="development"))
    messages.append(_retraction(broken_seq + 1, broken_seq, role="critic"))
    try:
        channel.read_phases(messages)
        raise AssertionError("a foreign-role retraction must refuse")
    except channel.ChannelError as e:
        assert any("роль-автор" in r for r in e.reasons)


def test_the_retraction_builder_validates_before_posting():
    messages = _complete()
    broken_seq = len(messages) + 1
    messages.append(_broken_run_record(broken_seq))
    body = channel.phase_retraction_message(
        messages, target_seq=broken_seq, reason="ручной пост под зарезервированной фазой",
        role="development",
    )
    assert body["payload"]["phase"] == "phase_retraction"
    try:
        channel.phase_retraction_message(
            messages, target_seq=messages[0]["seq"], reason="x", role="development"
        )
        raise AssertionError("the builder must refuse a retraction of a valid message")
    except channel.ChannelError:
        pass


# --- B.8 D-5: a waiver survives a contract version only by explicit carry -----------


from assistant_memory.audience.blind_report import finding_identity as _fid


def _blind_item(contract_sha, text="термин «леджер» без объяснения"):
    return {
        "id": _fid("непонятно", "S-1", text, contract_sha),
        "section": "S-1", "category": "непонятно", "text": text,
        "contract_item": "C-2", "contract_version": 1, "contract_sha256": contract_sha,
    }


def _prior_findings_message(item):
    return {
        "seq": 90, "role": "development", "kind": "notice",
        "payload": channel.blind_findings_message(
            39, findings=[item], run_record_id="blind-0", reader_digest=DIGEST,
            iteration=1, contract_public_items=["C-2", "C-4"],
        ),
        "created_at": (T0 - timedelta(days=1)).isoformat(),
    }


def test_a_refound_finding_under_a_live_waiver_is_marked_as_such_not_fresh():
    prior = _blind_item("c0")
    current = _blind_item("c1")  # same words, new contract -> new identity
    messages = _complete(findings=_findings(findings=[current]))
    messages.append(_prior_findings_message(prior))
    reasons = _refusals(messages, dispositions=[{"finding_id": prior["id"],
                                                 "outcome": "waived"}])
    marked = [r for r in reasons if current["id"] in r]
    assert marked and any("ПОВТОРНАЯ" in r and "waiver_carry" in r for r in marked)
    assert not any("не имеет терминальной диспозиции" in r for r in marked)


def test_a_valid_waiver_carry_covers_the_refound_finding():
    prior = _blind_item("c0")
    current = _blind_item("c1")
    messages = _complete(findings=_findings(findings=[current]))
    messages.append(_prior_findings_message(prior))
    dispositions = [
        {"finding_id": prior["id"], "outcome": "waived"},
        {"finding_id": current["id"], "outcome": "waived",
         "waiver_carry": {
             "prior_finding_id": prior["id"],
             "old_contract_sha256": "c0", "new_contract_sha256": "c1",
             "contract_items_diff": "+C-20 (знания) — предпосылка вейвера не задета",
             "premise_untouched": True, "confirmed_by": "seeing"}},
    ]
    reasons = _refusals(messages, dispositions=dispositions)
    assert not any(current["id"] in r for r in reasons)


def test_a_malformed_waiver_carry_is_refused_never_silently_working():
    prior = _blind_item("c0")
    current = _blind_item("c1")
    messages = _complete(findings=_findings(findings=[current]))
    messages.append(_prior_findings_message(prior))
    dispositions = [
        {"finding_id": prior["id"], "outcome": "waived"},
        {"finding_id": current["id"], "outcome": "waived",
         "waiver_carry": {
             "prior_finding_id": "B-nonexistent",
             "old_contract_sha256": "c1", "new_contract_sha256": "c1",
             "premise_untouched": "да", "confirmed_by": "development"}},
    ]
    reasons = _refusals(messages, dispositions=dispositions)
    assert any("contract_items_diff" in r for r in reasons)
    assert any("premise_untouched" in r for r in reasons)
    assert any("confirmed_by" in r for r in reasons)
    assert any("не найден среди вейвер-диспозиций" in r for r in reasons)
    assert any("отпечатки совпадают" in r for r in reasons)
