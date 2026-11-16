<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { safeText } from '../format/text';
  import { known, typed, type DigestPanel } from './data';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store } = useHud();
  const p = $derived(typed(store.panels.digest) as DigestPanel | undefined);
  const LEVELS = ['info', 'warn', 'critical'] as const;
  const glyph = { info: '●', warn: '▲', critical: '✗' } as const;
</script>

<Panel id="digest" title="Morning digest" sensitivity="personal" exposed={!!p && !p.redacted} staleSince={p?.update.stale_since} {collapsible} collapsed={collapsible}>
  {#if !p}
    <p class="empty">No digest yet.</p>
  {:else if p.redacted}
    <p class="empty">Hidden in privacy mode.</p>
  {:else}
    <p class="sub mono">{p.data.date}</p>
    {#if p.data.items.length === 0}
      <p class="empty">Nothing overnight.</p>
    {:else}
      <ul class="list">
        {#each p.data.items as item, i (item.ref ?? `${i}-${item.title}`)}
          {@const level = known(item.level, LEVELS, 'info')}
          <li class="row">
            <span class="grow untrusted"><span aria-hidden="true">{glyph[level]}</span> {safeText(item.title, 120)}</span>
            <span class="chip" data-tone={level === 'info' ? 'muted' : level === 'warn' ? 'warn' : 'danger'}>{safeText(item.kind, 20).replace('_', ' ')}</span>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</Panel>
