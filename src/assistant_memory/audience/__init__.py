# SPDX-License-Identifier: Apache-2.0
"""Audience review — the runner side of the genre (spec 2026-08-03, converged 2026-08-04).

STEP 1 — the binding. A launch profile, the canary that gates a run, and the run record
that joins them ship together on purpose: separately each is checkable and together they
proved nothing — a clean canary in one launch would silently count as isolation for a
different reading, and the report would look flawless. The binding is the point.

- ``profile`` — the blind reader's launch profile and its fingerprint, taken over the
  CONTENTS of the settings directory (not its path: the path stays the same when the
  settings are swapped or the account is re-authorised).
- ``journal`` — the single sanctioned launcher's append-only launch log, kept OUTSIDE the
  settings directory because the read-only sandbox forbids the blind reader to write, not
  to read, and a log of project runs in its own directory is one more channel of knowledge.
- ``records`` — one immutable record per model run (canary / blind / visual), and the
  creditability check over a linked pair.

STEP 2 — the contract, and the prompt built from it. What the reader is shown stops being
a paste of a markdown file and becomes a closed structure with a screen over the result.

- ``contract`` — the public part (a closed list of six fields), the private part (its own
  structure, its own file), and the ledger that hands out ``C-N`` identifiers and never
  takes one back.
- ``categories`` — the finding categories DERIVED from the contract rather than fixed,
  plus the version that makes two reports comparable by composition.
- ``blind_prompt`` — assembly from the public part alone, line-wise substitution, and the
  screen that blocks a prompt containing private values and asks the operator.

STEP 3 — the reader artefact and genre mode.

- ``sections`` — parts, ``S-N`` sections and the reader fingerprint that gates a re-read.
- ``ledger`` — the one implementation of "a number is issued once and never reused", shared
  by ``C-N``, ``S-N`` and ``M-N``.
- ``planner`` — which passes an iteration runs, and what must hold before convergence.

STEP 4 — the claim map.

- ``claim_map`` — one row per assertion the artefact makes, a four-state automaton with a
  named owner per state, and a confirmation bound to a triple so that an edit to the quote,
  the presentation form or the rounding step voids it.

STEP 5 — the mechanical layer.

- ``mechanical`` — deterministic measurement that spends no quota and blocks nothing: spoken
  text timed against its slot, a document measured by volume AND by returns to terms, where
  volume is the floor of the estimate and stale returns push it up.

STEP 6 — the visual pass.

- ``visual`` — the render addressed section by section, a pass keyed to the exact bytes it
  looked at, and the derived difference between a visual-layer fix and a new version of the
  source. A SEEING pass: none of the blindness machinery applies to it.

THE TWO MODEL ROLES SWAP BETWEEN PHASES (operator decision 2026-08-05). While the meaning is
being built, one side writes the artefact and the other criticises it; once the meaning is
settled, the finished text goes to the side that builds the VISUAL, and the first side
becomes the critic of that visual. What holds across both phases is the rule that survives
everywhere in this genre: the side that produces does not sign its own work off.

NOTHING HERE IS TIED TO A KIND OF ARTEFACT. The genre reads whatever can be presented as
text in sections; the role template is an assembly input, so another kind of artefact means
another template, not another mechanism.

What deliberately does NOT live here: posting to the review channel. The launcher PRODUCES
a record; a thin adapter sends it. That split is what lets the mechanism be tested without
a live review, and it keeps the genre's transport decision (everything rides inside
existing message kinds) out of the runner.
"""

from assistant_memory.audience.blind_prompt import (
    AssembledPrompt,
    AssemblyRefused,
    RoleTemplate,
    assemble_blind_prompt,
    prepare_blind_prompt,
    screen_for_private,
)
from assistant_memory.audience.categories import CategorySet, derive_categories
from assistant_memory.audience.claim_map import (
    ClaimIdLedger,
    ClaimMapError,
    ClaimRow,
    ClaimStatus,
    Party,
    PresentationForm,
    SimplificationPassport,
    attest,
    blocking_rows,
)
from assistant_memory.audience.contract import (
    AudienceContract,
    ContractError,
    ContractIdLedger,
    ContractItem,
    OperatorFlag,
    PrivateContract,
    PrivateField,
    PublicContract,
    PublicField,
    load_contract,
)
from assistant_memory.audience.journal import JournalEntry, LaunchJournal
from assistant_memory.audience.mechanical import (
    MechanicalRefused,
    MechanicalReport,
    MechanicalThresholds,
    TermInventory,
    measure,
)
from assistant_memory.audience.profile import LaunchProfile, ProfileFingerprint
from assistant_memory.audience.records import (
    CreditVerdict,
    RunKind,
    RunOutcome,
    RunRecord,
    credit_blind_reading,
)
from assistant_memory.audience.visual import (
    RenderedSlide,
    RenderManifest,
    VisualCategory,
    VisualFinding,
    VisualRefused,
    classify_change,
    manifest_refusals,
    visual_record,
)

__all__ = [
    "AssembledPrompt",
    "AssemblyRefused",
    "AudienceContract",
    "CategorySet",
    "ClaimIdLedger",
    "ClaimMapError",
    "ClaimRow",
    "ClaimStatus",
    "ContractError",
    "ContractIdLedger",
    "ContractItem",
    "CreditVerdict",
    "JournalEntry",
    "LaunchJournal",
    "LaunchProfile",
    "MechanicalRefused",
    "MechanicalReport",
    "MechanicalThresholds",
    "OperatorFlag",
    "Party",
    "PresentationForm",
    "PrivateContract",
    "PrivateField",
    "ProfileFingerprint",
    "PublicContract",
    "PublicField",
    "RenderManifest",
    "RenderedSlide",
    "RoleTemplate",
    "RunKind",
    "RunOutcome",
    "RunRecord",
    "SimplificationPassport",
    "TermInventory",
    "VisualCategory",
    "VisualFinding",
    "VisualRefused",
    "assemble_blind_prompt",
    "attest",
    "blocking_rows",
    "classify_change",
    "credit_blind_reading",
    "derive_categories",
    "load_contract",
    "manifest_refusals",
    "measure",
    "prepare_blind_prompt",
    "screen_for_private",
    "visual_record",
]
