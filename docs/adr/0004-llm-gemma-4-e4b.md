# ADR 0004: The LLM is Gemma 4 E4B, with reasoning off and a stall guard

- **Status:** Accepted 2026-11-25 (owner: "I'm good with Google Gemma"; reasoning off with a stall guard chosen the same day).
- **Date:** 2026-11-25
- **Decision record:** closes the "LLM model" row in PLAN §2.8 ("Decide after B5") and the Phase 1 exit criterion "a model chosen and recorded in an ADR". **Deviates** from the §2.8 selection criterion "tool-call accuracy ≥ 95%".
- **Contract change:** none. `reasoning_effort` and the stall guard are settings of `LMStudioBackend`, not of the `LLMBackend` contract.

## Context

B5 measured Gemma 4 E4B (Q4_K_M, 16K context) on the desktop (RTX 4060 Ti 8 GB) through the real orchestrator, policy engine and quarantined reader ([results](../../bench/results/B5-2026-11-25.md)):

| Criterion (PLAN §2.8) | Threshold | Gemma 4 E4B |
|---|---|---|
| Tool-call accuracy | ≥ 95% | **83–85%** in four runs (thinking on or off, with and without one self-correction) |
| TTFT at a 2K-token prompt | ≤ 800 ms | 454–539 ms |
| Decode | ≥ 25 tok/s | 48–51 tok/s |
| VRAM, whole GPU | ≤ 7.2 GB | 5,368 MiB of 8,188 |

The owner chose not to try the next candidate on the shortlist (Qwen3.5-9B) and to keep Gemma.

Gemma is a thinking model. With LM Studio's default it reasons first, and a planner step took 4.4 s at the median and 14 s at p95. With `reasoning_effort: "none"` (honoured by LM Studio 0.4.28; no other value or field turned it off) accuracy was the same and a step took 0.6 s at the median and 3.8 s at p95. But twice in about 300 graded turns with reasoning off, Gemma streamed a few tokens and then nothing until ATLAS's 60 s request budget ended the request; after one of them LM Studio refused new connections for a few seconds.

## Decision

1. **Model:** `google/gemma-4-e4b`, Q4_K_M, loaded with a context length of 16384 in LM Studio.
2. **Reasoning off:** atlas-core sends `reasoning_effort: "none"` with every chat request (`--reasoning-effort none`).
3. **Stall guard** in `LMStudioBackend` (on for every model): a stream that sends no bytes for 10 s, or no first bytes for 30 s, is dropped and the request fails as `LLM_ERROR`. Closing the connection lets the server stop generating, instead of keeping it busy until the 60 s budget runs out.
4. **The 95% accuracy criterion is waived for this model.** The other three criteria are met.

When the desktop is linked, atlas-core runs with:

```text
python -m atlas.core --backend lmstudio --model google/gemma-4-e4b --reasoning-effort none
```

(Today it runs with `--backend none`: no desktop is linked yet; [ui-on-pi.md](../runbooks/ui-on-pi.md).)

## Consequences

**What the accuracy miss means.** Wrong tool calls are a usefulness problem, not a safety one: every rule that matters is enforced in code whatever the model does (PLAN §2.9). The planner never sees untrusted text, tainted arguments can only produce a proposal, and nothing is sent without a PIN approval of the exact payload. In B5 no injection case produced a proposal. What the owner will notice:

- **Proposals.** Gemma often leaves out a required field (an email body, a Discord channel, a schedule interval). ATLAS refuses with the field named, but Gemma tends to answer in text instead of fixing the call. Asking again, more explicitly, usually works.
- **Tool choice.** It sometimes picks a near miss (summarize where classify was asked, three inbox calls instead of one) or answers from its own knowledge where a tool was needed.
- **Speed.** About 0.6 s per planner step, so a request with one tool call answers in a second or two after the model is awake.

**Stalls.** The guard turns a stall into a quick `LLM_ERROR` (about 10 s instead of 60 s). The cause is not known. If stalls show up in daily use, try LM Studio updates first, then reasoning on (slower, no stalls seen).

**Revisiting.** B5 stays runnable (`bench/B5-llm-matrix`). Rerun it when LM Studio or the model updates, or to try another model: any candidate that meets all four criteria replaces this decision with a new ADR.
