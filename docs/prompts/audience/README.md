# Audience review — prompts / skills (source of record)

The prompts that drive an audience review: the cycle in which one and the same text is read
every round by someone who does **not** know the subject and by someone who does. These
files are the **source of record**; at deploy they install as their runtime forms.

| file | role | phase | runtime form |
| --- | --- | --- | --- |
| [review_dev.md](review_dev.md) | development — drives the review | meaning | skill, interactive agent |
| [blind_reader.md](blind_reader.md) | the blind reader | meaning | prompt fed to the isolated runner |
| [seeing_pass.md](seeing_pass.md) | the seeing pass | meaning | skill, headless critic |
| [visual_dev.md](visual_dev.md) | development — builds the visual | visual | skill, headless agent |
| [visual_critic.md](visual_critic.md) | critic of the visual | visual | skill, interactive agent |

**A file edited without republishing its Skill node has not shipped.** These files and the
published graph Skill nodes are two live forms of one text; editing the file alone changes
no agent's behaviour, and the failure is silent — the loop keeps running the old words.

## The roles swap between phases, and that is the design

**Meaning phase.** One side writes the artefact and drives the review; the other criticises
it. **Visual phase.** Once the meaning has settled, the finished text goes to the side that
**builds the visual**, and the first side becomes the **critic of that visual**.

The reference binding is: interactive agent writes the meaning and criticises the visual; the
headless one criticises the meaning and builds the visual. The binding is configuration; the
rule that survives it is not — **the side that produces never signs its own work off.**

## The blind reader is delivered by PROMPT, and deliberately not as an installed skill

Its role text rides in the prompt the launcher assembles, not as a file inside the blind
profile. Two reasons, both load-bearing:

- everything inside that profile is one more channel of knowledge to a reader whose whole
  value is not having any;
- the role text is part of the **reader fingerprint** — the thing that decides whether a
  previous reading may still stand for the current version. A role living in a file the
  fingerprint does not cover could change silently, and a carried-forward reading would then
  pass a reading made under other instructions off as an assessment of this version.

The graph node for it is therefore the canonical STORE of the text, and the launcher is the
only thing that delivers it.

## What runs the mechanism

One command per round, driven by the development skill:

```
python -m assistant_memory.audience.run --review-dir <dir> --base-url <url> \
    --review-id <id> --token-file <file> --artifact-seq <n> --iteration <n>
```

It refuses before spending anything it can refuse for free, runs the free mechanical layer
first, decides whether the blind reading must be repeated, publishes the marker list before
the canary, and never declares convergence — that is the critic's to declare and the server's
to validate.

Exit codes: `0` the round completed (credited, or legitimately carried) · `1` refused before
spending, or the pair was not credited · `2` the operator is needed.
