import type { ClientPayloads, ClientType, ServerPayloads, ServerType } from '../generated/events';
import { SENSITIVE_KEYS, SERVER_TYPES } from '../generated/events';

export type Envelope<T extends string, P> = { v: 1; type: T; seq: number; ts: number; payload: P };

/** Every server -> client frame, discriminated on `type`. */
export type ServerMessage = { [K in ServerType]: Envelope<K, ServerPayloads[K]> }[ServerType];
export type ClientMessage = { [K in ClientType]: Envelope<K, ClientPayloads[K]> }[ClientType];

export type ParseResult =
  | { kind: 'ok'; msg: ServerMessage }
  /** A well-formed envelope of a type this client doesn't know (a newer MINOR revision). Log and drop. */
  | { kind: 'unknown-type'; type: string; seq: number }
  | { kind: 'invalid'; reason: string };

type Shape = Record<string, 'string' | 'number' | 'boolean' | 'object' | 'array'>;

/** Required top-level payload fields per event: just enough that rendering can't throw. */
const SHAPES: Record<ServerType, Shape> = {
  'state.changed': { state: 'string' },
  'audio.level': { stream: 'string', t_play: 'number', levels: 'array' },
  'transcript.partial': { utterance_id: 'string', role: 'string', text: 'string' },
  'transcript.final': { utterance_id: 'string', role: 'string', text: 'string' },
  'assistant.delta': { utterance_id: 'string', text: 'string' },
  'panel.update': { panel_id: 'string', data: 'object' },
  'host.status': { pi: 'object', llm: 'object', voice: 'object' },
  'task.progress': { task_id: 'string', status: 'string' },
  'action.pending': { action_id: 'string', redacted: 'boolean' },
  'action.resolved': { action_id: 'string', outcome: 'string' },
  alert: { alert_id: 'string', level: 'string', code: 'string', message: 'string' },
  'privacy.mode': { on: 'boolean' },
  'clock.pong': { client_t: 'number', server_t: 'number' },
  'sync.snapshot': {
    server: 'object',
    device: 'object',
    state: 'object',
    host: 'object',
    privacy: 'object',
    panels: 'array',
    actions: 'array',
    tasks: 'array',
    alerts: 'array',
    transcript: 'array',
    layouts: 'object'
  },
  'stepup.granted': { token: 'string', expires_at: 'number' },
  'request.result': { ref_seq: 'number', ok: 'boolean' }
};

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function matches(value: unknown, want: Shape[string]): boolean {
  if (want === 'array') return Array.isArray(value);
  if (want === 'object') return isRecord(value);
  return typeof value === want;
}

/**
 * Parse and validate one server frame. Tolerates MINOR changes: unknown fields are ignored (never
 * stripped or rejected) and unknown event types are reported, not treated as errors.
 */
export function parseServerFrame(raw: string): ParseResult {
  let frame: unknown;
  try {
    frame = JSON.parse(raw);
  } catch {
    return { kind: 'invalid', reason: 'not JSON' };
  }
  if (!isRecord(frame)) return { kind: 'invalid', reason: 'frame is not an object' };
  if (frame.v !== 1) return { kind: 'invalid', reason: `unsupported protocol version ${JSON.stringify(frame.v)}` };
  const { type, seq, ts, payload } = frame;
  if (typeof type !== 'string') return { kind: 'invalid', reason: 'missing type' };
  if (typeof seq !== 'number' || !Number.isInteger(seq) || seq < 0) return { kind: 'invalid', reason: 'bad seq' };
  if (typeof ts !== 'number' || !Number.isFinite(ts)) return { kind: 'invalid', reason: 'bad ts' };
  if (!isRecord(payload)) return { kind: 'invalid', reason: 'payload is not an object' };
  if (!(SERVER_TYPES as readonly string[]).includes(type)) return { kind: 'unknown-type', type, seq };
  const shape = SHAPES[type as ServerType];
  for (const [field, want] of Object.entries(shape)) {
    if (!matches(payload[field], want)) return { kind: 'invalid', reason: `${type}: field ${field} must be ${want}` };
  }
  return { kind: 'ok', msg: frame as unknown as ServerMessage };
}

/** Deep copy with every x-atlas-sensitive field replaced. Use before any logging of a frame. */
export function redactForLog(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactForLog);
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => [k, SENSITIVE_KEYS.includes(k) ? '[REDACTED]' : redactForLog(v)])
    );
  }
  return value;
}
