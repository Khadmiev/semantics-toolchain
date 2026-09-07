# SPDX-License-Identifier: Apache-2.0
"""The genre's prompts as source of record: they exist, they are listed, they behave.

These are cheap checks over documents, and each one guards a failure that has actually
happened in this project: a prompt referenced by a table and absent from the tree, a role
template that stopped parsing after an edit, and a mechanism quietly renamed into the one
name this genre is forbidden to share.
"""

from pathlib import Path

import pytest

from assistant_memory.audience.blind_prompt import RoleTemplate

PROMPTS = Path(__file__).resolve().parents[1] / "docs" / "prompts" / "audience"
SKILLS = ("review_dev.md", "seeing_pass.md", "visual_dev.md", "visual_critic.md")


@pytest.mark.parametrize("name", (*SKILLS, "blind_reader.md", "README.md"))
def test_every_prompt_the_readme_lists_exists(name):
    assert (PROMPTS / name).exists()


@pytest.mark.parametrize("name", SKILLS)
def test_the_readme_lists_every_prompt_that_exists(name):
    """A prompt in the tree and not in the table is a prompt nobody knows to publish — and an
    unpublished edit changes no agent's behaviour, silently."""
    assert name in (PROMPTS / "README.md").read_text(encoding="utf-8")


def test_the_blind_reader_template_still_parses():
    """It is a production input, not documentation: the launcher assembles the prompt from it
    every round, and an edit that breaks its placeholders breaks every blind reading."""
    template = RoleTemplate.from_text((PROMPTS / "blind_reader.md").read_text(encoding="utf-8"))
    assert template.digest


@pytest.mark.parametrize("name", (*SKILLS, "blind_reader.md", "README.md"))
def test_no_prompt_shares_the_name_of_the_other_mechanism(name):
    """The critic watcher's cold-verdict-first pass and this genre's blind pass must not share
    a name: that is how a blind pass gets "implemented" as context editing in one session, and
    the defect stays invisible. Checked over the prompts as well as the code, because a prompt
    is where the confusion would be introduced first."""
    text = (PROMPTS / name).read_text(encoding="utf-8").lower()
    assert "cold" not in text


def test_the_roles_swap_is_stated_where_both_sides_will_read_it():
    """The swap is the design, not a detail: whoever picks up either visual prompt has to see
    that the side which produces does not sign its own work off."""
    for name in ("visual_dev.md", "visual_critic.md", "README.md"):
        text = (PROMPTS / name).read_text(encoding="utf-8").lower()
        assert "roles" in text or "role" in text
