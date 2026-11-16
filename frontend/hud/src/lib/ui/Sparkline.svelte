<script lang="ts">
  let { values, label = 'Trend', width = 84, height = 22 }: { values: readonly number[]; label?: string; width?: number; height?: number } = $props();

  const pts = $derived.by(() => {
    const v = values.filter((n) => Number.isFinite(n));
    if (v.length < 2) return '';
    const min = Math.min(...v);
    const max = Math.max(...v);
    const span = max - min || 1;
    return v
      .map((n, i) => `${((i / (v.length - 1)) * (width - 2) + 1).toFixed(1)},${(height - 2 - ((n - min) / span) * (height - 4)).toFixed(1)}`)
      .join(' ');
  });
  const direction = $derived(values.length >= 2 && (values[values.length - 1] ?? 0) >= (values[0] ?? 0) ? 'up' : 'down');
</script>

{#if pts}
  <svg {width} {height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${label}: trending ${direction}`} class:up={direction === 'up'} class:down={direction === 'down'}>
    <polyline points={pts} fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
  </svg>
{/if}

<style>
  svg {
    display: block;
    color: var(--muted);
  }
  .up {
    color: var(--ok);
  }
  .down {
    color: var(--danger);
  }
</style>
