# Audience review — building the visual (skill)

> Source of record for the **audience-visual-dev** skill. The meaning has settled; you build
> the visual from it. In this phase you are DEVELOPMENT and the other side is your critic —
> the roles are the reverse of the meaning phase, on purpose, and the rule that survives the
> swap is that you do not sign your own work off.

Use this when the operator hands over a settled reader artefact to be turned into slides, a
rendered document, or any other form the audience will actually look at.

## What you are given, and what each piece is for

- **the reader artefact** — sections with permanent numbers. They are the addresses everything
  else in this cycle uses; keep them. One section is one addressable unit of the render;
- **the public part of the contract** — who the audience is, what they already know, what
  they are entitled to see. That last one is a **ceiling**, not a preference;
- **the parts declaration** — which parts are spoken and which are read. It decides layout
  more than taste does: text meant to be spoken is not text meant to be read.

You are not given the private part of the contract and do not need it.

## The two rules that are not style

**Do not add a fact the text does not carry.** A chart that says "it doubled" when no section
says so has introduced an assertion about reality that nobody recorded and nobody verified.
It will be found and it will be a floor-class finding, not a note. If the visual needs a
fact, the fact belongs in the text first — which means back to the meaning phase, not a
quiet addition here.

**Do not put real data on a slide as an image.** A screenshot of a live dashboard delivers to
the audience precisely what the ceiling of rights withholds, while every line of the text
stays inside it. This is the second floor class and it exists because it is invisible to
every text check.

Everything else is craft: does the picture belong to its section's subject, is the style one
style, is anything unreadable at the size it will be seen.

## What you hand back

A render and a **manifest**, and the manifest is not paperwork — it is what makes a finding
addressable and reproducible:

- one entry per section: the section identifier, the image file, its hash. **Both directions
  must close**: a section with no image is a section nobody will look at, and an image with
  no section is a slide that passed through no text check at all;
- the **version and hash of the source** you built from;
- the **build environment**: your builder's version and the fonts you had, with their hashes.
  The same source on another machine is a different render — fonts substitute, lines re-wrap,
  a slide overflows — so a finding raised against a render nobody can rebuild is a finding
  nobody can check. This is declared by you; the machine checks that it is there and not
  empty, and its truth rests on you saying it honestly.

## What happens to your work next

The critic looks at the render with the claim map and the contract in hand. Findings come
back keyed to the exact bytes they were raised against — so when you rebuild, old findings do
not silently carry onto pixels nobody has seen.

One consequence worth knowing before you start: **a fix that touches the text is not a visual
fix.** It is a new version of the source, and it costs a fresh blind reading, because what
the blind reader sees has moved. Layout, spacing, imagery, size — those are yours. Wording is
not, however small the change looks.
