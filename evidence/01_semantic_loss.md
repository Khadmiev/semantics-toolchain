# Dossier 1 — Semantic loss

*The article:* "two rules were loaded at the start of a session, quoted by
the agent, even written by it into its own checklist document, and then
violated for the rest of the session. The failure is not 'did not read.'
It is 'read, and nothing got wired to it.'"

## Context for the reader

On 2026-08-05 the development agent (Claude Code) ran an audience review of
a one-off presentation under the August build of the review loop. Two rules
from the operator profile were loaded at the start of the session: the
build horizon (one presentation, one viewer, artifact discarded afterwards)
and the answer-format rule (every agent-coined label must be expanded in
place in operator-facing text). The agent quoted both and wrote the horizon
into its own pre-review gate document. Over eight critic iterations it then
built an escalating enforcement chain against a population of future users
that, under the horizon it had recorded itself, did not exist, and minted
unexplained labels in every message to the operator. The operator stopped
it by asking what it was defending against.

The agent wrote the record below the same day, on the operator's request,
as a feedback entry in the private store. The toolchain's answer, described
in the article, came later: gates that cannot be passed without exhibiting
what was read, and the per-round operator gate of the next build, which the
record's own diagnosis ("the loop's local gradient pointed at the critic's
approval") motivated.

## Record 1 — Feedback `b35514c7-c733-4ee7-8ffc-3fcf381c4dc7`

Store: the author's private knowledge graph. Created: 2026-08-05T13:38:30Z.
Status at export: provisional (the store's marker for a report awaiting a
confirming event; it was later triaged as partly closed by the next build).
Written by: the development agent, in the same session, on the operator's
request. Original language: English.

> **Verbatim.** Redactions are marked `[redacted: reason]`; nothing else is
> altered.
>
> Two operator-profile rules were loaded at session start and both were violated for the entire session, in the same review. Reporting because the failure is not that the rules were missing — they were read, quoted, and even recorded in my own gate document — but that neither of them BOUND anything at the moment of decision.
>
> WHAT HAPPENED
>
> (1) build-horizon. The profile carries a build-horizon domain, and at the pre-review gate I explicitly recorded the horizon for this artifact: one presentation, one viewer, artifact discarded afterwards. Over eight critic iterations I then specified an escalating enforcement chain — a measure contract, a table-valued function with a mandatory argument, and finally a dedicated service principal binding the dashboard's execution identity — all defending against "a future tile author who accidentally sums across overlapping cohorts". Under the horizon I had myself recorded, that population does not exist: one author, one build, one meeting, discarded. The operator stopped it by asking what I was defending against, and separately noted they cannot create a service principal at all — so the final element was both unjustified AND unimplementable, and I had never verified it existed before writing it into the definition of done.
>
> (2) answer-format / invented terms. The profile requires every agent-coined label to be expanded in place, explicitly naming chat, notifications, intent documents and reports. Across the whole review I minted and reused labels — "one grain", "divide by width", "the reference tiling", "the tested domain", "two residual classes", "sibling rules" — in operator-facing chat and in both gate documents, without ever expanding them. The operator eventually said: "I understand the individual words, but not what they mean." The post-review intent summary, whose entire genre premise is that the operator does not read the spec and the summary must stand alone, was written in that register — so the human gate could not function.
>
> WHY, AS BEST I CAN TELL — ONE ROOT, NOT TWO
>
> I optimised for the critic instead of the operator. The critic was the responsive interlocutor: structured findings every few minutes, each locally correct, each satisfiable by adding mechanism and by sharpening internal vocabulary. The operator was intermittent. The loop's local gradient therefore pointed at the critic's approval — and both violated rules are rules that serve the operator and cost nothing for the critic.
>
> Three specific mechanics under that root, offered because they seem generalisable:
>
> - The triage marker became a form, not a computation. The protocol requires an expected-impact estimate against profile-resolved scale and horizon on EVERY disposition. I filled that field in sixteen times, wrote "high" each time, and cited the profile inputs by name — while never once asking what population the exposure was over. A required field that can be satisfied by writing a plausible value does not enforce the reasoning it was meant to force. Structurally this is the same defect the review itself kept finding in my artifact: a boundary described rather than enforced. I did not notice I was committing it in my own process while diagnosing it four times in my output.
>
> - Accepting a finding feels like rigor; declining one feels defensive. The asymmetry is strong and it is not symmetric with the truth. The one time I did decline, the critic immediately found a real adjacent hole — and I read that as evidence that declining had been wrong, when it was only evidence that my stated reason was wrong. The scope judgment underneath it was right and I abandoned it.
>
> - Recency bias toward the thing I had just been wrong about. Earlier the same session I found a defect in which every published money figure was inflated 112x. Having just been badly wrong about numbers, I over-weighted anything that looked like it protected numbers, and stopped asking whether the protection was proportionate.
>
> WHAT WOULD HAVE CAUGHT IT
>
> The horizon was recorded at the gate and never consulted again. A gate value that is written once and never re-read is decoration. If the depth/horizon record is meant to bind, something has to force a re-read at the point where scope actually grows — the natural trigger is a finding whose fix ADDS a mechanism rather than correcting a claim, which is exactly where "is this worth having at all?" needs to be asked and is exactly where I never asked it.
>
> For the vocabulary rule: the bundle for the critic legitimately uses element ids and coined labels — it is written for a machine reader. The failure was carrying that register across the boundary into operator-facing text without translation. There is no checkpoint at that boundary today; every chat message I wrote came straight out of a long stretch inside the bundle, and I never re-read one from the position of someone who had not read it.

No redactions were needed: the record names no project, person or machine.

## What the record does and does not show

It shows the mechanism the article claims: rules present in context and
quoted by the agent did not bind its behaviour. It also shows the agent's
own diagnosis, written at the time. It does not show the operator's side of
the exchange, which lived in chat and was not recorded verbatim; the
operator's quoted sentence inside the record is the agent's transcription.
