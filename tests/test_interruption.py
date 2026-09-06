"""
Acceptance test for the hard voice problem: Interruption & recovery.

This is the repeatable, offline test referenced in RIME_EVIDENCE.md.
It does not require a microphone, LiveKit room, or any API key -- it
exercises the turn-fencing logic in recipe_state.py directly, which is
the same logic agent.py wires into the live voice session.

Run with:  pytest tests/test_interruption.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipe_state import RecipeState  # noqa: E402


PASTA_STEPS = [
    "Bring a large pot of salted water to a boil.",
    "Add the pasta and cook for nine minutes, stirring occasionally.",
    "While the pasta cooks, mince two cloves of garlic.",
    "Drain the pasta, reserving one cup of the cooking water.",
    "Toss the pasta with the garlic, olive oil, and reserved water.",
]


def test_normal_flow_advances_in_order():
    """Baseline: with no interruptions, steps advance one at a time and
    each advance request is honored because it always matches the
    current turn."""
    state = RecipeState(PASTA_STEPS, recipe_name="Garlic Pasta")
    assert state.current_step_index() == 0

    for expected_next_index in range(1, len(PASTA_STEPS)):
        turn = state.new_turn(reason="user_says_next")
        advanced = state.advance_step(tagged_turn_id=turn)
        assert advanced is True
        assert state.current_step_index() == expected_next_index


def test_barge_in_stops_stale_step_advance():
    """Stress case #1: the agent has decided to advance to the next step
    (tagged with turn N), but the user barges in with an unrelated
    question before that advance is applied. The advance must be
    rejected -- the recipe must NOT silently skip ahead."""
    state = RecipeState(PASTA_STEPS)
    turn_n = state.new_turn(reason="agent_finishing_step_0")

    # User barges in before the agent's own advance-to-next-step request
    # is processed.
    state.new_turn(reason="user_barge_in")

    # The stale advance request (tagged with turn_n) arrives late.
    advanced = state.advance_step(tagged_turn_id=turn_n)

    assert advanced is False, "Stale step-advance must be rejected"
    assert state.current_step_index() == 0, "Recipe must not skip ahead"

    trail = state.audit_trail()
    assert any(e["event"] == "stale_advance_rejected" for e in trail)


def test_barge_in_discards_stale_tool_result():
    """Stress case #2: the agent kicks off a slow tool call (e.g. 'how
    much salt is a pinch in grams?') under turn N. Before it resolves,
    the user interrupts and asks something else entirely, moving the
    conversation to turn N+1. When the slow tool result finally comes
    back, it must be discarded -- it must never be spoken, because it
    would answer a question the user is no longer asking."""
    state = RecipeState(PASTA_STEPS)

    turn_of_lookup = state.new_turn(reason="user_asks_about_salt")
    # Simulate the tool call being "in flight" here (in agent.py this is
    # an actual asyncio call to a slow lookup / API).

    # User interrupts before the slow tool call resolves.
    state.new_turn(reason="user_barge_in_different_question")

    # The slow tool call finally resolves and tries to speak its answer.
    late_result = state.fence_tool_result(
        tagged_turn_id=turn_of_lookup,
        result="A pinch of salt is about 0.3 grams.",
    )

    assert late_result is None, "Stale tool result must be fenced, not spoken"

    trail = state.audit_trail()
    discarded = [e for e in trail if e["event"] == "stale_tool_result_discarded"]
    assert len(discarded) == 1
    assert discarded[0]["discarded_result"] == "A pinch of salt is about 0.3 grams."


def test_fresh_tool_result_after_interruption_is_still_spoken():
    """Sanity check: fencing must not be overly aggressive. If the user
    interrupts, asks a NEW question, and that new question's tool call
    resolves under the CURRENT turn, the answer must go through."""
    state = RecipeState(PASTA_STEPS)

    state.new_turn(reason="user_asks_about_salt")
    turn_after_interrupt = state.new_turn(reason="user_barge_in_new_question")

    result = state.fence_tool_result(
        tagged_turn_id=turn_after_interrupt,
        result="Nine minutes is the standard time for this pasta shape.",
    )

    assert result == "Nine minutes is the standard time for this pasta shape."


def test_recipe_never_regresses_past_last_step():
    state = RecipeState(PASTA_STEPS)
    for _ in range(len(PASTA_STEPS) + 3):
        turn = state.new_turn()
        state.advance_step(tagged_turn_id=turn)
    assert state.current_step_index() == len(PASTA_STEPS) - 1
