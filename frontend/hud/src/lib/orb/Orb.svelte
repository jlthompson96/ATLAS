<script lang="ts">
  import { onMount } from 'svelte';
  import { useHud } from '../ctx';
  import { OrbAnimator, WAVE_POINTS } from './animator';
  import { QualityGovernor, type Tier } from './governor';
  import { LiteOrb } from './lite';
  import { describeOrb } from './spec';
  import StaticOrb from './StaticOrb.svelte';
  import { WebglOrb } from './webgl';

  const { store, settings, ticker } = useHud();

  // --- what to draw --------------------------------------------------------------------------------------
  const wake = $derived(store.orb.wake);
  /** Wake seconds advance locally between server updates, capped at the deadline. */
  const wakeElapsed = $derived(
    wake ? Math.min(wake.deadline_ms, wake.elapsed_ms + Math.max(0, ticker.now - Math.round(store.orbAt))) : undefined
  );
  const spec = $derived(describeOrb(store.orb, wakeElapsed));

  // --- tier selection (PLAN.md §2.14, §3.4) ---------------------------------------------------------------
  const ceiling = $derived<Tier>(settings.reducedMotion ? 'static' : settings.quality === 'auto' ? 'full' : settings.quality);
  const governor = new QualityGovernor({ ceiling: 'full' }, 0);
  let governed = $state<Tier>('full');
  let webglFailed = $state(false);
  const tier = $derived<Tier>(
    ceiling === 'static' || governed === 'static' ? 'static' : ceiling === 'lite' || governed === 'lite' || webglFailed ? 'lite' : 'full'
  );

  $effect(() => {
    governor.setCeiling(ceiling, performance.now());
    governed = governor.tier;
  });
  $effect(() => {
    const t = store.host?.pi.temp_c;
    if (t !== undefined) governed = governor.recordTemp(t, performance.now());
  });

  // --- canvas loop -------------------------------------------------------------------------------------------
  let canvas: HTMLCanvasElement | undefined = $state();
  const animator = new OrbAnimator(false, 0);
  const wave = new Array<number>(WAVE_POINTS).fill(0);

  $effect(() => {
    animator.reduceMotion = settings.reducedMotion;
    animator.setSpec(spec, performance.now() / 1000);
  });

  $effect(() => {
    const c = canvas;
    if (!c || tier === 'static') return;
    const kiosk = store.device?.role === 'kiosk';
    const lite = tier === 'lite';
    let renderer: WebglOrb | LiteOrb | null = lite ? LiteOrb.create(c) : WebglOrb.create(c);
    if (!renderer) {
      // WebGL2 unavailable (or the shader failed): fall back one level.
      if (!lite) webglFailed = true;
      else governed = 'static';
      return;
    }
    if (renderer instanceof WebglOrb) renderer.onLost = () => (webglFailed = true);
    animator.fx = lite ? 0 : 1;
    const fps = kiosk || lite ? 30 : 60;
    const minInterval = 1000 / fps - 2;
    let raf = 0;
    let last = 0;
    let n = 0;
    const origin = performance.timeOrigin;

    const resize = (): void => {
      const dpr = lite ? 0.5 : Math.min(window.devicePixelRatio || 1, kiosk ? 1 : 2);
      const w = Math.max(64, Math.round(c.clientWidth * dpr));
      const h = Math.max(64, Math.round(c.clientHeight * dpr));
      renderer?.resize(w, h);
    };
    resize();
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
    ro?.observe(c);

    const frame = (ts: number): void => {
      raf = requestAnimationFrame(frame);
      if (document.hidden || !renderer) return;
      if (last && ts - last < minInterval) return;
      const dt = last ? ts - last : 16;
      last = ts;
      const next = governor.recordFrame(dt, ts);
      if (next !== governed) {
        governed = next;
        return;
      }
      // Sample audio at the time this frame will be on screen, in server time (§4.4).
      const serverNow = store.toServer(origin + ts);
      let level = 0;
      if (spec.level === 'tts') {
        level = store.audio.tts.sample(serverNow);
        store.audio.tts.history(serverNow, 10, wave);
      } else if (spec.level === 'mic') {
        level = store.audio.mic.sample(serverNow);
      }
      if (++n % 120 === 0) {
        store.audio.tts.prune(serverNow);
        store.audio.mic.prune(serverNow);
      }
      renderer.draw(animator.step(Math.min(dt, 100) / 1000, ts / 1000, { level, wave }));
    };
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      ro?.disconnect();
      if (renderer instanceof WebglOrb) renderer.dispose();
      renderer = null;
    };
  });

  // Static tier: when frames stop, give recovery a chance after 5 minutes (governor is time-based there).
  onMount(() => {
    const t = setInterval(() => {
      governed = governor.tick(performance.now());
    }, 15_000);
    return () => clearInterval(t);
  });

  // --- static level bar (reduced motion): updates at 4 Hz, never faster -----------------------------------
  let bar = $state(0);
  $effect(() => {
    if (tier !== 'static' || spec.level === 'none') {
      bar = 0;
      return;
    }
    const kind = spec.level;
    const t = setInterval(() => {
      const now = store.toServer(Date.now());
      bar = kind === 'tts' ? store.audio.tts.sample(now) : store.audio.mic.sample(now);
    }, 250);
    return () => clearInterval(t);
  });

  const stageText: Record<string, string> = { packet: 'Packet', link: 'Link', api: 'API', model: 'Model' };
</script>

<div class="orb-wrap">
  <div class="orb" role="img" aria-label={`ATLAS state: ${spec.label}`} data-state={spec.state} data-tier={tier}>
    {#if tier === 'static'}
      <StaticOrb {spec} level={bar} />
    {:else}
      {#key tier}
        <canvas bind:this={canvas} aria-hidden="true"></canvas>
      {/key}
    {/if}
  </div>

  <p class="label mono" data-testid="orb-label" data-state={spec.state}>{spec.label}</p>

  {#if tier === 'static' && spec.level !== 'none'}
    <div class="levelbar" role="meter" aria-label={spec.level === 'mic' ? 'Microphone level' : 'Speech level'} aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(bar * 100)}>
      <div class="fill" style:width={`${Math.round(bar * 100)}%`}></div>
    </div>
  {/if}

  {#if wake && wakeElapsed !== undefined}
    <div class="wake">
      <ol class="stages" aria-label="Wake progress">
        {#each spec.stages as s, i (s)}
          <li data-stage={i < spec.stageIndex ? 'done' : i === spec.stageIndex ? 'active' : 'pending'}>
            <span aria-hidden="true">{i < spec.stageIndex ? '✓' : i === spec.stageIndex ? '◔' : '·'}</span>
            {stageText[s] ?? s}
            <span class="sr-only">{i < spec.stageIndex ? ' done' : i === spec.stageIndex ? ' in progress' : ' pending'}</span>
          </li>
        {/each}
      </ol>
      <progress max={wake.deadline_ms} value={wakeElapsed} aria-label="Time until wake deadline" aria-valuetext={`${Math.floor(wakeElapsed / 1000)} of ${Math.round(wake.deadline_ms / 1000)} seconds`}></progress>
      <p class="sub mono">{Math.floor(wakeElapsed / 1000)} s of {Math.round(wake.deadline_ms / 1000)} s</p>
    </div>
  {/if}

  {#if store.orb.state === 'error' && store.orb.code}
    <p class="sub mono code">{store.orb.code}</p>
  {/if}
</div>

<style>
  .orb-wrap {
    display: grid;
    justify-items: center;
    gap: 6px;
    min-height: 0;
  }
  .orb {
    width: min(100%, var(--orb-size, 460px));
    aspect-ratio: 1;
    position: relative;
  }
  canvas {
    width: 100%;
    height: 100%;
    display: block;
  }
  .label {
    margin: 0;
    font-size: 1.02rem;
    letter-spacing: 0.22em;
    text-align: center;
    color: var(--text);
  }
  .orb[data-state='error'] ~ .label {
    color: var(--danger);
  }
  .orb[data-state='waking_host'] ~ .label {
    color: var(--warn);
  }
  .orb[data-state='llm_unreachable'] ~ .label {
    color: var(--muted);
  }
  .levelbar {
    width: min(240px, 60%);
    height: 8px;
    border: 1px solid var(--line);
  }
  .fill {
    height: 100%;
    background: var(--accent);
  }
  .wake {
    display: grid;
    justify-items: center;
    gap: 4px;
    width: min(320px, 90%);
  }
  .stages {
    list-style: none;
    display: flex;
    justify-content: space-between;
    width: 100%;
    margin: 0;
    padding: 0;
    font-family: var(--font-data);
    font-size: 0.75rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .stages li[data-stage='done'] {
    color: var(--ok);
  }
  .stages li[data-stage='active'] {
    color: var(--warn);
  }
  .wake progress {
    width: 100%;
    height: 6px;
    accent-color: var(--warn);
  }
  .wake p {
    margin: 0;
  }
  .code {
    margin: 0;
  }
</style>
