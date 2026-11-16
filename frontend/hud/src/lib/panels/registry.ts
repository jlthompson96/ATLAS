import type { Component } from 'svelte';
import type { LayoutPanelId, LayoutProfile, LayoutSlots } from '../generated/events';
import Calendar from './Calendar.svelte';
import Clock from './Clock.svelte';
import Digest from './Digest.svelte';
import Discord from './Discord.svelte';
import EgressAudit from './EgressAudit.svelte';
import Email from './Email.svelte';
import Host from './Host.svelte';
import Markets from './Markets.svelte';
import Pending from './Pending.svelte';
import Portfolio from './Portfolio.svelte';
import Research from './Research.svelte';
import System from './System.svelte';
import Transcript from './Transcript.svelte';
import Weather from './Weather.svelte';

export interface PanelDef {
  title: string;
  component: Component<{ collapsible?: boolean }>;
}

/** Panels are compiled into the bundle: there is no third-party panel code (§4.6). */
export const REGISTRY: Record<LayoutPanelId, PanelDef> = {
  clock: { title: 'Clock', component: Clock },
  weather: { title: 'Weather', component: Weather },
  system: { title: 'System', component: System },
  host: { title: 'Core host', component: Host },
  egress_audit: { title: 'Egress / audit', component: EgressAudit },
  pending: { title: 'Pending approvals', component: Pending },
  research: { title: 'Research', component: Research },
  digest: { title: 'Morning digest', component: Digest },
  email: { title: 'Email', component: Email },
  discord: { title: 'Discord', component: Discord },
  calendar: { title: 'Calendar', component: Calendar },
  markets: { title: 'Markets', component: Markets },
  portfolio: { title: 'Portfolio', component: Portfolio },
  transcript: { title: 'Transcript', component: Transcript }
};

/** Used only until the server's layouts arrive (or if it omits a profile). Mirrors §4.6 defaults. */
export const FALLBACK_LAYOUTS: Record<LayoutProfile, LayoutSlots> = {
  kiosk: {
    top: 'clock',
    left: ['system', 'email', 'discord', 'calendar'],
    right: ['pending', 'research', 'egress_audit', 'markets'],
    bottom: 'transcript'
  },
  remote: {
    top: 'clock',
    left: ['system', 'weather', 'email', 'discord', 'calendar'],
    right: ['pending', 'research', 'markets', 'portfolio', 'egress_audit'],
    bottom: 'transcript'
  },
  compact: { left: ['pending', 'research', 'system', 'portfolio'], right: [], bottom: 'transcript' }
};

export function isPanelId(id: string): id is LayoutPanelId {
  return Object.hasOwn(REGISTRY, id);
}

/** Drop panel ids this build doesn't know (a newer MINOR revision may add some). */
export function sanitizeSlots(slots: LayoutSlots): LayoutSlots {
  const out: LayoutSlots = {
    left: slots.left.filter(isPanelId),
    right: slots.right.filter(isPanelId)
  };
  if (slots.top && isPanelId(slots.top)) out.top = slots.top;
  if (slots.bottom && isPanelId(slots.bottom)) out.bottom = slots.bottom;
  return out;
}
