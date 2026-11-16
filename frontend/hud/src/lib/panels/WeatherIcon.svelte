<script lang="ts">
  import { weatherKind } from './weather';

  let { code, size = 28 }: { code: number; size?: number } = $props();
  const kind = $derived(weatherKind(code));
</script>

<svg width={size} height={size} viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false" data-kind={kind}>
  {#if kind === 'clear'}
    <circle cx="16" cy="16" r="5" />
    <path d="M16 4v4M16 24v4M4 16h4M24 16h4M7.5 7.5l2.8 2.8M21.7 21.7l2.8 2.8M7.5 24.5l2.8-2.8M21.7 10.3l2.8-2.8" />
  {:else if kind === 'partly'}
    <circle cx="12" cy="12" r="4" />
    <path d="M12 4v2M4 12h2M6.3 6.3l1.4 1.4M17.7 6.3l-1.4 1.4" />
    <path d="M10 25h13a4.5 4.5 0 0 0 0-9 6 6 0 0 0-11.5 1.5A3.8 3.8 0 0 0 10 25z" />
  {:else if kind === 'cloud' || kind === 'unknown'}
    <path d="M8 24h16a5 5 0 0 0 0-10 7 7 0 0 0-13.5 1.5A4.2 4.2 0 0 0 8 24z" />
    {#if kind === 'unknown'}<path d="M16 17v0.1" />{/if}
  {:else if kind === 'fog'}
    <path d="M7 12h18M5 17h22M9 22h14" />
  {:else if kind === 'drizzle' || kind === 'rain'}
    <path d="M8 18h16a4.5 4.5 0 0 0 0-9 6.5 6.5 0 0 0-12.5 1.4A3.8 3.8 0 0 0 8 18z" />
    <path d={kind === 'rain' ? 'M11 22l-1.5 4M17 22l-1.5 4M23 22l-1.5 4' : 'M11 23v1M17 23v1M23 23v1'} />
  {:else if kind === 'snow'}
    <path d="M8 18h16a4.5 4.5 0 0 0 0-9 6.5 6.5 0 0 0-12.5 1.4A3.8 3.8 0 0 0 8 18z" />
    <path d="M11 23v0.1M17 24v0.1M23 23v0.1M14 27v0.1M20 27v0.1" stroke-width="2.4" />
  {:else if kind === 'storm'}
    <path d="M8 18h16a4.5 4.5 0 0 0 0-9 6.5 6.5 0 0 0-12.5 1.4A3.8 3.8 0 0 0 8 18z" />
    <path d="M17 19l-4 5h5l-3 5" />
  {/if}
</svg>
