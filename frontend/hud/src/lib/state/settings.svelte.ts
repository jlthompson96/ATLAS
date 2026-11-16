import type { LayoutProfile } from '../generated/events';

export type QualitySetting = 'auto' | 'full' | 'lite' | 'static';
export type MotionSetting = 'system' | 'reduce' | 'full';
export type ProfileSetting = 'auto' | LayoutProfile;

const KEY = 'atlas.hud.settings.v1';
const QUALITY: readonly QualitySetting[] = ['auto', 'full', 'lite', 'static'];
const MOTION: readonly MotionSetting[] = ['system', 'reduce', 'full'];
const PROFILE: readonly ProfileSetting[] = ['auto', 'kiosk', 'remote', 'compact'];

function pick<T extends string>(allowed: readonly T[], v: unknown, fallback: T): T {
  return typeof v === 'string' && (allowed as readonly string[]).includes(v) ? (v as T) : fallback;
}

/**
 * Per-browser display preferences only (orb quality, motion override, layout profile).
 * No secrets, tokens or content ever go into localStorage. Every access is guarded: storage can be
 * blocked or throw (private windows, kiosk profiles) and the HUD must work without it.
 */
export class Settings {
  quality = $state<QualitySetting>('auto');
  motion = $state<MotionSetting>('system');
  profile = $state<ProfileSetting>('auto');
  /** Live value of the OS-level prefers-reduced-motion media query. */
  systemReducedMotion = $state(false);
  viewportWidth = $state(1280);

  constructor(private readonly storage: Pick<Storage, 'getItem' | 'setItem'> | null = safeStorage()) {
    try {
      const raw = this.storage?.getItem(KEY);
      if (raw) {
        const o = JSON.parse(raw) as Record<string, unknown>;
        this.quality = pick(QUALITY, o.quality, 'auto');
        this.motion = pick(MOTION, o.motion, 'system');
        this.profile = pick(PROFILE, o.profile, 'auto');
      }
    } catch {
      /* corrupt or blocked storage: defaults */
    }
  }

  get reducedMotion(): boolean {
    return this.motion === 'reduce' || (this.motion === 'system' && this.systemReducedMotion);
  }

  set(patch: Partial<{ quality: QualitySetting; motion: MotionSetting; profile: ProfileSetting }>): void {
    if (patch.quality) this.quality = pick(QUALITY, patch.quality, 'auto');
    if (patch.motion) this.motion = pick(MOTION, patch.motion, 'system');
    if (patch.profile) this.profile = pick(PROFILE, patch.profile, 'auto');
    try {
      this.storage?.setItem(KEY, JSON.stringify({ quality: this.quality, motion: this.motion, profile: this.profile }));
    } catch {
      /* ignore */
    }
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

/** Which layout profile applies: device role first, then viewport width (§4.2). */
export function resolveProfile(setting: ProfileSetting, role: 'kiosk' | 'remote' | undefined, width: number): LayoutProfile {
  if (setting !== 'auto') return setting;
  if (width < 768) return 'compact';
  return role === 'kiosk' ? 'kiosk' : 'remote';
}

/** Layout breakpoints (§4.2): wide >= 1280, medium 768..1279, compact < 768. */
export type Breakpoint = 'wide' | 'medium' | 'compact';
export function breakpoint(width: number): Breakpoint {
  return width >= 1280 ? 'wide' : width >= 768 ? 'medium' : 'compact';
}
