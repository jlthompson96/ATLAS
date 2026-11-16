import type { StateChanged, WakeStage } from '../generated/events';
import { StateValues, WakeStageValues } from '../generated/events';
import { UNREACHABLE_LABELS } from '../format/codes';
import { safeText } from '../format/text';

export type KnownState = (typeof StateValues)[number];
/** An orb state this client knows, or 'unknown' for a value added by a newer MINOR revision. */
export type OrbState = KnownState | 'unknown';
export type Rgb = readonly [number, number, number];

/** Shape family: distinct per state so status never depends on colour alone (§4.1). */
export type OrbShape = 'ring' | 'ring-wide' | 'arcs' | 'wave' | 'segments' | 'broken' | 'broken-grey' | 'unknown';

export interface OrbSpec {
  state: OrbState;
  label: string;
  /** Wake stage names in order, for the progress readout. Empty unless waking. */
  stages: readonly WakeStage[];
  /** Index of the current wake stage, or -1. */
  stageIndex: number;
  shape: OrbShape;
  color: Rgb;
  /** Hex of `color`, for SVG and CSS. */
  cssColor: string;
  core: number;
  radius: number;
  alpha: number;
  breatheHz: number;
  arcSpeed: number;
  counterArcs: boolean;
  particles: boolean;
  sweep: boolean;
  segments: number;
  broken: boolean;
  blinkHz: number;
  flash: boolean;
  desaturate: number;
  level: 'none' | 'mic' | 'tts';
}

const hex = (c: string): Rgb => [
  parseInt(c.slice(1, 3), 16) / 255,
  parseInt(c.slice(3, 5), 16) / 255,
  parseInt(c.slice(5, 7), 16) / 255
];

export const COLORS = {
  accent: '#3ff0ff',
  bright: '#8ffcff',
  white: '#e8fdff',
  warn: '#ffb547',
  danger: '#ff4d5e',
  muted: '#7fa3b3'
} as const;

const BASE: Omit<OrbSpec, 'state' | 'label' | 'shape' | 'color' | 'cssColor'> = {
  stages: [],
  stageIndex: -1,
  core: 0.3,
  radius: 0.5,
  alpha: 1,
  breatheHz: 0,
  arcSpeed: 0.05,
  counterArcs: false,
  particles: false,
  sweep: false,
  segments: 0,
  broken: false,
  blinkHz: 0,
  flash: false,
  desaturate: 0,
  level: 'none'
};

export function isKnownState(s: string): s is KnownState {
  return (StateValues as readonly string[]).includes(s);
}

/** Seconds shown in the WAKING label. */
export function wakeSeconds(elapsedMs: number): number {
  return Math.max(0, Math.floor(elapsedMs / 1000));
}

/**
 * Visual description of one orb state (PLAN.md §4.3 table). `wakeElapsedMs` overrides the payload's
 * elapsed time (the HUD interpolates between server updates).
 */
export function describeOrb(payload: Pick<StateChanged, 'state' | 'detail' | 'code' | 'wake'>, wakeElapsedMs?: number): OrbSpec {
  const s = payload.state as string;
  const make = (state: OrbState, label: string, shape: OrbShape, color: string, over: Partial<OrbSpec>): OrbSpec => ({
    ...BASE,
    state,
    label,
    shape,
    color: hex(color),
    cssColor: color,
    ...over
  });
  if (!isKnownState(s)) {
    return make('unknown', 'STATE UNKNOWN', 'unknown', COLORS.muted, { core: 0.15, breatheHz: 0.1 });
  }
  switch (s) {
    case 'idle':
      return make('idle', 'STANDBY', 'ring', COLORS.accent, { core: 0.22, radius: 0.62, breatheHz: 0.1, arcSpeed: 0.12 });
    case 'listening':
      return make('listening', 'LISTENING', 'ring-wide', COLORS.bright, {
        core: 0.5,
        radius: 0.7,
        arcSpeed: 0.3,
        level: 'mic'
      });
    case 'thinking':
      return make('thinking', 'PROCESSING', 'arcs', COLORS.white, {
        core: 0.4,
        radius: 0.62,
        arcSpeed: 1.6,
        counterArcs: true,
        particles: true
      });
    case 'speaking':
      return make('speaking', 'SPEAKING', 'wave', COLORS.accent, { core: 0.6, radius: 0.6, arcSpeed: 0.2, level: 'tts' });
    case 'waking_host': {
      const wake = payload.wake;
      const idx = wake ? WakeStageValues.indexOf(wake.stage) : -1;
      const secs = wakeSeconds(wakeElapsedMs ?? wake?.elapsed_ms ?? 0);
      return make('waking_host', `WAKING CORE · ${secs} s`, 'segments', COLORS.warn, {
        stages: WakeStageValues,
        stageIndex: idx,
        core: 0.25,
        radius: 0.64,
        arcSpeed: 1.2,
        sweep: true,
        segments: 4
      });
    }
    case 'error': {
      const reason = safeText(payload.detail, 80) || safeText(payload.code, 48) || 'UNKNOWN';
      return make('error', `ERROR: ${reason}`, 'broken', COLORS.danger, {
        core: 0.12,
        radius: 0.62,
        arcSpeed: 0,
        broken: true,
        flash: true
      });
    }
    case 'llm_unreachable':
      return make('llm_unreachable', UNREACHABLE_LABELS[payload.code ?? ''] ?? 'CORE OFFLINE · LOCAL MODE', 'broken-grey', COLORS.muted, {
        core: 0.1,
        radius: 0.62,
        arcSpeed: 0,
        broken: true,
        blinkHz: 0.5,
        desaturate: 1
      });
  }
}

/**
 * Text for the screen-reader live region. Unlike the visible label it has no ticking seconds, so the
 * wake countdown is announced once per stage instead of once per second.
 */
export function announceText(payload: Pick<StateChanged, 'state' | 'detail' | 'code' | 'wake'>): string {
  if (payload.state === 'waking_host' && payload.wake) {
    const names: Record<string, string> = { packet: 'sending wake packet', link: 'waiting for link', api: 'waiting for API', model: 'loading model' };
    return `Waking core: ${names[payload.wake.stage] ?? payload.wake.stage}`;
  }
  return describeOrb(payload).label;
}

/** One label per state, for tests and the legend. */
export const ALL_STATES: readonly KnownState[] = StateValues;
