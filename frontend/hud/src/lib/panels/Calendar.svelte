<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { formatClock, formatDay, safeText } from '../format/text';
  import { typed, type CalendarPanel } from './data';
  import AuthChip from './AuthChip.svelte';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store } = useHud();
  const p = $derived(typed(store.panels.calendar) as CalendarPanel | undefined);
</script>

<Panel id="calendar" title="Calendar" sensitivity="secret" exposed={!!p && !p.redacted} staleSince={p?.update.stale_since} {collapsible} collapsed={collapsible}>
  {#if !p}
    <p class="empty">No calendar data.</p>
  {:else if p.redacted}
    {#if p.data.next_event_start === null}
      <p class="empty">No upcoming events.</p>
    {:else}
      <p class="empty">Next event {formatDay(p.data.next_event_start)} {formatClock(p.data.next_event_start)}. Details hidden in privacy mode.</p>
    {/if}
  {:else}
    <p class="auth"><AuthChip auth={p.data.auth} /></p>
    {#if p.data.events.length === 0}
      <p class="empty">Nothing scheduled.</p>
    {:else}
      <ul class="list">
        {#each p.data.events.slice(0, 8) as e (e.id)}
          <li>
            <div class="row">
              <span class="grow truncate untrusted">{safeText(e.title, 120)}</span>
              <span class="sub mono">{e.all_day ? 'ALL DAY' : formatClock(e.start)}</span>
            </div>
            <div class="sub truncate untrusted">
              {formatDay(e.start)}{#if !e.all_day} · {formatClock(e.start)}–{formatClock(e.end)}{/if}{#if e.location} · {safeText(e.location, 80)}{/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</Panel>

<style>
  .auth {
    margin: 0 0 4px;
  }
  li {
    padding: 2px 0;
    border-bottom: 1px solid rgba(31, 209, 255, 0.12);
  }
</style>
