import type { HostStatus } from '../generated/events';
import { known } from './data';

export type Tone = 'ok' | 'warn' | 'danger' | 'muted';
export interface Badge {
  /** Shape glyph: status never relies on colour alone. */
  glyph: string;
  text: string;
  tone: Tone;
}

const POWER = ['asleep', 'waking', 'up', 'unknown'] as const;
const LINK = ['up', 'down'] as const;
const SERVICE = ['ready', 'loading', 'degraded', 'busy', 'signed_out', 'down', 'unknown'] as const;

export function powerBadge(host: HostStatus | null): Badge {
  switch (known(host?.llm.power, POWER, 'unknown')) {
    case 'asleep':
      return { glyph: '◌', text: 'HOST: ASLEEP', tone: 'muted' };
    case 'waking':
      return { glyph: '◔', text: 'HOST: WAKING', tone: 'warn' };
    case 'up':
      return { glyph: '●', text: 'HOST: UP', tone: 'ok' };
    default:
      return { glyph: '?', text: 'HOST: UNKNOWN', tone: 'muted' };
  }
}

export function linkBadge(host: HostStatus | null): Badge {
  switch (known(host?.llm.link, LINK, 'down')) {
    case 'up':
      return { glyph: '✓', text: 'LINK: WG ✓', tone: 'ok' };
    default:
      return { glyph: '✗', text: 'LINK: DOWN ✗', tone: host ? 'danger' : 'muted' };
  }
}

/** LLM server availability (§5.6): degraded, busy and signed-out get their own labels. */
export function serviceBadge(host: HostStatus | null): Badge | null {
  switch (known(host?.llm.service, SERVICE, 'unknown')) {
    case 'ready':
      return { glyph: '●', text: 'CORE: READY', tone: 'ok' };
    case 'loading':
      return { glyph: '◔', text: 'CORE: LOADING', tone: 'warn' };
    case 'degraded':
      return { glyph: '▲', text: 'HOST: DEGRADED', tone: 'warn' };
    case 'busy':
      return { glyph: '■', text: 'HOST: BUSY', tone: 'warn' };
    case 'signed_out':
      return { glyph: '✗', text: 'CORE OFFLINE · SIGNED OUT', tone: 'danger' };
    case 'down':
      return { glyph: '✗', text: 'CORE: DOWN', tone: 'danger' };
    default:
      return null;
  }
}

export function voiceBadge(host: HostStatus | null): Badge {
  if (host && known(host.voice.status, ['ok', 'unavailable'] as const, 'ok') === 'unavailable') {
    return { glyph: '✗', text: 'VOICE UNAVAILABLE · TYPE BELOW', tone: 'danger' };
  }
  const wake = known(host?.voice.wake_word, ['active', 'suppressed', 'off'] as const, 'off');
  return {
    glyph: wake === 'active' ? '●' : '◌',
    text: wake === 'active' ? 'VOICE: LISTENING FOR WAKE WORD' : wake === 'suppressed' ? 'VOICE: WAKE WORD PAUSED' : 'VOICE: WAKE WORD OFF',
    tone: wake === 'active' ? 'ok' : 'muted'
  };
}
