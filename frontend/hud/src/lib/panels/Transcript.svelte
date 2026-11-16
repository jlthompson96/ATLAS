<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { safeText } from '../format/text';
  import { known } from './data';

  let { dock = false, collapsible = false }: { dock?: boolean; collapsible?: boolean } = $props();
  const { store } = useHud();

  let text = $state('');
  let sending = $state(false);
  let log: HTMLElement | undefined = $state();

  const entries = $derived(dock ? store.transcript.slice(-4) : store.transcript.slice(-30));
  const hidden = $derived(store.privacy.on);
  const live = $derived(store.connection === 'open');
  const listening = $derived(store.pttActive);
  const voiceDown = $derived(known(store.host?.voice.status, ['ok', 'unavailable'] as const, 'ok') === 'unavailable');

  $effect(() => {
    // Keep the newest line in view as text streams in.
    void entries.length;
    void store.streaming?.text;
    if (log) log.scrollTop = log.scrollHeight;
  });

  async function send(e: SubmitEvent): Promise<void> {
    e.preventDefault();
    const value = text.trim();
    if (!value || sending) return;
    sending = true;
    const r = await store.sendText(value);
    sending = false;
    if (r.ok) text = '';
  }
</script>

{#snippet body()}
  {#if hidden}
    <p class="empty">Transcript hidden in privacy mode. You can still type.</p>
  {:else}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <!-- A scrollable region must be keyboard-focusable (WCAG 2.1.1). -->
    <div class="log" bind:this={log} tabindex="0" role="log" aria-label="Conversation transcript" aria-live="polite" aria-relevant="additions">
      {#if entries.length === 0 && !store.streaming && !store.partial}
        <p class="empty">No conversation yet. Say "Hey Atlas" or type below.</p>
      {/if}
      {#each entries as t (`${t.utterance_id}:${t.role}`)}
        <p class="line {t.role}">
          <span class="who mono">{t.role === 'user' ? 'you' : 'atlas'} ▸</span>
          <span class="txt untrusted">{safeText(t.text, 4000)}</span>
          {#if t.provenance?.tainted}<span class="flag bad"><span aria-hidden="true">▲</span> untrusted source</span>{/if}
        </p>
      {/each}
      {#if store.partial && store.partial.role === 'user'}
        <p class="line user live" aria-hidden="true"><span class="who mono">you ▸</span> <span class="txt untrusted">{safeText(store.partial.text, 4000)}</span><span class="cursor">…</span></p>
      {/if}
      {#if store.streaming}
        <p class="line assistant live" aria-hidden="true"><span class="who mono">atlas ▸</span> <span class="txt untrusted">{safeText(store.streaming.text, 4000)}</span><span class="cursor">▌</span></p>
      {/if}
    </div>
  {/if}
  <form class="input-row" onsubmit={send}>
    <label class="sr-only" for={dock ? 'text-input-dock' : 'text-input'}>Type a message to ATLAS</label>
    <input id={dock ? 'text-input-dock' : 'text-input'} class="input grow" type="text" maxlength="2000" autocomplete="off" placeholder="type…" bind:value={text} disabled={!live} />
    <button class="btn" type="submit" disabled={!live || sending || !text.trim()}>Send ⏎</button>
    <button class="btn" type="button" aria-pressed={listening} disabled={!live || voiceDown} onclick={() => store.togglePtt()} title={voiceDown ? 'Voice is unavailable; type instead' : 'Push to talk: press to start, press again to stop'}>
      <span aria-hidden="true">{listening ? '◉' : '🎙'}</span>
      {listening ? 'Stop' : 'Talk'}
    </button>
  </form>
{/snippet}

{#if dock}
  <section class="dock" aria-label="Transcript and input" data-panel="transcript">
    {@render body()}
  </section>
{:else}
  <Panel id="transcript" title="Transcript" sensitivity="secret" exposed={store.transcript.length > 0} {collapsible} collapsed={false}>
    {@render body()}
  </Panel>
{/if}

<style>
  .dock {
    padding: 8px 14px 10px;
    background: rgba(5, 8, 13, 0.85);
    border-top: 1px solid var(--line);
  }
  .log {
    max-height: 9.5rem;
    overflow-y: auto;
    display: grid;
    gap: 2px;
    margin-bottom: 6px;
    font-size: 0.9rem;
  }
  :global(.panel) .log {
    max-height: 18rem;
  }
  .line {
    margin: 0;
  }
  .who {
    color: var(--muted);
    font-size: 0.78rem;
    margin-right: 4px;
  }
  .assistant .who {
    color: var(--accent);
  }
  .live .txt {
    opacity: 0.85;
  }
  .cursor {
    color: var(--accent);
  }
  .input-row {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .grow {
    flex: 1 1 auto;
    min-width: 0;
  }
</style>
