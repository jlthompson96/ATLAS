// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { ACTIVITY_MIN_INTERVAL_MS, startActivityPings } from '../src/lib/protocol/activity';

function rig(hidden = false) {
  let t = 1000;
  const state = { hidden, sent: 0 };
  // Stub target: script can't create isTrusted events, so call the registered listeners directly.
  const handlers = new Map<string, (e: Event) => void>();
  const target = {
    addEventListener: (type: string, fn: (e: Event) => void) => void handlers.set(type, fn),
    removeEventListener: (type: string) => void handlers.delete(type)
  };
  const stop = startActivityPings({ target, send: () => state.sent++, isHidden: () => state.hidden, now: () => t });
  const fire = (type: string, trusted = true): void => handlers.get(type)?.({ isTrusted: trusted } as Event);
  return { state, fire, stop, advance: (ms: number) => (t += ms) };
}

describe('activity.ping sender', () => {
  it('sends nothing without user input', () => {
    const r = rig();
    r.advance(10 * ACTIVITY_MIN_INTERVAL_MS);
    expect(r.state.sent).toBe(0);
  });

  it('sends on trusted input, at most once per 30 s', () => {
    const r = rig();
    r.fire('pointerdown');
    r.fire('keydown');
    r.fire('wheel');
    expect(r.state.sent).toBe(1);
    r.advance(ACTIVITY_MIN_INTERVAL_MS - 1);
    r.fire('pointermove');
    expect(r.state.sent).toBe(1);
    r.advance(1);
    r.fire('touchstart');
    expect(r.state.sent).toBe(2);
  });

  it('ignores synthetic (untrusted) events', () => {
    const r = rig();
    r.fire('keydown', false);
    expect(r.state.sent).toBe(0);
  });

  it('sends nothing while the tab is hidden, and does not burn the interval', () => {
    const r = rig(true);
    r.fire('pointerdown');
    expect(r.state.sent).toBe(0);
    r.state.hidden = false;
    r.fire('pointerdown');
    expect(r.state.sent).toBe(1);
  });

  it('stops after teardown', () => {
    const r = rig();
    r.stop();
    r.fire('keydown');
    expect(r.state.sent).toBe(0);
  });

  it('is wired to send an empty-payload activity.ping from App', async () => {
    const { readFileSync } = await import('node:fs');
    const { join } = await import('node:path');
    expect(readFileSync(join(__dirname, '../src/App.svelte'), 'utf8')).toContain("client.send('activity.ping', {})");
  });
});
