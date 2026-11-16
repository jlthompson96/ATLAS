/**
 * Text helpers. Everything here returns plain strings meant for text interpolation only: never feed
 * the result of any of these (or any untrusted field) to an HTML sink, a link target or an image source.
 */

// Code point ranges removed from untrusted text: C0/C1 controls (except tab and newline), zero-width
// characters, bidi overrides/isolates, and the BOM. Written as numbers so no invisible character sits in source.
const UNSAFE_RANGES: readonly (readonly [number, number])[] = [
  [0x00, 0x08],
  [0x0b, 0x1f],
  [0x7f, 0x9f],
  [0x200b, 0x200f],
  [0x2028, 0x2029],
  [0x202a, 0x202e],
  [0x2060, 0x2069],
  [0xfeff, 0xfeff]
];

function isUnsafe(cp: number): boolean {
  return UNSAFE_RANGES.some(([lo, hi]) => cp >= lo && cp <= hi);
}

/** Strip characters that can spoof or reorder rendered text. Content stays plain text. */
export function safeText(value: unknown, maxLength = 500): string {
  if (typeof value !== 'string') return '';
  let clean = '';
  for (const ch of value) if (!isUnsafe(ch.codePointAt(0) ?? 0)) clean += ch;
  return clean.length > maxLength ? `${clean.slice(0, maxLength - 1)}…` : clean;
}

const GROUP = /\B(?=(\d{3})+(?!\d))/g;

/**
 * Format a decimal string ("12345.6") for display WITHOUT parsing it to a number: money never goes
 * through floating point (§2.16). Groups thousands, keeps the digits the server sent, pads to
 * `minFraction` places. Anything that isn't a decimal string is returned as "—".
 */
export function formatDecimal(value: unknown, minFraction = 2): string {
  if (typeof value !== 'string') return '—';
  const m = /^(-?)(0|[1-9][0-9]*)(?:\.([0-9]{1,8}))?$/.exec(value);
  if (!m) return '—';
  const [, sign = '', int = '0', frac = ''] = m;
  const fraction = frac.padEnd(minFraction, '0');
  return `${sign}${int.replace(GROUP, ',')}${fraction ? `.${fraction}` : ''}`;
}

/** Sign of a decimal string without parsing it as a number. */
export function decimalSign(value: unknown): -1 | 0 | 1 {
  if (typeof value !== 'string' || !/^-?[0-9.]+$/.test(value)) return 0;
  if (/^-?0*\.?0*$/.test(value)) return 0;
  return value.startsWith('-') ? -1 : 1;
}

/** "+1.23%" / "−0.40%" for a percentage that arrives as a JSON number (not money). */
export function formatPct(value: unknown, digits = 2): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const s = Math.abs(value).toFixed(digits);
  if (Number(s) === 0) return `${s}%`;
  return `${value < 0 ? '−' : '+'}${s}%`;
}

/** Arrow glyph for a signed value: the shape carries meaning as well as colour. */
export function trendGlyph(sign: number): string {
  return sign > 0 ? '▲' : sign < 0 ? '▼' : '■';
}

export function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

export function formatClock(ms: number): string {
  const d = new Date(ms);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function formatDay(ms: number): string {
  const d = new Date(ms);
  return `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

/** "3 min ago" style age. */
export function formatAge(ms: number, now: number): string {
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function formatCountdown(ms: number): string {
  const s = Math.max(0, Math.ceil(ms / 1000));
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${pad2(Math.floor((s % 3600) / 60))}m`;
  return `${Math.floor(s / 60)}:${pad2(s % 60)}`;
}
