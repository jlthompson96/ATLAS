import type { ClientPayloads, ClientType } from '../generated/events';
import { ClockSync, clientNow } from './clock';
import { parseServerFrame, redactForLog, type ClientMessage, type ServerMessage } from './messages';

export type ConnectionStatus = 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface Logger {
  info(msg: string, data?: unknown): void;
  warn(msg: string, data?: unknown): void;
}

export interface ClientStats {
  received: number;
  applied: number;
  unknownTypes: number;
  invalid: number;
  outOfOrder: number;
  gaps: number;
  resyncs: number;
  snapshots: number;
  sent: number;
}

export interface ClientOptions {
  /** Absolute ws:// or wss:// URL, or a function returning one (resolved on every connect). */
  url: string | (() => string);
  onMessage: (msg: ServerMessage) => void;
  onStatus?: (status: ConnectionStatus) => void;
  /** Called when a new socket opens or closes, so the store can drop per-connection state. */
  onConnection?: (open: boolean) => void;
  logger?: Logger;
  WebSocketImpl?: typeof WebSocket;
  now?: () => number;
  pingIntervalMs?: number;
  /** Ask for a snapshot if the server hasn't sent one this long after the socket opens. */
  snapshotTimeoutMs?: number;
  reconnect?: boolean;
}

const OPEN = 1;
const SILENT: Logger = { info() {}, warn() {} };

/**
 * WebSocket client for /ws/events (contracts/events.v1.schema.json).
 *
 * - Validates every frame's envelope; drops and counts bad ones.
 * - Tolerates MINOR revisions: unknown fields pass through, unknown event types are logged and dropped.
 * - Tracks the server's `seq`; a gap triggers `sync.request`, and `sync.snapshot` is applied as a full reset
 *   by the consumer.
 * - Numbers its own frames with an increasing `seq`, starting at 1 on each connection.
 * - Runs clock.ping/clock.pong every 10 s for the clock-offset estimate.
 */
export class HudClient {
  readonly clock = new ClockSync();
  readonly stats: ClientStats = {
    received: 0,
    applied: 0,
    unknownTypes: 0,
    invalid: 0,
    outOfOrder: 0,
    gaps: 0,
    resyncs: 0,
    snapshots: 0,
    sent: 0
  };
  status: ConnectionStatus = 'closed';

  private ws: WebSocket | null = null;
  private nextSeq = 1;
  private lastServerSeq: number | null = null;
  private haveSnapshot = false;
  private syncPendingSince = 0;
  private closedByUser = true;
  private attempt = 0;
  private timers = new Set<ReturnType<typeof setTimeout>>();
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private waiters = new Map<number, (m: ServerMessage | null) => void>();
  private untilWaiters = new Set<{ until: (m: ServerMessage) => boolean; resolve: (m: ServerMessage | null) => void }>();
  private readonly log: Logger;
  private readonly now: () => number;
  private readonly WS: typeof WebSocket;

  constructor(private readonly opts: ClientOptions) {
    this.log = opts.logger ?? SILENT;
    this.now = opts.now ?? clientNow;
    this.WS = opts.WebSocketImpl ?? WebSocket;
  }

  connect(): void {
    this.closedByUser = false;
    this.open(this.attempt === 0 ? 'connecting' : 'reconnecting');
  }

  close(): void {
    this.closedByUser = true;
    this.clearTimers();
    this.ws?.close(1000);
    this.ws = null;
    this.failWaiters();
    this.setStatus('closed');
  }

  get isOpen(): boolean {
    return this.ws?.readyState === OPEN;
  }

  /** Send one client frame. Returns its `seq`, or null if the socket isn't open. Never logs sensitive fields. */
  send<T extends ClientType>(type: T, payload: ClientPayloads[T]): number | null {
    if (!this.ws || this.ws.readyState !== OPEN) return null;
    const seq = this.nextSeq++;
    const frame = { v: 1, type, seq, ts: Math.round(this.now()), payload } as ClientMessage;
    this.ws.send(JSON.stringify(frame));
    this.stats.sent++;
    if (type !== 'clock.ping') this.log.info(`send ${type}`, redactForLog(frame));
    return seq;
  }

  /**
   * Send a frame and wait for its `request.result` (matched on `ref_seq`) or, if given, for any message
   * accepted by `until` (e.g. `stepup.granted`, which answers `stepup.verify` without a request.result).
   * Resolves null on timeout, send failure or disconnect.
   */
  request<T extends ClientType>(
    type: T,
    payload: ClientPayloads[T],
    opts: { until?: (m: ServerMessage) => boolean; timeoutMs?: number; onSent?: (seq: number) => void } = {}
  ): Promise<ServerMessage | null> {
    return new Promise((resolve) => {
      const seq = this.send(type, payload);
      if (seq === null) return resolve(null);
      opts.onSent?.(seq);
      let done = false;
      const entry = opts.until ? { until: opts.until, resolve: (m: ServerMessage | null) => finish(m) } : null;
      const timer = setTimeout(() => finish(null), opts.timeoutMs ?? 10_000);
      const finish = (m: ServerMessage | null): void => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        this.waiters.delete(seq);
        if (entry) this.untilWaiters.delete(entry);
        resolve(m);
      };
      this.waiters.set(seq, finish);
      if (entry) this.untilWaiters.add(entry);
    });
  }

  /** Ask the server for a fresh `sync.snapshot`. Deduplicated while one is outstanding. */
  requestSync(reason: string): void {
    const t = this.now();
    if (this.syncPendingSince && t - this.syncPendingSince < 1500) return;
    this.syncPendingSince = t;
    this.stats.resyncs++;
    this.log.info(`sync.request (${reason})`);
    this.send('sync.request', this.lastServerSeq === null ? {} : { last_seq: this.lastServerSeq });
  }

  // --- socket lifecycle ----------------------------------------------------------------------------

  private open(status: ConnectionStatus): void {
    this.setStatus(status);
    const url = typeof this.opts.url === 'function' ? this.opts.url() : this.opts.url;
    let ws: WebSocket;
    try {
      ws = new this.WS(url);
    } catch (e) {
      this.log.warn('could not open WebSocket', String(e));
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.nextSeq = 1;
      this.lastServerSeq = null;
      this.haveSnapshot = false;
      this.syncPendingSince = 0;
      this.setStatus('open');
      this.opts.onConnection?.(true);
      this.startPings();
      this.later(this.opts.snapshotTimeoutMs ?? 2000, () => {
        if (!this.haveSnapshot) this.requestSync('no snapshot after connect');
      });
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (this.ws === ws) this.onFrame(typeof ev.data === 'string' ? ev.data : '');
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.stopPings();
      this.failWaiters();
      this.opts.onConnection?.(false);
      if (this.closedByUser || this.opts.reconnect === false) this.setStatus('closed');
      else this.scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose follows; nothing to add and nothing sensitive to log.
    };
  }

  private scheduleReconnect(): void {
    this.setStatus('reconnecting');
    const delay = Math.min(8000, 500 * 2 ** this.attempt);
    this.attempt++;
    this.later(delay, () => {
      if (!this.closedByUser) this.open('reconnecting');
    });
  }

  private startPings(): void {
    this.ping();
    this.later(300, () => this.ping());
    this.later(700, () => this.ping());
    this.pingTimer = setInterval(() => this.ping(), this.opts.pingIntervalMs ?? 10_000);
  }

  private stopPings(): void {
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  private ping(): void {
    this.send('clock.ping', { client_t: this.now() });
  }

  // --- inbound ----------------------------------------------------------------------------------------

  private onFrame(raw: string): void {
    this.stats.received++;
    const parsed = parseServerFrame(raw);
    if (parsed.kind === 'invalid') {
      this.stats.invalid++;
      this.log.warn(`dropped invalid frame: ${parsed.reason}`);
      return;
    }
    const seq = parsed.kind === 'ok' ? parsed.msg.seq : parsed.seq;
    if (this.lastServerSeq !== null && seq <= this.lastServerSeq) {
      this.stats.outOfOrder++;
      this.log.warn(`dropped out-of-order frame seq ${seq} (last ${this.lastServerSeq})`);
      return;
    }
    const gap = this.lastServerSeq !== null && seq > this.lastServerSeq + 1;
    this.lastServerSeq = seq;
    if (gap) {
      this.stats.gaps++;
      this.requestSync(`gap before seq ${seq}`);
    }
    if (parsed.kind === 'unknown-type') {
      this.stats.unknownTypes++;
      this.log.warn(`dropped unknown event type ${parsed.type}`);
      return;
    }
    const msg = parsed.msg;
    if (msg.type === 'clock.pong') {
      this.clock.add(msg.payload.client_t, this.now(), msg.payload.server_t);
    } else if (msg.type === 'sync.snapshot') {
      this.haveSnapshot = true;
      this.syncPendingSince = 0;
      this.attempt = 0;
      this.stats.snapshots++;
    }
    this.stats.applied++;
    try {
      this.opts.onMessage(msg);
    } catch (e) {
      this.log.warn(`handler failed for ${msg.type}; resyncing`, String(e));
      this.requestSync('handler error');
    }
    this.settleWaiters(msg);
  }

  private settleWaiters(msg: ServerMessage): void {
    if (msg.type === 'request.result') this.waiters.get(msg.payload.ref_seq)?.(msg);
    for (const w of [...this.untilWaiters]) if (w.until(msg)) w.resolve(msg);
  }

  private failWaiters(): void {
    for (const w of [...this.waiters.values()]) w(null);
  }

  private later(ms: number, fn: () => void): void {
    const t = setTimeout(() => {
      this.timers.delete(t);
      fn();
    }, ms);
    this.timers.add(t);
  }

  private clearTimers(): void {
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
    this.stopPings();
  }

  private setStatus(s: ConnectionStatus): void {
    if (this.status === s) return;
    this.status = s;
    this.opts.onStatus?.(s);
  }
}

/** ws(s)://<this origin>/ws/events: same origin, so the CSP's `connect-src 'self'` covers it. */
export function defaultSocketUrl(loc: Pick<Location, 'protocol' | 'host'> = window.location): string {
  return `${loc.protocol === 'https:' ? 'wss:' : 'ws:'}//${loc.host}/ws/events`;
}
