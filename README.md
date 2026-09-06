# Kitchen Copilot

A hands-free cooking voice assistant, built for the **Rime x DataForge Hackathon**.

**The user:** someone cooking, with wet/dirty/full hands, who cannot touch a
screen but needs to move through a recipe and ask ad-hoc questions
("how much salt is that?", "wait, how long does the pasta cook?").

**The problem removing speech would break:** without voice, this person has
to stop, wash their hands, and touch a phone every time they need the next
step or have a question -- which defeats the entire point of a "hands-free"
assistant. Voice isn't a convenience layer here, it's the only viable input
and output channel.

**The hard voice problem we solved:** Interruption & recovery. The user must
be able to barge in on the agent mid-sentence, and:
1. The agent stops talking immediately.
2. Any tool call or generation that belonged to the interrupted turn is
   fenced out -- it can never re-enter the conversation later and be spoken
   as if it were current.
3. The recipe's step index never advances based on speech that was cut off;
   it only advances on an explicit, still-current confirmation from the user.

See [`RIME_EVIDENCE.md`](./RIME_EVIDENCE.md) for the acceptance test, exact
procedure, and results.

## Architecture

```
 mic audio                                        Rime TTS audio
    │                                                    ▲
    ▼                                                    │
┌─────────────┐   ┌───────────┐   ┌────────────┐   ┌──────────┐
│ Silero VAD  │──▶│ Groq-hosted Whisper STT │──▶  │ Groq LLM │──▶│ Rime TTS │
└─────────────┘   └───────────┘   └─────┬──────┘   └──────────┘
                                          │
                                          ▼
                                 ┌───────────────────┐
                                 │  KitchenAgent      │
                                 │  (agent.py)        │
                                 │  function_tools:   │
                                 │   advance_to_next_ │
                                 │   step,            │
                                 │   lookup_ingredient│
                                 │   _info            │
                                 └─────────┬──────────┘
                                           │ tags every call/result
                                           │ with a turn id
                                           ▼
                                 ┌────────────────────┐
                                 │ RecipeState        │
                                 │ (recipe_state.py)  │
                                 │ - turn fencing     │
                                 │ - step progress    │
                                 │ - audit trail      │
                                 └────────────────────┘
```

All transport, turn handling, VAD, and orchestration is provided by
[LiveKit Agents](https://docs.livekit.io/agents/) (`livekit-agents` 1.8.0).
Rime is wired in as the TTS plugin and is the **only** speech output in the
judged flow (no fallback provider is active by default -- see "Failure
behavior" below).

The turn-fencing logic that actually solves the hard problem
(`recipe_state.py`) is deliberately framework-agnostic so it can be unit
tested without a microphone, a LiveKit room, or any API key.

## Exact Rime configuration used in the demo

| Setting    | Value                                   |
|------------|------------------------------------------|
| Model      | `mistv2`                                  |
| Speaker    | `cove`                                    |
| Language   | `eng`                                     |
| Endpoint   | Rime's default HTTP TTS endpoint via `livekit-plugins-rime` (`reduce_latency=True`) |
| Audio format | PCM frames at the LiveKit Agents session's negotiated sample rate (framework-managed; no custom transcoding) |
| Transport  | LiveKit WebRTC room audio track (browser mic in, browser speaker out via the LiveKit Agents Playground) |

These are set explicitly in `agent.py`, not left to plugin defaults, so the
combination is reproducible.

## Setup

1. **Clone and install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
2. **Create your env file**
   ```bash
   cp .env.example .env
   # fill in LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET (from cloud.livekit.io)
   # fill in RIME_API_KEY (from rime.ai)
   # fill in GROQ_API_KEY (from console.groq.com -- free, no card required)
   ```
3. **Run the automated acceptance test (no keys required)**
   ```bash
   pytest tests/test_interruption.py -v
   ```
4. **Run the live agent**
   ```bash
   python agent.py dev
   ```
5. **Connect from the browser** using the
   [LiveKit Agents Playground](https://agents-playground.livekit.io/), pointed
   at your LiveKit Cloud project. Talk to it, then interrupt it mid-sentence
   to see the stress case described in `RIME_EVIDENCE.md`.

## Known limitations

- The ingredient/measurement lookup (`lookup_ingredient_info`) is a small
  canned dictionary plus an artificial delay, not a real nutrition API --
  it exists specifically to make the tool-call race condition reproducible
  on demand for the stress-case demo.
- Only one recipe (`fixtures/garlic_pasta.py`) is wired in; swapping recipes
  means editing that fixture, there's no recipe-search UI.
- Tested with a browser microphone via the LiveKit Agents Playground, not
  over a telephony transport.
- Turn-taking relies on Silero VAD; very quiet or very noisy environments
  may affect how quickly a barge-in is detected (see
  `min_interruption_duration` / `min_interruption_words` in `agent.py`).

## Failure behavior

- If Rime's API is unreachable, `livekit-plugins-rime` raises a TTS error
  which LiveKit Agents surfaces as a session `error` event; no fallback TTS
  provider is configured, so the agent will not silently switch providers
  -- this keeps the judged flow's speech provider unambiguous, per the
  hackathon's "make fallbacks visible" rule. (No fallback = nothing to
  disclose.)
- If the LLM tool-calls `lookup_ingredient_info` and the turn has gone
  stale before the (simulated) slow lookup resolves, the tool raises
  `ToolError` instead of returning a spoken-able string, so the LLM cannot
  accidentally narrate outdated information.
- If `advance_to_next_step` is called on a stale turn, it returns a message
  explaining the request was superseded rather than silently skipping a
  step.

## Third-party services

- **Rime** (`rime.ai`) -- text-to-speech, required.
- **Groq** (`console.groq.com`) -- speech-to-text (`whisper-large-v3-turbo`)
  and reasoning (`openai/gpt-oss-20b`); free tier, no card required.
- **LiveKit Cloud** -- WebRTC transport and the Agents Playground / Console demo UI.
- **Silero VAD** -- runs locally, no external service/API key required.
