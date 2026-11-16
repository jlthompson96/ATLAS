<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { formatCountdown, safeText } from '../format/text';
  import type { FullAction } from '../approvals/hash';
  import ApprovalDialog from './ApprovalDialog.svelte';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store, ticker } = useHud();

  const KIND: Record<string, string> = {
    'email.send': 'EMAIL',
    'discord.post': 'DISCORD',
    'research.schedule': 'SCHEDULE',
    'portfolio.import': 'IMPORT'
  };

  let openId = $state<string | null>(null);
  const actions = $derived(store.actions);
  const open = $derived(actions.find((a): a is FullAction => a.action_id === openId && !a.redacted));
</script>

<Panel id="pending" title="Pending approvals" sensitivity="secret" exposed={actions.some((a) => !a.redacted)} status={actions.length ? `${actions.length} pending` : ''} statusTone="warn" {collapsible} collapsed={false}>
  {#if actions.length === 0}
    <p class="empty">Nothing waiting for approval.</p>
  {:else}
    <ul class="list">
      {#each actions as a (a.action_id)}
        {#if a.redacted}
          <li class="item"><span class="sub">A request is waiting. Details hidden in privacy mode.</span></li>
        {:else}
          {@const left = a.expires_at - store.toServer(ticker.now)}
          <li class="item">
            <div class="row">
              <span class="chip" data-tone="muted">{KIND[a.kind] ?? 'ACTION'}</span>
              <span class="sub mono">{left > 0 ? `◔ ${formatCountdown(left)}` : '✗ expired'}</span>
            </div>
            <div class="untrusted summary">{safeText(a.summary, 200)}</div>
            <div class="meta">
              {#if a.provenance.tainted}<span class="flag bad"><span aria-hidden="true">▲</span> untrusted source</span>{/if}
              {#if a.warnings?.length}<span class="flag"><span aria-hidden="true">▲</span> {a.warnings.length} warning{a.warnings.length === 1 ? '' : 's'}</span>{/if}
              {#if store.approved[a.action_id]}
                <span class="chip" data-tone="ok">✓ approved, running</span>
              {:else}
                <button class="btn small" type="button" onclick={() => (openId = a.action_id)} aria-label={`Review: ${safeText(a.summary, 80)}`}>Review</button>
              {/if}
            </div>
          </li>
        {/if}
      {/each}
    </ul>
  {/if}
</Panel>

{#if open}
  <ApprovalDialog action={open} onclose={() => (openId = null)} />
{/if}

<style>
  .item {
    padding: 4px 0 6px;
    border-bottom: 1px solid rgba(31, 209, 255, 0.12);
  }
  .summary {
    font-size: 0.9rem;
    margin: 2px 0;
  }
  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 6px;
    align-items: center;
  }
  .meta .btn {
    margin-left: auto;
  }
</style>
