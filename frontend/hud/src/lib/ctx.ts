import { getContext } from 'svelte';
import type { HudStore } from './state/hud.svelte';
import type { Settings } from './state/settings.svelte';
import type { Ticker } from './state/ticker.svelte';

export interface HudContext {
  store: HudStore;
  settings: Settings;
  ticker: Ticker;
}

export const HUD_CONTEXT = Symbol('atlas.hud');

export function useHud(): HudContext {
  const ctx = getContext<HudContext | undefined>(HUD_CONTEXT);
  if (!ctx) throw new Error('HUD context missing: mount components inside <App> or provide HUD_CONTEXT');
  return ctx;
}
