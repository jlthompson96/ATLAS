// GENERATED FILE. Do not edit.
// Source: contracts/events.v1.schema.json (via `npm run gen:types`).
// A test and `npm run lint` fail if this file is stale.

export const SCHEMA_REVISION = "1.0.0" as const;

export const SERVER_TYPES = ["state.changed","audio.level","transcript.partial","transcript.final","assistant.delta","panel.update","host.status","task.progress","action.pending","action.resolved","alert","privacy.mode","clock.pong","sync.snapshot","stepup.granted","request.result"] as const;
export type ServerType = (typeof SERVER_TYPES)[number];
export const CLIENT_TYPES = ["text.input","ptt.start","ptt.stop","action.decide","task.kill","killswitch.all","privacy.set","portfolio.unlock","layout.save","clock.ping","sync.request","stepup.verify"] as const;
export type ClientType = (typeof CLIENT_TYPES)[number];

/** Fields that must never be logged (x-atlas-sensitive). */
export const SENSITIVE_KEYS: readonly string[] = ["passphrase","pin","step_up_token","token"];

export const STATE_TRANSITIONS: Readonly<Record<string, readonly string[]>> = {
  "idle": [
    "listening",
    "thinking",
    "waking_host",
    "llm_unreachable"
  ],
  "listening": [
    "thinking",
    "waking_host",
    "idle",
    "llm_unreachable",
    "error"
  ],
  "thinking": [
    "speaking",
    "idle",
    "llm_unreachable",
    "error",
    "waking_host"
  ],
  "speaking": [
    "idle",
    "llm_unreachable",
    "listening",
    "error"
  ],
  "waking_host": [
    "waking_host",
    "thinking",
    "error"
  ],
  "error": [
    "idle",
    "llm_unreachable"
  ],
  "llm_unreachable": [
    "llm_unreachable",
    "idle",
    "listening",
    "thinking",
    "waking_host"
  ]
};

/** Milliseconds since the Unix epoch, server clock unless stated. */
export type EpochMs = number;

export type Id = string;

export type Sha256Hex = string;

/** Machine-readable reason, e.g. WAKE_FAILED. Catalogue: contracts/README.md. */
export type Code = string;

/** Money and prices are decimal strings, never JSON numbers (computed with Python Decimal on the Pi, §2.16). */
export type Decimal = string;

export type Pct = number;

export type SignedPct = number;

export type ShortText = string;

/** Derived from third-party content (email, Discord, calendar, web). The HUD MUST render it as plain text only: never as HTML or Markdown, never as a link target or image source. */
export type UntrustedText = string;

export type Date = string;

export type Symbol = string;

/** Orb state (§4.3). 'idle' and 'llm_unreachable' are the two resting states: the server returns to llm_unreachable instead of idle while the core is offline. */
export const StateValues = ["idle","listening","thinking","speaking","waking_host","error","llm_unreachable"] as const;
export type State = (typeof StateValues)[number];

/** Wake stages in order (§5.4). Within one waking_host episode the stage never goes backwards. */
export const WakeStageValues = ["packet","link","api","model"] as const;
export type WakeStage = (typeof WakeStageValues)[number];

export type StateChanged = {
  state: State;
  /** Short human text shown after the label, e.g. ERROR: <detail>. */
  detail?: string;
  code?: Code;
  wake?: WakeProgress;
};

export type WakeProgress = {
  stage: WakeStage;
  elapsed_ms: number;
  /** 60000 for interactive wakes, 120000 for unattended (§5.4, §5.5). */
  deadline_ms: number;
};

export type AudioLevel = {
  stream: "tts" | "mic";
  /** tts: server-clock epoch ms at which levels[0] becomes audible; batches are sent >= 100 ms ahead of t_play (§4.4). mic: server-clock capture time of levels[0]. */
  t_play: number;
  frame_ms: 16;
  levels: number[];
};

export type SourceRef = {
  kind: "user" | "email" | "discord" | "calendar" | "web" | "memory" | "market" | "system";
  /** Resource ID, e.g. gmail:msg/18c2f..., web:src/42. */
  ref: string;
  label?: UntrustedText;
};

export type Provenance = {
  /** True when any input came from untrusted content. The HUD MUST show a visible provenance flag (§8.3). */
  tainted: boolean;
  sources: SourceRef[];
};

export type Transcript = {
  utterance_id: Id;
  role: "user" | "assistant";
  text: string;
  /** user: how the input arrived. assistant: whether the reply was spoken or text-only. */
  source: "voice" | "text";
  provenance?: Provenance;
};

export type AssistantDelta = {
  utterance_id: Id;
  text: string;
};

/** Panels fed by panel.update. Other panels are derived client-side: clock (synced server time), system and host (host.status), pending (action.*), research (task.progress), transcript (transcript.*, assistant.delta). */
export const DataPanelIdValues = ["weather","egress_audit","digest","email","discord","calendar","markets","portfolio"] as const;
export type DataPanelId = (typeof DataPanelIdValues)[number];

export const LayoutPanelIdValues = ["clock","weather","system","host","egress_audit","pending","research","digest","email","discord","calendar","markets","portfolio","transcript"] as const;
export type LayoutPanelId = (typeof LayoutPanelIdValues)[number];

export type PanelUpdate = {
  panel_id: DataPanelId;
  /** Fixed per panel (§4.6); enforced below. */
  sensitivity: "public" | "personal" | "secret";
  /** True when the server has applied privacy-mode redaction for this device; data then uses the panel's redacted shape. */
  redacted: boolean;
  updated_at: EpochMs;
  /** Present when the source is failing and data is the last good copy (e.g. FI-12). */
  stale_since?: EpochMs;
  data: Record<string, unknown>;
};

/** Panel is hidden in privacy mode. */
export type HiddenRedacted = Record<string, never>;

/** Count only in privacy mode. */
export type CountRedacted = {
  unread: number;
};

export type WeatherData = {
  location_label?: string;
  temp_c: number;
  feels_like_c?: number;
  /** WMO weather interpretation code, as returned by Open-Meteo. */
  condition_code: number;
  condition: string;
  high_c: number;
  low_c: number;
  precip_prob_pct?: Pct;
  wind_kph?: number;
  hourly?: ({
    t: EpochMs;
    temp_c: number;
    condition_code: number;
  })[];
  /** Required by Open-Meteo's licence (§2.15). The panel must display it. */
  attribution: "Weather data by Open-Meteo.com (CC BY 4.0)";
};

export type EgressAuditData = {
  egress: {
    proxy: "up" | "down";
    allowed_24h: number;
    blocked_24h: number;
  };
  audit: {
    /** PT-13: 'broken' must render as a clear failure (✗). */
    chain: "ok" | "broken" | "unverified";
    records_24h: number;
    last_verified_at?: EpochMs;
    last_checkpoint_at?: EpochMs;
  };
};

export type DigestData = {
  date: Date;
  items: ({
    kind: "research" | "wake_failure" | "market" | "alert" | "reauth" | "actions";
    title: string;
    level?: "info" | "warn" | "critical";
    ref?: Id;
  })[];
};

export type AuthStatus = {
  /** 'expiring' from 24 h before expiry (weekly re-authorization, §2.10). */
  status: "ok" | "expiring" | "expired" | "missing";
  expires_at?: EpochMs;
};

export type EmailData = {
  unread: number;
  auth: AuthStatus;
  items: ({
    id: Id;
    from_display: UntrustedText;
    from_domain: string;
    subject: UntrustedText;
    received_at: EpochMs;
    unread: boolean;
    flags?: ("new_sender" | "suspicious")[];
  })[];
};

export type DiscordData = {
  unread: number;
  items: ({
    id: Id;
    channel: UntrustedText;
    author_display: UntrustedText;
    excerpt?: UntrustedText;
    at: EpochMs;
  })[];
};

export type CalendarData = {
  auth: AuthStatus;
  events: ({
    id: Id;
    title: UntrustedText;
    start: EpochMs;
    end: EpochMs;
    all_day: boolean;
    location?: UntrustedText;
  })[];
};

/** Next event's time only in privacy mode (§4.6). */
export type CalendarRedacted = {
  next_event_start: EpochMs | null;
};

export type Quote = {
  symbol: Symbol;
  label?: string;
  /** Set when an ETF stands in for an index, e.g. 'S&P 500' for SPY (§2.16). The HUD labels it as a proxy. */
  proxy_for?: string;
  price: Decimal;
  change_pct: SignedPct;
  as_of: EpochMs;
  delayed: boolean;
  sparkline?: number[];
};

export type MarketsData = {
  session: "pre" | "open" | "after" | "closed" | "holiday";
  /** Indices and the user's display watchlist. Decoy tickers (D22) are never sent to the HUD. */
  quotes: Quote[];
  credits?: {
    used_today: number;
    limit_today: number;
  };
};

export type PortfolioData = {
  locked: true;
} | {
  locked: false;
  currency: "USD";
  total_value: Decimal;
  day_change: Decimal;
  day_change_pct: SignedPct;
  total_gain?: Decimal;
  total_gain_pct?: SignedPct;
  as_of: EpochMs;
  /** Date of the Fidelity export used to value mutual funds (§2.16). */
  nav_as_of?: Date;
  top_movers: ({
    symbol: Symbol;
    change_pct: SignedPct;
    value?: Decimal;
    weight_pct?: Pct;
  })[];
  alerts?: ({
    alert_id: Id;
    symbol: Symbol;
    message: string;
  })[];
};

export type PortfolioRedacted = {
  locked: true;
} | {
  locked: false;
  day_change_pct: SignedPct;
};

export type HostStatus = {
  pi: {
    cpu_pct: Pct;
    ram_pct: Pct;
    temp_c: number;
    /** Compared against the 100 MB/day microSD budget (§3.5). */
    disk_write_mb_24h: number;
    throttled?: boolean;
  };
  llm: {
    power: "asleep" | "waking" | "up" | "unknown";
    /** WireGuard tunnel. */
    link: "up" | "down";
    /** LLM server availability (§5.6): degraded → HOST: DEGRADED; busy → HOST: BUSY (D16); signed_out → CORE OFFLINE · SIGNED OUT. */
    service: "ready" | "loading" | "degraded" | "busy" | "signed_out" | "down" | "unknown";
    gpu?: {
      util_pct: Pct;
      vram_used_mb: number;
      vram_total_mb: number;
      temp_c: number;
    };
    model_loaded?: string;
  };
  voice: {
    /** unavailable → VOICE UNAVAILABLE · TYPE BELOW (§5.6, FI-4). */
    status: "ok" | "unavailable";
    /** suppressed while ATLAS speaks unless AEC is verified (§2.2). */
    wake_word: "active" | "suppressed" | "off";
  };
};

export type Budget = {
  steps: number;
  time_s: number;
  tokens: number;
  bytes: number;
};

export type TaskProgress = {
  task_id: Id;
  kind: "research";
  status: "queued" | "running" | "blocked" | "done" | "failed" | "killed";
  /** The user's research question, shortened. Omitted when redacted. */
  title?: string;
  redacted: boolean;
  step: number;
  /** Limits (§2.13). */
  budget: Budget;
  /** Consumed so far. */
  used: Budget;
  unattended: boolean;
  flags?: ("low_source_coverage" | "paused_window_end" | "citation_failures")[];
  result_ref?: Id;
};

export const ActionKindValues = ["email.send","discord.post","research.schedule","portfolio.import"] as const;
export type ActionKind = (typeof ActionKindValues)[number];

export type ActionPending = {
  action_id: Id;
  kind: ActionKind;
  summary: ShortText;
  /** The exact payload the executor will run, serialized with RFC 8785 (JCS). payload_sha256 = SHA-256 of its UTF-8 bytes. The HUD MUST recompute the hash and refuse to offer Approve on mismatch, and displays the payload parsed from this string. Kind-specific payload shapes are added in later minor revisions. */
  payload_canonical: string;
  payload_sha256: Sha256Hex;
  provenance: Provenance;
  warnings?: ("new_recipient" | "not_on_allowlist" | "tainted_args" | "unattended_origin")[];
  created_at: EpochMs;
  expires_at: EpochMs;
  redacted: false;
} | {
  action_id: Id;
  expires_at: EpochMs;
  redacted: true;
};

/** Removes the item from the pending list (approved items stay visible until executed or failed). */
export type ActionResolved = {
  action_id: Id;
  outcome: "approved" | "denied" | "expired" | "executed" | "failed" | "withdrawn";
  code?: Code;
  at: EpochMs;
};

export type Alert = {
  alert_id: Id;
  level: "info" | "warn" | "critical";
  code: Code;
  message: ShortText;
};

/** Per device. While on, the server redacts personal/secret data for this device and stops sending transcript.* and assistant.delta. On every change the server follows with a fresh sync.snapshot. */
export type PrivacyMode = {
  on: boolean;
  reason: "user" | "inactivity";
};

export type ClockPong = {
  client_t: number;
  server_t: EpochMs;
};

export type Device = {
  device_id: Id;
  role: "kiosk" | "remote";
  /** False for the kiosk by default (AP-4). */
  can_approve: boolean;
  /** False for the kiosk unless allowed in settings (§4.5). */
  can_unlock_portfolio: boolean;
};

export type LayoutSlots = {
  top?: LayoutPanelId;
  left: LayoutPanelId[];
  right: LayoutPanelId[];
  bottom?: LayoutPanelId;
};

export const LayoutProfileValues = ["kiosk","remote","compact"] as const;
export type LayoutProfile = (typeof LayoutProfileValues)[number];

export type SyncSnapshot = {
  server: {
    version: string;
    schema_revision: string;
    boot_id: Id;
  };
  device: Device;
  state: StateChanged;
  host: HostStatus;
  privacy: PrivacyMode;
  panels: PanelUpdate[];
  actions: ActionPending[];
  tasks: TaskProgress[];
  /** The most recent alerts, oldest first. */
  alerts: Alert[];
  /** Most recent final utterances, oldest first. Empty while privacy mode is on. */
  transcript: Transcript[];
  layouts: {
    kiosk?: LayoutSlots;
    remote?: LayoutSlots;
    compact?: LayoutSlots;
  };
};

export type StepupGranted = {
  token: string;
  /** 60 s after the PIN was verified (§4.5). */
  expires_at: EpochMs;
};

export type RequestResult = {
  /** seq of the client frame this answers. */
  ref_seq: number;
  ok: boolean;
  code?: Code;
  message?: ShortText;
};

export type Empty = Record<string, never>;

export type TextInput = {
  text: string;
};

export type ActionDecide = {
  action_id: Id;
  decision: "approve" | "deny";
  /** The hash the HUD itself computed from payload_canonical. */
  payload_sha256: Sha256Hex;
  step_up_token: string;
};

export type TaskKill = {
  task_id: Id;
};

export type PrivacySet = {
  on: boolean;
  /** Required to turn privacy mode off from a remote device (§4.6). */
  step_up_token?: string;
};

/** Never logged, never sent to an LLM, rate-limited; refused from the kiosk unless allowed (§4.5). */
export type PortfolioUnlock = {
  passphrase: string;
};

export type LayoutSave = {
  profile: LayoutProfile;
  slots: LayoutSlots;
};

export type ClockPing = {
  client_t: number;
};

export type SyncRequest = {
  last_seq?: number;
};

export type StepupVerify = {
  pin: string;
};

/** Payload type of each server -> client event. */
export interface ServerPayloads {
  "state.changed": StateChanged;
  "audio.level": AudioLevel;
  "transcript.partial": Transcript;
  "transcript.final": Transcript;
  "assistant.delta": AssistantDelta;
  "panel.update": PanelUpdate;
  "host.status": HostStatus;
  "task.progress": TaskProgress;
  "action.pending": ActionPending;
  "action.resolved": ActionResolved;
  "alert": Alert;
  "privacy.mode": PrivacyMode;
  "clock.pong": ClockPong;
  "sync.snapshot": SyncSnapshot;
  "stepup.granted": StepupGranted;
  "request.result": RequestResult;
}

/** Payload type of each client -> server message. */
export interface ClientPayloads {
  "text.input": TextInput;
  "ptt.start": Empty;
  "ptt.stop": Empty;
  "action.decide": ActionDecide;
  "task.kill": TaskKill;
  "killswitch.all": Empty;
  "privacy.set": PrivacySet;
  "portfolio.unlock": PortfolioUnlock;
  "layout.save": LayoutSave;
  "clock.ping": ClockPing;
  "sync.request": SyncRequest;
  "stepup.verify": StepupVerify;
}
