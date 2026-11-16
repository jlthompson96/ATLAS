import type { OrbSpec, Rgb } from './spec';

export const WAVE_POINTS = 32;

/** Everything a renderer needs for one frame. Mutated in place by the animator (no per-frame allocation). */
export interface OrbFrame {
  time: number;
  color: [number, number, number];
  core: number;
  radius: number;
  alpha: number;
  arcAngle: number;
  counterAngle: number;
  sweepAngle: number;
  segments: number;
  /** Index of the wake segment currently filling (-1 none). */
  segActive: number;
  /** 0..1 pulse for the active segment. */
  segPulse: number;
  broken: number;
  flash: number;
  level: number;
  particles: number;
  counter: number;
  sweep: number;
  speaking: number;
  /** Radial waveform, newest sample first. */
  wave: number[];
  /** Scanline/grain strength: 0 in lite and static modes. */
  fx: number;
}

export interface AudioInput {
  /** Mic or TTS envelope value for this frame, 0..1. */
  level: number;
  /** Envelope history (newest first) for the radial waveform. */
  wave?: readonly number[];
}

const TAU = Math.PI * 2;
/** Time constant for state transitions: ~63% in 80 ms, ~95% in 240 ms (inside the 150-250 ms spec). */
const TAU_TRANSITION = 0.08;

const lerp = (a: number, b: number, k: number): number => a + (b - a) * k;

export function newFrame(): OrbFrame {
  return {
    time: 0,
    color: [0.25, 0.94, 1],
    core: 0,
    radius: 0.5,
    alpha: 1,
    arcAngle: 0,
    counterAngle: 0,
    sweepAngle: 0,
    segments: 0,
    segActive: -1,
    segPulse: 0,
    broken: 0,
    flash: 0,
    level: 0,
    particles: 0,
    counter: 0,
    sweep: 0,
    speaking: 0,
    wave: new Array<number>(WAVE_POINTS).fill(0),
    fx: 0
  };
}

/**
 * Turns the discrete orb state into smooth per-frame parameters. Colour, radius and glow ease toward the
 * target over ~200 ms; rotation angles integrate speed over time so a speed change never jumps.
 */
export class OrbAnimator {
  readonly frame: OrbFrame = newFrame();
  private spec: OrbSpec | null = null;
  private specChangedAt = 0;
  private flashStart = -1;
  private levelSmooth = 0;
  private color: [number, number, number] = [0.25, 0.94, 1];

  constructor(
    /** Reduced motion: no flash, no sweep, no drift; state still changes shape. */
    public reduceMotion = false,
    /** Strength of scanline/grain; 0 in lite mode. */
    public fx = 0
  ) {}

  setSpec(spec: OrbSpec, now: number): void {
    if (this.spec?.state !== spec.state) {
      this.specChangedAt = now;
      this.flashStart = spec.flash && !this.reduceMotion ? now : -1;
    }
    this.spec = spec;
  }

  /** Advance by `dt` seconds at absolute time `now` (seconds). */
  step(dt: number, now: number, audio: AudioInput): OrbFrame {
    const f = this.frame;
    const s = this.spec;
    if (!s) return f;
    const k = 1 - Math.exp(-dt / TAU_TRANSITION);
    const rm = this.reduceMotion;

    const target: Rgb = s.color;
    // Desaturated grey for llm_unreachable: pull the colour toward its own luma.
    const luma = target[0] * 0.3 + target[1] * 0.59 + target[2] * 0.11;
    for (let i = 0; i < 3; i++) {
      const c = lerp(target[i] as number, luma, s.desaturate * 0.5);
      this.color[i] = lerp(this.color[i] as number, c, k);
      f.color[i] = this.color[i] as number;
    }

    this.levelSmooth = lerp(this.levelSmooth, s.level === 'none' ? 0 : audio.level, 1 - Math.exp(-dt / 0.03));
    f.level = this.levelSmooth;

    const breathe = rm || s.breatheHz === 0 ? 0 : Math.sin(now * TAU * s.breatheHz) * 0.025;
    const levelLift = s.level === 'mic' ? f.level * 0.12 : 0;
    f.radius = lerp(f.radius, s.radius + breathe + levelLift, k);
    f.core = lerp(f.core, s.core + (s.level === 'tts' ? f.level * 0.5 : 0), k);

    let alpha = 1;
    if (s.blinkHz > 0 && !rm) alpha = 0.55 + 0.45 * (0.5 + 0.5 * Math.sin(now * TAU * s.blinkHz));
    f.alpha = lerp(f.alpha, alpha, k);

    const speed = rm ? 0 : s.arcSpeed;
    f.arcAngle = (f.arcAngle + speed * dt) % TAU;
    f.counterAngle = (f.counterAngle + (s.counterArcs && !rm ? speed * 1.3 * dt : 0)) % TAU;
    f.sweepAngle = (f.sweepAngle + (s.sweep && !rm ? 2.4 * dt : 0)) % TAU;
    f.sweep = s.sweep && !rm ? 1 : 0;
    f.counter = s.counterArcs ? 1 : 0;
    f.particles = s.particles && !rm ? 1 : 0;
    f.segments = s.segments;
    f.segActive = s.state === 'waking_host' ? s.stageIndex : -1;
    f.segPulse = rm ? 1 : 0.5 + 0.5 * Math.sin(now * TAU * 1.2);
    f.broken = lerp(f.broken, s.broken ? 1 : 0, k);
    f.speaking = lerp(f.speaking, s.level === 'tts' ? 1 : 0, k);

    // One 300 ms red flash on entering error, then a static dim ring.
    f.flash = 0;
    if (this.flashStart >= 0) {
      const t = now - this.flashStart;
      if (t < 0.3) f.flash = 1 - t / 0.3;
    }

    if (audio.wave && s.level === 'tts') {
      for (let i = 0; i < WAVE_POINTS; i++) f.wave[i] = audio.wave[i % audio.wave.length] ?? 0;
    } else {
      for (let i = 0; i < WAVE_POINTS; i++) f.wave[i] = (f.wave[i] as number) * (1 - k);
    }
    f.time = now;
    f.fx = rm ? 0 : this.fx;
    return f;
  }

  /** Seconds since the current state began (for tests and transitions). */
  since(now: number): number {
    return now - this.specChangedAt;
  }
}
