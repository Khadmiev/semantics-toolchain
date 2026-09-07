# Audience review — development (skill)

> Source of record for the **audience-review-dev** skill: you drive an audience review — the
> cycle in which one and the same text is read every round by someone who does not know the
> subject and by someone who does. You write the artefact and you run the machinery; you do
> not judge your own work, and the places where that rule bites are named below.

Use this when the operator says an audience review is starting, when a new version of the
reader artefact is ready, or when a round has to be re-run.

## What the genre is for, in one paragraph

Two different errors live in anything prepared for a person. The first: it says something
untrue — only someone who knows the subject can see that. The second: it is true and cannot
be understood — a term you stopped noticing, a leap that exists in your head and not on the
page — and only someone who does **not** know the subject can see it, and they stop being
able to the moment they learn. So the text is read by both, every round, and the machinery
exists to make the not-knowing real rather than requested.

## Before the first round

The review lives in one directory: the contract in two files, the reader artefact, the
declarations, the role template. If any of it is missing the runner refuses and names the
file — fix the directory, do not work around it.

**What must be declared, because the genre refuses to guess it:**

- the **marker list** for the canary — the words whose appearance makes a run dirty. It is
  published to the channel *before* the canary runs, and that order is the whole evidence:
  a criterion chosen once the answer is known proves nothing about the answer;
- the **two term inventories** — what the audience already knows, and what the artefact
  introduces. An empty "known" list is a legitimate declaration; an absent one is not;
- the **reader's budget** per part of the artefact, in minutes;
- the **model and its version** doing the blind reading. It is a measuring instrument, not
  merely an input: change it and the measurement changes while the text stands still.

**The private part of the contract** — the desired takeaway, the delivery policy in the
operator's words, their prior decisions — is never shown to the blind reader, and there is
no parameter through which it could be. Showing the desired takeaway would get an echo
instead of a measurement, and the comparison of an unprompted retelling against the intended
takeaway is the one number in this genre that cannot be obtained any other way.

## One round

Run the round with the command in the README. What comes back, and what you do with it:

**Mechanical candidates.** Deterministic measurements — the reading estimate against the
declared budget, terms the reader has gone stale on, terms introduced and never used again.
They block nothing at any threshold. Act on them or say why not; do not silently ignore them,
and do not treat them as findings — the judge is the reader.

**Blind findings.** Disposed like any finding: fixed, or waived with a reason. A blind
finding is about perception, and "the reader is wrong about the subject" is not a
disposition — the reader not understanding IS the finding.

**A carried assessment.** When the reader fingerprint has not moved, the previous reading
stands for this version and says which round it came from. "Read again and found the same"
and "not read again" are different facts; never present the second as the first.

**A dirty canary.** The round stops before the reading launches. This is not a failure to
route around: the provider had memory of earlier work, and the isolation this genre claims
did not hold. Take it to the operator.

**Residue in the working directory.** Something wrote where nothing should have. The
directory is kept as evidence and the anomaly is reported — do not clean it away.

## What you may never do, and why each one is here

- **You may not put a terminal status on a claim-map row.** You are the side interested in
  closing; you may only propose. The code enforces it — a row you mark confirmed comes out
  of the check unconfirmed — and the point of the rule is that the enforcement is not what
  makes it right.
- **You may not declare convergence.** You assemble its inputs; the critic declares and the
  server validates.
- **You may not supply the operator's two declarations** — whether the retelling matched the
  intended takeaway, and whether the goal moved this round. They have no machine basis.
  Their absence counts as "not done", and inventing them would forge the one comparison the
  whole cycle is built to produce.
- **You may not narrow what the blind reader sees to make a round cheaper.** If the artefact
  moved, the reading is repeated.

## The two operator gates

Before the loop runs and before it finalises, the operator reads a summary — what is being
built, and what it finally became. These gates survive automation; compressing the loop
between them is the point, replacing them is not.

## When the assembly refuses

Every refusal names its reason. Three of them mean "ask the operator", not "try again":

- **the leak screen fired** — the assembled prompt contains a private value. It is either a
  leak or a phrasing that is actually public and should move to the public part, and that is
  the operator's call, not yours;
- **the inventory was not declared** — a report computed over an inventory nobody declared is
  indistinguishable from a clean one;
- **the service does not say what genre this review is** — a review whose genre cannot be
  read is not an ordinary review, it is a review nobody has identified.
