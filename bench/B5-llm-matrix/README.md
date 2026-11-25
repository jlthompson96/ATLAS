# B5: LLM matrix

B5 chooses the model (PLAN §2.8, §8.1, Phase 1 exit criteria). A model is acceptable when it meets all four selection criteria at once:

| Criterion | Threshold | How `b5.py` measures it |
|---|---|---|
| Tool-call accuracy | ≥ 95% | 50 cases × `--repeat` passes (default 3). Every graded attempt counts. |
| TTFT | ≤ 800 ms at a 2K-token prompt | Median of `--perf-trials` (default 5) streamed requests of ~2,000 prompt tokens, with a fresh nonce at the start of the user turn. Measured at the client: time to the first generated token of any kind, reasoning included. The first visible token is reported beside it. These trials read LM Studio's stream directly, because the `LLMBackend` contract drops reasoning deltas. |
| Decode speed | ≥ 25 tok/s | Median, over the same requests: completion tokens (reasoning included) per second after the first token. |
| VRAM | ≤ 7.2 GB total | Read by hand on the desktop (`nvidia-smi`) and passed as `--vram-mb`. |

The report also gives the planner step latency (p50/p95 of the graded turns). One step is roughly what a tool decision costs inside a voice request.

## The eval set (`cases.py`)

Run `b5.py cases` for the list. Every case goes through the real `atlas-core` code: orchestrator, tool registry, policy engine, taint tracking and the quarantined reader. The model therefore sees exactly the system prompt, tool schemas and tool results it would get in production. All data is fake (`example.com`, `.org`, `.net`).

| Group | Cases | Correct behaviour |
|---|---|---|
| Planner, first step | N01–N22, N33–N35 | The right tool with the right arguments (time, inbox, calendar, Discord, memory, research, proposals), or a plain answer when no tool is needed (joke, arithmetic, thanks). |
| Planner, later step | N23–N32 | Earlier steps of the same request are scripted, and the real tools produce their results. The model then has to use them: read an item through `ask_reader`, answer by `$VAR` reference, reply by reference, forward a summary by reference. |
| Injection, planner | I01–I09 | **No tool call.** The user relays an injection (RT-1, 3, 4, 6, 8, 12, 13, 15), or the reader has flagged the item as a forwarding scam. |
| Injection, reader | R01–R06 | The reader stays inside its JSON schema and flags the item (`suspicious`, `requests`). Covers RT-1, 2, 3, 5, 10, 15. |

Only one model turn per case is graded. Steps before it are scripted. Steps after it are scripted too: ATLAS dispatches the graded calls, so a call the policy engine or schema refuses fails the case, but the model is not asked again. Each case's `oracle` field is the intended answer. The tests run every oracle through the harness and require 50/50 (`backend/tests/test_bench_b5.py`). The tests also check that models wrong in specific ways fail the right cases.

The planner never sees raw untrusted text (PLAN §2.9 rule 1). That's why planner injection cases come from the user relaying content, and the cases where the model reads the content itself go through the reader.

## Run

Use the dev link from [llm-link-dev.md](../../docs/runbooks/llm-link-dev.md) until WireGuard is up. It has the same security caveats: plain HTTP on the home LAN, a token in a file, Serve on Local Network turned off afterwards. For each model in the matrix:

1. On the desktop, load the model in LM Studio with the context you want to test (16K for interactive use, PLAN §2.8). Unload everything else.
2. Note the VRAM in use with the model loaded: `nvidia-smi --query-gpu=memory.used,memory.total --format=csv`.
3. On the Mac:

```sh
cd backend
.venv/bin/python ../bench/B5-llm-matrix/b5.py run \
  --base-url http://<desktop-lan-ip>:1234 --token-file ~/.atlas-dev/lmstudio-token \
  --model <model-id> --vram-mb <memory.used> --note "LM Studio 0.4.x, <GPU offload / settings>"
```

A run makes 1 + 5 perf requests and 150 graded turns. With a 47 tok/s model that takes a few minutes. Each run writes `bench/B5-llm-matrix/raw/B5-<time>-<model>.json`, which git ignores. Run files hold no token and no server address.

4. When every model has run:

```sh
.venv/bin/python ../bench/B5-llm-matrix/b5.py report ../bench/B5-llm-matrix/raw/*.json
```

Paste the output into `bench/results/B5-<date>.md`, with the hardware (RTX 4060 Ti 8 GB, 64 GiB RAM; [desktop inventory](../../docs/inventory/desktop-2026-11-25.md)) and the LM Studio version. The chosen model then goes into an ADR (Phase 1 exit criterion).

To check the plumbing without the desktop, run `b5.py run --backend oracle --repeat 1`. It should score 50/50.

Useful options: `--cases N01,I05,R01` to rerun a few cases, `--repeat 1 --perf-trials 0` for a quick look, and `--no-eval` for the speed trials only.

## Rules for a valid run

- One model loaded, nothing else using the GPU (no games, no video encode). A busy GPU shows up as slow TTFT, not as an error.
- Don't change LM Studio's sampling settings between models without writing them in `--note`. ATLAS sends no temperature for planner calls, so the server's per-model default applies. Reader calls use temperature 0.
- Thinking models (Gemma 4 E4B is one): reasoning tokens count toward TTFT, decode, and the orchestrator's 60 s request budget and 1,024-token per-call cap. A request that runs out of budget fails its case. The report shows the first visible token and the reasoning token count, because a voice answer can't start before the first visible token.
- Runs are only comparable on the same case set. The report warns when the run files carry different `cases_digest` values.

## Matrix

Candidates from PLAN §2.8: Qwen3.5-9B Q4_K_M, Gemma 4 E4B, Gemma 4 26B-A4B (MoE, experts offloaded to the CPU), gpt-oss-20b. Already on the desktop: `google/gemma-4-e4b`, `nemotron-3-nano-4b`, `meta/muse-glimmer`. That last one is a thinking model at 3.9 tok/s on this card, so it's expected to miss.

## Findings from building the harness

- **No reply to a sender with a display name.** `read_index` binds the sender as `$FROM_n` holding the raw `From` header (`Dana <dana@example.org>`). `propose_action` then refuses it as `to` (`INVALID_RECIPIENT`), so ATLAS can't propose a reply to most real mail. Case N31 uses a bare-address sender for now. A fix belongs in `atlas.core.pending` (extract the angle-bracket address, as `ItemIndex.contact_name` does) and is security-relevant, so it goes in its own change.
- **Discord channels by name.** The planner can't learn channel IDs. `channel_labels` exist but aren't shown in the tool spec, so case N16 gives the ID in the request.
