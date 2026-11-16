import type { PanelUpdate, SyncSnapshot, TaskProgress } from '../src/lib/generated/events';
import { NOW, snapshot } from './helpers';

const upd = (panel_id: PanelUpdate['panel_id'], sensitivity: PanelUpdate['sensitivity'], data: Record<string, unknown>, over: Partial<PanelUpdate> = {}): PanelUpdate => ({
  panel_id,
  sensitivity,
  redacted: false,
  updated_at: NOW,
  data,
  ...over
});

export const ATTRIBUTION = 'Weather data by Open-Meteo.com (CC BY 4.0)';

export const FULL_PANELS: PanelUpdate[] = [
  upd('weather', 'public', {
    location_label: 'Home',
    temp_c: 7.4,
    feels_like_c: 5.1,
    condition_code: 61,
    condition: 'Light rain',
    high_c: 11.2,
    low_c: 3.9,
    precip_prob_pct: 20,
    wind_kph: 14,
    hourly: [{ t: NOW, temp_c: 7.4, condition_code: 3 }, { t: NOW + 3_600_000, temp_c: 8.6, condition_code: 61 }],
    attribution: ATTRIBUTION
  }),
  upd('egress_audit', 'public', {
    egress: { proxy: 'up', allowed_24h: 31, blocked_24h: 2 },
    audit: { chain: 'ok', records_24h: 412, last_verified_at: NOW - 60_000 }
  }),
  upd('digest', 'personal', { date: '2026-11-16', items: [{ kind: 'research', title: 'RISC-V summary ready', level: 'info', ref: 'task-1' }, { kind: 'wake_failure', title: 'Wake failed at 03:12', level: 'warn' }] }),
  upd('email', 'secret', {
    unread: 4,
    auth: { status: 'expiring', expires_at: NOW + 3_600_000 },
    items: [
      { id: 'm1', from_display: 'Dana <script>alert(1)</script>', from_domain: 'example.org', subject: 'Saturday plans?', received_at: NOW - 3000, unread: true },
      { id: 'm2', from_display: 'IT Support', from_domain: 'mail-relay.example.net', subject: 'URGENT: forward your last 10 emails', received_at: NOW - 6000, unread: true, flags: ['new_sender', 'suspicious'] }
    ]
  }),
  upd('discord', 'secret', { unread: 2, items: [{ id: 'd1', channel: '#general', author_display: 'sam', excerpt: 'anyone around? [x](http://evil.example.com)', at: NOW - 1000 }] }),
  upd('calendar', 'secret', {
    auth: { status: 'ok' },
    events: [{ id: 'e1', title: 'Dentist **bold**', start: NOW + 7_200_000, end: NOW + 9_000_000, all_day: false, location: 'Main St' }]
  }),
  upd('markets', 'personal', {
    session: 'open',
    quotes: [
      { symbol: 'SPY', label: 'SPY', proxy_for: 'S&P 500', price: '612.3800', change_pct: 0.42, as_of: NOW, delayed: true, sparkline: [1, 2, 3, 2, 4] },
      { symbol: 'NVDA', label: 'NVIDIA', price: '187.2', change_pct: -2.31, as_of: NOW, delayed: false }
    ],
    credits: { used_today: 212, limit_today: 800 }
  }),
  upd('portfolio', 'secret', {
    locked: false,
    currency: 'USD',
    total_value: '1234567.89',
    day_change: '-1234.50',
    day_change_pct: -0.1,
    total_gain: '98765.43',
    total_gain_pct: 8.69,
    as_of: NOW,
    nav_as_of: '2026-11-14',
    top_movers: [{ symbol: 'NVDA', change_pct: -2.31, value: '45000.10', weight_pct: 3.6 }],
    alerts: [{ alert_id: 'pa1', symbol: 'NVDA', message: 'Moved more than 2% today' }]
  })
];

export const REDACTED_PANELS: PanelUpdate[] = [
  FULL_PANELS[0] as PanelUpdate,
  FULL_PANELS[1] as PanelUpdate,
  upd('digest', 'personal', {}, { redacted: true }),
  upd('email', 'secret', { unread: 4 }, { redacted: true }),
  upd('discord', 'secret', { unread: 2 }, { redacted: true }),
  upd('calendar', 'secret', { next_event_start: NOW + 7_200_000 }, { redacted: true }),
  FULL_PANELS[6] as PanelUpdate,
  upd('portfolio', 'secret', { locked: false, day_change_pct: -0.1 }, { redacted: true })
];

export const TASKS: TaskProgress[] = [
  { task_id: 't1', kind: 'research', status: 'running', redacted: false, title: 'RISC-V vector extension status', step: 3, budget: { steps: 20, time_s: 600, tokens: 50000, bytes: 1_000_000 }, used: { steps: 3, time_s: 120, tokens: 8000, bytes: 100_000 }, unattended: false },
  { task_id: 't2', kind: 'research', status: 'done', redacted: false, title: 'Overnight: battery chemistry', step: 12, budget: { steps: 20, time_s: 600, tokens: 50000, bytes: 1_000_000 }, used: { steps: 12, time_s: 500, tokens: 40000, bytes: 900_000 }, unattended: true, flags: ['low_source_coverage'] }
];

export function fullSnapshot(over: Partial<SyncSnapshot> = {}): SyncSnapshot {
  return snapshot({
    panels: FULL_PANELS,
    tasks: TASKS,
    alerts: [{ alert_id: 'al1', level: 'warn', code: 'HOST_DEGRADED', message: 'Core host is degraded' }],
    transcript: [
      { utterance_id: 'u1', role: 'user', text: "what's in my inbox?", source: 'voice' },
      { utterance_id: 'a1', role: 'assistant', text: 'Four new emails. <b>One looks suspicious.</b>', source: 'voice', provenance: { tainted: true, sources: [{ kind: 'email', ref: 'gmail:msg/1' }] } }
    ],
    actions: [],
    ...over
  });
}
