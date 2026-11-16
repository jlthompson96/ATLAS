import { SvelteSet } from 'svelte/reactivity';
import type {
  ActionPending,
  Alert,
  ClientPayloads,
  ClientType,
  Device,
  HostStatus,
  LayoutProfile,
  LayoutSlots,
  PanelUpdate,
  PrivacyMode,
  StateChanged,
  SyncSnapshot,
  TaskProgress,
  Transcript
} from '../generated/events';
import { REQUEST_RESULT_MESSAGES, resultMessage } from '../format/codes';
import { safeText } from '../format/text';
import { EnvelopeBuffer } from '../orb/envelope';
import { describeOrb } from '../orb/spec';
import type { ClockSync } from '../protocol/clock';
import type { ConnectionStatus } from '../protocol/client';
import type { ServerMessage } from '../protocol/messages';

/** The slice of HudClient the store needs, so tests can supply a fake. */
export interface Transport {
  readonly isOpen: boolean;
  readonly clock: ClockSync;
  send<T extends ClientType>(type: T, payload: ClientPayloads[T]): number | null;
  request<T extends ClientType>(
    type: T,
    payload: ClientPayloads[T],
    opts?: { until?: (m: ServerMessage) => boolean; timeoutMs?: number; onSent?: (seq: number) => void }
  ): Promise<ServerMessage | null>;
}

export type FullAction = Extract<ActionPending, { redacted: false }>;
export type Layouts = Partial<Record<LayoutProfile, LayoutSlots>>;

export interface Notice {
  id: number;
  level: 'info' | 'warn' | 'error';
  text: string;
}

export interface Outcome {
  ok: boolean;
  code?: string;
  message: string;
}

const MAX_ALERTS = 20;
const MAX_TRANSCRIPT = 50;

export class HudStore {
  connection = $state<ConnectionStatus>('closed');
  /** True once a sync.snapshot has been applied on this connection. */
  synced = $state(false);
  device = $state<Device | null>(null);
  server = $state<SyncSnapshot['server'] | null>(null);

  orb = $state<StateChanged>({ state: 'idle' });
  /** Client clock (ms) when `orb` was received, for interpolating the wake countdown. */
  orbAt = $state(0);
  host = $state<HostStatus | null>(null);
  privacy = $state<PrivacyMode>({ on: false, reason: 'user' });
  panels = $state<Record<string, PanelUpdate>>({});
  actions = $state<ActionPending[]>([]);
  /** Approved but not yet executed/failed. */
  approved = $state<Record<string, true>>({});
  /** Latest terminal outcome per action, so an open dialog can show what happened. */
  resolved = $state<Record<string, { outcome: string; code?: string }>>({});
  tasks = $state<TaskProgress[]>([]);
  alerts = $state<Alert[]>([]);
  dismissedAlerts = $state<Record<string, true>>({});
  transcript = $state<Transcript[]>([]);
  partial = $state<Transcript | null>(null);
  streaming = $state<{ utterance_id: string; text: string } | null>(null);
  layouts = $state<Layouts>({});
  notices = $state<Notice[]>([]);
  /** Text for the ARIA live regions. */
  announcement = $state('');
  urgent = $state('');
  /** Step-up token: kept in memory only, never logged or persisted. `expiresAt` is on the client clock. */
  stepUp = $state<{ token: string; expiresAt: number } | null>(null);
  pttActive = $state(false);
  /** Frame counters for diagnostics (no content). */
  unknownEnumSeen = $state(0);

  readonly audio = { tts: new EnvelopeBuffer(), mic: new EnvelopeBuffer(60, 160) };

  private silent = new SvelteSet<number>();
  private noticeSeq = 0;
  private transport: Transport | null = null;
  private clientNow: () => number;

  constructor(clientNow: () => number = () => performance.timeOrigin + performance.now()) {
    this.clientNow = clientNow;
  }

  attach(transport: Transport): void {
    this.transport = transport;
  }

  // --- derived -----------------------------------------------------------------------------------------

  get canApprove(): boolean {
    return this.device?.can_approve === true;
  }
  get canUnlock(): boolean {
    return this.device?.can_unlock_portfolio === true;
  }
  get pendingCount(): number {
    return this.actions.length;
  }
  get activeAlerts(): Alert[] {
    return this.alerts.filter((a) => !this.dismissedAlerts[a.alert_id]);
  }
  get stepUpValid(): boolean {
    return this.stepUp !== null && this.stepUp.expiresAt > this.clientNow();
  }
  /** Server time now, from the clock-offset estimate. */
  serverNow(): number {
    return this.transport ? this.transport.clock.serverNow(this.clientNow()) : this.clientNow();
  }

  /** Convert a client-clock time (e.g. the 1 Hz ticker) to server time. */
  toServer(clientMs: number): number {
    return this.transport ? clientMs + this.transport.clock.offsetMs : clientMs;
  }

  // --- connection ------------------------------------------------------------------------------------

  setConnection(status: ConnectionStatus): void {
    this.connection = status;
  }

  /** The socket dropped or reopened: forget per-connection state (a snapshot will follow). */
  onConnection(open: boolean): void {
    this.synced = false;
    this.pttActive = false;
    this.silent.clear();
    this.audio.tts.clear();
    this.audio.mic.clear();
    if (!open) this.announce('Disconnected from ATLAS core');
  }

  // --- inbound -----------------------------------------------------------------------------------------

  apply(msg: ServerMessage): void {
    switch (msg.type) {
      case 'state.changed':
        this.setOrb(msg.payload);
        break;
      case 'audio.level': {
        const p = msg.payload;
        (p.stream === 'tts' ? this.audio.tts : p.stream === 'mic' ? this.audio.mic : null)?.push(p.t_play, p.frame_ms || 16, p.levels);
        break;
      }
      case 'transcript.partial':
        this.partial = msg.payload;
        break;
      case 'transcript.final':
        this.addTranscript(msg.payload);
        break;
      case 'assistant.delta': {
        const p = msg.payload;
        this.streaming =
          this.streaming?.utterance_id === p.utterance_id
            ? { utterance_id: p.utterance_id, text: this.streaming.text + p.text }
            : { utterance_id: p.utterance_id, text: p.text };
        break;
      }
      case 'panel.update':
        this.panels[msg.payload.panel_id] = msg.payload;
        break;
      case 'host.status':
        this.host = msg.payload;
        break;
      case 'task.progress':
        this.tasks = upsert(this.tasks, msg.payload, (t) => t.task_id).slice(-50);
        break;
      case 'action.pending':
        this.actions = upsert(this.actions, msg.payload, (a) => a.action_id).slice(-50);
        if (!msg.payload.redacted) this.announce(`Approval needed: ${safeText(msg.payload.summary, 120)}`);
        break;
      case 'action.resolved':
        this.onResolved(msg.payload);
        break;
      case 'alert':
        this.alerts = [...this.alerts.filter((a) => a.alert_id !== msg.payload.alert_id), msg.payload].slice(-MAX_ALERTS);
        if (msg.payload.level === 'critical') this.urgent = `${msg.payload.code}: ${safeText(msg.payload.message, 160)}`;
        else this.announce(`${msg.payload.level === 'warn' ? 'Warning' : 'Notice'}: ${safeText(msg.payload.message, 160)}`);
        break;
      case 'privacy.mode':
        this.privacy = msg.payload;
        this.announce(msg.payload.on ? 'Privacy mode on' : 'Privacy mode off');
        break;
      case 'clock.pong':
        break; // handled by the client's ClockSync
      case 'sync.snapshot':
        this.applySnapshot(msg.payload);
        break;
      case 'stepup.granted':
        this.stepUp = {
          token: msg.payload.token,
          expiresAt: this.transport ? this.transport.clock.toClient(msg.payload.expires_at) : msg.payload.expires_at
        };
        break;
      case 'request.result':
        if (!msg.payload.ok && !this.silent.has(msg.payload.ref_seq)) {
          this.notify('error', resultMessage(msg.payload.code, msg.payload.message));
        }
        this.silent.delete(msg.payload.ref_seq);
        break;
    }
  }

  private setOrb(p: StateChanged): void {
    this.orb = p;
    this.orbAt = this.clientNow();
    if (p.state !== 'listening') this.pttActive = false;
    this.announce(describeOrb(p).label);
  }

  private addTranscript(t: Transcript): void {
    const next = this.transcript.filter((x) => !(x.utterance_id === t.utterance_id && x.role === t.role));
    this.transcript = [...next, t].slice(-MAX_TRANSCRIPT);
    if (this.streaming?.utterance_id === t.utterance_id && t.role === 'assistant') this.streaming = null;
    if (this.partial?.utterance_id === t.utterance_id) this.partial = null;
  }

  private onResolved(p: { action_id: string; outcome: string; code?: string }): void {
    this.resolved[p.action_id] = { outcome: p.outcome, code: p.code };
    if (p.outcome === 'approved') {
      this.approved[p.action_id] = true;
      return;
    }
    if (p.outcome === 'executed' || p.outcome === 'failed' || p.outcome === 'denied' || p.outcome === 'expired' || p.outcome === 'withdrawn') {
      this.actions = this.actions.filter((a) => a.action_id !== p.action_id);
      delete this.approved[p.action_id];
    }
    if (p.outcome === 'failed') this.notify('error', `Action failed${p.code ? ` (${p.code})` : ''}.`);
    // Unknown outcomes (newer MINOR revision) change nothing and are ignored.
  }

  /** A sync.snapshot is a full reset of everything the server owns. */
  applySnapshot(s: SyncSnapshot): void {
    this.server = s.server;
    this.device = s.device;
    this.orb = s.state;
    this.orbAt = this.clientNow();
    this.host = s.host;
    this.privacy = s.privacy;
    this.panels = Object.fromEntries(s.panels.map((p) => [p.panel_id, p]));
    this.actions = [...s.actions];
    const live = s.actions.map((a) => a.action_id);
    this.approved = Object.fromEntries(Object.entries(this.approved).filter(([id]) => live.includes(id)));
    this.tasks = [...s.tasks];
    this.alerts = [...s.alerts];
    this.transcript = [...s.transcript];
    this.layouts = { ...s.layouts };
    this.partial = null;
    this.streaming = null;
    this.audio.tts.clear();
    this.audio.mic.clear();
    this.pttActive = this.pttActive && s.state.state === 'listening';
    this.synced = true;
    this.announce(describeOrb(s.state).label);
  }

  // --- notices and announcements -----------------------------------------------------------------

  announce(text: string): void {
    this.announcement = text;
  }

  notify(level: Notice['level'], text: string): void {
    const id = ++this.noticeSeq;
    this.notices = [...this.notices, { id, level, text }].slice(-4);
    setTimeout(() => this.dismissNotice(id), 9000);
  }

  dismissNotice(id: number): void {
    this.notices = this.notices.filter((n) => n.id !== id);
  }

  dismissAlert(id: string): void {
    this.dismissedAlerts[id] = true;
  }

  // --- outbound ---------------------------------------------------------------------------------------

  private async ask<T extends ClientType>(
    type: T,
    payload: ClientPayloads[T],
    opts: { silent?: boolean; until?: (m: ServerMessage) => boolean } = {}
  ): Promise<Outcome> {
    const t = this.transport;
    if (!t?.isOpen) return fail('NO_CONNECTION', 'Not connected to ATLAS core.');
    const m = await t.request(type, payload, {
      until: opts.until,
      onSent: (seq) => {
        if (opts.silent) this.silent.add(seq);
      }
    });
    if (!m) return fail('NO_RESPONSE', 'No response from ATLAS core.');
    if (m.type === 'request.result') {
      return m.payload.ok
        ? { ok: true, message: '' }
        : { ok: false, code: m.payload.code, message: resultMessage(m.payload.code, m.payload.message) };
    }
    return { ok: true, message: '' };
  }

  async sendText(text: string): Promise<Outcome> {
    return this.ask('text.input', { text: text.slice(0, 2000) }, { silent: false });
  }

  async togglePtt(): Promise<void> {
    if (!this.pttActive) {
      const r = await this.ask('ptt.start', {});
      if (r.ok) this.pttActive = true;
    } else {
      this.pttActive = false;
      await this.ask('ptt.stop', {});
    }
  }

  async killTask(taskId: string): Promise<Outcome> {
    return this.ask('task.kill', { task_id: taskId });
  }

  async killAll(): Promise<Outcome> {
    return this.ask('killswitch.all', {});
  }

  async unlockPortfolio(passphrase: string): Promise<Outcome> {
    return this.ask('portfolio.unlock', { passphrase }, { silent: true });
  }

  /** Verify the step-up PIN. On success `stepUp` holds the token (valid 60 s). */
  async verifyPin(pin: string): Promise<Outcome> {
    const r = await this.ask('stepup.verify', { pin }, { silent: true, until: (m) => m.type === 'stepup.granted' });
    if (r.ok && !this.stepUpValid) return fail('NO_TOKEN', 'The server did not grant a token.');
    return r;
  }

  async setPrivacy(on: boolean): Promise<Outcome> {
    const payload = !on && this.stepUpValid && this.stepUp ? { on, step_up_token: this.stepUp.token } : { on };
    return this.ask('privacy.set', payload, { silent: true });
  }

  /**
   * Send an approval decision. `hash` must be the SHA-256 the HUD computed itself from payload_canonical.
   * Consumes the step-up token (one decision per PIN entry).
   */
  async decide(actionId: string, decision: 'approve' | 'deny', hash: string): Promise<Outcome> {
    if (!this.stepUp || !this.stepUpValid) return fail('STEP_UP_REQUIRED', REQUEST_RESULT_MESSAGES.STEP_UP_REQUIRED ?? 'Enter your PIN.');
    const token = this.stepUp.token;
    const r = await this.ask('action.decide', { action_id: actionId, decision, payload_sha256: hash, step_up_token: token }, { silent: true });
    this.stepUp = null;
    return r;
  }

  async saveLayout(profile: LayoutProfile, slots: LayoutSlots): Promise<Outcome> {
    const r = await this.ask('layout.save', { profile, slots }, { silent: true });
    if (r.ok) this.layouts = { ...this.layouts, [profile]: slots };
    return r;
  }

  requestSync(): void {
    this.transport?.send('sync.request', {});
  }
}

function fail(code: string, message: string): Outcome {
  return { ok: false, code, message };
}

function upsert<T>(list: T[], item: T, key: (t: T) => string): T[] {
  const k = key(item);
  const i = list.findIndex((x) => key(x) === k);
  if (i === -1) return [...list, item];
  const next = list.slice();
  next[i] = item;
  return next;
}
