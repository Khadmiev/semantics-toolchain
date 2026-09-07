# SPDX-License-Identifier: Apache-2.0
"""B.12 — the development cycle as a graph concept, the form of the reconciliation report,
and the projection of the loop's own questions.

Three families, one diagnosis: **the loop's own state had no human projection.** The project
projects a spec into an Intent Summary and built code into a semantic map, and projected the
review's own questions into nothing — so the operator was asked to rule on "X of Y coverage
rows declared unreachable" having seen no rows and knowing neither how they are formed nor
what for.

Part A gives a slice of work a NAME and a place in memory, so the documents that carry what
was promised stop being scattered between a repository and a chat. Part B gives the
reconciliation report a form a tired reader survives: one flat list ordered by what it costs
to miss an entry, six categories that report an observation and never a cause. Part C makes
the projection a RECORD rather than a habit, because a habit cannot be audited.

What most of these tests are really checking is the slice's first constraint: **prose does
not bind at the moment of decision.** Wherever this slice could put a rule in the server, it
did; where it genuinely could not, the rule says so out loud and something else looks.
"""

import pytest
from tests.cycle_helpers import (
    POST_REVIEW,
    PRE_REVIEW,
    SPEC,
    TRANSCRIPT,
    default_documents,
    make_cycle,
    make_cycle_review,
    make_standalone_cycle,
    own_review,
)
from tests.instrument_helpers import seed_instruments

from assistant_memory.review import cycle, round_gate
from assistant_memory.review import watcher as w
from assistant_memory.review.errors import (
    InvalidMessagePayloadError,
    ReviewCreationRefusedError,
)
from assistant_memory.review.render import render_service_tail, render_timeline
from assistant_memory.models.review import Review
from assistant_memory.review.repository import (
    append_message,
    create_review,
    get_messages,
)
from assistant_memory.review.routes import _snapshot

BASE = "a" * 40
COMMIT = "c" * 40


def _inventory(docs=()):
    """The reconciliation's opening inventory: what the pass read, and what it could not.

    Required on every `produced` record since the sol review's first round (finding
    `b12-acting-constraints-not-bound`): without it an empty entry list is indistinguishable
    from a comparison that silently ran against part of the set, and the pass gets one
    attempt. Tests that do not care about the inventory still have to carry one, which is the
    point — the contract is not optional.
    """
    if docs:
        return [{"node_id": str(d.id), "read": True} for d in docs]
    return [{"node_id": "00000000-0000-0000-0000-000000000000", "read": True}]



async def _post(session, rid, role, kind, payload):
    return await append_message(
        session, review_id=rid, role=role, kind=kind, payload=payload
    )


async def _review(session, anchor_id=None, config=None):
    cfg = dict(config or {"coverage": {"in_play": False}})
    if anchor_id is not None:
        cfg["cycle_anchor"] = anchor_id
    issued = await create_review(
        session, slug="b12", mode="code",
        instrument=await seed_instruments(session), config=cfg,
    )
    return issued.review.id


async def _map_on(session, rid):
    await _post(session, rid, "development", "artifact", {
        "mode": "code", "artifact_ref": {"base": BASE, "commit": COMMIT},
    })
    return await _post(session, rid, "critic", "map", {
        "base": BASE, "commit": COMMIT, "modules": ["src/x.py"], "body_markdown": "карта",
    })


def _entry(**over):
    """A complete entry of the commonest category, for tests that vary ONE field."""
    entry = {
        "label": "contradicted",
        "cost_of_missing": "never",
        "quote": "набор документов читается с якоря",
        "address": "интент после ревью, часть вторая",
        "note": "код читает список, объявленный на канале",
        "document_refs": ["00000000-0000-0000-0000-000000000000"],
    }
    entry.update(over)
    return entry


# =======================================================================================
# A-7 / A-12 — the set is READ from the anchor, with its labels
# =======================================================================================


async def test_the_cycle_documents_are_read_with_their_type_and_their_identity_field(
    session, account, space
):
    """The pass must know which documents are intents and what identifies each one.

    Not decoration: the transcript may not ground a broken-promise claim (B-8); the
    POST-review intent is named by the review it reports (`review_affinity`); and the
    cycle's several PRE-review intents — which belong to the CYCLE, not to a review
    (operator's ruling, 2026-08-27) — are told apart by the subject each translates
    (`translates`), which the label alone cannot do because labels are not identity.
    """
    anchor, _docs = await make_cycle(
        session, account, space, documents=default_documents()
    )
    resolved = await cycle.resolve_cycle_documents(session, anchor.id)

    kinds = [d["kind"] for d in resolved["documents"]]
    assert kinds.count(TRANSCRIPT) == 2
    assert SPEC in kinds and PRE_REVIEW in kinds and POST_REVIEW in kinds
    assert all(d["readable"] for d in resolved["documents"])
    # Affinity: only the post-review intent names a review; everything else belongs to
    # the CYCLE, and that emptiness is written out rather than left undefined.
    by_kind = {d["kind"]: d for d in resolved["documents"] if d["kind"] != TRANSCRIPT}
    # Only the POST-review intent belongs to a review — it reports what that review did, and
    # its id exists by the time it is finalized. The PRE-review intent belongs to the cycle
    # and names the SUBJECT it translates (operator's ruling, 2026-08-27).
    assert by_kind[POST_REVIEW]["review_affinity"] is not None
    assert by_kind[PRE_REVIEW]["review_affinity"] is None
    assert by_kind[PRE_REVIEW]["translates"] == "implementation"
    assert by_kind[SPEC]["review_affinity"] is None
    # The TEXT travels, not a path: the pass has no repository and no graph.
    assert all(d["text"] for d in resolved["documents"])


async def test_an_unlabelled_document_is_NAMED_unreadable_never_skipped(
    session, account, space
):
    """Skipping it would shrink the comparison set without saying so — which is exactly the
    failure this whole arrangement exists to make visible. A guess would be worse: a guess
    in a selection fails silently, and this selection decides what may ground a
    broken-promise claim."""
    anchor, _ = await make_cycle(session, account, space, documents=[
        (PRE_REVIEW, "интент", "текст", None, "spec"),
        ("some_other_kind", "непонятный документ", "текст", None),
        (POST_REVIEW, "интент без текста", "", own_review),
    ])
    resolved = await cycle.resolve_cycle_documents(session, anchor.id)
    problems = {d["label"]: d["problem"] for d in resolved["documents"] if not d["readable"]}
    assert "непонятный документ" in problems
    assert "outside the vocabulary" in problems["непонятный документ"]
    assert "интент без текста" in problems
    assert "no text" in problems["интент без текста"]
    # ...and the well-formed one is still readable: one bad document does not void the set.
    assert any(d["readable"] and d["kind"] == PRE_REVIEW for d in resolved["documents"])


async def test_a_POST_review_intent_with_no_review_affinity_is_unreadable(
    session, account, space
):
    """A cycle has more than one review, so the label alone does not say which one this
    intent reports on. Only the POST-review intent carries this obligation: the pre-review
    one belongs to the cycle and names its subject instead (operator's ruling 2026-08-27)."""
    anchor, _ = await make_cycle(session, account, space, documents=[
        (POST_REVIEW, "интент ничей", "текст", None),
    ])
    resolved = await cycle.resolve_cycle_documents(session, anchor.id)
    assert resolved["documents"][0]["readable"] is False
    assert "review_affinity" in resolved["documents"][0]["problem"]


async def test_a_node_that_is_not_a_cycle_anchor_is_a_REFUSAL_not_an_empty_set(
    session, account, space
):
    """An empty set would let a reconciliation report "no discrepancies" over a comparison
    it never made — which is indistinguishable from a clean one, and is the exact shape this
    slice removed from B.11's declaration."""
    anchor, docs = await make_cycle(session, account, space, documents=[
        (SPEC, "спека", "текст", None),
    ])
    with pytest.raises(cycle.CycleAnchorError) as exc:
        await cycle.resolve_cycle_documents(session, docs[0].id)
    assert "not a 'DevelopmentCycle'" in str(exc.value)

    with pytest.raises(cycle.CycleAnchorError):
        await cycle.resolve_cycle_documents(session, "00000000-0000-0000-0000-000000000000")
    with pytest.raises(cycle.CycleAnchorError):
        await cycle.resolve_cycle_documents(session, "not-a-node-id")
    # ...and the real anchor resolves.
    assert len((await cycle.resolve_cycle_documents(session, anchor.id))["documents"]) == 1


async def test_a_wrong_anchor_is_refused_AT_CREATION(session):
    """With a human present, rather than at the far end of a converged review where the
    reconciliation's single attempt is already being spent."""
    with pytest.raises(ReviewCreationRefusedError) as exc:
        await create_review(
            session, slug="b12", mode="code",
            instrument=await seed_instruments(session),
            config={"cycle_anchor": "00000000-0000-0000-0000-000000000000"},
        )
    assert "does not exist" in str(exc.value)


async def test_a_produced_reconciliation_requires_the_review_to_NAME_its_cycle(session):
    """The successor of B.11's set-existence check, against the mechanism that replaced it.

    `failed` stays postable in every state — that is the half worth keeping from the
    original objection: the attempt is spent either way, so the pass must always be able to
    record its own failure with a reason.
    """
    rid = await _review(session)  # no anchor
    map_msg = await _map_on(session, rid)

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(), "entries": [],
        })
    message = str(exc.value)
    assert "cycle_anchor" in message and "development" in message

    landed = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "failed",
        "reason": "ревью не называет цикл — проход не должен был запускаться",
    })
    assert landed.seq > 0


async def test_entries_may_cite_only_documents_that_are_ON_THIS_CYCLES_ANCHOR(session):
    """B.11 checked citations against a DECLARATION, which nobody could verify. The check
    survives; what changed is that there is now a ground under it — the documents are in the
    same database the server is refusing from."""
    anchor_id, docs = await make_standalone_cycle(
        session, documents=default_documents(review_id="review-1")
    )
    rid = await _review(session, anchor_id)
    map_msg = await _map_on(session, rid)

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
            "entries": [_entry(document_refs=["интент какого-то другого цикла"])],
        })
    message = str(exc.value)
    assert "not on this cycle's anchor" in message

    # Cited by NODE ID: the label is display text and is not unique in a cycle.
    landed = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
        "entries": [_entry(document_refs=[str(docs[3].id), str(docs[4].id)])],
    })
    assert landed.seq > 0


def test_the_single_attempt_is_not_spent_against_a_cycle_the_review_does_not_name():
    """Refusing at POST would burn the one attempt and leave nothing on the channel. So the
    SCHEDULER holds it: not schedulable is not the same as failed."""
    channel = [
        {"seq": 1, "kind": "artifact", "role": "development",
         "payload": {"mode": "code", "artifact_ref": {"base": BASE, "commit": COMMIT}}},
        {"seq": 2, "kind": "map", "role": "critic",
         "payload": {"commit": COMMIT, "base": BASE}},
        {"seq": 3, "kind": "artifact", "role": "development",
         "payload": {"intent_summary": True, "converged_artifact_seq": 1,
                     "summary_markdown": "итог"}},
        {"seq": 4, "kind": "findings", "role": "critic",
         "payload": {"artifact_seq": 3, "items": []}},
    ]
    no_cycle = {"protocol": "B.7", "semantic_map": True}
    assert w.plan(channel, no_cycle, "converged")["action"] != "reconciliation"

    with_cycle = {**no_cycle, "cycle_anchor": "some-anchor-id"}
    decision = w.plan(channel, with_cycle, "converged")
    assert decision["action"] == "reconciliation"
    # The set is no longer handed along the decision: it is READ at pass time, because this
    # cycle's post-review intent is written to the anchor DURING this very review.
    assert "intents" not in decision


# =======================================================================================
# B-1…B-4 — six categories, the cost of missing, and evidence that follows the category
# =======================================================================================


async def test_six_categories_and_nothing_else(session):
    """B.11's three became six because six different things were found, not because the
    report is built around a number. The list is OPEN — but it grows by an operator
    decision, never by a pass improvising a seventh value."""
    anchor_id, docs = await make_standalone_cycle(
        session, documents=default_documents(review_id="review-1")
    )
    rid = await _review(session, anchor_id)
    map_msg = await _map_on(session, rid)

    assert set(round_gate.__dict__.get("__all__", [])) is not None  # module import sanity
    from assistant_memory.review.repository import RECONCILIATION_LABELS

    assert RECONCILIATION_LABELS == (
        "contradicted", "promised_absent", "stated_not_surfaced",
        "decided_in_talk_only", "silent", "dropped_in_talk",
    )
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
            "entries": [_entry(label="misreported", document_refs=[str(docs[3].id)])],
        })
    assert "misreported" in str(exc.value)


async def test_every_entry_carries_the_cost_of_missing_and_the_list_is_ORDERED_by_it(
    session,
):
    """The report has no sections. Stop after the first section and you have seen every case
    of one kind and none of any other; stop halfway down a cost-ordered list and you have
    seen the most expensive items of every kind.

    The order is checked by the SERVER because an ordering rule that lives only in the
    prompt is one the prompt can lose — and this report is read by someone who may stop
    halfway."""
    anchor_id, docs = await make_standalone_cycle(
        session, documents=default_documents(review_id="review-1")
    )
    rid = await _review(session, anchor_id)
    map_msg = await _map_on(session, rid)
    ref = [str(docs[3].id)]

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
            "entries": [_entry(document_refs=ref, cost_of_missing=None)],
        })
    assert "cost_of_missing" in str(exc.value)

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
            "entries": [
                _entry(document_refs=ref, cost_of_missing="self_announcing"),
                _entry(document_refs=ref, cost_of_missing="never"),
            ],
        })
    assert "ordered by `cost_of_missing`" in str(exc.value)

    landed = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
        "entries": [
            _entry(document_refs=ref, cost_of_missing="never"),
            _entry(document_refs=ref, cost_of_missing="next_review"),
            _entry(document_refs=ref, cost_of_missing="self_announcing"),
        ],
    })
    assert landed.seq > 0


async def test_silent_quotes_the_CODE_and_states_the_absence_in_words(session):
    """A single rule requiring every entry to quote a promise made `silent` unwritable — and
    a contract that cannot express a case it mandates forces fabrication, mislabelling, or
    silence. The empty half is written out IN WORDS: a blank reads as an omission by the
    pass, and a paraphrase would be a fabricated promise."""
    anchor_id, docs = await make_standalone_cycle(
        session, documents=default_documents(review_id="review-1")
    )
    rid = await _review(session, anchor_id)
    map_msg = await _map_on(session, rid)

    silent = {
        "label": "silent",
        "cost_of_missing": "never",
        "quote": "payload[\"granularity\"] = manifest.get(\"granularity\")",
        "address": "src/assistant_memory/review/repository.py, coverage cross-validation",
        "note": "сервер проставляет зернистость в отчёт покрытия",
    }
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs), "entries": [silent],
        })
    assert "claimed_absent" in str(exc.value)

    landed = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
        "entries": [{**silent, "claimed_absent": "ни один документ цикла этого не называет"}],
    })
    assert landed.seq > 0


async def test_a_talk_borne_entry_must_say_what_makes_the_quote_a_SETTLEMENT(session):
    """A false entry here is more expensive than a missed one: it accuses the reader of
    breaking a promise nobody gave, in a report written for a reader who by construction
    does not go back to the sources. A hypothesis, an option turned over, and a thought
    abandoned later in the same conversation do not qualify."""
    anchor_id, docs = await make_standalone_cycle(
        session, documents=default_documents(review_id="review-1")
    )
    rid = await _review(session, anchor_id)
    map_msg = await _map_on(session, rid)

    for label in ("decided_in_talk_only", "dropped_in_talk"):
        with pytest.raises(InvalidMessagePayloadError) as exc:
            await _post(session, rid, "critic", "reconciliation", {
                "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
                "entries": [_entry(label=label, document_refs=[str(docs[1].id)])],
            })
        assert "settlement" in str(exc.value)

    landed = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
        "entries": [_entry(
            label="decided_in_talk_only",
            document_refs=[str(docs[1].id)],
            settlement="оператор сказал «давай так» — это указание, а не размышление вслух",
        )],
    })
    assert landed.seq > 0


async def test_both_halves_of_an_entry_are_required(session):
    """An entry legible only to someone holding the documents is written for a reader who
    does not need it."""
    anchor_id, docs = await make_standalone_cycle(
        session, documents=default_documents(review_id="review-1")
    )
    rid = await _review(session, anchor_id)
    map_msg = await _map_on(session, rid)
    ref = [str(docs[3].id)]

    for field, needle in (("quote", "VERBATIM"), ("address", "VERBATIM"), ("note", "note")):
        with pytest.raises(InvalidMessagePayloadError) as exc:
            await _post(session, rid, "critic", "reconciliation", {
                "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
                "entries": [_entry(document_refs=ref, **{field: ""})],
            })
        assert needle in str(exc.value)

    for refs in (None, [], ["", "  "], "не список"):
        entry = _entry()
        entry["document_refs"] = refs
        if refs is None:
            entry.pop("document_refs")
        with pytest.raises(InvalidMessagePayloadError) as exc:
            await _post(session, rid, "critic", "reconciliation", {
                "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs), "entries": [entry],
            })
        assert "document_refs" in str(exc.value)


async def test_the_transcript_alone_may_not_ground_a_BROKEN_PROMISE(session):
    """B-8. An intent asserting something is a commitment; a sentence in a free-form
    discussion is not, so it cannot ground a claim that a promise was broken. Treating a
    musing as an assertion would manufacture divergences and, in practice, punish thinking
    out loud — which is the one thing those phases exist for.

    The weak form on purpose: the transcript may still CORROBORATE such an entry beside an
    intent. What is refused is the entry standing on it alone.
    """
    anchor_id, docs = await make_standalone_cycle(
        session, documents=default_documents(review_id="review-1")
    )
    rid = await _review(session, anchor_id)
    map_msg = await _map_on(session, rid)
    transcript, intent = str(docs[1].id), str(docs[3].id)

    for label in ("contradicted", "promised_absent"):
        with pytest.raises(InvalidMessagePayloadError) as exc:
            await _post(session, rid, "critic", "reconciliation", {
                "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
                "entries": [_entry(label=label, document_refs=[transcript])],
            })
        assert "transcript alone" in str(exc.value)

    # Beside an intent it corroborates, and that is legal.
    landed = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "produced", "documents_read": _inventory(docs),
        "entries": [_entry(document_refs=[intent, transcript])],
    })
    assert landed.seq > 0

    # ...and its OWN two categories stand on it alone, which is what it is for.
    second = await _review(session, anchor_id)
    map2 = await _map_on(session, second)
    landed2 = await _post(session, second, "critic", "reconciliation", {
        "map_seq": map2.seq, "outcome": "produced", "documents_read": _inventory(docs),
        "entries": [_entry(
            label="dropped_in_talk", document_refs=[transcript],
            settlement="оператор сказал «этого делать не будем» — это вывод, а не гипотеза",
        )],
    })
    assert landed2.seq > 0


# =======================================================================================
# B-5 / D-1 — the prompt carries the reader, and the fourth document
# =======================================================================================


def test_the_reconciliation_prompt_carries_the_operator_profile():
    """B.11 hard-coded "one or two sentences for the operator, in Russian" into the output
    contract. That freezes ONE field of a profile that carries several, and drifts the
    moment the profile does. The critic runs in a sandbox with no lookup of its own, so the
    prompt is the only route the profile has."""
    prompt = w.build_reconciliation_prompt(
        "PROMPT",
        map_payload={"body_markdown": "карта", "base": BASE, "commit": COMMIT},
        map_seq=2,
        summary_seqs=[7],
        cycle_documents={"anchor": {"node_id": "a1"}, "documents": []},
        profile={"version": "v9", "profile_markdown": "пиши по-русски, раскрывай жаргон"},
    )
    assert "пиши по-русски, раскрывай жаргон" in prompt
    assert "in Russian" not in w.RECONCILIATION_OUTPUT_CONTRACT
    # A review with no frozen profile still gets a named reader, never silence.
    bare = w.build_reconciliation_prompt(
        "PROMPT", map_payload={}, map_seq=2, summary_seqs=[], cycle_documents=None,
        profile=None,
    )
    assert "no frozen operator profile" in bare


def test_the_post_review_summary_seq_REACHES_the_rendered_block():
    """D-1, and it is a MUTATION CHECK by design: the value was collected, threaded through
    three call levels, and dropped at the last one — the body rendered the documents and
    nothing else. Nothing about the other lines would have noticed.

    On the first live run the pass would have compared against three documents of four and
    would not have known the fourth existed.
    """
    documents = {
        "anchor": {"node_id": "a1", "cycle": "B.12", "label": "цикл"},
        "documents": [{
            "node_id": "n1", "label": "интент до ревью", "kind": PRE_REVIEW,
            "review_affinity": "review-1", "chars": 5, "text": "текст",
            "repo_path": None, "readable": True, "problem": None,
        }],
    }
    with_seq = w.build_reconciliation_prompt(
        "PROMPT", map_payload={}, map_seq=2, summary_seqs=[41],
        cycle_documents=documents, profile=None,
    )
    assert "v41" in with_seq

    # ...and its ABSENCE is stated, never left as a silent short list.
    without = w.build_reconciliation_prompt(
        "PROMPT", map_payload={}, map_seq=2, summary_seqs=[],
        cycle_documents=documents, profile=None,
    )
    assert "No post-review Intent Summary" in without


def test_the_pre_review_subject_REACHES_the_rendered_block_and_labels_are_not_a_reference():
    """Two findings of the sol review's round 2, one rendered surface.

    `b12-pre-review-subject-not-projected`: the reader validated and returned `translates`
    and this block — its only consumer — dropped it, so the pass had to infer from labels
    the very identity the field exists to carry.

    `b12-label-reference-still-authorized-by-prompts`: the server accepts only node ids in
    `document_refs`, while this block still told the pass «by node id, or by the exact
    label» — an acting instruction whose faithful reader is refused at POST.
    """
    prompt = w.build_reconciliation_prompt(
        "PROMPT", map_payload={}, map_seq=2, summary_seqs=[],
        cycle_documents={
            "anchor": {"node_id": "a1", "cycle": "B.12"},
            "documents": [
                {"node_id": "n1", "label": "интент до ревью (спека)", "kind": PRE_REVIEW,
                 "review_affinity": None, "translates": "spec", "chars": 5,
                 "text": "текст", "repo_path": None, "readable": True, "problem": None},
                {"node_id": "n2", "label": "интент после ревью", "kind": POST_REVIEW,
                 "review_affinity": "review-1", "chars": 5, "text": "текст",
                 "repo_path": None, "readable": True, "problem": None},
            ],
        },
        profile=None,
    )
    assert "translates: spec" in prompt
    # The post-review intent is identified by its review, not by a subject.
    assert "review: review-1" in prompt
    # References are node ids and nothing else — the block may no longer authorize labels.
    assert "exact label" not in prompt
    assert "NODE ID" in prompt
    # The output contract carries the exact-set inventory beside the entries.
    assert "documents_read" in w.RECONCILIATION_OUTPUT_CONTRACT


def test_the_prompt_names_what_it_read_including_the_unreadable():
    """B-7: with the set READ rather than declared, the failure mode shifts from "the
    declaration was wrong" to "a document had not landed on the anchor yet" — and a list at
    the top makes that visible at a glance."""
    prompt = w.build_reconciliation_prompt(
        "PROMPT", map_payload={}, map_seq=2, summary_seqs=[],
        cycle_documents={
            "anchor": {"node_id": "a1", "cycle": "B.12"},
            "documents": [
                {"node_id": "n1", "label": "транскрипт", "kind": TRANSCRIPT,
                 "review_affinity": None, "chars": 4, "text": "речь", "repo_path": None,
                 "readable": True, "problem": None},
                {"node_id": "n2", "label": "непонятный", "kind": None,
                 "review_affinity": None, "chars": 0, "text": "", "repo_path": None,
                 "readable": False, "problem": "carries no `kind`"},
            ],
        },
        profile=None,
    )
    assert "транскрипт" in prompt and "речь" in prompt
    assert "UNREADABLE" in prompt and "непонятный" in prompt
    # The unreadable one's text is NOT poured into the prompt as if it were a document.
    assert prompt.count("--- DOCUMENT") == 1


async def test_an_over_cap_prompt_does_NOT_spend_the_single_attempt():
    """The pre-flight size check exists so an invocation that would be rejected is never
    made — "the review owes nothing and nothing is consumed" is its own contract. The
    blanket handler around the attempt used to swallow it and record a `failed`
    reconciliation, which consumes the one attempt this pass ever gets, for a call that
    never happened.

    Latent before this slice and made reachable BY it: the prompt used to carry four file
    paths as the other side of the comparison and now carries the cycle's documents in
    full — the only route they have to a pass with neither graph nor repository.
    """
    posted = []

    async def post(route, body):
        posted.append((route, body))
        return {}

    def invoke(_prompt):  # pragma: no cover - must never be reached
        raise AssertionError("the invocation must not be made over the cap")

    with pytest.raises(w.EnvironmentFault) as exc:
        await w.run_reconciliation_pass(
            invoke, post,
            reconciliation_prompt="x" * 5000,
            map_payload={"body_markdown": "карта"},
            map_seq=2,
            summary_seqs=[],
            cycle_documents=None,
            max_prompt_chars=100,
        )
    assert "over the configured critic input cap" in str(exc.value)
    assert posted == [], "an unspent fault must leave NOTHING on the channel"


def test_an_anchor_with_no_documents_tells_the_pass_to_fail_rather_than_go_looking():
    """Deriving the set from filenames is forbidden, and not as a matter of taste: intent
    filenames are not paired and several have no twin at all."""
    prompt = w.build_reconciliation_prompt(
        "PROMPT", map_payload={}, map_seq=2, summary_seqs=[],
        cycle_documents={"anchor": {}, "documents": []}, profile=None,
    )
    assert "outcome: failed" in prompt
    assert "forbidden" in prompt


# =======================================================================================
# C-1 — the projection is a RECORD, with its own kind
# =======================================================================================


async def test_a_projection_carries_a_key_the_original_and_four_non_empty_parts(session):
    """An empty part is the failure mode this element exists to prevent, and a form that
    accepts it guarantees nothing. Four separate fields rather than one blob for exactly
    that reason: a single `translation` string is "non-empty" the moment any one of the four
    is written."""
    rid = await _review(session)
    complete = {
        "item_key": "element:coverage-rows-42",
        "item_seq": 1,
        "original": "coverage row(s) ['a1b2'] were reported not-reached on two passes",
        "what_happened": "критик дважды не дошёл до одного места в коде",
        "concerns": "одна строка из 113, файл разбора команд",
        "proposal": "оставить в знаменателе — место несёт разбор операторских команд",
        "answers": "оставить = критик читает дальше; принять = слой уходит непроверенным",
    }
    landed = await _post(session, rid, "development", "operator_projection", complete)
    assert landed.seq > 0

    for field in ("item_key", "original", "what_happened", "concerns", "proposal", "answers"):
        with pytest.raises(InvalidMessagePayloadError) as exc:
            await _post(session, rid, "development", "operator_projection",
                        {**complete, field: "   "})
        assert field in str(exc.value)

    # `item_seq` is the attribution half and is refused by the same gate: a record that
    # names no raising cannot be joined to one, and the key alone does not attribute
    # (two raisings about one finding share it).
    for bad in (None, 0, -1, "2", 2.0):
        with pytest.raises(InvalidMessagePayloadError) as exc:
            await _post(session, rid, "development", "operator_projection",
                        {**complete, "item_seq": bad})
        assert "item_seq" in str(exc.value)


async def test_a_projection_is_DEVELOPMENTS_and_the_label_says_so(session):
    """Judging how to explain something is a semantic act, and neither the server nor the
    critic performs one on the operator's behalf here."""
    rid = await _review(session)
    payload = {
        "item_key": "q:7", "original": "machine text",
        "what_happened": "a", "concerns": "b", "proposal": "c", "answers": "d",
    }
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "operator_projection", payload)
    assert "labelled 'development'" in str(exc.value)


def test_the_projections_timeline_line_names_the_halves_without_printing_them():
    """A projection carries a machine item verbatim beside a four-part translation; dumping
    both into a timeline would spend exactly the attention the record exists to protect."""
    line = render_timeline([{
        "seq": 9, "role": "development", "kind": "operator_projection",
        "created_at": "2026-08-26T10:00:00Z",
        "payload": {"item_key": "element:x", "original": "raw",
                    "what_happened": "что произошло", "concerns": "чего касается",
                    "proposal": "предложение", "answers": "последствия"},
    }])
    assert "operator projection [element:x]" in line
    assert "4/4 parts" in line and "carried" in line
    assert "raw" not in line


# =======================================================================================
# H-6 — the join, and the two channels it MUST fail on
# =======================================================================================


def _channel_with(*extra):
    return [
        {"seq": 1, "kind": "artifact", "role": "development",
         "payload": {"mode": "code", "artifact_ref": {"base": BASE, "commit": COMMIT}}},
        *extra,
    ]


def _projection(seq, key, item_seq=None, **over):
    """A projection record. `item_seq` names the raising it explains — the key routes, the
    seq attributes (round 2 of this slice's own review). Defaults to `seq - 1`, which is the
    ordinary shape: the projection follows the message it explains."""
    payload = {
        "item_key": key, "item_seq": seq - 1 if item_seq is None else item_seq,
        "original": "machine text",
        "what_happened": "a", "concerns": "b", "proposal": "c", "answers": "d",
    }
    payload.update(over)
    return {"seq": seq, "kind": "operator_projection", "role": "development",
            "payload": payload}


def test_the_join_passes_when_every_operator_item_has_exactly_one_projection():
    """The enumeration is every item that exists AS A MESSAGE — escalations (the coverage
    fork included), human questions, `needs_human`, and the round gate."""
    channel = _channel_with(
        {"seq": 2, "kind": "escalation", "role": "system",
         "payload": {"kind": "contested_fork", "element_id": "coverage-rows-3"}},
        _projection(3, "element:coverage-rows-3"),
        {"seq": 4, "kind": "human_question", "role": "critic",
         "payload": {"id": "q1", "question": "?"}},
        _projection(5, "q:q1"),
        {"seq": 6, "kind": "proposals", "role": "development", "payload": {"entries": []}},
        _projection(7, "seq6"),
    )
    audit = round_gate.projection_audit(channel)
    assert audit["clean"] is True
    assert [i["key"] for i in audit["items"]] == [
        "element:coverage-rows-3", "q:q1", "seq6",
    ]


def test_the_join_FAILS_on_an_operator_item_with_no_projection():
    """The first of the two constructed channels the check must fail on. A check that has
    never been seen to fail is a check nobody knows the behaviour of."""
    channel = _channel_with(
        {"seq": 2, "kind": "escalation", "role": "critic",
         "payload": {"kind": "contested_disposition", "finding_id": "f1"}},
    )
    audit = round_gate.projection_audit(channel)
    assert audit["clean"] is False
    assert any("finding:f1" in f and "NO projection" in f for f in audit["failures"])


def test_a_SECOND_projection_of_one_item_is_legitimate_and_the_last_one_stood():
    """This test asserted the opposite until 2026-08-27, and the operator's amendment is why.

    The rule read a second record as "the operator was shown two accounts of one question".
    It is the reverse: a second translation appears exactly when the first did not land and
    the operator asked again — «я могу его запросить как сейчас, когда перевод меня не
    устроил с 1 раза… Так что переводов может быть более одного». The audit's job is to
    surface the item nobody explained, not to police how many attempts explaining it took.
    What the record must still answer is WHICH account stood, and that is the last one.
    """
    channel = _channel_with(
        {"seq": 2, "kind": "human_question", "role": "critic",
         "payload": {"id": "q1", "question": "?"}},
        _projection(3, "q:q1", item_seq=2),
        _projection(4, "q:q1", item_seq=2),
    )
    audit = round_gate.projection_audit(channel)
    assert audit["clean"] is True
    assert audit["failures"] == []
    (item,) = [i for i in audit["items"] if i["key"] == "q:q1"]
    assert item["projection_seqs"] == [3, 4]
    assert item["account_that_stood"] == 4


def test_a_second_projection_does_not_excuse_a_FIRST_one_that_is_incomplete():
    """The withdrawn rule must not take the completeness check down with it: every projection
    of an item is still checked for both halves, not just the one that stood. An empty part in
    a superseded attempt is still an empty part that was put in front of the operator.
    """
    channel = _channel_with(
        {"seq": 2, "kind": "human_question", "role": "critic",
         "payload": {"id": "q1", "question": "?"}},
        _projection(3, "q:q1", item_seq=2, concerns=""),
        _projection(4, "q:q1", item_seq=2),
    )
    audit = round_gate.projection_audit(channel)
    assert audit["clean"] is False
    assert any("seq 3" in f and "concerns" in f for f in audit["failures"])


def test_the_join_FAILS_on_a_projection_missing_half_of_the_pair():
    """Not reachable through the server, which refuses it at POST — checked anyway, because
    a check that only re-asks what the writer already refused says nothing about channels
    written before it or by anything else."""
    channel = _channel_with(
        {"seq": 2, "kind": "human_question", "role": "critic",
         "payload": {"id": "q1", "question": "?"}},
        _projection(3, "q:q1", original=""),
    )
    audit = round_gate.projection_audit(channel)
    assert audit["clean"] is False
    assert any("original" in f for f in audit["failures"])


def test_naming_the_two_exits_is_OUTSIDE_the_enumeration():
    """It is a conversational duty of development: it raises no message, has no key, and
    cannot be joined to anything. An enumeration that includes a member with no key is a
    check that always fails — and a check that always fails is turned off."""
    audit = round_gate.projection_audit(_channel_with(
        {"seq": 2, "kind": "notice", "role": "development",
         "payload": {"text": "оба выхода названы оператору: заброс и операторский стоп"}},
    ))
    assert audit["items"] == []
    assert audit["clean"] is True


def test_the_service_tail_carries_the_join_and_says_when_it_FAILS():
    """It is COMPUTED, beside the findings ledger and for the same reason: a computed ledger
    needs no faithfulness audit, and this is the claim least safe to leave to the
    recollection of the party it audits."""
    failing = render_service_tail(_channel_with(
        {"seq": 2, "kind": "escalation", "role": "critic",
         "payload": {"kind": "contested_fork", "element_id": "e1"}},
    ))
    assert "THE CHECK FAILS" in failing
    assert "element:e1" in failing

    passing = render_service_tail(_channel_with(
        {"seq": 2, "kind": "escalation", "role": "critic",
         "payload": {"kind": "contested_fork", "element_id": "e1"}},
        _projection(3, "element:e1"),
    ))
    assert "the check passes" in passing
    # The trust boundary is stated in the artifact the operator reads, not only in a spec.
    assert "faithful" in passing


def test_the_two_folds_agree_about_what_a_key_IS():
    """One implementation of the namespacing, not two. If the open-items fold and the audit
    spelled keys differently, the audit would be measuring its own copy — which is the
    failure it exists to catch."""
    for kind, payload in (
        ("escalation", {"kind": "external_defect", "id": "d7"}),
        ("escalation", {"element_id": "e1"}),
        ("escalation", {"finding_id": "f1"}),
        ("escalation", {}),
        ("human_question", {"id": "q1"}),
    ):
        message = {"seq": 5, "kind": kind, "role": "critic", "payload": payload}
        key = round_gate.operator_item_key(kind, payload, 5)
        assert round_gate.open_operator_item_keys([message]) == [key]


# =======================================================================================
# C-4 — a coverage count never travels without its granularity
# =======================================================================================


def test_the_coverage_report_line_names_the_granularity_beside_the_count():
    """"5 of 5" and "13 of 113" can describe the same reading at different depth tiers, and
    the number is what a human remembers. The manifest line always printed it; the REPORT
    line — the one that carries the numbers — did not."""
    line = render_timeline([{
        "seq": 4, "role": "critic", "kind": "coverage_report",
        "created_at": "2026-08-26T10:00:00Z",
        "payload": {"artifact_seq": 1, "manifest_id": "m1", "granularity": "file",
                    "rows": [{"row_id": "r1", "verdict": "reviewed-clean"}]},
    }])
    assert "granularity=file" in line


# =======================================================================================
# D-2 — the probe record carries the tool version it passed on
# =======================================================================================


async def test_a_stored_profile_version_must_record_WHICH_TOOL_VERSION_its_probes_passed_on(
    session,
):
    """"Tool version divergence" is a declared ground for devaluing a launch profile, and
    the store recorded only pass/fail per probe — so the ground existed in the prose of the
    rule and could never fire. The stake is not hypothetical: a Codex update from 0.145 to
    0.147 broke the sandbox once and was diagnosed by hand.

    No migration for this: `probe_evidence` is an existing nullable JSONB column, which is
    why D-2 says "no migration" as a statement rather than a hope.
    """
    from tests.instrument_helpers import TEST_HOSTNAME, TEST_USERNAME, make_content, make_evidence

    from assistant_memory.review import instruments
    from assistant_memory.review.errors import InstrumentValidationError

    profile = await instruments.upsert_profile(
        session, hostname=TEST_HOSTNAME, username=TEST_USERNAME, engine="codex",
    )
    bare = make_evidence()
    bare.pop("tool_versions")
    for evidence, needle in (
        (bare, "tool_versions"),
        ({**make_evidence(), "tool_versions": {}}, "tool_versions"),
        ({**make_evidence(), "tool_versions": {"codex": "  "}}, "non-empty version string"),
        ({**make_evidence(), "tool_versions": {"python": "3.14"}}, "must include the engine"),
    ):
        with pytest.raises(InstrumentValidationError) as exc:
            await instruments.add_profile_version(
                session, profile_id=profile.id, content=make_content(),
                probe_evidence=evidence,
            )
        assert needle in str(exc.value)

    stored = await instruments.add_profile_version(
        session, profile_id=profile.id, content=make_content(),
        probe_evidence=make_evidence(tool_versions={"codex": "0.147.0", "python": "3.12.4"}),
    )
    assert stored.probe_evidence["tool_versions"]["codex"] == "0.147.0"


async def test_the_recorded_tool_set_is_CLOSED_to_the_engine_and_the_interpreter(session):
    """Finding `b12-tool-version-name-contract-still-unsafe` (sol round 2), second half.

    The freshness check runs a tool at its RECORDED ABSOLUTE PATH, and only the engine
    (`engine_binary`) and the interpreter (`python`) have one — so a recorded version for
    any other tool is a comparison that can never be performed, and the prepare-repair
    skill used to authorise exactly such entries. Closing the set removes the
    contradiction without building a per-tool path registry.

    The first half — the NAME beside the value reaching the rendered script unchecked —
    is closed by the same rule: both accepted names are fixed printable literals.
    """
    from tests.instrument_helpers import TEST_HOSTNAME, TEST_USERNAME, make_content, make_evidence

    from assistant_memory.review import instruments
    from assistant_memory.review.errors import InstrumentValidationError

    profile = await instruments.upsert_profile(
        session, hostname=TEST_HOSTNAME, username=TEST_USERNAME, engine="codex",
    )
    for name in ("hammer", "codex\nextra_line", "codex "):
        with pytest.raises(InstrumentValidationError) as exc:
            await instruments.add_profile_version(
                session, profile_id=profile.id, content=make_content(),
                probe_evidence=make_evidence(
                    tool_versions={"codex": "0.147.0", name: "1.0"}
                ),
            )
        assert "closed tool set" in str(exc.value)


def test_the_rendered_launcher_NAMES_the_versions_it_was_proven_on():
    """Clause 2 of the render contract is that the artifact is self-identifying by
    deterministic inputs only — and the one input that decides whether the proof still holds
    was missing from it. A script that says «proven on codex 0.145» while the box has 0.147
    is the difference between a puzzle and a fact."""
    from tests.test_review_launcher import _render

    script, _digest = _render(probe_evidence={"tool_versions": {"codex": "0.147.0"}})
    assert "proven_tool_versions: codex=0.147.0" in script

    # A version stored before D-2 says so, rather than rendering a blank that reads as a bug.
    legacy, _ = _render(probe_evidence={})
    assert "not recorded" in legacy

    # Deterministic: the same inputs produce the same bytes, dict order included.
    a, ha = _render(probe_evidence={"tool_versions": {"codex": "1", "python": "2"}})
    b, hb = _render(probe_evidence={"tool_versions": {"python": "2", "codex": "1"}})
    assert a == b and ha == hb


def test_the_renderer_trusts_NEITHER_half_of_a_stored_tool_pair():
    """Round 1 fixed the value and interpolated the name unchecked; round 3 fixed the
    GRAMMAR (finding `b12-tool-version-shell-metachar-still-unsafe`): the consumer of the
    rendered header is a SHELL, and «&» is printable while separating commands even on a
    CMD rem line. The write path now refuses such pairs, but this renderer turns STORED
    bytes into script text — a version written before those checks, or by anything else,
    still renders here, and each half outside the shell-inert charset becomes a named
    refusal rather than a line of the script."""
    from tests.test_review_launcher import _render

    bad_value, _ = _render(
        probe_evidence={"tool_versions": {"codex": "0.147\nrm -rf /"}}
    )
    assert "rm -rf" not in bad_value
    assert "codex=<unrenderable" in bad_value

    bad_name, _ = _render(
        probe_evidence={"tool_versions": {"codex\nexec evil": "0.147.0"}}
    )
    assert "exec evil" not in bad_name
    assert "<unrenderable tool name" in bad_name

    # The round-3 case, on the CMD template specifically: «&» is one printable line and
    # still executable text on a rem line — the charset gate refuses it at the render.
    cmd_amp, _ = _render(
        content_over={"shell": "cmd"},
        probe_evidence={"tool_versions": {"codex": "0.147 & echo unexpected"}},
    )
    assert "echo unexpected" not in cmd_amp
    assert "codex=<unrenderable" in cmd_amp

    amp_name, _ = _render(
        content_over={"shell": "cmd"},
        probe_evidence={"tool_versions": {"codex & echo hi": "0.147.0"}},
    )
    assert "echo hi" not in amp_name
    assert "<unrenderable tool name" in amp_name

    # A TERMINAL LF in one pair of a multi-pair render would put the SECOND pair on a
    # new executable line (round 4, `b12-shell-charset-final-lf-gap`); the fullmatch
    # gate turns it into a named refusal and the header stays one line.
    lf_multi, _ = _render(
        content_over={"shell": "cmd"},
        probe_evidence={"tool_versions": {"codex": "0.147\n", "python": "3.12.4"}},
    )
    assert "proven_tool_versions: codex=<unrenderable" in lf_multi
    assert "codex=0.147\n" not in lf_multi
    assert "python=3.12.4" in lf_multi
    lf_name, _ = _render(probe_evidence={"tool_versions": {"codex\n": "0.147.0"}})
    assert "<unrenderable tool name" in lf_name


async def test_a_shell_metachar_version_value_is_refused_at_the_write(session):
    """The write side of the same round-3 finding: rounds 1-2 gated «one printable line»,
    which is a property of text; the consumer is a shell. The stored value must match the
    shell-inert charset, and the engine profile key passes the same identifier gate as
    hostname and username."""
    from tests.instrument_helpers import TEST_HOSTNAME, TEST_USERNAME, make_content, make_evidence

    from assistant_memory.review import instruments
    from assistant_memory.review.errors import InstrumentValidationError

    profile = await instruments.upsert_profile(
        session, hostname=TEST_HOSTNAME, username=TEST_USERNAME, engine="codex",
    )
    for value in ("0.147 & echo unexpected", "0.147|dir", "%PATH%", "0.147;ls"):
        with pytest.raises(InstrumentValidationError) as exc:
            await instruments.add_profile_version(
                session, profile_id=profile.id, content=make_content(),
                probe_evidence=make_evidence(tool_versions={"codex": value}),
            )
        assert "non-empty version string" in str(exc.value)

    with pytest.raises(InstrumentValidationError) as exc:
        await instruments.upsert_profile(
            session, hostname=TEST_HOSTNAME, username=TEST_USERNAME,
            engine="codex & echo hi",
        )
    assert "[A-Za-z0-9._-]" in str(exc.value)

    # A TERMINAL LF alone must be refused too (finding `b12-shell-charset-final-lf-gap`,
    # round 4): Python `$` matches before a trailing newline, so a `.match()` gate
    # accepted "0.147\n" against a grammar declared as one token — and in the rendered
    # header the next pair would start a new executable line.
    with pytest.raises(InstrumentValidationError) as exc:
        await instruments.add_profile_version(
            session, profile_id=profile.id, content=make_content(),
            probe_evidence=make_evidence(tool_versions={"codex": "0.147\n"}),
        )
    assert "non-empty version string" in str(exc.value)
    with pytest.raises(InstrumentValidationError) as exc:
        await instruments.upsert_profile(
            session, hostname=TEST_HOSTNAME, username=TEST_USERNAME, engine="codex\n",
        )
    assert "[A-Za-z0-9._-]" in str(exc.value)


# =======================================================================================
# C-7 — the install seeds the default that the operator does not read machine material
# =======================================================================================


async def _fresh_install(session, account, space):
    """Make the registry look like it has never been seeded, FOR THIS DOMAIN ONLY.

    The seeding condition is deployment-wide by construction — it asks "does this domain's
    registry row exist" — so a test of it is otherwise at the mercy of every row already in
    the database. On the dedicated test database that is normally none, but the app's own
    lifespan (which several tests run) bootstraps for real and commits, so the row survives
    into later runs. Constructing the fresh-install state here is what keeps this test about
    the behaviour instead of about the order the suite happened to run in.
    """
    from sqlalchemy import delete, select

    from assistant_memory.models.identity import Membership
    from assistant_memory.models.profile import ProfileDomain, ProfileEntry

    session.add(Membership(account_id=account.id, space_id=space.id, permission="admin"))
    await session.flush()
    await session.execute(
        delete(ProfileEntry).where(ProfileEntry.domain == "machine-artifact-reading")
    )
    await session.execute(
        delete(ProfileDomain).where(ProfileDomain.domain == "machine-artifact-reading")
    )
    await session.flush()
    assert await session.scalar(
        select(ProfileDomain).where(ProfileDomain.domain == "machine-artifact-reading")
    ) is None


async def test_the_install_seeds_the_machine_artifact_reading_default(session, account, space):
    """Not a property of this operator: a DEFAULT of every install. The operator it most
    protects — a new one — is the least likely to state it.

    What is NOT seeded here is the operator's LANGUAGE: that is asked for by the install
    procedure, which is external to this code. This bootstrap runs unattended inside a
    container with no human present, so it cannot ask — and naming an impossible source
    would be worse than naming none, because an absent owner reads as a gap while an
    impossible one reads as done.
    """
    from assistant_memory.profile import service as profile_service

    await _fresh_install(session, account, space)

    await profile_service.seed_registry(session, space_id=space.id, account_id=account.id)
    profile = await profile_service.build_profile(
        session, account=account, project_space_id=None, visible_spaces={space.id}
    )
    text = profile["profile_markdown"]
    assert "machine-artifact-reading" in text
    # The rule's own words. It used to assert the Russian stem, and the assertion was
    # left behind when the seeded text was translated — invisible, because this file
    # was not being collected at all (the repository root was missing from the test
    # path). Found 2026-09-05 by running the exported tree's own suite.
    assert "projection" in text

    # Idempotent: a second run of the install seeds nothing and rewrites nothing.
    assert await profile_service.seed_registry(
        session, space_id=space.id, account_id=account.id
    ) == 0


async def test_a_retired_default_is_NOT_resurrected_by_the_next_startup(session, account, space):
    """The guard is the registry slot's creation, not "does an entry exist" — because
    retiring a preference DELETES its entry row. A guard on the entry would bring the
    operator's discarded default back on the next container restart, silently."""
    from assistant_memory.profile import service as profile_service

    await _fresh_install(session, account, space)
    await profile_service.seed_registry(session, space_id=space.id, account_id=account.id)
    await profile_service.retire_preference(
        session, account=account, domain="machine-artifact-reading", scope="global",
        project_space_id=None,
    )

    await profile_service.seed_registry(session, space_id=space.id, account_id=account.id)
    winners = await profile_service.resolve_effective(
        session, account_id=account.id, project_space_id=None
    )
    assert "machine-artifact-reading" not in winners


def test_the_service_tails_report_line_carries_it_too():
    """The tail is copied verbatim into the post-review summary — a number that leaves this
    line without its granularity is a number the operator reads a month later with no way to
    know what a row was."""
    tail = render_service_tail([
        {"seq": 1, "kind": "artifact", "role": "development",
         "payload": {"mode": "code", "artifact_ref": {"base": BASE, "commit": COMMIT}}},
        {"seq": 2, "kind": "coverage_manifest", "role": "development",
         "payload": {"artifact_seq": 1, "manifest_id": "m1", "granularity": "symbol",
                     "rows": [{"row_id": "r1"}]}},
        {"seq": 3, "kind": "coverage_report", "role": "critic",
         "payload": {"artifact_seq": 1, "manifest_id": "m1", "granularity": "symbol",
                     "rows": [{"row_id": "r1", "verdict": "reviewed-clean"}]}},
    ])
    assert "granularity=symbol" in tail.split("last report:")[1]


async def test_the_projection_audit_REACHES_a_surface_read_DURING_the_review(session):
    """Round 1 of this slice's own implementation review, finding
    `b12-operator-projection-unbound-at-action`.

    The audit existed and was correct, and had exactly ONE caller — the service tail, pulled
    while the post-review summary is written. So the earliest an operator item raised with no
    projection could become visible was after every question of the review had been asked,
    which is past the point where the only failure mode the audit can prevent — honest
    forgetting — is still preventable. The remedy is reachability, not force: the verdict
    rides the state snapshot, the surface development polls every round.
    """
    rid = await _review(session)
    await _post(session, rid, "development", "artifact", {
        "mode": "code", "artifact_ref": {"base": BASE, "commit": COMMIT},
    })
    await _post(session, rid, "critic", "human_question", {"id": "q1", "question": "?"})

    messages = await get_messages(session, rid, after=0)
    review = await session.get(Review, rid)
    audit = _snapshot(review, messages)["projection_audit"]

    assert audit["clean"] is False
    assert audit["operator_items"] == 1
    # The failure travels NAMED, not as a count: "one item is missing something" sends the
    # reader back to the journal, which is the shape that gets skipped.
    assert any("q:q1" in f and "NO projection" in f for f in audit["failures"])

    raised = [m for m in messages if m.kind == "human_question"][0]
    await _post(session, rid, "development", "operator_projection", {
        "item_key": "q:q1", "item_seq": raised.seq, "original": "machine text",
        "what_happened": "a", "concerns": "b", "proposal": "c", "answers": "d",
    })
    audit = _snapshot(review, await get_messages(session, rid, after=0))["projection_audit"]
    assert audit["clean"] is True and audit["failures"] == []


def test_a_snapshot_with_no_channel_read_says_so_rather_than_claiming_CLEAN():
    """`_snapshot` is also built where the channel was not read — creation returns one before
    any message exists. Reporting `clean: True` there would be a clean bill of health issued
    without looking, which is worse than the absence it stands in for.
    """
    class _R:
        id, slug, mode, config = "r", "s", "code", {}
        state, parked, iteration = "created", False, 0
        created_at = closed_at = None

    assert _snapshot(_R())["projection_audit"] is None


def test_one_projection_does_NOT_cover_two_raisings_that_share_a_key():
    """Round 2 of this slice's own review, finding `b12-projection-key-aliases-distinct-items`.

    The key is shared with the settle-per-key fold by design, so two distinct raisings about
    ONE finding — a contested disposition, and later a contested fork — carry one key.
    Joining on the key alone credited a single projection to both, and the tail then reported
    every item as carrying its verbatim original although that original was verbatim for only
    one of them: the second question went unexplained and nothing saw it.

    The remedy is not a second key (that would give this loop two hand-rolled notions of
    identity, the defect `test_the_two_folds_agree_about_what_a_key_IS` exists to prevent).
    The record names the raising it was written for.
    """
    channel = _channel_with(
        {"seq": 2, "kind": "escalation", "role": "critic",
         "payload": {"kind": "contested_disposition", "finding_id": "f1"}},
        _projection(3, "finding:f1", item_seq=2),
        {"seq": 4, "kind": "escalation", "role": "system",
         "payload": {"kind": "contested_fork", "finding_id": "f1"}},
    )
    audit = round_gate.projection_audit(channel)
    assert audit["clean"] is False
    assert any("raised at seq 4" in f and "NO projection" in f for f in audit["failures"])
    # ...and the first raising is still credited: the fix must not lose what was explained.
    first = [i for i in audit["items"] if i["seq"] == 2][0]
    assert first["projection_seqs"] == [3]


def test_a_projection_that_names_a_raising_and_declares_a_FOREIGN_key_is_refused():
    """One record, two stories. The seq attributes and the key routes; when they disagree the
    record cannot be trusted for either purpose, so it counts for nothing and says so."""
    channel = _channel_with(
        {"seq": 2, "kind": "human_question", "role": "critic",
         "payload": {"id": "q1", "question": "?"}},
        _projection(3, "q:SOMETHING-ELSE", item_seq=2),
    )
    audit = round_gate.projection_audit(channel)
    assert audit["clean"] is False
    assert any("two stories" in f for f in audit["failures"])
    assert any("raised at seq 2" in f and "NO projection" in f for f in audit["failures"])


def test_a_projection_naming_a_message_that_asked_the_operator_NOTHING_is_refused():
    """`item_seq` pointing at an ordinary message is not a harmless typo: it would let a
    record exist, look complete, and attribute to nothing."""
    channel = _channel_with(
        {"seq": 2, "kind": "human_question", "role": "critic",
         "payload": {"id": "q1", "question": "?"}},
        _projection(3, "q:q1", item_seq=1),
    )
    audit = round_gate.projection_audit(channel)
    assert audit["clean"] is False
    assert any("not a message that put anything to the operator" in f
               for f in audit["failures"])


async def test_a_spec_or_transcript_carrying_an_affinity_is_UNREADABLE(session, account, space):
    """A-12 said this emptiness is by construction and written out explicitly. Nothing read
    it back until round 2 of this slice's own review, so 'explicitly empty' accepted any
    value — and an 'explicitly empty' that accepts any value is not a rule. A spec belongs to
    the cycle; an id here does not narrow that, it contradicts it."""
    anchor, _ = await make_cycle(session, account, space, documents=[
        (SPEC, "спека с принадлежностью", "текст", own_review),
        (TRANSCRIPT, "транскрипт с принадлежностью", "текст", own_review),
    ])
    resolved = await cycle.resolve_cycle_documents(session, anchor.id)
    assert [d["readable"] for d in resolved["documents"]] == [False, False]
    for d in resolved["documents"]:
        assert "belongs to the cycle rather than to either of its reviews" in d["problem"]


async def test_an_intent_naming_a_review_of_ANOTHER_cycle_is_UNREADABLE(
    session, account, space
):
    """The realistic corruption, and it needs no adversary: the id is pasted by hand at
    finalization, a cycle has two reviews, and the previous cycle had its own. A malformed
    record is visible at once; a foreign id READS as valid and moves which review a promise
    is attributed to — the very attribution the reconciliation report is built to make."""
    other_anchor, _ = await make_cycle(session, account, space, label="Чужой цикл")
    foreign = await make_cycle_review(session, other_anchor.id, slug="foreign-review")

    anchor, _ = await make_cycle(session, account, space, documents=[
        (POST_REVIEW, "интент чужого ревью", "текст", foreign),
        (POST_REVIEW, "интент своего ревью", "текст", own_review),
    ])
    resolved = await cycle.resolve_cycle_documents(session, anchor.id)
    by_label = {d["label"]: d for d in resolved["documents"]}
    assert by_label["интент чужого ревью"]["readable"] is False
    assert "not a review of this cycle" in by_label["интент чужого ревью"]["problem"]
    # ...and the well-formed sibling is untouched: one bad document does not void the set.
    assert by_label["интент своего ревью"]["readable"] is True


async def test_the_seeded_default_lands_in_the_PERSONAL_zone_not_the_callers(
    session, account, space
):
    """Round 2 of this slice's own review, finding
    `b12-seeded-global-preference-uses-caller-space`.

    The seeding called `personal_space_id` for its truth value and threw the answer away,
    then handed `write_preference` the CALLER's space. That function's contract puts the
    resolution on the caller, so nothing downstream corrected it, and a caller seeding the
    registry from another zone wrote the global default into that zone — the exact opposite
    of the rule stated three lines above the call, and of the canon: a global preference
    lands on the operator's person node in their personal space and refuses to fall back.
    """
    from sqlalchemy import select

    from assistant_memory.models.graph import Node, NodeSpace
    from assistant_memory.models.identity import Space
    from assistant_memory.profile import service as profile_service


    await _fresh_install(session, account, space)
    personal = await profile_service.personal_space_id(session, account.id)
    assert personal is not None

    other = Space(name="some-project-zone", template="project", created_by=account.id)
    session.add(other)
    await session.flush()
    assert other.id != personal, (
        "the seeding must be driven from a zone that is NOT the personal one, or this test "
        "passes for the wrong reason"
    )

    await profile_service.seed_registry(session, space_id=other.id, account_id=account.id)

    landed = (
        await session.scalars(
            select(NodeSpace.space_id)
            .join(Node, Node.id == NodeSpace.node_id)
            .where(
                Node.deleted_at.is_(None),
                Node.properties["domain"].astext == "machine-artifact-reading",
            )
        )
    ).all()
    assert landed, "the default was not seeded at all"
    assert other.id not in landed, "the global default fell into the caller's zone"
    assert personal in landed


async def test_a_PRE_review_intent_names_its_SUBJECT_and_never_a_review(
    session, account, space
):
    """Operator's ruling, 2026-08-27, settling finding `b12-review-affinity-before-review-id`.

    The rule used to demand that this document be written at its finalization — before the
    review exists — while carrying that review's id. Development hit the impossibility twice
    in one day and worked around it; the critic found it by reading the text. The operator
    removed the cause rather than the symptom: «интент до ревью создается, но он относится к
    циклу разработки… А ревью уже проходит по той спеке или коду, переводом которых на мой
    язык интент и является».

    So a pre-review intent belongs to the CYCLE and names the subject it translates. A cycle
    has more than one of them, and the label may not tell them apart — labels are not unique
    and are not machine identity (finding `b12-document-label-reference-ambiguous`).
    """
    foreign_anchor, _ = await make_cycle(session, account, space, label="Чужой цикл")
    a_review = await make_cycle_review(session, foreign_anchor.id, slug="some-review")

    anchor, _ = await make_cycle(session, account, space, documents=[
        (PRE_REVIEW, "интент спеки", "что строим", None, "spec"),
        (PRE_REVIEW, "интент реализации", "что построили", None, "implementation"),
        (PRE_REVIEW, "интент с принадлежностью", "текст", a_review, "spec"),
        (PRE_REVIEW, "интент без предмета", "текст", None),
        (PRE_REVIEW, "интент с чужим предметом", "текст", None, "что-то ещё"),
    ])
    by_label = {d["label"]: d for d in (
        await cycle.resolve_cycle_documents(session, anchor.id)
    )["documents"]}

    # The two well-formed ones are readable and TELL EACH OTHER APART by subject, which is
    # the whole reason the field exists.
    assert by_label["интент спеки"]["readable"] is True
    assert by_label["интент реализации"]["readable"] is True
    assert by_label["интент спеки"]["translates"] == "spec"
    assert by_label["интент реализации"]["translates"] == "implementation"
    assert by_label["интент спеки"]["review_affinity"] is None

    # An affinity here is not a harmless extra: it asserts a belonging this document cannot
    # have, and the emptiness is part of the write contract rather than an omission.
    assert by_label["интент с принадлежностью"]["readable"] is False
    assert "belongs to the cycle" in by_label["интент с принадлежностью"]["problem"]

    # No subject, or one outside the vocabulary: unreadable and NAMED, never guessed at from
    # the label or the text.
    for label in ("интент без предмета", "интент с чужим предметом"):
        assert by_label[label]["readable"] is False
        assert "translates" in by_label[label]["problem"]


async def test_a_projection_reference_is_checked_for_SHAPE_at_post_and_resolved_only_by_the_audit(
    session,
):
    """Finding `b12-projection-reference-post-claim-drift` (sol round 1): development had
    written that the server refuses an unresolvable reference at POST. It never did — it
    checks a positive integer and non-empty fields. The claim was narrowed to the truth
    rather than the mechanism strengthened, and this test states the resulting division of
    labour instead of leaving it to be inferred from a passing POST.

    Resolving at POST was considered and declined: it would make the write path depend on the
    journal to buy a check the audit performs anyway, and it would not remove the audit —
    channels written before the check, or by anything else, still need one.
    """
    rid = await _review(session)
    # A reference to a message that raised nothing — accepted, because POST checks shape.
    landed = await _post(session, rid, "development", "operator_projection", {
        "item_key": "q:nonexistent", "item_seq": 1, "original": "machine text",
        "what_happened": "a", "concerns": "b", "proposal": "c", "answers": "d",
    })
    assert landed.seq > 0

    # ...and the audit is what catches it, by name.
    audit = round_gate.projection_audit(
        [
            {"seq": m.seq, "kind": m.kind, "role": m.role, "payload": m.payload}
            for m in await get_messages(session, rid, after=0)
        ]
    )
    assert audit["clean"] is False
    assert any("not a message that put anything to the operator" in f
               for f in audit["failures"])


async def test_a_produced_reconciliation_must_OPEN_with_what_it_read_and_what_it_could_not(
    session,
):
    """Finding `b12-acting-constraints-not-bound` (sol round 1), first half.

    The pass was required to open by naming every document it read and every one it could
    not — and the requirement lived only in the prompt. The output was reduced to `entries`
    on the way out, the server accepted no inventory, and nothing rendered one. So an EMPTY
    entry list, which is the commonest good result, could not be told apart from a comparison
    that silently ran against three documents out of four — and the pass gets ONE attempt, so
    the silence is permanent.
    """
    anchor_id, docs = await make_standalone_cycle(session, documents=default_documents())
    rid = await _review(session, anchor_id)
    map_msg = await _map_on(session, rid)

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "entries": [],
        })
    assert "documents_read" in str(exc.value)

    # An unread document with no stated reason is the silence the field exists to break.
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "entries": [],
            "documents_read": [{"node_id": str(docs[0].id), "read": False}],
        })
    assert "no reason" in str(exc.value) or "problem" in str(exc.value)

    # `read` is a boolean by TYPE, not by equality (finding
    # `b12-inventory-read-bool-type-loose`): `1 in (True, False)` is True in Python,
    # while the renderer distinguishes by identity — an accepted numeric 0 would render
    # as an unexplained UNREAD row after the single attempt was spent.
    for flag in (1, 0):
        with pytest.raises(InvalidMessagePayloadError) as exc:
            await _post(session, rid, "critic", "reconciliation", {
                "map_seq": map_msg.seq, "outcome": "produced", "entries": [],
                "documents_read": [{"node_id": str(docs[0].id), "read": flag,
                                    "problem": "не важно"}]
                + [{"node_id": str(d.id), "read": True} for d in docs[1:]],
            })
        assert "boolean" in str(exc.value)

    landed = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "produced", "entries": [],
        "documents_read": [
            {"node_id": str(docs[0].id), "read": True},
            {"node_id": str(docs[1].id), "read": False, "problem": "текст не читается"},
        ] + [{"node_id": str(d.id), "read": True} for d in docs[2:]],
    })
    assert landed.seq > 0


async def test_the_inventory_is_JOINED_to_the_cycles_set_not_only_shaped(session):
    """Finding `b12-inventory-not-joined-to-cycle-set` (sol round 2).

    The shape check accepted any non-empty list of well-formed rows, and the renderer called
    a list with no `read: false` row "fully read" — so a partial or duplicated inventory
    recreated the exact misleading clean result the field was added to prevent. The very
    first version of THIS test certified the hole: it posted two inventory rows against a
    five-document cycle and asserted acceptance. The meaning of the inventory is a claim
    about the SET, checkable only against the set: exactly one row per document.
    """
    anchor_id, docs = await make_standalone_cycle(session, documents=default_documents())
    rid = await _review(session, anchor_id)
    map_msg = await _map_on(session, rid)

    # Partial: rows for two documents of five — the original hole.
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "entries": [],
            "documents_read": [
                {"node_id": str(docs[0].id), "read": True},
                {"node_id": str(docs[1].id), "read": True},
            ],
        })
    assert "omits documents of the cycle" in str(exc.value)

    # A duplicate row makes the opening counts meaningless.
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "entries": [],
            "documents_read": [{"node_id": str(docs[0].id), "read": True}]
            + [{"node_id": str(d.id), "read": True} for d in docs],
        })
    assert "more than once" in str(exc.value)

    # A foreign id claims a reading of a document the operator was never shown.
    foreign_rows = [{"node_id": str(d.id), "read": True} for d in docs[1:]] + [
        {"node_id": "11111111-1111-1111-1111-111111111111", "read": True},
    ]
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "entries": [],
            "documents_read": foreign_rows,
        })
    assert "not on this cycle's anchor" in str(exc.value)

    # Exactly one row per document — the only accepted shape.
    landed = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "produced", "entries": [],
        "documents_read": _inventory(docs),
    })
    assert landed.seq > 0


def test_no_discrepancy_over_a_PARTLY_READ_set_does_not_render_as_a_clean_result():
    """The single most misleading line this report can print. Rendered so the reader sees the
    difference without going back to the journal — the same reason the inventory is carried
    at all."""
    clean = render_timeline([{
        "seq": 1, "role": "critic", "kind": "reconciliation", "created_at": None,
        "payload": {"map_seq": 1, "outcome": "produced", "entries": [],
                    "documents_read": [{"node_id": "n1", "read": True}]},
    }])
    assert "fully read set" in clean

    partial = render_timeline([{
        "seq": 1, "role": "critic", "kind": "reconciliation", "created_at": None,
        "payload": {"map_seq": 1, "outcome": "produced", "entries": [],
                    "documents_read": [
                        {"node_id": "n1", "read": True},
                        {"node_id": "n2", "read": False, "problem": "нечитаем"},
                    ]},
    }])
    assert "NOT fully read" in partial
    assert "UNREAD n2" in partial and "нечитаем" in partial


def test_the_service_tail_prints_typed_failure_reasons():
    """Round 14, finding b14-service-tail-typed-failure-regression-test-missing: the
    round-13 fix covered both human surfaces; this pins the SECOND one — the tail the
    post-review summary copies verbatim."""
    tail = render_service_tail([
        {"seq": 1, "kind": "artifact", "role": "development",
         "payload": {"mode": "code", "artifact_ref": {"base": BASE, "commit": COMMIT}}},
        {"seq": 2, "kind": "coverage_manifest", "role": "development",
         "payload": {"artifact_seq": 1, "manifest_id": "m1", "granularity": "symbol",
                     "rows": [{"row_id": "b1::change"}, {"row_id": "b1::seam"},
                              {"row_id": "b2::seam"}]}},
        {"seq": 3, "kind": "coverage_report", "role": "critic",
         "payload": {"artifact_seq": 1, "manifest_id": "m1", "granularity": "symbol",
                     "rows": [
                         {"row_id": "b1::change", "verdict": "reviewed-clean"},
                         {"row_id": "b1::seam", "verdict": "cannot_reach",
                          "reason": "dead branch"},
                         {"row_id": "b2::seam", "verdict": "instrument_failure",
                          "reason": "sandbox died"},
                     ]}},
    ])
    assert "cannot_reach: b1::seam — dead branch" in tail
    assert "instrument_failure: b2::seam — sandbox died" in tail
