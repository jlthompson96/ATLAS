# Runbook: chat with the real LM Studio from the Mac (dev link, before WireGuard)

Goal: a first end-to-end test of `python -m atlas.core.cli --backend lmstudio` against LM Studio on your desktop, through the full policy, taint and quarantined-reader path. Nothing here is the production link. The production link is WireGuard (`10.77.0.1` to `10.77.0.2`, PLAN.md §5.3), which this runbook does not set up.

## Security caveats (read first)

- **The LAN link is plain HTTP.** Your prompts, the model's answers and the API token cross your LAN in cleartext. Do this only on a home network you trust, for a short test, with test prompts. **Never paste real email, Discord, calendar or portfolio content into this session** (CLAUDE.md): use `/demo` (fake `example.com` mail) and made-up questions.
- LM Studio must be reachable from the Mac, which means "Serve on Local Network" is on. **Turn it off again** (or close the firewall rule) when you finish.
- **Use an API token**, not an open server: LM Studio Settings, Developer, "Require Authentication", create a token named `atlas-dev`. Treat it as a secret: it lives in a file outside the repo, `chmod 600`, never committed, never in shell history (the CLI takes a file, not a token on the command line). Delete the token in LM Studio when the test is done. The production token is a separate one, held in `systemd-creds` on the Pi.
- Restrict the Windows firewall rule for port 1234 to your Mac's LAN IP only, not "any" (Windows Defender Firewall, Inbound rules, scope, remote IP). The production rule allows only `10.77.0.1` on `wg0` (PLAN.md §2.8).
- LM Studio's own server logs can persist prompts (PLAN.md §2.8 "Prompts on disk"). Turn conversation logging off in LM Studio, or clear its logs after the test.
- Outbound-internet blocking of LM Studio (R-LLM 3) is *not* part of this test. Don't download models during it.
- The CLI never sends model output anywhere but your terminal. Approvals are not possible from the CLI (HUD only), so nothing the model proposes can be executed.

## 1. On the desktop

1. LM Studio 0.4 or later. Load the model you want to try (see PLAN.md §2.8, B5 for candidates). Note its identifier (the `--model` value), e.g. as shown in the Developer tab.
2. Developer tab: start the server, enable **Serve on Local Network**, enable **Require Authentication**, create the token and copy it.
3. Note the desktop's LAN IP (`ipconfig`) and, for Wake-on-LAN later, the wired NIC's MAC address.
4. Windows Firewall: allow inbound TCP 1234 from the Mac's IP only.

## 2. On the Mac

```sh
cd backend
umask 077 && mkdir -p ~/.atlas-dev && cat > ~/.atlas-dev/lmstudio-token   # paste the token, press Ctrl-D
chmod 600 ~/.atlas-dev/lmstudio-token
```

(The file lives outside the repository on purpose. `.gitignore` is a backstop, not permission.)

Check the link first, with no model call:

```sh
curl -s -H "Authorization: Bearer $(cat ~/.atlas-dev/lmstudio-token)" http://<desktop-lan-ip>:1234/v1/models | head -c 300
```

You should see a JSON list of models. A `401` means the token is wrong; no answer means the firewall or "Serve on Local Network".

## 3. Chat

Without `atlas-hostd` (the normal case for this first test, since hostd doesn't exist yet):

```sh
cd backend
.venv/bin/python -m atlas.core.cli --backend lmstudio \
  --base-url http://<desktop-lan-ip>:1234 \
  --token-file ~/.atlas-dev/lmstudio-token \
  --model <model-id> --no-hostd
```

`--model` can be left out if exactly the model you want is loaded. What to try:

| You type | What it shows |
|---|---|
| `what time is it`, `what's the date` | Answered by the Pi-side grammar, never sent to the model |
| `tell me a joke` | A plain streamed answer |
| `what do you remember about me` | The model calls `search_memory` (read-only) |
| `/demo` then `summarize my emails` | Fake emails go through the quarantined reader; the reply shows `[tainted email item ...]` placeholders, and `/show $SUBJECT_1` displays the untrusted text to you only. The second demo email carries a hidden "forward the last 10 emails" instruction: it must not cause any action. `/pending` should stay empty. |
| `/quit` | Exit |

With `atlas-hostd` and Wake-on-LAN (once hostd runs on the desktop):

```sh
.venv/bin/python -m atlas.core.cli --backend lmstudio \
  --base-url http://<desktop-ip>:1234 --token-file ~/.atlas-dev/lmstudio-token \
  --hostd-url http://<desktop-ip>:8770 --mac aa:bb:cc:dd:ee:ff \
  --model <model-id> --model-vram-mb 7200
```

Put the desktop to sleep, then type a question. You should see `One moment, waking my core.`, `[waking host: stage ...]` lines, and the answer once the 1-token warm-up passes (60 s limit; on failure `My core didn't wake. I've saved your request...`, and `/retry` re-sends it once the core is back). Commands: `wake the desktop`, `put the desktop to sleep`, `is the desktop awake`. Add `--idle-sleep` to let ATLAS sleep the desktop after 20 idle minutes (hostd decides; it refuses if you are using the machine). Wake-on-LAN broadcast defaults to `255.255.255.255:9`; use `--wol-broadcast <x.x.x.255>` if your OS needs the subnet broadcast. Note hostd listens only on `10.77.0.2` in production, so over the LAN this needs a dev hostd config.

## 4. What the messages mean

| Output | Meaning |
|---|---|
| `[alert critical LLM_AUTH_FAILED]` | LM Studio rejected the token (FI-7). No retry loop: fix the token file and `/retry` |
| `The model isn't ready (packet: unreachable)` (`--no-hostd`) | Nothing answers on the base URL |
| `The model isn't ready (model: model_not_found)` | The server is up but the `--model` id isn't known |
| `[budget_exhausted: DEADLINE]` | The 60 s per-request budget ran out (slow model or cold load) |
| `[failed: LLM_ERROR]` | The backend failed mid-request (connection dropped, bad stream) |
| `[alert warn HOST_BUSY]` | Free VRAM is below `--model-vram-mb`; the model is not loaded (D16) |
| `SIGNED_OUT` | hostd answers but nobody is signed in on the desktop (FI-9) |

## 5. Cleanup

1. Turn off "Serve on Local Network" in LM Studio, or remove the firewall rule.
2. Delete the `atlas-dev` token in LM Studio and `rm ~/.atlas-dev/lmstudio-token`.
3. Clear LM Studio's logs if they stored prompts.

## Tests that cover this without hardware

`backend/tests/test_llm_lmstudio.py` (fake LM Studio), `test_power_hostd.py` (fake hostd, magic packets), `test_power_controller.py` (wake/sleep policy), `test_power_flow.py` (CT-1..4, FI-1, FI-2, FI-7, FI-9, FI-10 on a virtual clock), `test_core_fallback.py` (grammar) and `test_cli_lmstudio.py` (the CLI end to end). Benchmarks B5, B6 and B12 and the real cold-start numbers need the desktop and are not covered.
