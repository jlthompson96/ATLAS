import type {
  CalendarData,
  CalendarRedacted,
  CountRedacted,
  DigestData,
  DiscordData,
  EgressAuditData,
  EmailData,
  MarketsData,
  PanelUpdate,
  PortfolioData,
  PortfolioRedacted,
  WeatherData
} from '../generated/events';

/** Typed view of a panel.update. `redacted` selects the privacy-mode shape the server sent. */
export type TypedPanel<Full, Red = never> =
  | { redacted: false; data: Full; update: PanelUpdate }
  | { redacted: true; data: Red; update: PanelUpdate };

export function typed<Full, Red = never>(update: PanelUpdate | undefined): TypedPanel<Full, Red> | undefined {
  if (!update) return undefined;
  return update.redacted
    ? { redacted: true, data: update.data as Red, update }
    : { redacted: false, data: update.data as Full, update };
}

export type WeatherPanel = TypedPanel<WeatherData>;
export type EgressPanel = TypedPanel<EgressAuditData>;
export type DigestPanel = TypedPanel<DigestData, Record<string, never>>;
export type EmailPanel = TypedPanel<EmailData, CountRedacted>;
export type DiscordPanel = TypedPanel<DiscordData, CountRedacted>;
export type CalendarPanel = TypedPanel<CalendarData, CalendarRedacted>;
export type MarketsPanel = TypedPanel<MarketsData>;
export type PortfolioPanel = TypedPanel<PortfolioData, PortfolioRedacted>;

/** True for an unlocked, non-redacted portfolio payload. */
export function portfolioUnlocked(d: PortfolioData): d is Extract<PortfolioData, { locked: false }> {
  return d.locked === false;
}

/** Tolerant enum read: a value this client doesn't know (newer MINOR revision) maps to `fallback`. */
export function known<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === 'string' && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}
