import { describe, expect, it } from 'vitest';
import { linkBadge, powerBadge } from '../src/lib/panels/hostview';
import type { HostStatus } from '../src/lib/generated/events';

const host = (link: string): HostStatus =>
  ({
    pi: { cpu_pct: 1, ram_pct: 1, temp_c: 40, disk_write_mb_24h: 1 },
    llm: { power: 'up', link, service: 'ready' },
    voice: { status: 'ok', wake_word: 'off' }
  }) as HostStatus;

describe('host badges never claim more than the HUD was told', () => {
  it('says UNKNOWN, not DOWN, before any host status arrives (2026-11-25)', () => {
    expect(linkBadge(null)).toEqual({ glyph: '?', text: 'LINK: UNKNOWN', tone: 'muted' });
    expect(powerBadge(null).text).toBe('HOST: UNKNOWN');
  });

  it('shows the tunnel as reported', () => {
    expect(linkBadge(host('up')).text).toBe('LINK: WG ✓');
    expect(linkBadge(host('down'))).toEqual({ glyph: '✗', text: 'LINK: DOWN ✗', tone: 'danger' });
    expect(linkBadge(host('sideways')).text).toBe('LINK: DOWN ✗'); // an unknown value is not "up"
  });
});
