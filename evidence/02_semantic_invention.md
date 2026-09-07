# Dossier 2 — Semantic invention

*The article:* an escalation arrived about "how exactly to protect me from
the critic's executable being swapped for a malicious one. On my personal
laptop. With an antivirus and a firewall." In one review the operator asked
"protection from whom?" four times; the list of nonexistent adversaries had
to be written into the canon; "proportionality is measured against the
declared horizon."

*If you came to check the executable-substitution anecdote: Record 1 is its
direct durable outcome; Records 2 to 5 show the series it grew into.*

## Context for the reader

The class is the symmetric twin of loss: the model writes in requirements
nobody set. The records below are five entries the invention series left in
the private store, in the order the article tells them:

1. **2026-07-20.** The review that pinned the critic read-only (see dossier
   5) ended with the operator refusing to extend it to the identity of the
   critic's executable. The refusal is recorded as a waiver inside the
   decision that closed that review. This is the "swapped for a malicious
   one" case; the arguing itself happened in the review channel, and the
   waiver is its durable outcome.
2. **2026-07-21.** The operator stated the threat model as a profile
   preference. The list of what is *outside* the model is the "nonexistent
   adversaries written into the canon".
3. **2026-08-04.** In an audience review the critic asked "how is this
   proved?" three rounds running, development answered each time with a new
   mechanism (hash chains over a launch journal, and so on), and the
   operator stopped the spiral with one line. The decision records the
   operator's words and names the principle: proportionality is measured
   against the declared horizon. A companion note written the same day
   states the two honest answers to "how is this proved?".
4. **2026-08-17.** In the review of the next build the operator asked "from
   whom?" four times in one review, every time with the answer "from no one
   in the declared threat model". The decision made the threat frame part
   of the critic's standing input and required every security finding to
   name an in-model adversary.

## Record 1 — the waiver in Decision `0b4fb6ee-d9d8-4226-858d-d0f71a3def46`

Store: the author's private knowledge graph. Created: 2026-07-20T12:35:31Z.
Status at export: current. Written by: the development agent, at the close
of review `91d8a139`. Original language: Russian. Quoted: the waiver
paragraph of the decision text.

> **Verbatim (Russian).**
>
> WAIVER (решение оператора, зона доверия): идентичность codex-бинаря (guard проверяет basename `codex`/`codex.exe`, не резолвит путь) — вне scope. Обоснование: одноюзерная локальная машина, `--codex-cmd` = доверенный операторский конфиг. Известное ограничение; пересмотреть, если watcher будет гонять недоверенный `--codex-cmd`, на многопользовательской машине, либо над внешне-авторскими репозиториями (тогда — резолюция исполняемого через доверенный PATH вне репо / абсолютный путь).

> **Translation (English), by the author's agent; not part of the record.**
>
> WAIVER (operator's decision, trust zone): the identity of the codex binary (the guard checks the basename `codex`/`codex.exe`, does not resolve the path) is out of scope. Grounds: a single-user local machine; `--codex-cmd` is a trusted operator config. A known limitation; revisit if the watcher ever runs an untrusted `--codex-cmd`, on a multi-user machine, or over repositories authored by others (then: resolve the executable through a trusted PATH outside the repo / an absolute path).

## Record 2 — Note (profile preference, domain threat-model) `66081b2d-0d2d-472d-a961-3f44d4edb9f4`

Store: the author's private knowledge graph. Created: 2026-07-21T12:43:20Z.
Status at export: current. Written by: the development agent, recording a
preference the operator stated (the store marks it "operator stated
preference", scope global). Original language: Russian.

> **Verbatim (Russian).**
>
> Инструмент работает на личных машинах оператора. Посторонних пользователей нет, чужой код не исполняется, содержимое ревью не приходит из недоверенных источников. ВНЕ модели угроз: компрометация самого хоста и подмена исполняемых файлов, состязание нескольких одновременно пишущих процессов, злонамеренный внутренний пользователь. В МОДЕЛИ: утечка приватных данных наружу через публикуемые артефакты, порча собственных данных, дефекты, тихо искажающие метрики. Это ожидание сохраняется и после публикации: если кто-то запустит инструмент на публичной машине в недоверенном окружении — это его собственная зона ответственности, и проект не строит защиту от такого окружения.
>
> [why:] Соразмерность защиты меряется относительно объявленной модели угроз. Без неё пара «разработка + критик» по умолчанию защищает объединение всех мыслимых сценариев и наращивает механизмы, которых задача не требует.

> **Translation (English), by the author's agent; not part of the record.**
>
> The tool runs on the operator's personal machines. There are no third-party users, no foreign code is executed, review content does not arrive from untrusted sources. OUTSIDE the threat model: compromise of the host itself and substitution of executables, contention between several concurrently writing processes, a malicious insider. INSIDE the model: leakage of private data outward through published artifacts, corruption of one's own data, defects that quietly distort metrics. This expectation survives publication: if someone runs the tool on a public machine in an untrusted environment, that is their own responsibility, and the project builds no defence against such an environment.
>
> [why:] The proportionality of a defence is measured against the declared threat model. Without it, the pair "development + critic" defends by default against the union of every conceivable scenario and grows mechanisms the task does not require.

## Record 3 — Decision `88633f52-79ce-407d-9934-63b8ff2acb39`

Store: the author's private knowledge graph. Created: 2026-08-04T12:49:07Z.
Status at export: current. Written by: the development agent, recording the
operator's ruling in review `4ffd8b0e`. Original language: Russian. The
operator's sentence inside it is quoted by the record verbatim.

> **Verbatim (Russian).**
>
> Решение оператора 2026-08-04 в ревью 4ffd8b0e, спорная развилка (сервер сам поднял её, обнаружив переоткрытие закрытой находки).
>
> ЧТО СНЯТО. Из механизма, доказывающего, что между проверкой канала и слепым чтением не влез посторонний прогон, убраны: хэш-цепочка журнала запусков, публикация участка журнала для пересчёта, каноническая сериализация строки журнала, пересчёт цепочки в гейте. Осталось: монотонный счётчик запусков и требование соседства номеров.
>
> ОСНОВАНИЕ ОПЕРАТОРА, дословно: «Вы там с критиком издеваетесь? Это - локальная среда. Никто никуда влезать не будет». Снятое защищало от того, кто правит журнал ЗАДНИМ ЧИСЛОМ, чтобы скрыть свой прогон, — то есть от противника, которого объявленный горизонт постройки прямо выносит за скоуп (индивидуальный граф оператора, многопользовательская безопасность вне первой версии). Оставшееся ловит то, что в этой среде действительно случается: лишний прогон по недосмотру, который сдвигает счётчик и рвёт соседство номеров.
>
> ГЛАВНОЕ В ЭТОМ РЕШЕНИИ — НЕ ЧТО СНЯТО, А КАК ЭТО БЫЛО НАЙДЕНО. Критик три круга подряд спрашивал «чем это доказывается», и разработка три круга подряд отвечала новой сущностью: сначала неизменяемость записи, потом счётчик, потом цепочка хэшей, потом публикация участка для пересчёта. Каждый ответ переносил вопрос на шаг назад вместо ответа на него, и ни разу не был задан вопрос «от кого мы защищаемся». Остановил оператор. Горизонт при этом был объявлен ещё на пред-ревью гейте — то есть данные для правильного решения лежали в канале с самого начала и не применялись.
>
> УСЛОВИЕ ВОЗВРАТА ВОПРОСА записано в текст спеки: если жанр поедет в среду без доверия — многопользовательскую или работающую без присмотра, — защита от подделки нужна снова, и решать придётся заново.

> **Translation (English), by the author's agent; not part of the record.**
>
> Operator's decision of 2026-08-04 in review 4ffd8b0e, a disputed fork (the server raised it itself on detecting the reopening of a closed finding).
>
> WHAT WAS REMOVED. From the mechanism proving that no foreign run slipped in between the channel check and the blind reading, the following were removed: the hash chain over the launch journal, the publication of a journal segment for recomputation, the canonical serialisation of a journal line, the recomputation of the chain at the gate. What remains: a monotonic run counter and the requirement that numbers be adjacent.
>
> THE OPERATOR'S GROUNDS, verbatim: "Are you and the critic making fun of me? This is a local environment. Nobody is going to break into anything." What was removed defended against someone editing the journal AFTER THE FACT to hide their run, that is, against an adversary the declared build horizon explicitly puts out of scope (the operator's individual graph; multi-user security outside the first version). What remains catches what actually happens in this environment: a stray run by oversight, which shifts the counter and breaks the adjacency of numbers.
>
> THE MAIN THING IN THIS DECISION IS NOT WHAT WAS REMOVED BUT HOW IT WAS FOUND. Three rounds running the critic asked "how is this proved", and three rounds running development answered with a new entity: first immutability of the record, then a counter, then a hash chain, then publication of a segment for recomputation. Each answer moved the question one step back instead of answering it, and not once was the question asked "whom are we defending against". The operator stopped it. The horizon, meanwhile, had been declared at the pre-review gate, so the data for the right decision had been in the channel from the start and went unused.
>
> THE CONDITION FOR REOPENING THE QUESTION is written into the spec: if the genre moves into an environment without trust, multi-user or unattended, protection against forgery is needed again and will have to be decided anew.

## Record 4 — Note `7e953ffc-a2ee-404f-b9ec-579968c0ad59` (excerpt)

Store: the author's private knowledge graph. Created: 2026-08-04T12:49:40Z.
Status at export: current. Written by: the development agent. Original
language: Russian. Quoted: the opening and the rule; the note's later
paragraphs (a second confirmation on 2026-08-05) are omitted.

> **Verbatim (Russian).**
>
> Когда критик спрашивает «чем это утверждение доказывается», честных ответов ровно два: УСИЛИТЬ МЕХАНИЗМ и СУЗИТЬ ЗАЯВЛЕНИЕ. Разработка по умолчанию тянется к первому, и это ошибка соразмерности, а не мысли: каждый новый механизм переносит вопрос на шаг назад вместо ответа на него.
>
> Наблюдённая цепочка из живого цикла: «запись неизменяема» → но она не доказывает, что в неё всё вписали → «есть монотонный счётчик» → но его append-only держится на слове → «журнал сцеплен хэшами» → но опубликованный хэш не с чем сверить → «публикуем участок журнала для пересчёта». Три круга, четыре сущности, и ни разу не задан вопрос, ОТ КОГО защищаемся. Остановил оператор одной репликой про то, что среда локальная.
>
> ПРАВИЛО. Прежде чем усиливать механизм, спросить: какой отказ он ловит — ошибку или злой умысел? И входит ли источник этого умысла в объявленный горизонт постройки? Если нет — правильный ответ сузить заявление до того, что среда оправдывает, и назвать вслух, от чего оно не защищает.

> **Translation (English), by the author's agent; not part of the record.**
>
> When the critic asks "how is this claim proved", there are exactly two honest answers: STRENGTHEN THE MECHANISM and NARROW THE CLAIM. Development reaches for the first by default, and that is an error of proportion, not of thought: each new mechanism moves the question one step back instead of answering it.
>
> The chain observed in a live cycle: "the record is immutable" → but that does not prove everything was written into it → "there is a monotonic counter" → but its append-only property rests on a promise → "the journal is hash-chained" → but there is nothing to check the published hash against → "we publish a journal segment for recomputation". Three rounds, four entities, and not once the question of WHOM we are defending against. The operator stopped it with one line about the environment being local.
>
> THE RULE. Before strengthening a mechanism, ask: which failure does it catch, an error or malice? And is the source of that malice inside the declared build horizon? If not, the right answer is to narrow the claim to what the environment justifies, and to say out loud what it does not defend against.

## Record 5 — Decision `3db1a2c5-33a6-431c-a189-aec10506d8fd`

Store: the author's private knowledge graph. Created: 2026-08-18T07:59:50Z.
Status at export: current. Written by: the development agent, recording the
operator's ruling of 2026-08-17 in review `bb73572b`. Original language:
Russian. Quoted: the `decision` and `provenance` fields.

> **Verbatim (Russian).**
>
> Решение оператора посреди ревью B.9 (2026-08-17), родившееся из его вопроса «от кого защита?», заданного ЧЕТЫРЕЖДЫ за одно ревью (граница холодности, привязка владельца, наблюдатель обесценивания, целостность файла) — все четыре раза ответ был «ни от кого из модели угроз». Самый частый грех критика — защитная машинерия против внемодельных противников, и каждый такой страх структурно конвертировался в касание оператора (security-находки типизированы ждать гейта). Три правила: (1) СТОЯЧАЯ РАМКА — несущее ядро промпта критика несёт модель угроз и записанные границы словами оператора; границы живут в РЕЕСТРЕ [redacted: implementation detail of the retired build]. (2) ОБЯЗАННОСТЬ КРИТИКА — security-находка обязана называть конкретного противника ИЗ МОДЕЛИ; внемодельная защита никогда не blocking, максимум — «запишите границу явно». (3) МАРШРУТИЗАЦИЯ — [redacted: implementation detail of the retired build]; ждёт оператора только genuinely НОВАЯ граница.
>
> [provenance:] Оператор, чат 2026-08-17 («Я за одно ревью могу несколько раз задать вопрос от кого защита. Это — самый частый грех критика»; пакет подтверждён «Да. Закрывай»).

> **Translation (English), by the author's agent; not part of the record.**
>
> The operator's decision in the middle of review B.9 (2026-08-17), born of his question "protection from whom?", asked FOUR TIMES in one review (the coldness boundary, owner binding, the devaluation observer, file integrity), and all four times the answer was "from no one in the threat model". The critic's most frequent sin is defensive machinery against out-of-model adversaries, and each such fear was structurally converted into a touch of the operator (security findings are typed to wait for the gate). Three rules: (1) A STANDING FRAME: the load-bearing core of the critic's prompt carries the threat model and the recorded boundaries in the operator's words; the boundaries live in a REGISTER [redacted: implementation detail of the retired build]. (2) THE CRITIC'S DUTY: a security finding must name a concrete adversary FROM THE MODEL; an out-of-model defence is never blocking, at most "record the boundary explicitly". (3) ROUTING: [redacted: implementation detail of the retired build]; only a genuinely NEW boundary waits for the operator.
>
> [provenance:] The operator, chat of 2026-08-17 ("In one review I can ask several times whom the protection is against. That is the critic's most frequent sin"; the package was confirmed with "Yes. Close it").

## Redactions

Two bracketed phrases in Record 5 replace the register and routing
mechanics of the August build, which the article says was thrown out; they
are omitted as machinery, not as anything sensitive. No project, machine or
person is named in any of the five records.

## What the records do and do not show

They show the operator's own words at the moment of ruling (Record 3,
Record 5), the threat model those rulings appeal to (Record 2), the waiver
that the "executable swapped for a malicious one" argument ended in
(Record 1), and the rule the series produced (Record 4). They do not show
the critic's and development's turns in the review channels; those live in
the private review store and are not exported.
