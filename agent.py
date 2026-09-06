"""
Kitchen Copilot -- a hands-free cooking voice assistant built for the
Rime x DataForge hackathon.

Hard voice problem: Interruption & recovery.

  A cook's hands are dirty or full. They need to interrupt the agent
  mid-instruction ("wait, how much salt?") without the agent talking
  over them, without a slow lookup answering the wrong question later,
  and without the recipe silently skipping or repeating a step.

Rime provides the spoken output (see README.md / RIME_EVIDENCE.md for
the exact model/speaker/lang/endpoint/transport used in the judged demo).
The turn-fencing logic that solves the hard problem lives in
recipe_state.py and is unit-tested independently in tests/test_interruption.py.

Run locally:
    python agent.py dev
See README.md for full setup instructions and required environment
variables.
"""

from __future__ import annotations

import asyncio
import logging
import random

from dotenv import load_dotenv

from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import groq, rime, silero

from fixtures.garlic_pasta import GARLIC_PASTA
from recipe_state import RecipeState

load_dotenv()

logger = logging.getLogger("kitchen-copilot")
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Simulated "slow" lookup used to prove tool-result fencing under stress.
# In a real deployment this might be a nutrition API, a unit-conversion
# service, or a timer lookup -- anything slow enough that a barge-in can
# plausibly race it.
# ---------------------------------------------------------------------------
async def _slow_ingredient_lookup(query: str) -> str:
    # Deliberately slow (1.5-3s) so it's easy to interrupt mid-lookup
    # during the stress-case demo.
    await asyncio.sleep(random.uniform(1.5, 3.0))
    canned_answers = {
        "salt": "A pinch of salt is about 0.3 grams, or roughly a sixteenth of a teaspoon.",
        "garlic": "One medium clove of garlic is about one teaspoon when minced.",
        "pasta": "Nine minutes is the standard cook time for this pasta shape; check a minute early if you like it al dente.",
    }
    for key, answer in canned_answers.items():
        if key in query.lower():
            return answer
    return "I don't have a specific measurement for that ingredient on hand."


class KitchenAgent(Agent):
    """The cooking copilot. All recipe-progress and tool-result decisions
    are routed through `self.state` (a RecipeState) so that nothing about
    turn-fencing depends on the voice framework itself -- it's provable in
    isolation (see tests/test_interruption.py)."""

    def __init__(self, state: RecipeState) -> None:
        self.state = state
        super().__init__(
            instructions=(
                "You are Kitchen Copilot, a hands-free cooking assistant. "
                f"You are walking the user through this recipe: {state.recipe_name}. "
                "Speak the CURRENT step only when asked or when the session starts. "
                "Keep every spoken turn short -- one or two sentences. "
                "Only call advance_to_next_step when the user clearly confirms "
                "they finished the current step (e.g. 'done', 'okay next', "
                "'what's next'). If the user asks a side question (an "
                "ingredient amount, a substitution, a timing question), call "
                "lookup_ingredient_info instead of guessing, then answer their "
                "question -- do not repeat the current step unless asked."
            )
        )

    async def on_enter(self) -> None:
        await self.session.say(
            f"Let's make {self.state.recipe_name}. Step one: "
            f"{self.state.current_step_text()}",
            allow_interruptions=True,
        )

    @function_tool
    async def advance_to_next_step(self, context: RunContext) -> str:
        """Call this only when the user explicitly confirms they are done
        with the current step and ready to move on."""
        turn = self.state.current_turn()
        advanced = self.state.advance_step(tagged_turn_id=turn)
        if not advanced:
            if self.state.is_last_step():
                return "That was the last step. The dish is done."
            return (
                "That step-advance was superseded by a newer request and was "
                "ignored to avoid skipping ahead."
            )
        return f"Step {self.state.current_step_index() + 1}: {self.state.current_step_text()}"

    @function_tool
    async def lookup_ingredient_info(self, context: RunContext, query: str) -> str:
        """Look up a measurement, substitution, or timing detail for an
        ingredient or step. Use this instead of guessing at quantities.

        Args:
            query: what the user asked about, e.g. "how much salt" or
                "how long should the pasta cook".
        """
        turn_at_call_time = self.state.current_turn()
        raw_result = await _slow_ingredient_lookup(query)
        fenced_result = self.state.fence_tool_result(
            tagged_turn_id=turn_at_call_time, result=raw_result
        )
        if fenced_result is None:
            # The user has already moved the conversation on. Returning an
            # empty-ish, clearly-stale marker (rather than the real answer)
            # means the LLM will not speak outdated information as if it
            # were current.
            raise agents.llm.ToolError(
                "This lookup is stale (the user has moved on); do not speak "
                "this result."
            )
        return fenced_result


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    state = RecipeState(GARLIC_PASTA["steps"], recipe_name=GARLIC_PASTA["name"])

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=groq.STT(model="whisper-large-v3-turbo"),
        llm=groq.LLM(model="openai/gpt-oss-20b"),
        tts=rime.TTS(
            model="mistv2",
            speaker="cove",
            lang="eng",
            reduce_latency=True,
        ),
        allow_interruptions=True,
        # Tuned low so a real barge-in is detected (and audio cut) fast --
        # this is the "perceived response time of the interruption itself"
        # half of the hard problem.
        min_interruption_duration=0.2,
        min_interruption_words=0,
    )

    @session.on("user_state_changed")
    def _on_user_state_changed(ev) -> None:
        # Every time the user starts speaking -- including a barge-in
        # while the agent is mid-sentence -- mint a new turn id. Anything
        # still in flight from the previous turn (a pending step-advance,
        # a slow ingredient lookup) is now stale and will be fenced out
        # by RecipeState before it can affect state or be spoken.
        if ev.new_state == "speaking":
            new_turn = state.new_turn(reason="user_started_speaking")
            logger.info("Barge-in / new user turn -> turn_id=%s", new_turn)

    @session.on("speech_created")
    def _on_speech_created(ev) -> None:
        handle = ev.speech_handle

        def _log_outcome(_task) -> None:
            logger.info(
                "speech %s finished, interrupted=%s", handle.id, handle.interrupted
            )

        handle.add_done_callback(_log_outcome)

    await session.start(agent=KitchenAgent(state), room=ctx.room)


if __name__ == "__main__":
    # agent_name makes this worker selectable for explicit dispatch in the
    # LiveKit Cloud Console (Agents -> Console -> "Select an agent").
    # Without it, the worker still registers and accepts jobs fine, but the
    # Console's test UI has nothing to list in its picker.
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="kitchen-copilot"))
