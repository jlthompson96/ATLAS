import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HudClient, defaultSocketUrl } from '../src/lib/protocol/client';
import { ClockSync } from '../src/lib/protocol/clock';
import { parseServerFrame, redactForLog, type ServerMessage } from '../src/lib/protocol/messages';
import { HudStore } from '../src/lib/state/hud.svelte';
import { snapshot } from './helpers';

/** Minimal WebSocket double: tests push frames in and inspect what the client sent. */
class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }
  close(): void {
    this.readyState = 3;
    this.onclose?.();
  }
  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  push(frame: unknown): void {
    this.onmessage?.({ data: typeof frame === 'string' ? frame : JSON.stringify(frame) });
  }
}

const env = (type: string, seq: number, payload: unknown, extra: Record<string, unknown> = {}) => ({ v: 1, type, seq, ts: 1_763_279_000_000, payload, ...extra });

function setup(opts: { now?: () => number } = {}) {
  FakeSocket.instances = [];
  const received: ServerMessage[] = [];
  const warnings: string[] = [];
  const infos: unknown[] = [];
  const client = new HudClient({
    url: 'ws://test/ws/events',
    WebSocketImpl: FakeSocket as unknown as typeof WebSocket,
    onMessage: (m) => received.push(m),
    logger: { info: (_m, d) => infos.push(d), warn: (m) => warnings.push(m) },
    now: opts.now ?? (() => Date.now()),
    snapshotTimeoutMs: 2000
  });
  client.connect();
  const ws = FakeSocket.instances[0] as FakeSocket;
  ws.open();
  return { client, ws, received, warnings, infos };
}

describe('frame validation', () => {
  it('accepts a well-formed frame and keeps unknown fields (MINOR tolerance)', () => {
    const r = parseServerFrame(JSON.stringify(env('alert', 1, { alert_id: 'a1', level: 'info', code: 'FOO', message: 'm', extra_future_field: 7 }, { newer_envelope_field: true })));
    expect(r.kind).toBe('ok');
    if (r.kind === 'ok') expect((r.msg.payload as Record<string, unknown>).extra_future_field).toBe(7);
  });

  it('reports unknown event types separately from invalid frames', () => {
    expect(parseServerFrame(JSON.stringify(env('weather.forecast', 5, {})))).toEqual({ kind: 'unknown-type', type: 'weather.forecast', seq: 5 });
  });

  it.each([
    ['not json', '{nope'],
    ['an array', '[]'],
    ['wrong major version', JSON.stringify({ ...env('alert', 1, {}), v: 2 })],
    ['missing type', JSON.stringify({ v: 1, seq: 1, ts: 1, payload: {} })],
    ['negative seq', JSON.stringify(env('alert', -1, {}))],
    ['fractional seq', JSON.stringify(env('alert', 1.5, {}))],
    ['payload not an object', JSON.stringify(env('alert', 1, 'x'))],
    ['missing required payload field', JSON.stringify(env('state.changed', 1, {}))],
    ['wrong payload field type', JSON.stringify(env('privacy.mode', 1, { on: 'yes' }))]
  ])('rejects %s', (_name, raw) => {
    expect(parseServerFrame(raw).kind).toBe('invalid');
  });

  it('redacts every x-atlas-sensitive field before logging', () => {
    const out = redactForLog({ type: 'portfolio.unlock', payload: { passphrase: 'atlas-mock', nested: [{ pin: '0000', step_up_token: 't', token: 'x', ok: 1 }] } });
    expect(JSON.stringify(out)).not.toMatch(/atlas-mock|0000|"t"|"x"/);
    expect(JSON.stringify(out)).toContain('[REDACTED]');
    expect(JSON.stringify(out)).toContain('"ok":1');
  });
});

describe('HudClient', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('sends client frames with strictly increasing seq, starting a fresh sequence per connection', () => {
    const { client, ws } = setup();
    client.send('text.input', { text: 'hi' });
    client.send('ptt.start', {});
    client.send('privacy.set', { on: true });
    const seqs = ws.sent.map((f) => f.seq as number);
    expect(seqs.length).toBeGreaterThanOrEqual(4); // includes the initial clock.ping
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
    expect(new Set(seqs).size).toBe(seqs.length);
    expect(seqs[0]).toBe(1);
    expect(ws.sent.every((f) => f.v === 1 && typeof f.ts === 'number' && typeof f.payload === 'object')).toBe(true);
    client.close();
  });

  it('returns null and sends nothing while the socket is closed', () => {
    FakeSocket.instances = [];
    const client = new HudClient({ url: 'ws://x', WebSocketImpl: FakeSocket as unknown as typeof WebSocket, onMessage: () => {} });
    expect(client.send('ptt.start', {})).toBeNull();
  });

  it('drops unknown event types, logs them and keeps going', () => {
    const { ws, received, warnings, client } = setup();
    ws.push(env('state.changed', 0, { state: 'idle' }));
    ws.push(env('brand.new.event', 1, { anything: 1 }));
    ws.push(env('alert', 2, { alert_id: 'a', level: 'info', code: 'X', message: 'm' }));
    expect(received.map((m) => m.type)).toEqual(['state.changed', 'alert']);
    expect(warnings.some((w) => w.includes('brand.new.event'))).toBe(true);
    expect(client.stats.unknownTypes).toBe(1);
    expect(ws.sent.some((f) => f.type === 'sync.request')).toBe(false); // an unknown type consumes a seq: no false gap
    client.close();
  });

  it('drops invalid and out-of-order frames', () => {
    const { ws, received, client } = setup();
    ws.push(env('state.changed', 5, { state: 'idle' }));
    ws.push(env('state.changed', 5, { state: 'thinking' })); // duplicate seq
    ws.push(env('state.changed', 3, { state: 'thinking' })); // goes backwards
    ws.push('{broken');
    expect(received).toHaveLength(1);
    expect(client.stats.outOfOrder).toBe(2);
    expect(client.stats.invalid).toBe(1);
    client.close();
  });

  it('asks for a snapshot when it sees a seq gap, and only once while waiting', () => {
    const { ws, client } = setup();
    ws.push(env('sync.snapshot', 0, snapshot()));
    ws.push(env('state.changed', 1, { state: 'idle' }));
    ws.push(env('state.changed', 5, { state: 'listening' })); // 2,3,4 missing
    ws.push(env('state.changed', 6, { state: 'idle' }));
    const syncs = ws.sent.filter((f) => f.type === 'sync.request');
    expect(syncs).toHaveLength(1);
    expect(client.stats.gaps).toBe(1);
    client.close();
  });

  it('requests a snapshot if none arrives after connecting', () => {
    const { ws, client } = setup();
    expect(ws.sent.some((f) => f.type === 'sync.request')).toBe(false);
    vi.advanceTimersByTime(2100);
    expect(ws.sent.some((f) => f.type === 'sync.request')).toBe(true);
    client.close();
  });

  it('does not request a snapshot if one arrived in time', () => {
    const { ws, client } = setup();
    ws.push(env('sync.snapshot', 0, snapshot()));
    vi.advanceTimersByTime(2100);
    expect(ws.sent.some((f) => f.type === 'sync.request')).toBe(false);
    client.close();
  });

  it('pings every 10 s and feeds pongs to the clock estimator', () => {
    let t = 1000;
    const { ws, client } = setup({ now: () => t });
    const pings = () => ws.sent.filter((f) => f.type === 'clock.ping');
    const initial = pings().length;
    vi.advanceTimersByTime(10_000);
    expect(pings().length).toBeGreaterThan(initial);
    // The server clock runs 500 ms ahead; the round trip is 20 ms.
    t = 5000;
    ws.push(env('clock.pong', 0, { client_t: 4980, server_t: 4980 + 10 + 500 }));
    expect(client.clock.offsetMs).toBeCloseTo(500, 5);
    expect(client.clock.rttMs).toBeCloseTo(20, 5);
    client.close();
  });

  it('resolves request() on the matching request.result', async () => {
    const { ws, client } = setup();
    const p = client.request('privacy.set', { on: true });
    const sent = ws.sent.at(-1) as { seq: number };
    ws.push(env('request.result', 0, { ref_seq: sent.seq, ok: false, code: 'BUSY' }));
    const m = await p;
    expect(m?.type).toBe('request.result');
    client.close();
  });

  it('resolves request() null on timeout', async () => {
    const { client } = setup();
    const p = client.request('ptt.start', {}, { timeoutMs: 500 });
    vi.advanceTimersByTime(600);
    expect(await p).toBeNull();
    client.close();
  });

  it('reconnects with backoff after the socket closes', () => {
    const { ws, client } = setup();
    ws.close();
    expect(client.status).toBe('reconnecting');
    vi.advanceTimersByTime(600);
    expect(FakeSocket.instances).toHaveLength(2);
    client.close();
  });

  it('never logs a sensitive field', () => {
    const { client, infos } = setup();
    client.send('portfolio.unlock', { passphrase: 'atlas-mock' });
    client.send('stepup.verify', { pin: '0000' });
    expect(JSON.stringify(infos)).not.toMatch(/atlas-mock|0000/);
    client.close();
  });

  it('builds the socket URL from the page origin (same origin, so CSP connect-src self applies)', () => {
    expect(defaultSocketUrl({ protocol: 'http:', host: '127.0.0.1:5173' })).toBe('ws://127.0.0.1:5173/ws/events');
    expect(defaultSocketUrl({ protocol: 'https:', host: 'atlas.lan' })).toBe('wss://atlas.lan/ws/events');
  });
});

describe('ClockSync', () => {
  it('uses the sample with the smallest round trip', () => {
    const c = new ClockSync();
    c.add(1000, 1200, 1100 + 300); // rtt 200, offset 300 + 0 (symmetric)
    c.add(2000, 2020, 2010 + 250); // rtt 20, offset 250
    c.add(3000, 3300, 3150 + 900); // rtt 300, jittery
    expect(c.offsetMs).toBeCloseTo(250, 5);
    expect(c.rttMs).toBeCloseTo(20, 5);
    expect(c.serverNow(10_000)).toBeCloseTo(10_250, 5);
    expect(c.toClient(10_250)).toBeCloseTo(10_000, 5);
  });

  it('ignores impossible samples and starts at offset 0', () => {
    const c = new ClockSync();
    expect(c.offsetMs).toBe(0);
    c.add(2000, 1000, 5000); // negative rtt
    expect(c.ready).toBe(false);
  });
});

describe('snapshot handling in the store', () => {
  it('applies sync.snapshot as a full reset', () => {
    const store = new HudStore(() => Date.now());
    store.applySnapshot(
      snapshot({
        state: { state: 'speaking' },
        alerts: [{ alert_id: 'old', level: 'warn', code: 'PI_THERMAL', message: 'hot' }],
        transcript: [{ utterance_id: 'u1', role: 'user', text: 'old text', source: 'text' }],
        tasks: [{ task_id: 't1', kind: 'research', status: 'running', redacted: false, title: 'old', step: 1, budget: { steps: 5, time_s: 60, tokens: 1, bytes: 1 }, used: { steps: 1, time_s: 1, tokens: 0, bytes: 0 }, unattended: false }]
      })
    );
    store.apply({ v: 1, type: 'assistant.delta', seq: 1, ts: 1, payload: { utterance_id: 'a1', text: 'stream' } });
    expect(store.streaming?.text).toBe('stream');
    expect(store.orb.state).toBe('speaking');

    store.applySnapshot(snapshot({ state: { state: 'idle' } }));
    expect(store.orb.state).toBe('idle');
    expect(store.alerts).toEqual([]);
    expect(store.transcript).toEqual([]);
    expect(store.tasks).toEqual([]);
    expect(store.streaming).toBeNull();
    expect(store.synced).toBe(true);
  });

  it('treats an unknown state value as unknown instead of crashing', () => {
    const store = new HudStore(() => Date.now());
    store.apply({ v: 1, type: 'state.changed', seq: 1, ts: 1, payload: { state: 'hovering' as never } });
    expect(store.orb.state).toBe('hovering');
  });
});
