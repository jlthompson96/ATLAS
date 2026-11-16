<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { formatClock, safeText } from '../format/text';
  import { typed, type EmailPanel } from './data';
  import AuthChip from './AuthChip.svelte';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store } = useHud();
  const p = $derived(typed(store.panels.email) as EmailPanel | undefined);
  const unread = $derived(p ? p.data.unread : 0);
  const FLAG_TEXT: Record<string, string> = { new_sender: 'NEW SENDER', suspicious: 'SUSPICIOUS' };
</script>

<Panel id="email" title="Email" sensitivity="secret" exposed={!!p && !p.redacted} status={p ? `${unread} new` : ''} staleSince={p?.update.stale_since} {collapsible} collapsed={collapsible}>
  {#if !p}
    <p class="empty">No email data.</p>
  {:else if p.redacted}
    <p class="empty">{p.data.unread} unread. Details hidden in privacy mode.</p>
  {:else}
    <p class="auth"><AuthChip auth={p.data.auth} /></p>
    {#if p.data.items.length === 0}
      <p class="empty">Inbox is clear.</p>
    {:else}
      <ul class="list">
        {#each p.data.items.slice(0, 8) as m (m.id)}
          <li>
            <div class="row">
              <span class="grow truncate untrusted" class:unread={m.unread}>
                <span aria-hidden="true">{m.unread ? '●' : '○'}</span>
                <span class="sr-only">{m.unread ? 'Unread: ' : 'Read: '}</span>
                {safeText(m.from_display, 80)}
              </span>
              <span class="sub mono">{formatClock(m.received_at)}</span>
            </div>
            <div class="subject truncate untrusted">{safeText(m.subject, 200)}</div>
            <div class="sub truncate">
              {safeText(m.from_domain, 100)}
              {#each m.flags ?? [] as f (f)}
                <span class="flag" class:bad={f === 'suspicious'}>▲ {FLAG_TEXT[f] ?? 'FLAGGED'}</span>
              {/each}
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
  .unread {
    font-weight: 600;
  }
  .subject {
    font-size: 0.85rem;
  }
  li {
    padding: 2px 0;
    border-bottom: 1px solid rgba(31, 209, 255, 0.12);
  }
</style>
