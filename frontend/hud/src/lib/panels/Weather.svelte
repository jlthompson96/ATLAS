<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { safeText, formatClock } from '../format/text';
  import { typed, type WeatherPanel } from './data';
  import { formatTemp } from './weather';
  import WeatherIcon from './WeatherIcon.svelte';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store } = useHud();
  const p = $derived(typed(store.panels.weather) as WeatherPanel | undefined);
  const hourly = $derived(p?.data.hourly?.slice(0, 6) ?? []);
</script>

<Panel id="weather" title="Weather" staleSince={p?.update.stale_since} {collapsible} collapsed={collapsible}>
  {#if p}
    {@const w = p.data}
    <div class="now">
      <WeatherIcon code={w.condition_code} size={40} />
      <div class="temp mono">{formatTemp(w.temp_c)}C</div>
      <div class="desc">
        <div>{safeText(w.condition, 40)}</div>
        {#if w.location_label}<div class="sub">{safeText(w.location_label, 40)}</div>{/if}
      </div>
    </div>
    <dl class="kv">
      <dt>High / low</dt>
      <dd>{formatTemp(w.high_c)} / {formatTemp(w.low_c)}</dd>
      {#if w.feels_like_c !== undefined}<dt>Feels like</dt><dd>{formatTemp(w.feels_like_c)}</dd>{/if}
      {#if w.precip_prob_pct !== undefined}<dt>Rain chance</dt><dd>{Math.round(w.precip_prob_pct)}%</dd>{/if}
      {#if w.wind_kph !== undefined}<dt>Wind</dt><dd>{Math.round(w.wind_kph)} km/h</dd>{/if}
    </dl>
    {#if hourly.length}
      <ul class="hours" aria-label="Next hours">
        {#each hourly as h (h.t)}
          <li>
            <span class="sub mono">{formatClock(h.t)}</span>
            <WeatherIcon code={h.condition_code} size={20} />
            <span class="mono">{formatTemp(h.temp_c)}</span>
          </li>
        {/each}
      </ul>
    {/if}
    <!-- Required by Open-Meteo's CC BY 4.0 licence (PLAN.md §2.15). Plain text, no link: the HUD makes no external requests. -->
    <p class="attribution sub">{w.attribution}</p>
  {:else}
    <p class="empty">Waiting for weather data.</p>
  {/if}
</Panel>

<style>
  .now {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--accent);
    margin-bottom: 6px;
  }
  .temp {
    font-size: 1.9rem;
    color: var(--text);
  }
  .desc {
    color: var(--text);
    font-size: 0.9rem;
  }
  .hours {
    list-style: none;
    margin: 8px 0 4px;
    padding: 0;
    display: flex;
    justify-content: space-between;
    gap: 4px;
    color: var(--accent);
  }
  .hours li {
    display: grid;
    justify-items: center;
    gap: 1px;
    font-size: 0.75rem;
    color: var(--text);
  }
  .attribution {
    margin: 8px 0 0;
  }
</style>
