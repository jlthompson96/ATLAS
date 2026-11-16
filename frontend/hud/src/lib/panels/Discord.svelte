<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { formatClock, safeText } from '../format/text';
  import { typed, type DiscordPanel } from './data';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store } = useHud();
  const p = $derived(typed(store.panels.discord) as DiscordPanel | undefined);
</script>

<Panel id="discord" title="Discord" sensitivity="secret" exposed={!!p && !p.redacted} status={p ? `${p.data.unread} new` : ''} staleSince={p?.update.stale_since} {collapsible} collapsed={collapsible}>
  {#if !p}
    <p class="empty">No Discord data.</p>
  {:else if p.redacted}
    <p class="empty">{p.data.unread} unread. Details hidden in privacy mode.</p>
  {:else if p.data.items.length === 0}
    <p class="empty">No new messages.</p>
  {:else}
    <ul class="list">
      {#each p.data.items.slice(0, 8) as m (m.id)}
        <li>
          <div class="row">
            <span class="grow truncate untrusted"><span class="sub">#</span>{safeText(m.channel, 60).replace(/^#/, '')} · {safeText(m.author_display, 60)}</span>
            <span class="sub mono">{formatClock(m.at)}</span>
          </div>
          {#if m.excerpt}<div class="excerpt untrusted">{safeText(m.excerpt, 200)}</div>{/if}
        </li>
      {/each}
    </ul>
  {/if}
</Panel>

<style>
  .excerpt {
    font-size: 0.82rem;
    color: var(--muted);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  li {
    padding: 2px 0;
    border-bottom: 1px solid rgba(31, 209, 255, 0.12);
  }
</style>
