export type WeatherKind = 'clear' | 'partly' | 'cloud' | 'fog' | 'drizzle' | 'rain' | 'snow' | 'storm' | 'unknown';

/** WMO weather interpretation code (Open-Meteo) to a coarse icon family. */
export function weatherKind(code: number): WeatherKind {
  if (code === 0) return 'clear';
  if (code === 1 || code === 2) return 'partly';
  if (code === 3) return 'cloud';
  if (code === 45 || code === 48) return 'fog';
  if (code >= 51 && code <= 57) return 'drizzle';
  if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return 'rain';
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return 'snow';
  if (code >= 95 && code <= 99) return 'storm';
  return 'unknown';
}

/** A Celsius reading (the contract's unit) in the unit the viewer chose, rounded: `7°`, or `45°` in Fahrenheit. */
export function formatTemp(c: unknown, unit: 'c' | 'f' = 'c'): string {
  if (typeof c !== 'number' || !Number.isFinite(c)) return '—';
  const value = unit === 'f' ? (c * 9) / 5 + 32 : c;
  return `${Math.round(value) || 0}°`; // `|| 0`: never "-0°"
}

/** Wind (km/h in the contract) in the units that go with the temperature choice: mph with Fahrenheit. */
export function formatWind(kph: unknown, unit: 'c' | 'f' = 'c'): string {
  if (typeof kph !== 'number' || !Number.isFinite(kph)) return '—';
  return unit === 'f' ? `${Math.round(kph * 0.621371)} mph` : `${Math.round(kph)} km/h`;
}
