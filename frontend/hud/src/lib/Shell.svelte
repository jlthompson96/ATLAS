<script lang="ts">
  import { useHud } from './ctx';
  import type { LayoutPanelId } from './generated/events';
  import Orb from './orb/Orb.svelte';
  import Transcript from './panels/Transcript.svelte';
  import { FALLBACK_LAYOUTS, REGISTRY, sanitizeSlots } from './panels/registry';
  import { breakpoint, resolveProfile } from './state/settings.svelte';
  import Alerts from './ui/Alerts.svelte';
  import LiveRegions from './ui/LiveRegions.svelte';
  import Notices from './ui/Notices.svelte';
  import TopBar from './ui/TopBar.svelte';

  const { store, settings } = useHud();

  const profile = $derived(resolveProfile(settings.profile, store.device?.role, settings.viewportWidth));
  const bp = $derived(breakpoint(settings.viewportWidth));
  const slots = $derived(sanitizeSlots(store.layouts[profile] ?? FALLBACK_LAYOUTS[profile]));
  const collapsible = $derived(bp === 'compact');
  const clockInBar = $derived(slots.top === 'clock' || slots.top === undefined);

  $effect(() => {
    const root = document.documentElement;
    if (settings.motion === 'system') delete root.dataset.motion;
    else root.dataset.motion = settings.motion === 'reduce' ? 'reduce' : 'full';
  });
</script>

{#snippet panel(id: LayoutPanelId)}
  {@const C = REGISTRY[id].component}
  <C {collapsible} />
{/snippet}

<a class="skip" href="#main">Skip to main content</a>
<LiveRegions />

<div class="shell" data-bp={bp} data-profile={profile} data-privacy={store.privacy.on ? 'on' : 'off'} data-connection={store.connection}>
  <TopBar showClock={clockInBar} />
  {#if slots.top && slots.top !== 'clock'}
    <div class="strip">{@render panel(slots.top)}</div>
  {/if}

  <main id="main" tabindex="-1">
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <!-- A scrollable region must be keyboard-focusable (WCAG 2.1.1). -->
    <aside class="side left" aria-label="Left panels" tabindex="0">
      {#each slots.left as id (id)}{@render panel(id)}{/each}
    </aside>

    <section class="center" aria-label="ATLAS core">
      <Orb />
      {#if store.privacy.on}<p class="privacy-tag mono"><span aria-hidden="true">◐</span> PRIVACY</p>{/if}
      <Alerts />
    </section>

    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <!-- A scrollable region must be keyboard-focusable (WCAG 2.1.1). -->
    <aside class="side right" aria-label="Right panels" tabindex="0">
      {#each slots.right as id (id)}{@render panel(id)}{/each}
    </aside>
  </main>

  {#if slots.bottom === 'transcript'}
    <Transcript dock />
  {:else if slots.bottom}
    <div class="strip">{@render panel(slots.bottom)}</div>
  {/if}
</div>

<Notices />

<style>
  .skip {
    position: absolute;
    left: 8px;
    top: -48px;
    z-index: 100;
    padding: 6px 10px;
    background: var(--panel-solid);
    border: 1px solid var(--accent);
    color: var(--text);
  }
  .skip:focus {
    top: 8px;
  }

  .shell {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    background:
      radial-gradient(ellipse at 50% 38%, rgba(31, 209, 255, 0.07), transparent 60%),
      var(--bg);
  }
  .shell[data-connection='reconnecting'] main,
  .shell[data-connection='closed'] main {
    opacity: 0.7;
  }
  .strip {
    padding: 8px 14px 0;
  }

  main {
    flex: 1 1 auto;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(230px, 310px) minmax(0, 1fr) minmax(230px, 310px);
    gap: 12px;
    padding: 12px 14px;
    outline: none;
  }
  .side {
    display: grid;
    gap: 10px;
    align-content: start;
    min-height: 0;
    overflow-y: auto;
    padding: 2px;
  }
  .center {
    --orb-size: min(46vh, 460px);
    display: grid;
    align-content: center;
    justify-items: center;
    gap: 14px;
    min-width: 0;
    min-height: 0;
  }
  .privacy-tag {
    margin: 0;
    padding: 2px 10px;
    border: 1px solid var(--line);
    letter-spacing: 0.2em;
    font-size: 0.8rem;
    color: var(--muted);
  }

  /* Medium (768-1279): centerpiece on top, panels in a two-column grid, transcript pinned below. */
  .shell[data-bp='medium'] main {
    grid-template-columns: 1fr 1fr;
    grid-template-rows: auto 1fr;
    overflow-y: auto;
    align-content: start;
  }
  .shell[data-bp='medium'] .center {
    --orb-size: min(340px, 36vh);
    grid-column: 1 / -1;
    grid-row: 1;
  }
  .shell[data-bp='medium'] .side {
    overflow: visible;
    grid-row: 2;
  }

  /* Compact (< 768): one column of collapsible cards. */
  .shell[data-bp='compact'] main {
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    gap: 10px;
    padding: 8px 12px;
  }
  .shell[data-bp='compact'] .center {
    --orb-size: min(30vh, 220px);
    order: 0;
    flex: 0 0 auto;
  }
  .shell[data-bp='compact'] .left {
    order: 1;
  }
  .shell[data-bp='compact'] .right {
    order: 2;
  }
  .shell[data-bp='compact'] .side {
    overflow: visible;
  }
</style>
