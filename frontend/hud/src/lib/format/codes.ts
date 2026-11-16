/** Human-readable text for the `code` catalogue in contracts/README.md. Unknown codes fall back to the server's message. */

export const REQUEST_RESULT_MESSAGES: Readonly<Record<string, string>> = {
  INVALID_JSON: 'The server could not read that message.',
  INVALID_MESSAGE: 'The server rejected that message as malformed.',
  SEQ_NOT_INCREASING: 'Out-of-order message; the connection is resyncing.',
  BAD_PIN: 'Wrong PIN.',
  BAD_PASSPHRASE: 'Wrong passphrase.',
  RATE_LIMITED: 'Too many attempts. Wait a while and try again.',
  STEP_UP_REQUIRED: 'Enter your PIN to confirm this.',
  FORBIDDEN_DEVICE: 'This device is not allowed to do that.',
  UNKNOWN_ACTION: 'That action no longer exists.',
  UNKNOWN_TASK: 'That task no longer exists.',
  HASH_MISMATCH: 'The request changed since you reviewed it. Review it again.',
  EXPIRED: 'That request has expired.',
  BUSY: 'ATLAS is busy. Try again in a moment.',
  NOT_LISTENING: 'ATLAS is not listening.',
  MOCK_SCENARIO_PLAYING: 'A mock scenario is playing. Run the mock with --scenario interactive to try this.'
};

export const ALERT_TITLES: Readonly<Record<string, string>> = {
  WAKE_FAILED: 'Core failed to wake',
  CORE_UNREACHABLE: 'Core unreachable',
  CORE_RECOVERED: 'Core recovered',
  LLM_AUTH_FAILED: 'Core sign-in failed',
  HOST_DEGRADED: 'Host degraded',
  HOST_BUSY: 'Host busy',
  VOICE_UNAVAILABLE: 'Voice unavailable',
  PI_THERMAL: 'Pi running hot',
  WRITE_BUDGET_EXCEEDED: 'Disk write budget exceeded',
  GOOGLE_REAUTH_DUE: 'Google sign-in due',
  MARKET_STALE: 'Market data stale',
  EGRESS_DOWN: 'Egress proxy down',
  AUDIT_CHAIN_BROKEN: 'Audit chain broken',
  PORTFOLIO_LOCKED: 'Portfolio locked',
  TASK_KILLED: 'Task killed',
  KILLSWITCH: 'Kill switch used',
  UNEXPECTED_WAKE: 'Unexpected wake',
  RESEARCH_NOT_RUN: 'Research did not run'
};

export const RESOLVED_MESSAGES: Readonly<Record<string, string>> = {
  EXECUTOR_REJECTED: 'The executor rejected the action.',
  SEND_FAILED: 'Sending failed.',
  GRANT_EXPIRED: 'The Google grant expired.'
};

export const ERROR_STATE_MESSAGES: Readonly<Record<string, string>> = {
  WAKE_FAILED: 'The core did not wake in time.',
  LLM_AUTH_FAILED: 'The core rejected its sign-in token.',
  LLM_ERROR: 'The core returned an error.',
  STT_FAILED: 'Speech recognition failed.',
  TTS_FAILED: 'Speech output failed.',
  TIMEOUT: 'The request timed out.'
};

/** Orb label suffix for `llm_unreachable` per code (§5.6). */
export const UNREACHABLE_LABELS: Readonly<Record<string, string>> = {
  CORE_UNREACHABLE: 'CORE OFFLINE · LOCAL MODE',
  LINK_DOWN: 'CORE OFFLINE · LINK DOWN',
  SIGNED_OUT: 'CORE OFFLINE · SIGNED OUT',
  WAKE_FAILED: 'CORE OFFLINE · WAKE FAILED'
};

export function resultMessage(code: string | undefined, message: string | undefined): string {
  if (code && REQUEST_RESULT_MESSAGES[code]) return REQUEST_RESULT_MESSAGES[code];
  if (message) return message;
  return code ? `Request failed (${code}).` : 'Request failed.';
}
