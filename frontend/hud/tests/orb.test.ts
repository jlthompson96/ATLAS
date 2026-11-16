// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { StateValues } from '../src/lib/generated/events';
import { OrbAnimator } from '../src/lib/orb/animator';
import { EnvelopeBuffer } from '../src/lib/orb/envelope';
import { QualityGovernor } from '../src/lib/orb/governor';
import Orb from '../src/lib/orb/Orb.svelte';
import { describeOrb, ALL_STATES } from '../src/lib/orb/spec';
import { frame, mountHud, q, settle, snapshot, type Harness } from './helpers';

describe('describeOrb: one distinct, labelled look per state (§4.3)', () => {
  const payload = (state: string, extra: Record<string, unknown> = {}) => ({ state: state as never, ...extra });

  it('covers every state in the contract', () => {
    expect([...ALL_STATES].sort()).toEqual([...StateValues].sort());
  });

  it('gives each state its own label and shape, so status never relies on colour alone', () => {
    const specs = ALL_STATES.map((s) =>
      describeOrb(
        payload(s, { detail: 'token rejected', code: s === 'error' ? 'LLM_AUTH_FAILED' : 'CORE_UNREACHABLE', wake: s === 'waking_host' ? { stage: 'link', elapsed_ms: 12_400, deadline_ms: 60_000 } : undefined })
      )
    );
    expect(new Set(specs.map((s) => s.label)).size).toBe(specs.length);
    expect(new Set(specs.map((s) => s.shape)).size).toBe(specs.length);
    expect(Object.fromEntries(specs.map((s) => [s.state, s.label]))).toEqual({
      idle: 'STANDBY',
      listening: 'LISTENING',
      thinking: 'PROCESSING',
      speaking: 'SPEAKING',
      waking_host: 'WAKING CORE · 12 s',
      error: 'ERROR: token rejected',
      llm_unreachable: 'CORE OFFLINE · LOCAL MODE'
    });
  });

  it('shows the wake stage and the elapsed seconds', () => {
    const s = describeOrb(payload('waking_host', { wake: { stage: 'api', elapsed_ms: 9_000, deadline_ms: 60_000 } }));
    expect(s.stageIndex).toBe(2);
    expect(s.stages).toEqual(['packet', 'link', 'api', 'model']);
    expect(s.label).toBe('WAKING CORE · 9 s');
    expect(describeOrb(payload('waking_host', { wake: { stage: 'api', elapsed_ms: 9_000, deadline_ms: 60_000 } }), 31_500).label).toBe('WAKING CORE · 31 s');
  });

  it('labels each llm_unreachable code', () => {
    expect(describeOrb(payload('llm_unreachable', { code: 'SIGNED_OUT' })).label).toBe('CORE OFFLINE · SIGNED OUT');
    expect(describeOrb(payload('llm_unreachable', { code: 'LINK_DOWN' })).label).toBe('CORE OFFLINE · LINK DOWN');
  });

  it('maps an unknown future state to a neutral "unknown" look', () => {
    const s = describeOrb(payload('hovering'));
    expect(s.state).toBe('unknown');
    expect(s.label).toBe('STATE UNKNOWN');
  });

  it('strips control and bidi characters from the error detail', () => {
    const s = describeOrb(payload('error', { detail: 'bad‮token\u0007', code: 'X' }));
    expect(s.label).toBe('ERROR: badtoken');
  });
});

describe('OrbAnimator', () => {
  it('flashes once on error (300 ms) unless motion is reduced', () => {
    const a = new OrbAnimator(false);
    a.setSpec(describeOrb({ state: 'error', code: 'X', detail: 'x' }), 0);
    expect(a.step(0.016, 0.05, { level: 0 }).flash).toBeGreaterThan(0.5);
    expect(a.step(0.016, 0.31, { level: 0 }).flash).toBe(0);

    const r = new OrbAnimator(true);
    r.setSpec(describeOrb({ state: 'error', code: 'X', detail: 'x' }), 0);
    expect(r.step(0.016, 0.05, { level: 0 }).flash).toBe(0);
  });

  it('stops all rotation under reduced motion', () => {
    const a = new OrbAnimator(true);
    a.setSpec(describeOrb({ state: 'thinking' }), 0);
    const f = a.step(1, 1, { level: 0 });
    expect(f.arcAngle).toBe(0);
    expect(f.counterAngle).toBe(0);
    expect(f.particles).toBe(0);
  });

  it('follows the audio level only in listening/speaking', () => {
    const a = new OrbAnimator(false);
    a.setSpec(describeOrb({ state: 'idle' }), 0);
    for (let i = 0; i < 20; i++) a.step(0.016, i * 0.016, { level: 1 });
    expect(a.frame.level).toBeLessThan(0.01);
    a.setSpec(describeOrb({ state: 'listening' }), 1);
    for (let i = 0; i < 20; i++) a.step(0.016, 1 + i * 0.016, { level: 1 });
    expect(a.frame.level).toBeGreaterThan(0.8);
  });
});

describe('EnvelopeBuffer: audio-reactive sync by t_play (§4.4)', () => {
  it('samples the envelope at the playback time, not the arrival time', () => {
    const b = new EnvelopeBuffer();
    const tPlay = 10_000;
    // Batch arrives (conceptually) >= 100 ms early; frames are 16 ms apart.
    b.push(tPlay, 16, [0, 0.5, 1, 0.5, 0]);
    expect(b.sample(tPlay)).toBe(0);
    expect(b.sample(tPlay + 16)).toBeCloseTo(0.5);
    expect(b.sample(tPlay + 32)).toBeCloseTo(1);
    expect(b.sample(tPlay + 24)).toBeCloseTo(0.75); // linear between frames
    expect(b.sample(tPlay - 1)).toBe(0); // before the batch is audible
  });

  it('holds then fades after the newest frame', () => {
    const b = new EnvelopeBuffer(48, 120);
    b.push(0, 16, [1]);
    expect(b.sample(40)).toBe(1);
    expect(b.sample(48 + 60)).toBeCloseTo(0.5);
    expect(b.sample(48 + 120)).toBe(0);
  });

  it('keeps batches in time order even if they arrive out of order, and prunes old frames', () => {
    const b = new EnvelopeBuffer(48, 120, 500);
    b.push(1000, 16, [0.2, 0.4]);
    b.push(500, 16, [0.6, 0.8]);
    expect(b.sample(500)).toBeCloseTo(0.6);
    expect(b.sample(1000)).toBeCloseTo(0.2);
    b.prune(2000);
    expect(b.size).toBe(0);
  });

  it('converts through the clock offset: client time + offset = server time', () => {
    const b = new EnvelopeBuffer();
    const offset = 250; // server clock is 250 ms ahead of the client
    b.push(5_000, 16, [0, 1, 0]);
    const clientNowAtPeak = 5_016 - offset; // the instant the client should show the peak
    expect(b.sample(clientNowAtPeak + offset)).toBeCloseTo(1);
    // Ignoring the offset would show the wrong value (this is the failure the clock sync prevents).
    expect(b.sample(clientNowAtPeak)).toBe(0);
  });

  it('clamps garbage levels into 0..1', () => {
    const b = new EnvelopeBuffer();
    b.push(0, 16, [5, -3, Number.NaN]);
    expect([b.sample(0), b.sample(16), b.sample(32)]).toEqual([1, 0, 0]);
  });
});

describe('QualityGovernor: auto-degrade and recover (UP-4 logic; fps itself is measured on the Pi)', () => {
  const feed = (g: QualityGovernor, from: number, to: number, ms: number): void => {
    for (let t = from; t <= to; t += 33) g.recordFrame(ms, t);
  };

  it('stays at full while frames are on budget', () => {
    const g = new QualityGovernor({ ceiling: 'full' }, 0);
    feed(g, 0, 30_000, 30);
    expect(g.tier).toBe('full');
  });

  it('drops a level when p95 frame time exceeds 40 ms for 10 s, one level at a time', () => {
    const g = new QualityGovernor({ ceiling: 'full' }, 0);
    feed(g, 0, 9_000, 60);
    expect(g.tier).toBe('full'); // not yet 10 s
    feed(g, 9_000, 12_000, 60);
    expect(g.tier).toBe('lite');
    feed(g, 12_000, 30_000, 60);
    expect(g.tier).toBe('static');
  });

  it('drops a level when the SoC is hotter than 75 C', () => {
    const g = new QualityGovernor({ ceiling: 'full' }, 0);
    g.recordTemp(74, 1000);
    expect(g.tier).toBe('full');
    g.recordTemp(78, 2000);
    expect(g.tier).toBe('lite');
  });

  it('recovers one level after 5 minutes under target, not before', () => {
    const g = new QualityGovernor({ ceiling: 'full' }, 0);
    feed(g, 0, 10_100, 60);
    expect(g.tier).toBe('lite');
    feed(g, 10_100, 10_100 + 200_000, 20);
    expect(g.tier).toBe('lite');
    feed(g, 210_100, 10_100 + 330_000, 20);
    expect(g.tier).toBe('full');
  });

  it('never exceeds the ceiling (reduced motion or the user choosing lite)', () => {
    const g = new QualityGovernor({ ceiling: 'lite' }, 0);
    expect(g.tier).toBe('lite');
    feed(g, 0, 400_000, 10);
    expect(g.tier).toBe('lite');
  });

  it('does not recover while still hot', () => {
    const g = new QualityGovernor({ ceiling: 'full' }, 0);
    g.recordTemp(80, 1000);
    expect(g.tier).toBe('lite');
    g.recordTemp(80, 400_000);
    expect(g.tier).not.toBe('full');
  });
});

describe('Orb component, static tier: a snapshot per state (§8 Phase 2: snapshot tests for every state)', () => {
  let h: Harness | undefined;
  afterEach(() => h?.cleanup());

  const states: [string, Record<string, unknown>][] = [
    ['idle', {}],
    ['listening', {}],
    ['thinking', {}],
    ['speaking', {}],
    ['waking_host', { wake: { stage: 'link', elapsed_ms: 12_000, deadline_ms: 60_000 } }],
    ['error', { code: 'LLM_AUTH_FAILED', detail: 'token rejected' }],
    ['llm_unreachable', { code: 'CORE_UNREACHABLE' }]
  ];

  it.each(states)('%s', async (state, extra) => {
    h = mountHud(Orb, { snapshot: snapshot({ state: { state, ...extra } as never }) });
    await settle();
    const orb = q(h.target, '.orb') as HTMLElement;
    expect(orb.dataset.tier).toBe('static');
    const svg = q(h.target, 'svg.static-orb') as SVGElement;
    expect({
      label: q(h.target, '[data-testid=orb-label]')?.textContent,
      ariaLabel: orb.getAttribute('aria-label'),
      shape: svg.dataset.shape,
      state: svg.dataset.state,
      stages: Array.from(h.target.querySelectorAll('.stages li')).map((li) => `${li.getAttribute('data-stage')}:${li.textContent?.replace(/\s+/g, ' ').trim()}`),
      segments: Array.from(svg.querySelectorAll('[data-segment]')).map((p) => p.getAttribute('data-segment')),
      progress: q(h.target, 'progress')?.getAttribute('value')
    }).toMatchSnapshot();
  });

  it('updates when state.changed arrives, announcing it to screen readers', async () => {
    h = mountHud(Orb);
    await settle();
    expect(q(h.target, '[data-testid=orb-label]')?.textContent).toBe('STANDBY');
    h.store.apply(frame('state.changed', { state: 'thinking' }));
    await settle();
    expect(q(h.target, '[data-testid=orb-label]')?.textContent).toBe('PROCESSING');
    expect(h.store.announcement).toBe('PROCESSING');
  });

  it('forces the static tier under prefers-reduced-motion and shows a level bar instead of animation', async () => {
    h = mountHud(Orb, { snapshot: snapshot({ state: { state: 'listening' } }) });
    h.settings.set({ quality: 'full' });
    h.settings.systemReducedMotion = true;
    await settle();
    expect((q(h.target, '.orb') as HTMLElement).dataset.tier).toBe('static');
    expect(q(h.target, 'canvas')).toBeNull();
    expect(q(h.target, '[role=meter]')).not.toBeNull();
    // The in-app override wins over the system setting.
    h.settings.set({ motion: 'full', quality: 'static' });
    expect(h.settings.reducedMotion).toBe(false);
  });
});
