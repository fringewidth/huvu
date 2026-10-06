"""Each source as a Hermes skill, and act, which reaches the source actions
Hermes doesn't see."""

from __future__ import annotations

import stat

import pytest

from huvu import skills
from huvu.spec import Caller
from huvu.tools import ToolError


def test_a_source_becomes_a_skill(kernel):
    text = skills.render(kernel.registry.spec("home"))
    assert text.startswith("---\nname: huvu-home\ndescription: \"A fake house. Load before")
    assert "Search for a light before turning it on." in text
    assert "- `home_turn_on(entity_id)`: Turns a light on." in text
    assert "- `home_unlock()`: Unlocks the front door. (always needs a person's tap)" in text
    assert "- `home.light[entity_id]`: State of a light (string, pushed)" in text
    assert "- `home.unlock`: The door was unlocked. Fields: method (string), person (string)." in text


def test_skills_are_written_read_only_and_stale_ones_go(kernel, tmp_path):
    folder = tmp_path / "skills"
    (folder / "huvu-gone").mkdir(parents=True)
    (folder / "huvu-gone" / "SKILL.md").write_text("old")
    (folder / "someone-elses").mkdir()
    skills.write_all(kernel.registry.specs(), folder)
    written = folder / "huvu-home" / "SKILL.md"
    assert written.read_text() == skills.render(kernel.registry.spec("home"))
    assert not written.stat().st_mode & stat.S_IWUSR
    assert not (folder / "huvu-gone").exists() and (folder / "someone-elses").exists()
    # Writing again over a read-only file works.
    skills.write_all(kernel.registry.specs(), folder)
    assert {p.name for p in folder.iterdir()} == {"huvu-home", "huvu-huvu", "huvu-clock", "someone-elses"}


async def test_act_reaches_source_actions(kernel, home, family):
    me = Caller(kind="hermes", user="me")
    done = await kernel.tools.call("act", {"name": "home_turn_on", "args": {"entity_id": "light.porch"}, "reason": "asked"}, me)
    assert done.data == {"done": "turn_on"} and home.acted == [("turn_on", {"entity_id": "light.porch"})]
    with pytest.raises(ToolError, match="Did you mean home_turn_on"):
        await kernel.tools.call("act", {"name": "home_turn_onn"}, me)
    with pytest.raises(ToolError, match=r"needs entity_id; it is home_turn_on\(entity_id, reason\?\)"):
        await kernel.tools.call("act", {"name": "home_turn_on"}, me)
    with pytest.raises(ToolError, match="takes no colour"):
        await kernel.tools.call("act", {"name": "home_turn_on", "args": {"entity_id": "x", "colour": "red"}}, me)
    # A kernel tool isn't an action.
    with pytest.raises(ToolError, match="no action named 'state_get'"):
        await kernel.tools.call("act", {"name": "state_get"}, me)


async def test_search_says_how_to_work_a_thing(kernel):
    found = await kernel.tools.call("state_search", {"query": "porch"}, Caller(kind="hermes"))
    assert found.data[0].actions == ["home_turn_on"]


def test_skills_are_linked_into_the_hermes_folder(kernel, tmp_path):
    specs = kernel.registry.specs()
    folder, links = tmp_path / "data" / "skills", tmp_path / "hermes-skills"
    skills.write_all(specs, folder)
    links.mkdir()
    (links / "my-own-skill").mkdir()
    (links / "huvu-gone").symlink_to(tmp_path, target_is_directory=True)
    (links / "huvu-home").symlink_to(tmp_path, target_is_directory=True)
    skills.link_all(specs, folder, links)
    assert (links / "huvu-home").resolve() == (folder / "huvu-home").resolve()
    assert (links / "huvu-home" / "SKILL.md").read_text().startswith("---\nname: huvu-home")
    assert not (links / "huvu-gone").exists() and (links / "my-own-skill").is_dir()
    # A wiped data folder and a fresh start put it all back.
    import shutil

    skills._writable(folder)
    shutil.rmtree(folder)
    skills.write_all(specs, folder)
    skills.link_all(specs, folder, links)
    assert (links / "huvu-clock" / "SKILL.md").exists()
