<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { safeText } from '../format/text';
  import { known } from './data';
  import { voiceBadge } from './hostview';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store } = useHud();
  const host = $derived(store.host);
  const gpu = $derived(host?.llm.gpu);
  const asleep = $derived(known(host?.llm.power, ['asleep', 'waking', 'up', 'unknown'] as const, 'unknown') === 'asleep');
  const voice = $derived(voiceBadge(host));
  /** Daily microSD write budget (PLAN.md §3.5). */
  const BUDGET_MB = 100;
</script>

<Panel id="system" title="System" {collapsible} collapsed={collapsible} status={host?.pi.throttled ? '▲ THROTTLED' : ''} statusTone="warn">
  {#if host}
    <h3 class="sub">Pi</h3>
    <dl class="kv">
      <dt><label for="m-cpu">CPU</label></dt>
      <dd>{Math.round(host.pi.cpu_pct)}%</dd>
    </dl>
    <meter id="m-cpu" min="0" max="100" low="70" high="90" optimum="10" value={host.pi.cpu_pct}>{host.pi.cpu_pct}%</meter>
    <dl class="kv">
      <dt><label for="m-ram">RAM</label></dt>
      <dd>{Math.round(host.pi.ram_pct)}%</dd>
    </dl>
    <meter id="m-ram" min="0" max="100" low="70" high="90" optimum="10" value={host.pi.ram_pct}>{host.pi.ram_pct}%</meter>
    <dl class="kv">
      <dt>Temp</dt>
      <dd class:neg={host.pi.temp_c > 75}>{host.pi.temp_c.toFixed(0)}°C{host.pi.temp_c > 75 ? ' ▲' : ''}</dd>
      <dt><label for="m-disk">Writes 24 h</label></dt>
      <dd>{host.pi.disk_write_mb_24h.toFixed(0)} / {BUDGET_MB} MB</dd>
    </dl>
    <meter id="m-disk" min="0" max={BUDGET_MB} low={BUDGET_MB * 0.7} high={BUDGET_MB * 0.9} optimum="10" value={Math.min(host.pi.disk_write_mb_24h, BUDGET_MB)}>{host.pi.disk_write_mb_24h} MB</meter>

    <h3 class="sub">GPU (core host)</h3>
    {#if gpu}
      <dl class="kv">
        <dt>Util</dt>
        <dd>{Math.round(gpu.util_pct)}%</dd>
        <dt>VRAM</dt>
        <dd>{gpu.vram_used_mb} / {gpu.vram_total_mb} MB</dd>
        <dt>Temp</dt>
        <dd>{gpu.temp_c.toFixed(0)}°C</dd>
        {#if host.llm.model_loaded}<dt>Model</dt><dd class="truncate">{safeText(host.llm.model_loaded, 100)}</dd>{/if}
      </dl>
    {:else}
      <p class="empty">{asleep ? '— asleep —' : '— no GPU data —'}</p>
    {/if}

    <p class="voice"><span class="chip" data-tone={voice.tone}><span aria-hidden="true">{voice.glyph}</span> {voice.text}</span></p>
  {:else}
    <p class="empty">Waiting for host status.</p>
  {/if}
</Panel>

<style>
  h3 {
    margin: 6px 0 2px;
    font-weight: 500;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .voice {
    margin: 8px 0 0;
  }
  .voice .chip {
    white-space: normal;
  }
</style>
