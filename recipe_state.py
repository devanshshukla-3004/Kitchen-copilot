"""
recipe_state.py

This module holds the core "interruption & recovery" logic for the
Kitchen Copilot hands-free cooking assistant. It is deliberately kept
independent of LiveKit/Rime so it can be unit tested offline, in CI,
without a microphone, a room, or any API keys.

The hard voice problem being solved:

  When the user barges in on the agent mid-instruction, the agent must:
    1. Stop speaking immediately (handled by the voice framework's VAD +
       allow_interruptions -- see agent.py).
    2. Never let a tool call or LLM generation that belonged to the
       interrupted turn "leak" into the conversation after the user has
       moved on (a stale ingredient lookup answering the wrong question,
       for example).
    3. Never advance recipe progress based on speech that was cut off
       partway through. A step is only marked complete when the user
       explicitly confirms it, not just because the agent finished (or
       almost finished) narrating it.

The mechanism is a monotonically increasing "turn id". Every time the
user starts a new turn (including a barge-in interruption), the turn id
increments. Any in-flight work (tool calls, pending step-advance
requests) is tagged with the turn id that was current when the work
started. Before that work is allowed to affect state or be spoken, it is
checked against the CURRENT turn id. If the turn has since moved on, the
result is discarded ("fenced") and logged -- it is never spoken and never
mutates state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Step:
    index: int
    text: str


@dataclass
class LogEntry:
    event: str
    detail: dict = field(default_factory=dict)


class StaleResultError(Exception):
    """Raised (or returned as None, depending on call site) when a piece
    of work tagged with an old turn id tries to re-enter the conversation
    after the user has already moved on."""


class RecipeState:
    def __init__(self, steps: list[str], recipe_name: str = "Recipe"):
        if not steps:
            raise ValueError("A recipe needs at least one step")
        self.recipe_name = recipe_name
        self.steps: list[Step] = [Step(i, t) for i, t in enumerate(steps)]
        self.current_index: int = 0
        self.turn_id: int = 0
        self.log: list[LogEntry] = []

    # ------------------------------------------------------------------
    # Turn management
    # ------------------------------------------------------------------
    def new_turn(self, reason: str = "user_turn") -> int:
        """Call this the moment the user starts speaking -- including a
        barge-in while the agent is still talking. Returns the new turn id.
        Any work still tagged with an earlier turn id becomes stale."""
        self.turn_id += 1
        self._record("new_turn", {"turn_id": self.turn_id, "reason": reason})
        return self.turn_id

    def current_turn(self) -> int:
        return self.turn_id

    def is_stale(self, tagged_turn_id: int) -> bool:
        return tagged_turn_id != self.turn_id

    # ------------------------------------------------------------------
    # Recipe progress
    # ------------------------------------------------------------------
    def current_step_text(self) -> str:
        return self.steps[self.current_index].text

    def current_step_index(self) -> int:
        return self.current_index

    def is_last_step(self) -> bool:
        return self.current_index == len(self.steps) - 1

    def advance_step(self, tagged_turn_id: int) -> bool:
        """Move to the next step, but ONLY if this request still belongs
        to the current turn. A request tagged with a stale turn id (e.g.
        the agent had decided to advance right as the user interrupted)
        is rejected and logged, never applied.

        Returns True if the step advanced, False if it was fenced/rejected
        or there is no next step.
        """
        if self.is_stale(tagged_turn_id):
            self._record(
                "stale_advance_rejected",
                {"tagged_turn_id": tagged_turn_id, "current_turn_id": self.turn_id},
            )
            return False
        if self.is_last_step():
            self._record("advance_noop_last_step", {"turn_id": self.turn_id})
            return False
        self.current_index += 1
        self._record(
            "advanced",
            {"turn_id": self.turn_id, "new_index": self.current_index},
        )
        return True

    # ------------------------------------------------------------------
    # Tool-result fencing
    # ------------------------------------------------------------------
    def fence_tool_result(self, tagged_turn_id: int, result: str) -> Optional[str]:
        """A slow tool call (e.g. an ingredient/unit lookup) finishes and
        wants to speak `result` back to the user. Only allow it through if
        the turn it was launched under is still the current turn.

        Returns the result if it's still valid, or None if it must be
        discarded (fenced) because the user has already moved the
        conversation on.
        """
        if self.is_stale(tagged_turn_id):
            self._record(
                "stale_tool_result_discarded",
                {
                    "tagged_turn_id": tagged_turn_id,
                    "current_turn_id": self.turn_id,
                    "discarded_result": result,
                },
            )
            return None
        self._record(
            "tool_result_accepted",
            {"turn_id": self.turn_id, "result": result},
        )
        return result

    # ------------------------------------------------------------------
    # Evidence / audit trail
    # ------------------------------------------------------------------
    def _record(self, event: str, detail: dict) -> None:
        self.log.append(LogEntry(event=event, detail=detail))

    def audit_trail(self) -> list[dict]:
        return [{"event": e.event, **e.detail} for e in self.log]
