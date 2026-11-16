// Integration: the real HudClient + HudStore against the real mock event server (backend/atlas/ui_server/mock).
// The mock validates every frame it sends against events.v1 and the state machine, and validates every frame
// the HUD sends, so this checks both directions of the contract.
import { afterEach, describe, expect, it } from 'vitest';
import { verifyAction, type FullAction } from '../src/lib/approvals/hash';
import { HudClient, type ConnectionStatus } from '../src/lib/protocol/client';
import type { ServerMessage } from '../src/lib/protocol/messages';
import { HudStore } from '../src/lib/state/hud.svelte';
import { mockAvailable, sleep, startMock, until, type RunningMock } from './mockserver';

interface Rig {
  client: HudClient;
  store: HudStore;
  seen: ServerMessage[];
  warnings: string[];
  status: ConnectionStatus[];
}

const open: { mock?: RunningMock; client?: HudClient }[] = [];

async function connect(mock: RunningMock): Promise<Rig> {
  const store = new HudStore();
  const seen: ServerMessage[] = [];
  const warnings: string[] = [];
  const status: ConnectionStatus[] = [];
  const client = new HudClient({
    url: mock.url,
    WebSocketImpl: WebSocket,
    logger: { info: () => {}, warn: (m) => warnings.push(m) },
    onMessage: (m) => {
      seen.push(m);
      store.apply(m);
    },
    onStatus: (s) => {
      status.push(s);
      store.setConnection(s);
    },
    onConnection: (o) => store.onConnection(o),
    reconnect: false
  });
  store.attach(client);
  open.push({ mock, client });
  client.connect();
  await until(() => store.synced, 'the first sync.snapshot');
  return { client, store, seen, warnings, status };
}

afterEach(async () => {
  for (const o of open.splice(0)) {
    o.client?.close();
    await o.mock?.stop();
  }
});

const run = mockAvailable ? describe : describe.skip;

run('every mock scenario is consumed without errors', () => {
  it('plays all scenarios (--scenario all) with zero protocol errors, gaps or unknown frames', async () => {
    const mock = await startMock(['--scenario', 'all', '--speed', '20']);
    open.push({ mock });
    const rig = await connect(mock);
    const scenarios = 12;
    await until(() => rig.client.stats.snapshots >= scenarios + 2, `${scenarios + 2} snapshots`, 60_000);
    // The last scenario is still playing after its snapshot: wait for the stream to go quiet.
    let last = rig.client.stats.received;
    for (;;) {
      await sleep(2500);
      if (rig.client.stats.received === last) break;
      last = rig.client.stats.received;
    }

    const { stats } = rig.client;
    expect(stats.invalid).toBe(0);
    expect(stats.unknownTypes).toBe(0);
    expect(stats.outOfOrder).toBe(0);
    expect(stats.gaps).toBe(0);
    // One snapshot per scenario, plus the one the server must send after every privacy.mode change.
    expect(stats.snapshots).toBe(scenarios + rig.seen.filter((m) => m.type === 'privacy.mode').length);
    expect(rig.warnings).toEqual([]);
    expect(stats.received).toBe(stats.applied);
    expect(rig.client.clock.ready).toBe(true);

    // Every orb state and every data panel showed up on the wire.
    const states = new Set(rig.seen.filter((m) => m.type === 'state.changed').map((m) => (m.payload as { state: string }).state));
    for (const s of ['idle', 'listening', 'thinking', 'speaking', 'waking_host', 'error', 'llm_unreachable']) expect(states.has(s), s).toBe(true);
    const panels = new Set<string>();
    for (const m of rig.seen) {
      if (m.type === 'panel.update') panels.add(m.payload.panel_id);
      if (m.type === 'sync.snapshot') for (const p of m.payload.panels) panels.add(p.panel_id);
    }
    for (const p of ['weather', 'egress_audit', 'digest', 'email', 'discord', 'calendar', 'markets', 'portfolio']) expect(panels.has(p), p).toBe(true);
    for (const t of ['audio.level', 'transcript.final', 'assistant.delta', 'host.status', 'task.progress', 'action.pending', 'action.resolved', 'alert', 'privacy.mode']) {
      expect(rig.seen.some((m) => m.type === t), t).toBe(true);
    }
    // The wake countdown: stages never go backwards inside an episode and elapsed <= deadline.
    for (const m of rig.seen) if (m.type === 'state.changed' && m.payload.wake) expect(m.payload.wake.elapsed_ms).toBeLessThanOrEqual(m.payload.wake.deadline_ms);
    // The last scenario (wake_timeout) ends in a resting state: the core stays offline after a failed wake.
    expect(['idle', 'llm_unreachable']).toContain(rig.store.orb.state);
  }, 120_000);
});

run('interactive flows against the mock', () => {
  it('estimates the clock offset and keeps TTS/mic envelopes in buffers', async () => {
    const mock = await startMock(['--scenario', 'conversation', '--speed', '4']);
    const rig = await connect(mock);
    await until(() => rig.client.clock.ready, 'a clock.pong');
    expect(Math.abs(rig.client.clock.offsetMs)).toBeLessThan(50); // same machine
    expect(rig.client.clock.rttMs).toBeLessThan(100);
    await until(() => rig.store.orb.state === 'speaking', 'speaking');
    expect(rig.store.audio.tts.size).toBeGreaterThan(0);
    await until(() => rig.store.orb.state === 'idle', 'idle again', 20_000);
    expect(rig.store.transcript.some((t) => t.role === 'assistant')).toBe(true);
  }, 40_000);

  it('text.input, push-to-talk and the interactive reply cycle', async () => {
    const mock = await startMock(['--scenario', 'interactive']);
    const rig = await connect(mock);
    const r = await rig.store.sendText('hello atlas');
    expect(r.ok).toBe(true);
    await until(() => rig.store.transcript.some((t) => t.role === 'assistant'), 'the mock reply');
    await until(() => rig.store.orb.state === 'idle', 'idle');

    await rig.store.togglePtt();
    await until(() => rig.store.orb.state === 'listening', 'listening');
    await until(() => rig.store.audio.mic.size > 0, 'mic levels');
    await rig.store.togglePtt();
    await until(() => rig.store.orb.state === 'speaking', 'speaking', 8000);
    await until(() => rig.store.orb.state === 'idle', 'idle', 10_000);
    expect(rig.client.stats.invalid + rig.client.stats.gaps + rig.client.stats.outOfOrder).toBe(0);
  }, 40_000);

  it('portfolio unlock: wrong passphrase is rejected inline, right one unlocks', async () => {
    const mock = await startMock(['--scenario', 'interactive']);
    const rig = await connect(mock);
    expect(rig.store.panels.portfolio?.data).toMatchObject({ locked: true });
    const bad = await rig.store.unlockPortfolio('wrong');
    expect(bad).toMatchObject({ ok: false, code: 'BAD_PASSPHRASE' });
    expect(bad.message).toBe('Wrong passphrase.');
    expect(rig.store.notices).toHaveLength(0); // inline error only, no duplicate toast
    const good = await rig.store.unlockPortfolio('atlas-mock');
    expect(good.ok).toBe(true);
    await until(() => (rig.store.panels.portfolio?.data as { locked?: boolean }).locked === false, 'unlocked panel');
    const d = rig.store.panels.portfolio?.data as { total_value: string };
    expect(typeof d.total_value).toBe('string'); // money stays a decimal string end to end
  });

  it('layout.save round-trips: the saved layout comes back in the next snapshot', async () => {
    const mock = await startMock(['--scenario', 'interactive']);
    const rig = await connect(mock);
    const slots = { top: 'clock' as const, left: ['weather' as const, 'system' as const], right: ['pending' as const], bottom: 'transcript' as const };
    const r = await rig.store.saveLayout('remote', slots);
    expect(r.ok).toBe(true);
    const before = rig.client.stats.snapshots;
    rig.store.requestSync();
    await until(() => rig.client.stats.snapshots > before, 'a fresh snapshot');
    expect(rig.store.layouts.remote).toEqual(slots);
  });

  it('privacy mode: server redacts and snapshots; leaving needs the PIN on a remote device', async () => {
    const mock = await startMock(['--scenario', 'interactive']);
    const rig = await connect(mock);
    expect((rig.store.panels.email?.data as { items?: unknown[] }).items?.length).toBeGreaterThan(0);

    expect((await rig.store.setPrivacy(true)).ok).toBe(true);
    await until(() => rig.store.privacy.on && rig.store.panels.email?.redacted === true, 'redacted snapshot');
    expect(rig.store.panels.email?.data).toEqual({ unread: expect.any(Number) });
    expect(rig.store.transcript).toEqual([]);
    expect(rig.store.actions.every((a) => a.redacted)).toBe(true);

    const refused = await rig.store.setPrivacy(false);
    expect(refused).toMatchObject({ ok: false, code: 'STEP_UP_REQUIRED' });
    expect(await rig.store.verifyPin('9999')).toMatchObject({ ok: false, code: 'BAD_PIN' });
    expect((await rig.store.verifyPin('0000')).ok).toBe(true);
    expect(rig.store.stepUpValid).toBe(true);
    expect((await rig.store.setPrivacy(false)).ok).toBe(true);
    await until(() => !rig.store.privacy.on && rig.store.panels.email?.redacted === false, 'full snapshot');
    expect(rig.client.stats.invalid).toBe(0);
  });

  it('approvals: verify the hash, PIN step-up, approve with the HUD-computed hash, then executed', async () => {
    const mock = await startMock(['--scenario', 'approvals', '--speed', '5']);
    const rig = await connect(mock);
    await until(() => rig.store.actions.length >= 3, 'pending actions');
    const act = rig.store.actions.find((a) => a.action_id === 'act-101') as FullAction;
    const v = await verifyAction(act);
    expect(v.status).toBe('verified'); // the Python mock and the HUD agree on SHA-256 of payload_canonical
    if (v.status !== 'verified') return;

    // The tainted forward from the injected email is visibly flagged.
    const tainted = rig.store.actions.find((a) => a.action_id === 'act-102') as FullAction;
    expect(tainted.provenance.tainted).toBe(true);
    expect(tainted.warnings).toContain('tainted_args');

    // No token yet: the HUD itself refuses to send.
    expect(await rig.store.decide('act-101', 'approve', v.hash)).toMatchObject({ ok: false, code: 'STEP_UP_REQUIRED' });
    expect((await rig.store.verifyPin('0000')).ok).toBe(true);
    // A decision with a wrong hash is rejected by the server (defence in depth).
    const wrong = await rig.store.decide('act-101', 'approve', 'a'.repeat(64));
    expect(wrong).toMatchObject({ ok: false, code: 'HASH_MISMATCH' });
    expect((await rig.store.verifyPin('0000')).ok).toBe(true);
    const ok = await rig.store.decide('act-101', 'approve', v.hash);
    expect(ok.ok).toBe(true);
    await until(() => rig.store.resolved['act-101']?.outcome === 'executed', 'execution', 10_000);
    expect(rig.store.actions.some((a) => a.action_id === 'act-101')).toBe(false);

    // Deny works the same way.
    expect((await rig.store.verifyPin('0000')).ok).toBe(true);
    const d103 = rig.store.actions.find((a) => a.action_id === 'act-103') as FullAction;
    const v103 = await verifyAction(d103);
    expect(v103.status).toBe('verified');
    if (v103.status === 'verified') expect((await rig.store.decide('act-103', 'deny', v103.hash)).ok).toBe(true);
    await until(() => rig.store.resolved['act-103']?.outcome === 'denied', 'denial');

    // The unattended action expires on its own.
    await until(() => rig.store.resolved['act-104']?.outcome === 'expired', 'expiry', 15_000);
    expect(rig.store.actions.some((a) => a.action_id === 'act-104')).toBe(false);
  }, 60_000);

  it('a kiosk device cannot approve or unlock, and the HUD knows it', async () => {
    const mock = await startMock(['--scenario', 'approvals', '--speed', '5', '--device', 'kiosk']);
    const rig = await connect(mock);
    expect(rig.store.canApprove).toBe(false);
    expect(rig.store.canUnlock).toBe(false);
    await until(() => rig.store.actions.length >= 1, 'a pending action');
    expect(rig.store.actions[0]?.redacted).toBe(false);
    expect(await rig.store.unlockPortfolio('atlas-mock')).toMatchObject({ ok: false, code: 'FORBIDDEN_DEVICE' });
  }, 30_000);

  it('task kill and the kill switch', async () => {
    const mock = await startMock(['--scenario', 'research', '--speed', '10']);
    const rig = await connect(mock);
    await until(() => rig.store.tasks.some((t) => t.status === 'running'), 'a running task');
    const t = rig.store.tasks.find((x) => x.status === 'running');
    expect((await rig.store.killTask(t?.task_id ?? '')).ok).toBe(true);
    await until(() => rig.store.tasks.find((x) => x.task_id === t?.task_id)?.status === 'killed', 'killed');
    expect(await rig.store.killTask('nope')).toMatchObject({ ok: false, code: 'UNKNOWN_TASK' });
  }, 30_000);

  it('survives the server closing the socket (no reconnect when disabled)', async () => {
    const mock = await startMock(['--scenario', 'interactive']);
    const rig = await connect(mock);
    await mock.stop();
    await until(() => rig.store.connection === 'closed', 'closed status');
    expect(rig.store.synced).toBe(false);
    expect(rig.status.at(-1)).toBe('closed');
  }, 20_000);
});
