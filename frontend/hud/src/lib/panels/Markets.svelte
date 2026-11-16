<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import Sparkline from '../ui/Sparkline.svelte';
  import { useHud } from '../ctx';
  import { formatClock, formatDecimal, formatPct, safeText, trendGlyph } from '../format/text';
  import { known, typed, type MarketsPanel } from './data';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store } = useHud();
  const p = $derived(typed(store.panels.markets) as MarketsPanel | undefined);
  const SESSION = {
    pre: 'PRE-MARKET',
    open: 'MARKET OPEN',
    after: 'AFTER HOURS',
    closed: 'MARKET CLOSED',
    holiday: 'MARKET HOLIDAY'
  } as const;
  const session = $derived(known(p?.data.session, ['pre', 'open', 'after', 'closed', 'holiday'] as const, 'closed'));
</script>

<Panel id="markets" title="Markets" sensitivity="personal" status={p ? SESSION[session] : ''} statusTone={session === 'open' ? 'ok' : 'muted'} staleSince={p?.update.stale_since} {collapsible} collapsed={collapsible}>
  {#if p}
    {#if p.data.quotes.length === 0}
      <p class="empty">No quotes.</p>
    {:else}
      <ul class="list">
        {#each p.data.quotes as q (q.symbol)}
          {@const sign = q.change_pct > 0 ? 1 : q.change_pct < 0 ? -1 : 0}
          <li class="quote">
            <div class="who">
              <span class="sym mono">{q.symbol}</span>
              <span class="sub truncate">{safeText(q.proxy_for ? `${q.proxy_for} (ETF proxy)` : (q.label ?? ''), 60)}</span>
            </div>
            <Sparkline values={q.sparkline ?? []} label={`${q.symbol} trend`} width={60} height={20} />
            <div class="px">
              <span class="mono">{formatDecimal(q.price)}</span>
              <span class="mono" class:pos={sign > 0} class:neg={sign < 0}><span aria-hidden="true">{trendGlyph(sign)}</span> {formatPct(q.change_pct)}</span>
            </div>
            {#if q.delayed}
              <span class="flag" title={`Quote as of ${formatClock(q.as_of)}`}>◇ DELAYED {formatClock(q.as_of)}</span>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if p.data.credits}
      <p class="sub credits">Data credits today: {p.data.credits.used_today} / {p.data.credits.limit_today}</p>
    {/if}
  {:else}
    <p class="empty">Waiting for market data.</p>
  {/if}
</Panel>

<style>
  .quote {
    display: grid;
    grid-template-columns: 1fr auto auto;
    align-items: center;
    gap: 2px 8px;
    padding: 3px 0;
    border-bottom: 1px solid rgba(31, 209, 255, 0.12);
  }
  .who {
    display: grid;
    min-width: 0;
  }
  .sym {
    font-weight: 600;
  }
  .px {
    display: grid;
    justify-items: end;
    font-size: 0.82rem;
  }
  .quote .flag {
    grid-column: 1 / -1;
    justify-self: start;
  }
  .credits {
    margin: 6px 0 0;
  }
</style>
