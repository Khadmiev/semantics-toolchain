# Dossier 5 — Declared but unenforced (second echelon)

*The article:* "the critic's prompt assured it was locked in a read-only
sandbox while it in fact had full write access, so the boundary existed
only as text. Hence an aphorism earned in blood: a boundary described as an
intention is not a boundary."

## Context for the reader

On 2026-07-18, while diagnosing why the critic had started finding one or
two defects per review instead of eight to fifteen, the development agent
ran a live test of the headless critic and found that it loaded with a full
set of write tools for the production knowledge graph, while its prompt
told it "you have a read-only sandbox with no graph/API access". On a
harmless test prompt the critic behaved as a well-brought-up memory agent:
it read the conventions, searched, and tried to create a node. The write
did not go through only because the sandbox happened to cancel it.

The incident record was written that evening. Two days later a code review
(`91d8a139`, ten passes) landed the fix the article describes: the critic is
launched as a read-only sandbox by the shape of the command itself, the
launcher refuses to start without that pin, and the prompt was rewritten to
say what is true. The decision that closed that review is Record 2; its
final paragraph, the waiver on the identity of the executable, is quoted in
dossier 2 as the start of the invention series.

## Record 1 — Incident `db5b243d-52cf-41ec-90fb-ac684bff9702`

Store: the author's private knowledge graph. Created: 2026-07-18T22:40:29Z.
Status at export: current. Written by: the development agent. Original
language: Russian. Quoted: the `text`, `symptom` and `lesson` fields. A test
token and a session thread identifier are redacted; the fields that name a
machine, a local path and a tunnel host are not quoted.

> **Verbatim (Russian).** Redactions are marked `[redacted: reason]`; nothing
> else is altered.
>
> Headless-критик из review-watcher (codex exec) стартует с ПОЛНЫМ write-доступом к прод-графу assistant_memory, хотя промпт критика §6a прямо заявляет обратное: «you have a read-only sandbox with no graph/API access». Промпт декларирует границу, которую харнесс НЕ обеспечивает. Проявляется и в текущей stateless-схеме, и в любой будущей stateful — фикс общий.
>
> [symptom:] Live-тест 2026-07-18 (при проверке session-resume для stateful-критика): codex exec (модель gpt-5.5) на безобидном промпте «Remember this token: [redacted: test token]» выполнил связную работу memory-агента — mcp_tool_call assistant_memory.conventions {} -> assistant_memory.search {query:[redacted: test token]} -> assistant_memory.create_node {type:Note, label:'Secret token for later'}. Запись НЕ прошла только потому, что песочница/approval её отменили ('I couldn't store it because the memory write was cancelled') — по везению, не по дизайну. thread [redacted: session thread id].
>
> [lesson:] «never write» в промпте — не граница безопасности, когда config+MCP дают роль и write-инструменты. Граница = capability: read-only sandbox codex денаит и FS-, и MCP-запись независимо от approval_policy, при этом пропускает чтение — переносимый примитив read-all/write-nothing для любого codex-подагента. Плюс: свободную shell-строку под shell=True безопасностью-парсингом не защитить (класс обходов бесконечен) — правильный фикс = argv + shell=False, где проверенное == исполняемое.

> **Translation (English), by the author's agent; not part of the record.**
>
> The headless critic launched by the review watcher (codex exec) starts with FULL write access to the production graph of assistant_memory, while section 6a of the critic's prompt states the opposite outright: "you have a read-only sandbox with no graph/API access". The prompt declares a boundary the harness does NOT enforce. It shows in the current stateless scheme and in any future stateful one; the fix is common to both.
>
> [symptom:] Live test of 2026-07-18 (while checking session resume for a stateful critic): codex exec (model gpt-5.5) on the harmless prompt "Remember this token: [redacted: test token]" did the coherent work of a memory agent: mcp_tool_call assistant_memory.conventions {} -> assistant_memory.search {query: [redacted: test token]} -> assistant_memory.create_node {type: Note, label: 'Secret token for later'}. The write did NOT go through only because the sandbox/approval cancelled it ('I couldn't store it because the memory write was cancelled'), by luck, not by design. thread [redacted: session thread id].
>
> [lesson:] "never write" in a prompt is not a security boundary when config plus MCP hand out the role and the write tools. A boundary is a capability: codex's read-only sandbox denies both filesystem and MCP writes regardless of approval_policy, while letting reads through; a portable read-all/write-nothing primitive for any codex sub-agent. Also: a free-form shell string under shell=True cannot be protected by security parsing (the class of bypasses is endless); the right fix is argv plus shell=False, where what was checked is what gets executed.

## Record 2 — Decision `0b4fb6ee-d9d8-4226-858d-d0f71a3def46` (excerpt)

Store: the author's private knowledge graph. Created: 2026-07-20T12:35:31Z.
Status at export: current. Written by: the development agent, at the close
of review `91d8a139`. Original language: Russian. Quoted: the first three
paragraphs of the decision text; the waiver paragraph is quoted in dossier
2 and the operational remainder is omitted.

> **Verbatim (Russian).** Redactions are marked `[redacted: reason]`; nothing
> else is altered.
>
> Критик оркестрованного ревью (review-orchestration watcher) исполняется как read-only codex и не может писать никуда, кроме своего stdout-отчёта — это обеспечено ХАРНЕССОМ, не промптом. Реализовано в ревью 91d8a139 (code-mode, converged 2026-07-20 за 10 проходов; ветка review/critic-read-only, коммиты 95fdf83..791aacf, смёржено в main 693c85f).
>
> Что сделано: (1) watcher.py codex_invoker исполняет ВАЛИДИРОВАННЫЙ argv с shell=False (было — shell-строка под shell=True); guard _codex_cmd_is_read_only и инвокер делят одну токенизацию, поэтому «что проверено, то и исполняется» — класс shell-инъекции/экспансии убран by construction. (2) main() ОТКАЗЫВАЕТСЯ стартовать, если команда критика не пиннит read-only во ВСЕХ sandbox-директивах (структурная проверка: это должен быть `codex exec`, все `-s`/`--sandbox`/`-c`/`--config` == read-only, без danger-full-access/bypass); break-glass `--allow-unsafe-sandbox` с громким stderr-варнингом. (3) critic.md §6a переписан честно.
>
> Механизм (проверено живьём, codex-cli 0.144.6): под `-s read-only` харнесс codex денаит ЛЮБУЮ запись — файловую И любой MCP write-тул — НЕЗАВИСИМО от approval_policy (create_node отменяется даже при approval_policy=never), при этом ЧТЕНИЕ репозитория и графа проходит. Это закрывает Incident db5b243d.

> **Translation (English), by the author's agent; not part of the record.**
>
> The critic of the orchestrated review (the review-orchestration watcher) runs as read-only codex and cannot write anywhere except its own stdout report; this is enforced by the HARNESS, not by the prompt. Implemented in review 91d8a139 (code mode, converged 2026-07-20 in 10 passes; branch review/critic-read-only, commits 95fdf83..791aacf, merged into main as 693c85f).
>
> What was done: (1) the watcher's codex invoker executes a VALIDATED argv with shell=False (it used to be a shell string under shell=True); the read-only guard and the invoker share one tokenisation, so "what was checked is what runs"; the class of shell injection/expansion is removed by construction. (2) main() REFUSES to start if the critic's command does not pin read-only in ALL sandbox directives (a structural check: it must be `codex exec`, every `-s`/`--sandbox`/`-c`/`--config` equal to read-only, no danger-full-access/bypass); a break-glass `--allow-unsafe-sandbox` with a loud stderr warning. (3) Section 6a of critic.md rewritten honestly.
>
> The mechanism (verified live, codex-cli 0.144.6): under `-s read-only` the codex harness denies ANY write, filesystem AND any MCP write tool, REGARDLESS of approval_policy (create_node is cancelled even under approval_policy=never), while READING the repository and the graph goes through. This closes Incident db5b243d.

## Redactions

Record 1: one test token and the identifier of the critic's session thread
(an identifier of a live session at the vendor, removed out of caution).
The record's other fields (root cause, severity, proposed fix) name a
machine, a local configuration path and a tunnel host and are not quoted.
Record 2: none.

## What the records do and do not show

They show the gap between the prompt's declaration and the launcher's
capability on the day it was found, with the live trace that found it, and
the fix that moved the boundary from text to command shape two days later.
The September build carries the same rule in `src/review_harness/readonly_guard.py`;
the watcher named in Record 2 belongs to the retired build and is not in
this repository.
