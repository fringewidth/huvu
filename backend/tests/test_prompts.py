"""prompts.py holds every text a model reads; these pin the ones built at
run time, word for word."""

from __future__ import annotations

from huvu import prompts


def test_the_situation_block_for_a_person_and_for_a_shared_device():
    mine = prompts.situation("Thu 2026-10-08 09:00", "Hrishik", None, [("huvu.mode", "normal", "now")], 0)
    assert mine == "Now: Thu 2026-10-08 09:00\nYou are helping: Hrishik\nhuvu.mode = normal (now)\nPending approvals: none"
    tv = prompts.situation("Thu 2026-10-08 09:00", None, ("Home", "Me (me), Mom (mom)"), [], 2)
    assert tv == (
        "Now: Thu 2026-10-08 09:00\n"
        "You are serving Home from a shared device; nobody in particular is speaking. "
        "The people who use it: Me (me), Mom (mom). If someone says who they are, call people_identify.\n"
        "Pending approvals: 2"
    )


def test_notes_ride_with_an_ask():
    assert prompts.huvu_note("09:00", "you were recognized") == "(huvu, 09:00: you were recognized. Not a request, no reply needed.)"
    assert prompts.action_note("Thu 2026-10-08 09:00", "opened the live feed balcony") == (
        "(Owner action in the app, Thu 2026-10-08 09:00: opened the live feed balcony. Not a request, no reply needed.)"
    )


def test_the_guide_fits_a_hermes_prompt_section():
    assert len(prompts.GUIDE) <= 4000
