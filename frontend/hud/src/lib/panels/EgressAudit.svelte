<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { formatAge } from '../format/text';
  import { known, typed, type EgressPanel } from './data';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store, ticker } = useHud();
  const p = $derived(typed(store.panels.egress_audit) as EgressPanel | undefined);
  const proxy = $derived(known(p?.data.egress.proxy, ['up', 'down'] as const, 'down'));
  const chain = $derived(known(p?.data.audit.chain, ['ok', 'broken', 'unverified'] as const, 'unverified'));
</script>

<Panel id="egress_audit" title="Egress / audit" staleSince={p?.update.stale_since} {collapsible} collapsed={collapsible}>
  {#if p}
    {@const d = p.data}
    <ul class="list">
      <li>
        <span class="chip" data-tone={proxy === 'up' ? 'ok' : 'danger'}>
          <span aria-hidden="true">{proxy === 'up' ? '●' : '✗'}</span>
          EGRESS PROXY {proxy === 'up' ? 'UP' : 'DOWN'}
        </span>
      </li>
      <li>
        <span class="chip" data-tone={chain === 'ok' ? 'ok' : chain === 'broken' ? 'danger' : 'warn'}>
          <span aria-hidden="true">{chain === 'ok' ? '✓' : chain === 'broken' ? '✗' : '?'}</span>
          {#if chain === 'ok'}CHAIN ✓ VERIFIED{:else if chain === 'broken'}CHAIN ✗ BROKEN{:else}CHAIN UNVERIFIED{/if}
        </span>
      </li>
    </ul>
    {#if chain === 'broken'}
      <p class="alarm" role="alert">Audit chain verification failed. Records may have been altered.</p>
    {/if}
    <dl class="kv">
      <dt>Allowed 24 h</dt>
      <dd>{d.egress.allowed_24h}</dd>
      <dt>Blocked 24 h</dt>
      <dd class:neg={d.egress.blocked_24h > 0}>{d.egress.blocked_24h}</dd>
      <dt>Audit records 24 h</dt>
      <dd>{d.audit.records_24h}</dd>
      {#if d.audit.last_verified_at}<dt>Last verified</dt><dd>{formatAge(d.audit.last_verified_at, store.toServer(ticker.now))}</dd>{/if}
    </dl>
  {:else}
    <p class="empty">Waiting for audit status.</p>
  {/if}
</Panel>

<style>
  .alarm {
    margin: 6px 0;
    padding: 4px 8px;
    border: 1px solid var(--danger);
    color: var(--danger);
    font-size: 0.82rem;
  }
</style>
