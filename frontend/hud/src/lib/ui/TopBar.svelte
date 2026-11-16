<script lang="ts">
  import { useHud } from '../ctx';
  import { formatClock, formatDay } from '../format/text';
  import { linkBadge, powerBadge, serviceBadge } from '../panels/hostview';
  import PinDialog from './PinDialog.svelte';
  import SettingsMenu from './SettingsMenu.svelte';

  let { showClock = true }: { showClock?: boolean } = $props();
  const { store, ticker } = useHud();

  const now = $derived(store.toServer(ticker.now));
  const power = $derived(powerBadge(store.host));
  const link = $derived(linkBadge(store.host));
  const service = $derived(serviceBadge(store.host));
  const online = $derived(store.connection === 'open');

  let askPin = $state(false);
  let settingsOpen = $state(false);
  let toggling = $state(false);

  async function applyPrivacy(on: boolean): Promise<void> {
    toggling = true;
    const r = await store.setPrivacy(on);
    toggling = false;
    if (!r.ok) {
      if (r.code === 'STEP_UP_REQUIRED') askPin = true;
      else store.notify('error', r.message);
    }
  }

  function togglePrivacy(): void {
    if (store.privacy.on) {
      // Leaving privacy mode needs the PIN on a remote device, a tap on the kiosk (§4.6).
      if (store.device?.role === 'remote' && !store.stepUpValid) askPin = true;
      else void applyPrivacy(false);
    } else {
      void applyPrivacy(true);
    }
  }
</script>

<header class="bar">
  <h1 class="brand mono">ATLAS <span aria-hidden="true">▸</span>{#if showClock} <span class="clock">{formatClock(now)}</span> <span class="day">{formatDay(now)}</span>{/if}</h1>

  <div class="chips">
    {#if !online}
      <span class="chip" data-tone="danger" role="status"><span aria-hidden="true">✗</span> {store.connection === 'closed' ? 'DISCONNECTED' : 'RECONNECTING…'}</span>
    {/if}
    <span class="chip" data-tone={power.tone}><span aria-hidden="true">{power.glyph}</span> {power.text}</span>
    <span class="chip" data-tone={link.tone}>{link.text}</span>
    {#if service && service.tone !== 'ok'}
      <span class="chip" data-tone={service.tone}><span aria-hidden="true">{service.glyph}</span> {service.text}</span>
    {/if}
    <button class="btn small privacy" type="button" aria-pressed={store.privacy.on} disabled={toggling || !online} onclick={togglePrivacy}>
      <span aria-hidden="true">{store.privacy.on ? '◐' : '○'}</span>
      Privacy {store.privacy.on ? 'on' : 'off'}
    </button>
    <button class="btn small" type="button" aria-haspopup="dialog" onclick={() => (settingsOpen = true)} aria-label="Settings"><span aria-hidden="true">⚙</span> Settings</button>
  </div>
</header>

{#if askPin}
  <PinDialog
    title="PIN to leave privacy mode"
    ongranted={() => {
      askPin = false;
      void applyPrivacy(false);
    }}
    oncancel={() => (askPin = false)}
  />
{/if}
{#if settingsOpen}
  <SettingsMenu onclose={() => (settingsOpen = false)} />
{/if}

<style>
  .bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 6px 12px;
    padding: 6px 14px;
    border-bottom: 1px solid var(--line);
    background: rgba(5, 8, 13, 0.85);
  }
  .brand {
    margin: 0;
    font-size: 0.95rem;
    font-weight: 600;
    letter-spacing: 0.18em;
    color: var(--accent);
  }
  .clock {
    color: var(--text);
  }
  .day {
    color: var(--muted);
    letter-spacing: 0.05em;
    font-weight: 400;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
</style>
