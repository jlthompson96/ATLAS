<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { decimalSign, formatClock, formatDecimal, formatPct, safeText, trendGlyph } from '../format/text';
  import { portfolioUnlocked, typed, type PortfolioPanel } from './data';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store } = useHud();
  const p = $derived(typed(store.panels.portfolio) as PortfolioPanel | undefined);

  let passphrase = $state('');
  let busy = $state(false);
  let error = $state('');

  async function unlock(e: SubmitEvent): Promise<void> {
    e.preventDefault();
    if (!passphrase || busy) return;
    busy = true;
    error = '';
    const r = await store.unlockPortfolio(passphrase);
    busy = false;
    passphrase = ''; // never kept around after sending
    if (!r.ok) error = r.message;
  }
</script>

<Panel
  id="portfolio"
  title="Portfolio"
  sensitivity="secret"
  exposed={!!p && !p.redacted && !p.data.locked}
  status={p ? (p.data.locked ? '✗ LOCKED' : '● UNLOCKED') : ''}
  statusTone={p && !p.data.locked ? 'ok' : 'muted'}
  staleSince={p?.update.stale_since}
  {collapsible}
  collapsed={collapsible}
>
  {#if !p}
    <p class="empty">No portfolio data.</p>
  {:else if p.data.locked}
    <p class="empty">Locked until you enter the passphrase. Market data keeps updating.</p>
    {#if store.canUnlock && !p.redacted}
      <form onsubmit={unlock} class="unlock">
        <label class="sub" for="pf-pass">Portfolio passphrase</label>
        <div class="row">
          <input id="pf-pass" class="input grow" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" bind:value={passphrase} />
          <button class="btn primary" type="submit" disabled={busy || !passphrase}>Unlock</button>
        </div>
        {#if error}<p class="err" role="alert">{error}</p>{/if}
      </form>
    {:else if !store.canUnlock}
      <p class="sub">This device can't unlock the portfolio. Use a remote device.</p>
    {/if}
  {:else if p.redacted}
    {@const pct = (p.data as { day_change_pct: number }).day_change_pct}
    <p class="big mono" class:pos={pct > 0} class:neg={pct < 0}><span aria-hidden="true">{trendGlyph(Math.sign(pct))}</span> {formatPct(pct)} today</p>
    <p class="empty">Amounts hidden in privacy mode.</p>
  {:else if portfolioUnlocked(p.data)}
    {@const d = p.data}
    {@const ds = decimalSign(d.day_change)}
    <p class="value mono"><span class="sub">USD</span> {formatDecimal(d.total_value)}</p>
    <p class="mono day" class:pos={ds > 0} class:neg={ds < 0}>
      <span aria-hidden="true">{trendGlyph(ds)}</span>
      {ds > 0 ? '+' : ''}{formatDecimal(d.day_change)} ({formatPct(d.day_change_pct)}) today
    </p>
    {#if d.total_gain !== undefined}
      {@const gs = decimalSign(d.total_gain)}
      <p class="mono sub" class:pos={gs > 0} class:neg={gs < 0}>
        Total gain {gs > 0 ? '+' : ''}{formatDecimal(d.total_gain)}{d.total_gain_pct !== undefined ? ` (${formatPct(d.total_gain_pct)})` : ''}
      </p>
    {/if}
    <p class="sub">As of {formatClock(d.as_of)}{d.nav_as_of ? ` · fund NAV as of ${d.nav_as_of}` : ''}</p>
    {#if d.top_movers.length}
      <h3 class="sub">Top movers</h3>
      <ul class="list">
        {#each d.top_movers as m (m.symbol)}
          {@const s = m.change_pct > 0 ? 1 : m.change_pct < 0 ? -1 : 0}
          <li class="row">
            <span class="mono">{m.symbol}</span>
            <span class="sub mono">{m.weight_pct !== undefined ? `${m.weight_pct.toFixed(1)}%` : ''}{m.value ? ` · ${formatDecimal(m.value)}` : ''}</span>
            <span class="mono" class:pos={s > 0} class:neg={s < 0}><span aria-hidden="true">{trendGlyph(s)}</span> {formatPct(m.change_pct)}</span>
          </li>
        {/each}
      </ul>
    {/if}
    {#if d.alerts?.length}
      <ul class="list alerts" aria-label="Portfolio alerts">
        {#each d.alerts as a (a.alert_id)}
          <li><span class="flag"><span aria-hidden="true">▲</span> {a.symbol}</span> {safeText(a.message, 120)}</li>
        {/each}
      </ul>
    {/if}
  {/if}
</Panel>

<style>
  .unlock {
    margin-top: 8px;
    display: grid;
    gap: 4px;
  }
  .err {
    margin: 2px 0 0;
    color: var(--danger);
    font-size: 0.82rem;
  }
  .value {
    margin: 0;
    font-size: 1.5rem;
  }
  .day,
  .big {
    margin: 0 0 2px;
  }
  .big {
    font-size: 1.4rem;
  }
  h3 {
    margin: 8px 0 2px;
    font-weight: 500;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .alerts {
    margin-top: 6px;
    font-size: 0.82rem;
  }
</style>
