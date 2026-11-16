<script lang="ts">
  import type { OrbSpec } from './spec';

  /** The "static" tier: no animation loop. Redrawn only when the state changes (event-driven), ~0 CPU. */
  let { spec, level = 0 }: { spec: OrbSpec; level?: number } = $props();

  const C = 120;
  const arc = (r: number, a0: number, a1: number): string => {
    const p = (a: number): string => `${(C + r * Math.cos(a)).toFixed(2)} ${(C + r * Math.sin(a)).toFixed(2)}`;
    return `M ${p(a0)} A ${r} ${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${p(a1)}`;
  };
  const wave = (base: number, amp: number): string => {
    const pts: string[] = [];
    for (let i = 0; i <= 120; i++) {
      const a = (i / 120) * Math.PI * 2;
      const r = base + amp * Math.sin(a * 7) * (0.5 + 0.5 * Math.sin(a * 3));
      pts.push(`${i === 0 ? 'M' : 'L'} ${(C + r * Math.cos(a)).toFixed(2)} ${(C + r * Math.sin(a)).toFixed(2)}`);
    }
    return `${pts.join(' ')} Z`;
  };
  const segs = [0, 1, 2, 3];
</script>

<svg viewBox="0 0 240 240" class="static-orb" data-state={spec.state} data-shape={spec.shape} aria-hidden="true" focusable="false" fill="none" stroke={spec.cssColor} stroke-linecap="round">
  <circle cx={C} cy={C} r="30" fill={spec.cssColor} fill-opacity={Math.min(0.9, spec.core + level * 0.4)} stroke="none" />
  {#if spec.shape === 'ring'}
    <circle cx={C} cy={C} r="84" stroke-width="3" stroke-dasharray="26 8" />
    <path d={arc(94, -0.5, 0.6)} stroke-width="2" stroke-opacity="0.55" />
  {:else if spec.shape === 'ring-wide'}
    <circle cx={C} cy={C} r="100" stroke-width="4" />
    <circle cx={C} cy={C} r="112" stroke-width="2" stroke-dasharray="2 8" stroke-opacity="0.7" />
  {:else if spec.shape === 'arcs'}
    <path d={arc(72, 0.3, 2.2)} stroke-width="5" />
    <path d={arc(94, 3.4, 4.9)} stroke-width="4" />
    <circle cx={C} cy={C} r="84" stroke-width="1" stroke-opacity="0.35" />
    {#each [0, 1, 2, 3, 4] as i (i)}
      <circle cx={C + 84 * Math.cos(i * 1.26)} cy={C + 84 * Math.sin(i * 1.26)} r="3" fill={spec.cssColor} stroke="none" />
    {/each}
  {:else if spec.shape === 'wave'}
    <path d={wave(88, 8 + level * 14)} stroke-width="3" />
  {:else if spec.shape === 'segments'}
    {#each segs as i (i)}
      <path
        d={arc(94, i * (Math.PI / 2) + 0.1, i * (Math.PI / 2) + Math.PI / 2 - 0.2)}
        stroke-width={i === spec.stageIndex ? 9 : 7}
        stroke-opacity={i < spec.stageIndex ? 0.95 : i === spec.stageIndex ? 0.7 : 0.18}
        data-segment={i < spec.stageIndex ? 'done' : i === spec.stageIndex ? 'active' : 'pending'}
      />
    {/each}
  {:else if spec.shape === 'broken'}
    <path d={arc(88, 1.6, 0.9 + Math.PI * 2)} stroke-width="4" stroke-opacity="0.8" />
    <path d="M 120 20 l -8 -10 M 128 24 l 8 -12" stroke-width="2" />
  {:else if spec.shape === 'broken-grey'}
    <path d={arc(88, 1.6, 0.9 + Math.PI * 2)} stroke-width="3" stroke-dasharray="3 9" stroke-opacity="0.8" />
  {:else}
    <circle cx={C} cy={C} r="86" stroke-width="2" stroke-dasharray="1 8" />
  {/if}
</svg>

<style>
  .static-orb {
    width: 100%;
    height: 100%;
    display: block;
  }
</style>
