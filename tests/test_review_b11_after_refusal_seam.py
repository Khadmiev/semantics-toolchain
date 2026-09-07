# SPDX-License-Identifier: Apache-2.0
"""B.11 — the after-refusal seam, and the operator's own gate at the end of a cycle.

Two families in one slice, and they share a diagnosis rather than a mechanism.

PARTS A AND B: the loop's refusals are correct and its after-refusal routing is not. Nine
live defects land on the same seam — the server knows how to refuse, but not how to hand
the move on. So what is under test here is never "does it refuse"; it is what the channel,
the scheduler and the surfaces say AFTER a refusal, and whether a completed pass survives
one.

PARTS E AND F: the map the critic writes from the FINAL CODE, and the single auxiliary
attempt that lays it beside what the operator was told. The load-bearing property of both
is what they CANNOT do: change state, raise findings, reopen a converged review.
"""

import pytest

from assistant_memory.models.review import Review
from assistant_memory.review import coverage, round_gate
from assistant_memory.review import watcher as w
from assistant_memory.review.errors import InvalidMessagePayloadError
from assistant_memory.review.genres import coverage_in_play, freeze_role_settings
from assistant_memory.review.repository import (
    advance_state,
    append_message,
    create_review,
    get_messages,
)
from assistant_memory.review.routes import _snapshot

from tests.cycle_helpers import default_documents, make_standalone_cycle
from tests.instrument_helpers import seed_instruments

BASE = "a" * 40
COMMIT = "c" * 40


async def _post(session, rid, role, kind, payload):
    return await append_message(
        session, review_id=rid, role=role, kind=kind, payload=payload
    )


async def _cycle_inventory(session, rid):
    """The exact-set opening inventory a `produced` reconciliation now owes (B.12, finding
    `b12-inventory-not-joined-to-cycle-set`): one row per document of the review's cycle,
    resolved the same way the server resolves the set it joins against."""
    from assistant_memory.review import cycle

    review = await session.get(Review, rid)
    resolved = await cycle.resolve_cycle_documents(
        session, review.config["cycle_anchor"]
    )
    return [{"node_id": d["node_id"], "read": True} for d in resolved["documents"]]


async def _code_review(session, session_config=None, *, with_cycle=True):
    """A current-protocol code review with a real ref pair — the map's subject needs one.

    B.12 A-7: and with a development-cycle anchor, because a `produced` reconciliation now
    names the cycle whose documents it compared against. The declared-set notice this
    helper's callers used to post is gone with the mechanism it served.
    """
    config = dict(session_config or {})
    if with_cycle and "cycle_anchor" not in config:
        anchor_id, _docs = await make_standalone_cycle(
            session, documents=default_documents(review_id="b11-review")
        )
        config["cycle_anchor"] = anchor_id
    issued = await create_review(
        session, slug="b11", mode="code",
        instrument=await seed_instruments(session),
        config=config or None,
    )
    return issued.review.id


async def _artifact(session, rid, commit=COMMIT):
    return await _post(session, rid, "development", "artifact", {
        "mode": "code", "artifact_ref": {"base": BASE, "commit": commit},
    })


# =======================================================================================
# A-1 — the first manifest of a review is OWED, not absent
# =======================================================================================


def test_coverage_in_play_is_a_setting_with_a_name_and_a_default():
    """The predicate's whole defect was having no name: three copies each inferred the
    answer from an empty channel, and an empty channel is the state EVERY review is in
    before its first manifest lands — a manifest references an artifact and cannot precede
    one."""
    assert freeze_role_settings(None, "code")["coverage"]["in_play"] is True
    assert freeze_role_settings(None, "spec")["coverage"]["in_play"] is True
    assert freeze_role_settings({"coverage": {"in_play": False}}, "code")["coverage"][
        "in_play"
    ] is False


def test_a_declaration_must_be_a_strict_boolean():
    """`False` and "the key is missing" are the same string in a config file and very
    different facts. The difference is resolved at creation, not argued about later."""
    with pytest.raises(ValueError) as exc:
        freeze_role_settings({"coverage": {"in_play": "yes"}}, "code")
    assert "strict boolean" in str(exc.value)


def test_the_predicate_reads_the_setting_not_the_channel():
    """One answer, and the message scan is only the pre-B.11 fallback."""
    frozen_off = {"coverage": {"in_play": False}}
    assert coverage_in_play(frozen_off, any_manifest=True) is False
    frozen_on = {"coverage": {"in_play": True}}
    assert coverage_in_play(frozen_on, any_manifest=False) is True
    # No key at all: a review created before this slice, and the OLD inference is kept for
    # it deliberately — an in-flight review must not start being refused mid-channel by a
    # rule its author never agreed to.
    assert coverage_in_play(None, any_manifest=False) is False
    assert coverage_in_play(None, any_manifest=True) is True


async def test_the_first_version_owes_its_denominator(session):
    """THE MEASURED DEFECT, from the other side. With no manifest anywhere the old
    predicate answered "coverage is not in play" and two consecutive passes of B.11's own
    spec review ran with no denominator in complete silence — nothing on the channel
    recorded that they were unmeasured. Now the very first evidence message is refused
    until the table exists."""
    rid = await _code_review(session)
    artifact = await _artifact(session, rid)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "findings", {
            "artifact_seq": artifact.seq, "items": [],
        })
    assert "owes a coverage_manifest" in str(exc.value)


async def test_a_review_that_declares_coverage_off_is_not_gated(session):
    """The absence of a denominator becomes a STATED decision rather than a silence — which
    is the whole reason the setting has a name."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    artifact = await _artifact(session, rid)
    landed = await _post(session, rid, "critic", "findings", {
        "artifact_seq": artifact.seq, "items": [],
    })
    assert landed.seq > 0


async def test_a_protocol_pinned_reproduction_keeps_the_old_inference(session):
    """The B.7 rollout device, reused: a review explicitly reproducing an older protocol
    must reproduce it EXACTLY, or it is not a reproduction. It carries no frozen coverage
    setting and falls back to the inference from message presence."""
    issued = await create_review(
        session, slug="old", mode="code", config={"protocol": "B.6"}
    )
    assert "coverage" not in (issued.review.config or {})
    assert "semantic_map" not in (issued.review.config or {})
    artifact = await _artifact(session, issued.review.id)
    landed = await _post(session, issued.review.id, "critic", "findings", {
        "artifact_seq": artifact.seq, "items": [],
    })
    assert landed.seq > 0


def test_the_watcher_scheduler_follows_the_same_answer():
    """A copy only in the sense of being asked here too; the ANSWER has one home."""
    messages = [{
        "seq": 1, "kind": "artifact", "role": "development",
        "payload": {"mode": "code", "artifact_ref": {"base": BASE, "commit": COMMIT}},
    }]
    assert w.manifest_owed(messages, {"coverage": {"in_play": True}}) is True
    assert w.manifest_owed(messages, {"coverage": {"in_play": False}}) is False
    assert w.manifest_owed(messages, None) is False  # pre-B.11: the old inference


# =======================================================================================
# The slice's one migration — and the reversal it cannot perform
# =======================================================================================


def _migration_module():
    """The migration, imported by path — it lives outside the package."""
    import importlib.util
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "migrations" / "versions" / "a1b2c3d4e5f6_review_map_reconciliation_kinds.py"
    )
    spec = importlib.util.spec_from_file_location("b11_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_guard_names_exactly_the_kinds_the_widening_adds():
    """The drift this pins: a fourth kind added to the constraint but not to the guard
    would be reversed away silently — the downgrade would pass its check and then be
    refused by the database, which is the opaque failure the guard exists to replace."""
    module = _migration_module()
    import re

    def listed(text):
        return set(re.findall(r"'([a-z_]+)'", text))

    added = listed(module._KIND_NEW) - listed(module._KIND_OLD)
    assert set(module._ADDED_KINDS) == added


def test_the_downgrade_refuses_legibly_once_the_feature_has_been_used(monkeypatch):
    """Critic finding `migration-downgrade-rejects-existing-new-kinds`, round 4.

    The reversal is NOT made to work: it could only work by deleting channel content, and
    the channel is append-only and never rewritten — a stronger invariant than the
    convenience of a reversal. What is under test is that the refusal SAYS SO, with the
    count and the route that does exist, instead of surfacing as a check-constraint
    violation from PostgreSQL.
    """
    module = _migration_module()
    dropped: list = []

    class _Result:
        def __init__(self, n):
            self._n = n

        def scalar_one(self):
            return self._n

    class _Bind:
        def __init__(self, n):
            self._n = n

        def execute(self, _sql):
            return _Result(self._n)

    def _op(count):
        monkeypatch.setattr(module.op, "get_bind", lambda: _Bind(count))
        monkeypatch.setattr(module.op, "drop_constraint", lambda *a, **k: dropped.append(a))
        monkeypatch.setattr(module.op, "create_check_constraint", lambda *a, **k: None)

    _op(7)
    with pytest.raises(RuntimeError) as exc:
        module.downgrade()
    message = str(exc.value)
    assert "7 message(s)" in message
    assert "append-only" in message and "backup" in message
    assert not dropped, "the constraint must not be touched when the reversal is refused"

    # ...and with nothing written, the reversal is ordinary.
    _op(0)
    module.downgrade()
    assert dropped, "with no message of the new kinds the downgrade proceeds"


# =======================================================================================
# A-2 — a semantic refusal is not an environmental fault
# =======================================================================================


def test_a_documented_semantic_refusal_is_recognised_and_says_what_follows():
    """Returning the CONSEQUENCE rather than a boolean is the point: the defect A-2 repairs
    was a channel that recorded a refusal and then said something FALSE about what would
    happen next ("retrying in 30s", a retry no component owned)."""
    detail = (
        "coverage_manifest 'm1' is in force for artifact_seq 3, so this pass must post a "
        "coverage_report answering THAT manifest before ending — one verdict per row"
    )
    consequence = w.semantic_refusal(detail)
    assert consequence is not None
    assert "cannot be answered later" in consequence.lower() or "CANNOT" in consequence
    assert "Nothing is retried" in consequence


def test_a_transport_failure_is_not_claimed_as_semantic():
    """The registry is EXPLICIT on the same principle as the repair shapes: the watcher
    never authors semantics it has not observed, so an unrecognised refusal keeps its old
    route rather than being guessed at."""
    assert w.semantic_refusal("connection reset by peer") is None
    assert w.semantic_refusal("") is None


def test_the_semantic_refusal_class_is_not_an_environment_fault():
    """SUBCLASSING WAS THE BUG. `PostRefusalFault` is an `EnvironmentFault`, which is what
    routed a semantic refusal into the H-3 envelope; this one deliberately is not, so no
    fault stretch opens and the failure cadence does not slow down for a healthy machine."""
    assert not issubclass(w.SemanticPassRefusal, w.EnvironmentFault)
    assert issubclass(w.PostRefusalFault, w.EnvironmentFault)


async def test_a_semantic_refusal_posts_a_truthful_notice_and_hands_the_round_on():
    """The whole route, end to end: the pass concluded, the server refused its conclusion,
    and what reaches the channel is what failed and what follows — with `retry` explicitly
    null rather than a promise."""
    posted = []

    async def post(path, body):
        if path == "state":
            return {}
        if body.get("kind") == "status":
            raise RuntimeError(
                "post messages rejected: 422 {\"detail\": \"coverage_manifest 'm1' is in "
                "force for artifact_seq 1, so this pass must post a coverage_report "
                "answering THAT manifest before ending\"}"
            )
        posted.append(body)
        return {}

    messages = [{
        "seq": 1, "kind": "artifact", "role": "development",
        "payload": {"mode": "code", "artifact_ref": {"base": BASE, "commit": COMMIT}},
    }]
    result = await w.run_pass(
        messages,
        "CRITIC",
        lambda prompt: (
            '{"findings": {"items": []}, "status": {"value": "needs_iteration"}}'
        ),
        post,
        mode="code",
        artifact_seq=1,
    )
    # The pass is NOT owed again: it ran, and no retry is scheduled or promised.
    assert result is True
    notices = [
        b for b in posted
        if b.get("kind") == "notice"
        and (b.get("payload") or {}).get("phase") == w.SEMANTIC_REFUSAL_PHASE
    ]
    assert len(notices) == 1
    payload = notices[0]["payload"]
    assert payload["refused_kind"] == "status"
    assert payload["retry"] is None
    assert "no retry is scheduled" in payload["note"]
    assert "coverage report" in payload["consequence"]
    # And nothing anywhere claims a retry is coming.
    assert not any("retrying in" in str(b) for b in posted)


# =======================================================================================
# A-3 — waiting_on names the party that owes the move
# =======================================================================================


async def test_waiting_on_names_development_once_findings_have_landed(session):
    """For roughly twenty minutes both sides read the surface as "the critic is slow" while
    the ball was with development (bd787de7, 2026-08-21). The review's STATE is still
    `critic_reviewing`; the round gate is not, and the gate is who owes the move."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    artifact = await _artifact(session, rid)
    await advance_state(session, rid, "critic_reviewing")
    await _post(session, rid, "critic", "findings", {
        "artifact_seq": artifact.seq,
        "items": [{"id": "f1", "title": "something", "finding_type": "correctness"}],
    })
    review = await session.get(Review, rid)
    messages = await get_messages(session, rid, after=0)
    assert review.state == "critic_reviewing"
    assert _snapshot(review, messages)["waiting_on"] == "development"
    # Without the channel the surface still answers — less precisely, and by the old rule.
    assert _snapshot(review)["waiting_on"] == "critic"


# =======================================================================================
# B-1 — the unreached escalation distinguishes its causes
# =======================================================================================


def test_an_all_instrument_failure_group_is_recognised():
    """The cause was already machine-readable at the moment the fork was raised — the
    watcher's own classification on the downgrade. The fork just did not look at it."""
    from assistant_memory.review.repository import _all_downgraded_by_instrument

    def _pass(seq, reason):
        return [
            {"seq": seq, "kind": "coverage_report", "role": "critic",
             "payload": {"artifact_seq": 1, "rows": [{"row_id": "r1", "reason": reason}]}},
            {"seq": seq + 1, "kind": "status", "role": "critic",
             "payload": {"value": "needs_iteration", "artifact_seq": 1}},
        ]

    prefix = coverage.INSTRUMENT_FAILURE_PREFIX
    both = [
        {"seq": 1, "kind": "artifact", "role": "development", "payload": {"mode": "code"}},
        *_pass(2, prefix + "the quote served docs"),
        *_pass(4, prefix + "the quote served docs"),
    ]
    assert _all_downgraded_by_instrument(_as_rows(both), ["r1"]) is True

    mixed = [
        {"seq": 1, "kind": "artifact", "role": "development", "payload": {"mode": "code"}},
        *_pass(2, prefix + "the quote served docs"),
        *_pass(4, "generated code, could not reach"),
    ]
    assert _all_downgraded_by_instrument(_as_rows(mixed), ["r1"]) is False


def test_a_row_merely_omitted_is_not_an_instrument_failure():
    """STRICT IN BOTH DIRECTIONS. A row omitted from a report carries no reason at all —
    that is the "never looked" case, not "the evidence did not hold up". Offering an
    instrument remedy for rows the critic genuinely could not reach would be the same shape
    of false answer this element exists to remove."""
    from assistant_memory.review.repository import _all_downgraded_by_instrument

    messages = _as_rows([
        {"seq": 1, "kind": "artifact", "role": "development", "payload": {"mode": "code"}},
        {"seq": 2, "kind": "coverage_report", "role": "critic",
         "payload": {"artifact_seq": 1, "rows": []}},
        {"seq": 3, "kind": "status", "role": "critic",
         "payload": {"value": "needs_iteration", "artifact_seq": 1}},
        {"seq": 4, "kind": "coverage_report", "role": "critic",
         "payload": {"artifact_seq": 1, "rows": []}},
        {"seq": 5, "kind": "status", "role": "critic",
         "payload": {"value": "needs_iteration", "artifact_seq": 1}},
    ])
    assert _all_downgraded_by_instrument(messages, ["r1"]) is False


class _Row:
    """A stand-in for the ORM message rows `_all_downgraded_by_instrument` folds over."""

    def __init__(self, d):
        self.seq = d["seq"]
        self.kind = d["kind"]
        self.role = d.get("role")
        self.payload = d.get("payload")


def _as_rows(dicts):
    return [_Row(d) for d in dicts]


# =======================================================================================
# B-2 — an answer says which of two things it means
# =======================================================================================


async def test_an_answer_naming_neither_disposition_is_refused(session):
    """Silence used to mean `accepted_unreviewable`, and that is how ~128 rows left the
    denominator on an answer that said "keep making passes" (5be9cf61). POST is the only
    place the ambiguity can be caught while the author is still there to resolve it."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "operator", "decision_response", {
            "element_id": "coverage-rows:42", "decision": "keep making passes",
        })
    message = str(exc.value)
    assert "accepted_unreviewable" in message and "keep_gating" in message


async def test_both_dispositions_are_accepted(session):
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    for disposition in ("accepted_unreviewable", "keep_gating"):
        landed = await _post(session, rid, "operator", "decision_response", {
            "element_id": f"coverage-row:{disposition}",
            "coverage_disposition": disposition,
        })
        assert landed.seq > 0


# =======================================================================================
# B-4 — a downgrade says which ground it expected
# =======================================================================================


def test_the_downgrade_reason_names_the_remedy():
    """The downgrade is the ONLY feedback the next pass receives, and the next pass is a
    stateless invocation that cannot remember an operator's correction. A reason that names
    the remedy is the difference between an eight-row downgrade repeating and it not."""
    parsed = {
        "coverage_report": {
            "artifact_seq": 1,
            "rows": [{
                "row_id": "r1", "verdict": "reviewed-clean", "observation": "read it",
                "evidence": {"ground": "repo", "read_ref": "r-1", "quote": "x"},
            }],
        },
        "read_log": [{
            "id": "r-1", "ground": "docs", "locator": "docs/spec.md",
            "version": "HEAD", "range": [1, 5],
        }],
    }
    downgrades = w.verify_report_evidence(parsed, None, ())
    assert len(downgrades) == 1
    reason = downgrades[0]["reason"]
    assert "the quote served 'docs'" in reason
    assert "mark the confirmation 'docs'" in reason
    # And the ROW carries the machine-readable classification the server reads back when it
    # decides whether the unreached escalation should offer the instrument remedy (B-1).
    row = parsed["coverage_report"]["rows"][0]
    assert row["verdict"] == "not-reached"
    assert row["reason"].startswith(coverage.INSTRUMENT_FAILURE_REASON)


# =======================================================================================
# C-1 — a repeat that cannot help is not spent
# =======================================================================================


def test_a_closed_vocabulary_refusal_is_classified_without_touching_its_text():
    """The marker is a `str` subclass, so the operator-facing reason is untouched and the
    classification is structural — matching refusal WORDING would be a lexical sweep, and a
    lexical sweep over prose is not a class closure."""
    from assistant_memory.audience.structured import (
        annulment_is_deterministic,
        closed_vocabulary,
    )

    marked = closed_vocabulary("категория 'ШАМП' вне таблицы машинности")
    assert marked == "категория 'ШАМП' вне таблицы машинности"  # identical as text
    assert isinstance(marked, str)
    assert annulment_is_deterministic([marked]) is True
    assert annulment_is_deterministic(["ответ обрезан на середине"]) is False
    assert annulment_is_deterministic([]) is False


def test_the_measured_category_slip_is_marked_deterministic():
    """«ШАМП» for «ШТАМП» — one dropped letter, made by the same model on the run AND on
    its automatic retry, twice each (audience review 66651b62 round 5)."""
    from assistant_memory.audience.machine_comb import report_refusals
    from assistant_memory.audience.structured import annulment_is_deterministic

    refusals = report_refusals(
        {"находки": [{
            "категория": "ШАМП", "scope": "весь артефакт",
            "текст": "нечто", "цитаты": [],
        }]},
        section_ids=["S-1"],
        section_texts={"S-1": "текст"},
        table_version="1",
    )
    assert annulment_is_deterministic(refusals) is True


# =======================================================================================
# E-2 / E-3 — the map role and the operator profile are FROZEN at creation
# =======================================================================================


def test_the_map_role_defaults_on_for_code_and_off_for_spec():
    """A map of a spec would be a model reading a text against a model's summary of the
    same text — one medium, weak independence, poorer yield (G-1)."""
    assert freeze_role_settings(None, "code")["semantic_map"] is True
    assert freeze_role_settings(None, "spec")["semantic_map"] is False
    assert freeze_role_settings({"semantic_map": False}, "code")["semantic_map"] is False


def test_turning_the_map_role_off_must_be_a_strict_boolean():
    with pytest.raises(ValueError) as exc:
        freeze_role_settings({"semantic_map": "no"}, "code")
    assert "stated decision" in str(exc.value)


async def test_creation_freezes_the_role_and_the_profile(session):
    """The map is written «на моем языке, согласно моему профилю», and a document produced
    by an instrument that can change underneath it is not comparable to the next one."""
    rid = await _code_review(session)
    review = await session.get(Review, rid)
    assert review.config["semantic_map"] is True
    assert review.config["coverage"]["in_play"] is True


# =======================================================================================
# E-8 — the map is not an artifact, and therefore cannot reopen anything
# =======================================================================================


async def _converged_with_map(session, rid, artifact_seq):
    await _post(session, rid, "critic", "map", {
        "base": BASE, "commit": COMMIT, "modules": ["src/x.py"],
        "body_markdown": "# Что построено\n\nПравило такое-то.",
    })


async def test_a_map_does_not_reopen_a_converged_review(session):
    """Every `artifact` posted while a review is `converged` moves it back to
    `artifact_ready`, the sole exception being the post-review intent summary. A map posted
    as an artifact would therefore reopen the circle BY MACHINERY, against the operator's
    ruling: «Карта ничего не переоткроет. Это мое и только мое решение.»"""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    artifact = await _artifact(session, rid)
    await advance_state(session, rid, "critic_reviewing")
    await _post(session, rid, "critic", "findings", {"artifact_seq": artifact.seq, "items": []})
    review = await session.get(Review, rid)
    frozen = (review.config["instrument"] or {})["critic"]
    await _post(session, rid, "critic", "status", {
        "value": "converged", "artifact_seq": artifact.seq,
        # B.9 D-2: a pass stamps the instrument it ran under, and the server checks it for
        # EQUALITY with the frozen snapshot. Read from the snapshot rather than invented,
        # because a test that invented it would only ever test the invention.
        "model": frozen["model"], "effort": frozen["effort"],
    })
    review = await session.get(Review, rid)
    assert review.state == "converged"

    await _converged_with_map(session, rid, artifact.seq)
    review = await session.get(Review, rid)
    assert review.state == "converged", "the map changed the review's state"


async def test_a_map_needs_its_pair_its_scope_and_its_body(session):
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "map", {
            "base": BASE, "commit": COMMIT, "modules": [],
            "body_markdown": "",
        })
    message = str(exc.value)
    assert "body_markdown" in message and "modules" in message


async def test_a_disposition_always_names_the_map_it_resolves(session):
    """A ruling that names no map is a signature on an unnamed document — so `map_seq` is
    required for EVERY outcome, not only for `task_in_graph`."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    await _artifact(session, rid)
    map_msg = await _post(session, rid, "critic", "map", {
        "base": BASE, "commit": COMMIT, "modules": ["src/x.py"], "body_markdown": "карта",
    })
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "operator", "map_disposition", {
            "outcome": "accepted", "operator_words": "принято",
        })
    assert "map_seq" in str(exc.value)

    landed = await _post(session, rid, "operator", "map_disposition", {
        "outcome": "accepted", "map_seq": map_msg.seq, "operator_words": "принято",
    })
    assert landed.seq > 0


async def test_a_task_disposition_carries_its_provenance_both_ways(session):
    """Without it, a month later the task exists with no way back to what raised it."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    await _artifact(session, rid)
    map_msg = await _post(session, rid, "critic", "map", {
        "base": BASE, "commit": COMMIT, "modules": ["src/x.py"], "body_markdown": "карта",
    })
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "operator", "map_disposition", {
            "outcome": "task_in_graph", "map_seq": map_msg.seq,
            "operator_words": "заводим задачу",
        })
    message = str(exc.value)
    assert "task_node_id" in message and "review_id" in message


async def test_a_ruling_may_only_name_the_material_that_stood_over_ITS_map(session):
    """Critic finding `map-disposition-cross-map-reconciliation-ref`, round 7.

    Existence was checked and belonging was not. These references exist for exactly one
    purpose — to say WHICH material the operator had in front of them — so a ruling over
    map A naming map B's reconciliation states something false about the operator's own
    reading. Reachable whenever a review holds maps for two base+commit pairs, which the
    identity repair of round 2 made an ordinary state rather than an exotic one.
    """
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    await _artifact(session, rid)
    other_base = "b" * 40

    map_a = await _post(session, rid, "critic", "map", {
        "base": BASE, "commit": COMMIT, "modules": ["src/a.py"], "body_markdown": "карта A",
    })
    map_b = await _post(session, rid, "critic", "map", {
        "base": other_base, "commit": COMMIT, "modules": ["src/b.py"],
        "body_markdown": "карта B",
    })
    reconciliation_b = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_b.seq, "outcome": "produced", "documents_read": await _cycle_inventory(session, rid), "entries": [],
    })

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "operator", "map_disposition", {
            "outcome": "accepted", "map_seq": map_a.seq, "operator_words": "принято",
            "reconciliation_seq": reconciliation_b.seq,
        })
    message = str(exc.value)
    assert f"map seq {map_b.seq}" in message and f"map seq {map_a.seq}" in message

    # ...and the reconciliation of its OWN map is accepted.
    reconciliation_a = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_a.seq, "outcome": "produced", "documents_read": await _cycle_inventory(session, rid), "entries": [],
    })
    landed = await _post(session, rid, "operator", "map_disposition", {
        "outcome": "accepted", "map_seq": map_a.seq, "operator_words": "принято",
        "reconciliation_seq": reconciliation_a.seq,
    })
    assert landed.seq > 0


async def test_a_map_reference_that_resolves_to_nothing_is_refused(session):
    """The references identify WHICH material the operator had in front of them, and a
    reference that resolves to nothing identifies nothing."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    await _artifact(session, rid)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "operator", "map_disposition", {
            "outcome": "accepted", "map_seq": 999, "operator_words": "принято",
        })
    assert "names no `map` message" in str(exc.value)


# =======================================================================================
# F-1 / E-11 — one attempt, and the operator's re-licence
# =======================================================================================


#: B.12 A-7: the declared-set notice these helpers used to post is GONE. The documents a
#: reconciliation may cite are the ones on the review's development-cycle anchor, resolved
#: from the graph — `_code_review` attaches one. What the pass compares against is a fact
#: the server reads, not a claim development posts.
async def _map_on(session, rid):
    await _artifact(session, rid)
    return await _post(session, rid, "critic", "map", {
        "base": BASE, "commit": COMMIT, "modules": ["src/x.py"], "body_markdown": "карта",
    })


async def test_reconciliation_validates_its_two_shapes_symmetrically(session):
    """What one shape requires the other refuses. Without the symmetry a failed attempt
    could quietly carry a half-list, and "exactly one attempt" would stop meaning anything
    measurable."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    map_msg = await _map_on(session, rid)

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "failed",
            "entries": [{"label": "silent", "quote": "q", "address": "a"}],
        })
    assert "carries NO `entries`" in str(exc.value)

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "failed",
        })
    assert "requires a `reason`" in str(exc.value)


# B.12 A-7 replaced the DECLARED intent set with resolution from the development cycle's
# anchor, so five tests that lived here moved rather than being deleted: the entry-shape
# checks and the set-existence checks now describe a different mechanism, and they are in
# `test_review_b12_development_cycle.py` against it. What stays here is what B.11 still
# owns — the once-only attempt, the operator's re-licence, the author binding, and the
# scheduling order.


async def test_a_comparison_that_found_nothing_is_produced_not_failed(session):
    """The commonest good outcome. `failed` means the comparison did not happen — the two
    words are what distinguishes them, never the list being empty."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    map_msg = await _map_on(session, rid)
    landed = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "produced", "documents_read": await _cycle_inventory(session, rid), "entries": [],
    })
    assert landed.seq > 0


async def test_a_failed_attempt_is_a_spent_attempt(session):
    """Treating it as unspent would turn «Попытка пусть будет одна» into an unbounded retry
    after every failure — the false-promise shape Part A removes elsewhere."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    map_msg = await _map_on(session, rid)
    await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "failed", "reason": "интенты не разрешились",
    })
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "documents_read": await _cycle_inventory(session, rid), "entries": [],
        })
    message = str(exc.value)
    assert "already been attempted" in message
    assert "no unconsumed request stands" in message


async def test_the_operator_relicenses_exactly_one_further_attempt(session):
    """A request is CLAIMED by exactly one attempt, and the claim is readable from the
    channel alone — no counter and no stored state anywhere."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    map_msg = await _map_on(session, rid)
    await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "failed", "reason": "не сошлось",
    })
    request = await _post(session, rid, "operator", "decision_response", {
        "reconciliation_retry": {"map_seq": map_msg.seq},
        "operator_words": "попробуй ещё раз",
    })
    second = await _post(session, rid, "critic", "reconciliation", {
        "map_seq": map_msg.seq, "outcome": "produced", "documents_read": await _cycle_inventory(session, rid), "entries": [],
        "retry_request_seq": request.seq,
    })
    assert second.seq > 0

    # ...and the SAME request cannot license a third.
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "critic", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "documents_read": await _cycle_inventory(session, rid), "entries": [],
            "retry_request_seq": request.seq,
        })
    assert "already consumed" in str(exc.value)


async def test_a_retry_request_needs_something_to_re_license(session):
    """The first attempt is scheduled by the watcher and needs no request; a request before
    any attempt has nothing to re-license."""
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    map_msg = await _map_on(session, rid)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "operator", "decision_response", {
            "reconciliation_retry": {"map_seq": map_msg.seq},
            "operator_words": "ещё раз",
        })
    assert "nothing to re-license" in str(exc.value)


# =======================================================================================
# E-10 — the map pass has a scheduling contract
# =======================================================================================


def _channel(*extra):
    return [
        {"seq": 1, "kind": "artifact", "role": "development",
         "payload": {"mode": "code", "artifact_ref": {"base": BASE, "commit": COMMIT}}},
        *extra,
    ]


def test_the_map_is_due_once_the_review_converges():
    config = {"protocol": "B.7", "semantic_map": True}
    decision = w.plan(_channel(), config, "converged")
    assert decision["action"] == "map"
    assert decision["base"] == BASE and decision["commit"] == COMMIT


def test_idempotence_is_the_existing_map_and_nothing_else():
    """No separate "already done" marker is introduced to drift from the channel."""
    config = {"protocol": "B.7", "semantic_map": True}
    done = _channel({"seq": 2, "kind": "map", "role": "critic",
                     "payload": {"commit": COMMIT, "base": BASE}})
    assert w.plan(done, config, "converged")["action"] != "map"
    # A map for a DIFFERENT commit does not satisfy this one.
    other = _channel({"seq": 2, "kind": "map", "role": "critic",
                      "payload": {"commit": "d" * 40, "base": BASE}})
    assert w.plan(other, config, "converged")["action"] == "map"


async def test_a_kind_whose_meaning_depends_on_its_author_checks_the_label(session):
    """Critic finding `semantic-map-origin-unbound`, round 2 of this slice's own review.

    A CONSISTENCY CHECK, NOT AUTHORIZATION, and the test is written to say so: roles are
    prompt-maintained in this service and the check is satisfied by a truthful label, not
    by a credential. What it catches is a client posting under the wrong one — and a `map`
    authored by development would be precisely the self-description the map pass exists to
    replace, while also suppressing the real map's scheduling.
    """
    rid = await _code_review(session, {"coverage": {"in_play": False}})
    await _artifact(session, rid)
    good_map = {
        "base": BASE, "commit": COMMIT, "modules": ["src/x.py"], "body_markdown": "карта",
    }
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "development", "map", good_map)
    assert "labelled 'critic'" in str(exc.value)
    assert "not an authorization check" in str(exc.value)

    map_msg = await _post(session, rid, "critic", "map", good_map)

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "development", "map_disposition", {
            "outcome": "accepted", "map_seq": map_msg.seq, "operator_words": "принято",
        })
    assert "labelled 'operator'" in str(exc.value)

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "development", "reconciliation", {
            "map_seq": map_msg.seq, "outcome": "produced", "documents_read": await _cycle_inventory(session, rid), "entries": [],
        })
    assert "labelled 'critic'" in str(exc.value)


def test_every_kind_this_slice_introduced_declares_its_author():
    """The table IS the closure: a kind missing from it is visible in one place, which is
    what makes this a class fix rather than three checks the fourth kind will not inherit.
    """
    from assistant_memory.models.review import MESSAGE_KINDS
    from assistant_memory.review.repository import AUTHOR_BOUND_KINDS, _AUTHOR_BOUND_WHY


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


    assert {"map", "map_disposition", "reconciliation"} <= set(AUTHOR_BOUND_KINDS)
    assert set(AUTHOR_BOUND_KINDS) <= set(MESSAGE_KINDS)
    # Every entry explains itself in the refusal; a bare "wrong role" leaves the author
    # guessing which half of the message is wrong.
    assert set(_AUTHOR_BOUND_WHY) == set(AUTHOR_BOUND_KINDS)


def test_the_map_identity_is_the_pair_not_half_of_it():
    """Critic finding `map-identity-drops-base`. The scope is derived from base+commit
    because "what was built" is a statement about a CHANGE — so the same commit against a
    DIFFERENT base is a different slice, and an earlier map must not answer for it."""
    config = {"protocol": "B.7", "semantic_map": True}
    other_base = "b" * 40
    channel = [
        {"seq": 1, "kind": "artifact", "role": "development",
         "payload": {"mode": "code", "artifact_ref": {"base": other_base, "commit": COMMIT}}},
        # a map for the SAME commit, but derived from a different pair
        {"seq": 2, "kind": "map", "role": "critic",
         "payload": {"base": BASE, "commit": COMMIT, "modules": ["src/x.py"]}},
    ]
    assert round_gate.map_seq_for(channel, {"base": other_base, "commit": COMMIT}) is None
    assert round_gate.map_seq_for(channel, {"base": BASE, "commit": COMMIT}) == 2
    # ...and the scheduler therefore still owes a map for the pair actually under review.
    assert w.plan(channel, config, "converged")["action"] == "map"


def test_a_declared_off_role_schedules_nothing():
    """A review that declared the role off ends exactly as reviews ended before this slice
    — with no map, and nothing waiting for a document that will never come."""
    config = {"protocol": "B.7", "semantic_map": False}
    assert w.plan(_channel(), config, "converged")["action"] != "map"


def test_reconciliation_waits_for_the_map_and_for_a_clean_audit():
    """It TRAILS the map rather than gating it, and it waits for the faithfulness audit
    because a comparison against a summary still under dispute compares to a moving
    document."""
    # B.12 A-7: the review names its development cycle in the frozen config; the pass is
    # not schedulable without one, exactly as it was not schedulable without a declared set.
    config = {"protocol": "B.7", "semantic_map": True, "cycle_anchor": "b12-anchor"}
    with_map = _channel({"seq": 2, "kind": "map", "role": "critic",
                         "payload": {"commit": COMMIT, "base": BASE}})
    # No audit yet: nothing is due.
    assert w.plan(with_map, config, "converged")["action"] not in ("map", "reconciliation")

    audited = [
        *with_map,
        {"seq": 3, "kind": "artifact", "role": "development",
         "payload": {"intent_summary": True, "converged_artifact_seq": 1,
                     "summary_markdown": "итог"}},
        {"seq": 4, "kind": "findings", "role": "critic",
         "payload": {"artifact_seq": 3, "items": []}},
    ]
    decision = w.plan(audited, config, "converged")
    assert decision["action"] == "reconciliation"
    assert decision["map_seq"] == 2
    assert decision["retry_request_seq"] is None


def test_the_audit_verdict_is_about_the_CURRENT_summary(session=None):
    """Critic finding `reconciliation-runs-on-superseded-summary`, round 3 of this slice's
    own review.

    The audit is iterative BY DESIGN — findings are folded in and the summary re-posted
    until a pass comes back clean — so "does some clean pass exist over some summary"
    answers yes forever after the first one. The reconciliation gets exactly ONE attempt;
    spending it against a summary that has since been superseded and disputed spends it
    for good, against the moving document the trigger exists to avoid.
    """
    def summary(seq, converged=1):
        return {"seq": seq, "kind": "artifact", "role": "development",
                "payload": {"intent_summary": True, "converged_artifact_seq": converged,
                            "summary_markdown": "итог"}}

    def pass_over(seq, anchor, items):
        return {"seq": seq, "kind": "findings", "role": "critic",
                "payload": {"artifact_seq": anchor, "items": items}}

    clean_a = [summary(3), pass_over(4, 3, [])]
    assert round_gate.intent_audit_clean(clean_a) is True

    # A NEWER summary lands and is still under audit: the old clean verdict must not carry.
    pending_b = [*clean_a, summary(5)]
    assert round_gate.intent_audit_clean(pending_b) is False

    disputed_b = [*pending_b, pass_over(6, 5, [{"id": "f1", "title": "the summary omits x"}])]
    assert round_gate.intent_audit_clean(disputed_b) is False

    settled_b = [*disputed_b, summary(7), pass_over(8, 7, [])]
    assert round_gate.intent_audit_clean(settled_b) is True

    # ...and order matters within one summary: a later non-empty pass takes it back.
    reopened = [*settled_b, pass_over(9, 7, [{"id": "f2", "title": "still wrong"}])]
    assert round_gate.intent_audit_clean(reopened) is False


def test_the_scheduler_and_the_server_share_one_once_only_rule():
    """Two implementations of "may another attempt be made" would eventually disagree, and
    disagreement here means either a second attempt nobody licensed or a licensed one
    refused. So the fold lives in one place and both sides call it."""
    channel = [
        {"seq": 5, "kind": "reconciliation", "role": "critic",
         "payload": {"map_seq": 2, "outcome": "failed", "reason": "x"}},
    ]
    assert round_gate.reconciliation_licensed(channel, 2) is False
    relicensed = [
        *channel,
        {"seq": 6, "kind": "decision_response", "role": "operator",
         "payload": {"reconciliation_retry": {"map_seq": 2}, "operator_words": "ещё"}},
    ]
    assert round_gate.reconciliation_licensed(relicensed, 2) is True
    standing = round_gate.reconciliation_standing(relicensed, 2)
    assert standing["claimable"] == 6


# =======================================================================================
# The map and reconciliation are not round-gate traffic, and they compact
# =======================================================================================


def test_the_new_kinds_are_outside_the_round_gate_by_construction():
    """Their absence from the legality table IS the mechanism: the table governs only
    `GATED_KINDS`, and a kind outside that set passes through in every gate state. Adding
    them would give the round gate an opinion about traffic that is not the round's."""
    for kind in ("map", "map_disposition", "reconciliation"):
        assert kind in round_gate.WIRE_KINDS
        assert kind not in round_gate.GATED_KINDS
        for legal in round_gate.LEGAL_KINDS.values():
            assert kind not in legal


def test_the_operator_facing_body_is_stubbed_in_the_replay():
    """No pass of the loop is a consumer of either document: the map's own pass runs on an
    EMPTY projection by contract, and the only other pass alive on a converged channel is
    the intent audit. So the replay keeps the addresses and drops the prose."""
    compacted = w.compact_replay([
        {"seq": 1, "kind": "map", "role": "critic",
         "payload": {"base": BASE, "commit": COMMIT, "modules": ["src/x.py"],
                     "body_markdown": "очень длинный текст" * 500}},
        {"seq": 2, "kind": "reconciliation", "role": "critic",
         "payload": {"map_seq": 1, "outcome": "produced", "documents_read": _inventory(),
                     "entries": [{"label": "silent", "quote": "q" * 4000, "address": "a"}]}},
    ])
    map_payload = compacted[0]["payload"]
    assert "body_markdown" not in map_payload
    assert map_payload["commit"] == COMMIT and map_payload["modules"] == ["src/x.py"]
    reconciliation_payload = compacted[1]["payload"]
    assert reconciliation_payload["entry_labels"] == ["silent"]
    assert "entries" not in reconciliation_payload


def test_every_wire_kind_still_declares_a_compaction_policy():
    """Three replay overflows, one mechanism: a new payload kind ships uncompacted because
    compaction was a list of special cases."""
    assert not (round_gate.WIRE_KINDS - w.VERBATIM_KINDS - set(w.COLLAPSE_TABLE))


# =======================================================================================
# E-1 — the map pass's prompt, and what it is deliberately not given
# =======================================================================================


def test_the_map_prompt_says_the_projection_is_empty_on_purpose():
    """A reader who has seen the review's own account of the work cannot help reproducing
    it — and a reproduced account is exactly what the operator already read in the intent."""
    prompt = w.build_map_prompt(
        "SKILL", base=BASE, commit=COMMIT, modules=["src/a.py"],
        profile={"version": "v1", "profile_markdown": "- жаргон раскрывать"},
    )
    assert "NO CHANNEL PROJECTION — DELIBERATELY" in prompt
    assert "no intent summaries" in prompt
    assert "be the ground of an assertion" in prompt
    assert BASE in prompt and COMMIT in prompt and "src/a.py" in prompt
    assert "жаргон раскрывать" in prompt


def test_a_review_without_a_frozen_profile_still_gets_a_register():
    """A review is not refused over a missing profile; the map is then written to the
    general register, and the prompt says so instead of pretending."""
    prompt = w.build_map_prompt(
        "SKILL", base=BASE, commit=COMMIT, modules=["src/a.py"], profile=None
    )
    assert "no frozen operator profile" in prompt
    assert "jargon expanded on first use" in prompt


async def test_the_map_pass_posts_the_derived_scope_not_the_model_s():
    """Two maps of the same slice are meant to be comparable, which a pass-chosen scope
    would not be — so the model supplies the PROSE and nothing else."""
    posted = []

    async def post(path, body):
        posted.append(body)
        return {}

    await w.run_map_pass(
        lambda prompt: '{"body_markdown": "# Что построено", "modules": ["ЧУЖОЕ"]}',
        post,
        map_prompt="SKILL", base=BASE, commit=COMMIT,
        modules=["src/derived.py"], profile=None,
    )
    assert len(posted) == 1
    payload = posted[0]["payload"]
    assert posted[0]["kind"] == "map"
    assert payload["modules"] == ["src/derived.py"]
    assert payload["body_markdown"] == "# Что построено"


async def test_a_map_pass_that_returns_no_document_raises_so_it_is_retried():
    """E-10's deliberate asymmetry: the map IS the subject of the operator's gate, so one
    bad attempt must not mean no map at all."""
    async def post(path, body):
        return {}

    with pytest.raises(ValueError) as exc:
        await w.run_map_pass(
            lambda prompt: '{"body_markdown": "   "}',
            post,
            map_prompt="SKILL", base=BASE, commit=COMMIT, modules=["x"], profile=None,
        )
    assert "announces nothing" in str(exc.value)


async def test_a_failed_reconciliation_attempt_records_itself_instead_of_raising():
    """The opposite contract, and the difference is the operator's ruling: the map is the
    gate, this trails it. An exception here would leave the channel saying nothing while
    the door is nonetheless closed."""
    posted = []

    async def post(path, body):
        posted.append(body)
        return {}

    def boom(prompt):
        raise RuntimeError("the model died")

    await w.run_reconciliation_pass(
        boom, post,
        reconciliation_prompt="PROMPT",
        map_payload={"body_markdown": "карта", "base": BASE, "commit": COMMIT},
        map_seq=2, summary_seqs=[3],
    )
    assert len(posted) == 1
    payload = posted[0]["payload"]
    assert posted[0]["kind"] == "reconciliation"
    assert payload["outcome"] == "failed"
    assert payload["map_seq"] == 2
    assert "the model died" in payload["reason"]
