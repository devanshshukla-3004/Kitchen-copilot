# RIME_EVIDENCE.md

## Claim

Kitchen Copilot handles user barge-in (interruption) during spoken recipe
narration without: (a) continuing to speak past the interruption, (b)
letting a tool call or generation started before the interruption re-enter
the conversation later as if it were still current, or (c) silently
skipping or repeating a recipe step.

## Acceptance test

Defined before building the stress-case demo, in two parts:

### Part A -- automated, offline, repeatable (proves the fencing logic itself)

File: `tests/test_interruption.py`. No microphone, no LiveKit room, no API
keys required.

1. **Baseline (`test_normal_flow_advances_in_order`):** with no
   interruptions, each step-advance request is tagged with the current
   turn id and is accepted; the recipe advances one step at a time in
   order.
2. **Stress case 1 (`test_barge_in_stops_stale_step_advance`):** the agent
   mints turn N and requests a step advance. Before that request is
   applied, the user barges in (a new turn, N+1, is minted). The stale
   advance request (still tagged N) arrives after. **Expected:** the
   advance is rejected, and the recipe stays on the current step.
3. **Stress case 2 (`test_barge_in_discards_stale_tool_result`):** the
   agent starts a slow ingredient lookup under turn N. The user interrupts
   with an unrelated question, minting turn N+1, before the lookup
   resolves. The lookup's result (tagged N) finally comes back.
   **Expected:** the result is discarded (`None`), never returned as
   something that could be spoken, and the discard is recorded in the
   audit trail.
4. **Control (`test_fresh_tool_result_after_interruption_is_still_spoken`):**
   confirms the fencing isn't overly aggressive -- a tool result tagged
   with the *current* turn (even right after an interruption) is still
   allowed through.
5. **Boundary (`test_recipe_never_regresses_past_last_step`):** repeated
   advance calls past the final step are no-ops, not errors or wraparound.

**Run it:**
```bash
pip install -r requirements.txt
pytest tests/test_interruption.py -v
```

**Result at time of submission:** all 5 tests pass.
```
tests/test_interruption.py::test_normal_flow_advances_in_order PASSED
tests/test_interruption.py::test_barge_in_stops_stale_step_advance PASSED
tests/test_interruption.py::test_barge_in_discards_stale_tool_result PASSED
tests/test_interruption.py::test_fresh_tool_result_after_interruption_is_still_spoken PASSED
tests/test_interruption.py::test_recipe_never_regresses_past_last_step PASSED
5 passed
```

### Part B -- live, end-to-end (proves it under real audio conditions with Rime)

Run `python agent.py dev` and connect via the LiveKit Agents Playground.

**Procedure:**
1. Let the agent speak step 2 in full (normal flow). Confirm it sounds
   natural and Rime is the active voice (visible in the LiveKit Agents
   Playground's active-track / logs, and audibly it's the configured
   `mistv2` / `cove` voice).
2. Ask a side question mid-step ("how much salt do I need?") while the
   agent is still talking about step 2. Confirm: (a) the agent's audio
   stops within a fraction of a second of you starting to talk, (b) it
   answers the salt question, not step 2, and (c) afterward it does **not**
   re-narrate step 2 from the beginning -- it stays on step 2 unless you
   confirm you're done with it.
3. **Deliberate stress case:** ask an ingredient question that triggers
   `lookup_ingredient_info` (which has an artificial 1.5-3s delay), and
   *immediately* (within \<1s) interrupt again with a different, unrelated
   question before the first lookup can possibly have returned. Confirm
   the first (now-stale) answer is never spoken -- only the second
   question gets answered. This is checked programmatically too: the
   `ToolError` raised by a fenced result is visible in the agent's log
   output for that run.
4. Confirm the recipe step index only ever moved forward when you
   explicitly said "done" / "next", by checking `RecipeState.audit_trail()`
   at the end of the session (logged via `agent.py`'s `speech_created`
   handler and RecipeState's own log).

**What's measured:** user-visible behavior (does stale info get spoken?
does the step counter skip or repeat?), not a synthetic proxy metric.

## Limitations

- Part A proves the fencing algorithm is correct in isolation; it does not
  by itself prove LiveKit's VAD-triggered audio cutoff is fast enough to
  feel instantaneous -- that's what Part B's live procedure checks
  qualitatively (audio stops before the tool-call side even starts
  discarding results).
- The artificial delay in `_slow_ingredient_lookup` is there specifically
  to make the race condition reliably reproducible on demand; a real
  ingredient/nutrition API would have variable latency, which the same
  fencing mechanism would still handle correctly (it fences on turn id,
  not on a fixed timing window).
- We did not run this over a telephony transport or in a noisy kitchen
  environment; VAD sensitivity in those conditions is untested.
