# Dossier 3 — Semantic contamination

*The article:* "a necessity of two critic passes was written in: a hot one
... and a cold one ... Nobody ever verified that necessity, but it hardened
into a given, and every reader of the canon defended it, including my own
assistant. When measurement finally reached it, the construction fell
apart. In one review, cold verdicts overturned three consecutive elegant and
wrong arguments of the development side ... In another, a critic's
hallucination survived the restart of its own session, and the decision
journal records it in as many words: the session faithfully preserves the
error too. ... the hot pass was removed entirely."

## Context for the reader

The contaminated party was the canon, not an agent. The two measured facts
that broke the dogma are both recorded, and the decision that ended it is
recorded twice: once as a compromise ("the session is allowed as a cache;
the journal stays the truth", 2026-08-16) and two days later as its own
supersession, when the operator withdrew the compromise entirely during the
review of the next build. The store keeps both, which is the point: the
history of a decision is readable in two steps.

The first measured fact comes from a review of a commercial ML system on
2026-07-14; that review's record is quoted with the project and the domain
specifics redacted. The second comes from a code review on 2026-07-29,
where a critic cited a coverage-row identifier that did not exist, twice,
across a resumed session.

## Record 1 — Decision `e0dad010-ea43-4753-b275-07598abbd17d`, with its supersession

Store: the author's private knowledge graph. Created: 2026-08-16T09:15:32Z.
Status at export: superseded (by its own `superseded_2026_08_18` field,
written after the operator's ruling of 2026-08-18). Written by: the
development agent. Original language: Russian. Quoted: the `why`,
`decision`, `measured_cost_of_session_memory` and `superseded_2026_08_18`
fields, in that order.

> **Verbatim (Russian).**
>
> [why:] За сессию сильный аргумент цены: сейчас каждый проход платит за всю историю заново — токенами и задержкой. Против сессии-ИСТОЧНИКА три механических довода. (1) Умирает холодный проход: живая сессия не может развидеть объяснения, увиденные в прошлом круге (ценность холодности измерена: в ревью 3fa2e317 холодные вердикты сняли три подряд элегантных и неверных аргумента разработки). (2) Перезапуск = амнезия, причём частичная: процесс падает, контекст поджимается провайдером — это неуправляемый пересказчик, который молча решает, что выбросить. Сборка из журнала переживает рестарт по построению. (3) Находка, не выводимая из журнала, неаудируема: канал перестаёт быть полным отчётом, и спор «ты это уже принял» становится неразрешимым.
>
> [decision:] Критик может вести одно ревью в ЖИВОЙ СЕССИИ, но сессия — КЭШ, а не ИСТОЧНИК. Три условия: (а) на каждом проходе в сессию доезжают авторитетные приращения из канала; (б) всё, что критик утверждает, обязано быть выводимо из журнала и артефакта, а не из «я помню»; (в) падение сессии — не потеря ревью, а пересборка кэша из журнала (сегодняшний путь остаётся запасным). ХОЛОДНЫЙ ПРОХОД ИДЁТ В ОТДЕЛЬНОЙ СВЕЖЕЙ СЕССИИ против самой строгой проекции — это единственное место, где память критика вредна, и он маленький, так что отдельная сессия дёшева.
>
> [measured_cost_of_session_memory:] В код-ревью f2f39623 (2026-07-29) критик дважды сослался на несуществующий идентификатор строки покрытия (11 символов вместо 12), и наиболее вероятное объяснение — галлюцинация ПЕРЕЖИЛА возобновление его сессии. Сессия честно сохраняет и ошибку тоже.
>
> [superseded_2026_08_18:] ПЕРЕСМОТРЕНО ревью B.9 (круг 5, решение оператора, финализировано 2026-08-18): разрешение «сессия как кэш» ОТОЗВАНО целиком — каждый проход критика идёт в свежей сессии, память критика — только журнал. Обоснование: экономика кэша подорвана самой файл-проекцией (дорог был вклеенный в промпт журнал, а не свежая сессия); вся измеренная история (773+ проходов) прожита стейтлес; холодный проход как отдельный обряд исчез — каждый проход холоден по обеим осям (память — свежестью, рационале — безусловным скрытием). Потеря «сосредоточенная проверка починок» компенсирована: секция «диспозиции, ожидающие проверки» первой в несущем ядре + первая обязанность прохода — вердикт по каждой. Канон — в спеке B.9 (A-2/A-5/B-4), docs/design/2026-08-16_review_loop_next_spec.md @ 24e8e28.

> **Translation (English), by the author's agent; not part of the record.**
>
> [why:] There is a strong cost argument for the session: today every pass pays for the whole history again, in tokens and latency. Against the session as SOURCE, three mechanical arguments. (1) The cold pass dies: a live session cannot unsee the explanations it saw in the previous round (the value of coldness has been measured: in review 3fa2e317 the cold verdicts removed three consecutive elegant and wrong arguments of the development side). (2) A restart is amnesia, and a partial one: the process dies, the context is compacted by the provider, an uncontrolled summariser that silently decides what to drop. Reassembly from the journal survives a restart by construction. (3) A finding not derivable from the journal is unauditable: the channel stops being a complete account, and the dispute "you already accepted this" becomes unresolvable.
>
> [decision:] The critic may conduct one review in a LIVE SESSION, but the session is a CACHE, not a SOURCE. Three conditions: (a) on every pass the authoritative increments from the channel reach the session; (b) everything the critic asserts must be derivable from the journal and the artifact, not from "I remember"; (c) a session crash is not the loss of the review but a rebuild of the cache from the journal (today's path remains the fallback). THE COLD PASS RUNS IN A SEPARATE FRESH SESSION against the strictest projection; it is the one place where the critic's memory is harmful, and it is small, so a separate session is cheap.
>
> [measured cost of session memory:] In code review f2f39623 (2026-07-29) the critic twice cited a nonexistent coverage-row identifier (11 characters instead of 12), and the most likely explanation is that the hallucination SURVIVED the resumption of its session. The session faithfully preserves the error too.
>
> [superseded 2026-08-18:] REVISED by review B.9 (round 5, operator's decision, finalised 2026-08-18): the permission "session as cache" is WITHDRAWN entirely; every critic pass runs in a fresh session, the critic's memory is the journal alone. Grounds: the economics of the cache were undermined by the file projection itself (what was expensive was the journal pasted into the prompt, not the fresh session); the whole measured history (773+ passes) was lived stateless; the cold pass as a separate rite has disappeared, every pass is cold on both axes (memory, by freshness; rationale, by unconditional hiding). The loss of "a focused check of the fixes" is compensated: the section "dispositions awaiting verification" comes first in the load-bearing core, and the pass's first duty is a verdict on each. The canon is in spec B.9 (A-2/A-5/B-4), docs/design/2026-08-16_review_loop_next_spec.md @ 24e8e28.

## Record 2 — Feedback `85e0dc5e-49af-4550-beb1-72771e8ac645` (excerpt)

Store: the author's private knowledge graph. Created: 2026-07-14T15:02:06Z.
Status at export: provisional (kept as a durable record after triage).
Written by: the development agent, the same day as the review it reports.
Original language: English. Quoted: the header, the third and fourth
numbered items, and the first protocol paragraph. The review was of a
commercial ML system: the project, the task and the domain-specific names
are redacted.

> **Verbatim.** Redactions are marked `[redacted: reason]`; nothing else is
> altered.
>
> EVIDENCE REPORT — the review plugin paid for itself, with hard numbers. Review 3fa2e317 ([redacted: project name], [redacted: task name], spec mode, 2026-07-14). Development = Claude Code (Opus 4.8), critic = Codex via the headless watcher. Outcome: 16 findings raised, 16 upheld and fixed, 0 waived, 0 dissents, 0 escalations, 1 operator fork, 6 artifact revisions, 5 iterations, plus 2 iterations of the B.2 faithfulness audit on the Intent Summary itself.
>
> [...]
>
> 3. THREE WITHDRAWN ARGUMENTS. Development's central claim ("a stratified split can never be leakage-free on this [redacted: data structure]") was withdrawn; its REPLACEMENT ("the overlap components are exactly the [redacted: business entities]") was also withdrawn; and a third ("the leak is monotonically biased toward the fitted model, so the decision stands a fortiori") was withdrawn. Every one was elegant, plausible, and wrong. The mechanism development designed was fine; every argument it offered in support was not.
>
> 4. THE ONE ELEMENT THAT SURVIVED UNTOUCHED WAS AUTHORED BY THE CRITIC. PF-12 — a direct attribution-level tripwire that measures the invariant instead of arguing it — exists only because the critic (C5) broke PF-11, the argument-dependent check development had called "unevadable". PF-12 needed zero revisions across six.
>
> WHICH PROTOCOL FEATURES DID THE WORK, specifically:
>
> - COLD-VERDICT-FIRST is not ceremony; it is load-bearing. The critic formed verdicts without development's rationale, so it did not inherit development's blind spots. Every one of the three withdrawn arguments was rejected COLD. Recommend keeping the "waiving it is an operator gate, not a development call" rule exactly as it is — development would plausibly have waived it here as "a short artifact" and lost everything above.

## Redactions

Record 2: the project name, the task name and two domain nouns are replaced;
the counts and the protocol vocabulary are untouched. Record 1 needed none.

## What the records do and do not show

They show the two measurements the article cites, each dated and each in a
record written at the time, and the decision arc in the store's own two
steps. They do not show the hot critic "staying persuaded on every
subsequent pass" as a separate record: that observation is the "cannot
unsee" argument inside Record 1, and its evidence is the same review
3fa2e317. The channels of reviews 3fa2e317 and f2f39623 are not exported;
the first belongs to a commercial project.
