<script lang="ts">
  import { useHud } from '../ctx';
  import { ALERT_TITLES } from '../format/codes';
  import { safeText } from '../format/text';
  import { known } from '../panels/data';

  const { store } = useHud();
  const LEVELS = ['info', 'warn', 'critical'] as const;
  const GLYPH = { info: '●', warn: '▲', critical: '✗' } as const;
  const LABEL = { info: 'INFO', warn: 'WARNING', critical: 'CRITICAL' } as const;
  const alerts = $derived(store.activeAlerts.slice(-4));
</script>

{#if alerts.length}
  <ul class="alerts" aria-label="Alerts">
    {#each alerts as a (a.alert_id)}
      {@const level = known(a.level, LEVELS, 'info')}
      <li class="alert" data-level={level}>
        <span class="lvl mono"><span aria-hidden="true">{GLYPH[level]}</span> {LABEL[level]}</span>
        <span class="msg">
          <strong>{ALERT_TITLES[a.code] ?? safeText(a.code, 48)}</strong>
          <span class="untrusted">{safeText(a.message, 200)}</span>
        </span>
        <button class="btn small" type="button" onclick={() => store.dismissAlert(a.alert_id)} aria-label={`Dismiss alert: ${ALERT_TITLES[a.code] ?? a.code}`}>✕</button>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .alerts {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 4px;
    width: min(520px, 100%);
  }
  .alert {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    gap: 8px;
    padding: 4px 8px;
    border: 1px solid var(--line-dim);
    background: rgba(5, 8, 13, 0.82);
    font-size: 0.85rem;
  }
  .alert[data-level='warn'] {
    border-color: var(--warn);
  }
  .alert[data-level='warn'] .lvl {
    color: var(--warn);
  }
  .alert[data-level='critical'] {
    border-color: var(--danger);
  }
  .alert[data-level='critical'] .lvl {
    color: var(--danger);
  }
  .lvl {
    font-size: 0.7rem;
    letter-spacing: 0.08em;
  }
  .msg {
    display: grid;
    min-width: 0;
  }
</style>
