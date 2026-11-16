<script lang="ts">
  import type { Snippet } from 'svelte';
  import { useHud } from '../ctx';
  import { formatAge } from '../format/text';

  interface Props {
    id: string;
    title: string;
    /** Sensitivity of the data shown; personal/secret panels get a second-layer blur in privacy mode. */
    sensitivity?: 'public' | 'personal' | 'secret';
    /**
     * True while the panel is showing personal/secret content. If privacy mode is on and a panel still
     * shows such content (the server should already have redacted it), blur it: a second layer only.
     */
    exposed?: boolean;
    /** Short status text beside the title (always text, never colour alone). */
    status?: string;
    statusTone?: 'ok' | 'warn' | 'danger' | 'muted';
    /** Epoch ms since which the source has been failing; shows a STALE label. */
    staleSince?: number | undefined;
    /** Collapsible (compact layout). */
    collapsible?: boolean;
    collapsed?: boolean;
    children: Snippet;
  }

  let {
    id,
    title,
    sensitivity = 'public',
    exposed = false,
    status = '',
    statusTone = 'muted',
    staleSince,
    collapsible = false,
    collapsed = $bindable(false),
    children
  }: Props = $props();

  const { store, ticker } = useHud();
  const blur = $derived(store.privacy.on && exposed);
  const headingId = $derived(`panel-${id}-h`);
  const bodyId = $derived(`panel-${id}-b`);
</script>

<section class="panel" class:blur data-panel={id} data-sensitivity={sensitivity} aria-labelledby={headingId}>
  <header>
    {#if collapsible}
      <h2 id={headingId}>
        <button class="fold" type="button" aria-expanded={!collapsed} aria-controls={bodyId} onclick={() => (collapsed = !collapsed)}>
          <span aria-hidden="true">{collapsed ? '▸' : '▾'}</span>
          {title}
        </button>
      </h2>
    {:else}
      <h2 id={headingId}>{title}</h2>
    {/if}
    {#if staleSince}
      <span class="chip" data-tone="warn" title="The source is failing; showing the last good data">◇ STALE {formatAge(staleSince, store.toServer(ticker.now))}</span>
    {/if}
    {#if status}<span class="chip" data-tone={statusTone}>{status}</span>{/if}
  </header>
  <div class="body" id={bodyId} hidden={collapsed}>
    {@render children()}
  </div>
</section>

<style>
  .panel {
    position: relative;
    background:
      linear-gradient(135deg, transparent 0 7px, var(--line) 7px 8.5px, transparent 8.5px) top left / 14px 14px no-repeat,
      linear-gradient(-45deg, transparent 0 7px, var(--line) 7px 8.5px, transparent 8.5px) bottom right / 14px 14px no-repeat,
      var(--panel);
    border: 1px solid var(--line-dim);
    border-top-color: var(--line);
    clip-path: polygon(10px 0, 100% 0, 100% calc(100% - 10px), calc(100% - 10px) 100%, 0 100%, 0 10px);
    padding: 8px 12px 10px;
    min-width: 0;
  }

  header {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 8px;
    margin-bottom: 6px;
  }

  h2 {
    margin: 0;
    flex: 1 1 auto;
    font-size: 0.74rem;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--accent);
  }

  .fold {
    all: unset;
    cursor: pointer;
    display: inline-flex;
    gap: 6px;
    width: 100%;
    min-height: 28px;
    align-items: center;
  }

  .fold:focus-visible {
    outline: 2px solid var(--focus);
    outline-offset: -2px;
  }

  .body {
    transition:
      filter var(--t) var(--ease),
      opacity var(--t) var(--ease);
  }

  /* Second layer only: the server already sent redacted data. */
  .blur .body {
    filter: blur(5px);
    opacity: 0.55;
  }
</style>
