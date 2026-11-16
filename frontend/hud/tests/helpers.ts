import { flushSync, mount, tick, unmount, type Component } from 'svelte';
import type { ClientPayloads, ClientType, SyncSnapshot } from '../src/lib/generated/events';
import { HUD_CONTEXT } from '../src/lib/ctx';
import { ClockSync } from '../src/lib/protocol/clock';
import type { ServerMessage } from '../src/lib/protocol/messages';
import { HudStore, type Transport } from '../src/lib/state/hud.svelte';
import { Settings } from '../src/lib/state/settings.svelte';
import { Ticker } from '../src/lib/state/ticker.svelte';
import { sha256Hex } from '../src/lib/approvals/hash';

export const NOW = 1_763_279_000_000;

let serverSeq = 0;
export function frame<T extends ServerMessage['type']>(type: T, payload: Extract<ServerMessage, { type: T }>['payload']): ServerMessage {
  return { v: 1, type, seq: serverSeq++, ts: NOW, payload } as unknown as ServerMessage;
}

export function snapshot(over: Partial<SyncSnapshot> = {}): SyncSnapshot {
  return {
    server: { version: 'test', schema_revision: '1.0.0', boot_id: 'boot-1' },
    device: { device_id: 'dev-remote', role: 'remote', can_approve: true, can_unlock_portfolio: true },
    state: { state: 'idle' },
    host: {
      pi: { cpu_pct: 12, ram_pct: 41, temp_c: 58.5, disk_write_mb_24h: 37.2 },
      llm: { power: 'up', link: 'up', service: 'ready', model_loaded: 'test-model' },
      voice: { status: 'ok', wake_word: 'active' }
    },
    privacy: { on: false, reason: 'user' },
    panels: [],
    actions: [],
    tasks: [],
    alerts: [],
    transcript: [],
    layouts: {},
    ...over
  };
}

export async function makeAction(payload: Record<string, unknown>, over: Record<string, unknown> = {}) {
  // Key order is already canonical (sorted) in these fixtures; the HUD hashes the string as given.
  const canonical = JSON.stringify(payload, Object.keys(payload).sort());
  return {
    action_id: 'act-1',
    kind: 'email.send' as const,
    summary: 'Reply to Dana',
    payload_canonical: canonical,
    payload_sha256: await sha256Hex(canonical),
    provenance: { tainted: false, sources: [{ kind: 'user' as const, ref: 'utterance:u1' }] },
    created_at: NOW,
    expires_at: Date.now() + 3_600_000,
    redacted: false as const,
    ...over
  };
}

/** A transport double. `reply` decides what the "server" sends back for each client frame. */
export class FakeTransport implements Transport {
  isOpen = true;
  clock = new ClockSync();
  sent: { type: string; payload: unknown; seq: number }[] = [];
  private seq = 0;
  reply: (type: string, payload: unknown, seq: number) => ServerMessage[] = (_t, _p, seq) => [
    frame('request.result', { ref_seq: seq, ok: true })
  ];

  constructor(private store: HudStore) {}

  send<T extends ClientType>(type: T, payload: ClientPayloads[T]): number | null {
    this.sent.push({ type, payload, seq: ++this.seq });
    return this.seq;
  }

  async request<T extends ClientType>(
    type: T,
    payload: ClientPayloads[T],
    opts: { until?: (m: ServerMessage) => boolean; onSent?: (seq: number) => void } = {}
  ): Promise<ServerMessage | null> {
    const seq = this.send(type, payload) as number;
    opts.onSent?.(seq);
    const replies = this.reply(type, payload, seq);
    for (const r of replies) this.store.apply(r);
    return replies.find((r) => (opts.until ? opts.until(r) : r.type === 'request.result')) ?? replies[0] ?? null;
  }

  last(type: string): { type: string; payload: Record<string, unknown>; seq: number } | undefined {
    return this.sent.filter((s) => s.type === type).at(-1) as never;
  }
}

export interface Harness {
  store: HudStore;
  settings: Settings;
  ticker: Ticker;
  transport: FakeTransport;
  target: HTMLElement;
  cleanup: () => void;
}

/** Mount a component inside a HUD context with the static orb tier and a fake transport. */
export function mountHud(component: Component<never>, opts: { snapshot?: SyncSnapshot; props?: Record<string, unknown> } = {}): Harness {
  const store = new HudStore(() => Date.now());
  const transport = new FakeTransport(store);
  store.attach(transport);
  const settings = new Settings(null);
  settings.set({ quality: 'static' });
  const ticker = new Ticker();
  store.setConnection('open');
  store.applySnapshot(opts.snapshot ?? snapshot());
  const target = document.createElement('div');
  document.body.appendChild(target);
  const context = new Map<symbol, unknown>([[HUD_CONTEXT, { store, settings, ticker }]]);
  let done = false;
  const instance = mount(component as Component<Record<string, unknown>>, { target, context, props: opts.props ?? {} });
  flushSync();
  return {
    store,
    settings,
    ticker,
    transport,
    target,
    cleanup: () => {
      if (done) return;
      done = true;
      unmount(instance);
      target.remove();
    }
  };
}

export async function settle(times = 3): Promise<void> {
  for (let i = 0; i < times; i++) {
    await tick();
    await new Promise((r) => setTimeout(r, 0));
  }
  flushSync();
}

export function q<T extends Element = HTMLElement>(root: ParentNode, selector: string): T | null {
  return root.querySelector<T>(selector);
}

export function byText(root: ParentNode, selector: string, text: string | RegExp): HTMLElement | undefined {
  return Array.from(root.querySelectorAll<HTMLElement>(selector)).find((el) =>
    typeof text === 'string' ? el.textContent?.includes(text) : text.test(el.textContent ?? '')
  );
}
