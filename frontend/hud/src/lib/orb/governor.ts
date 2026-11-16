export type Tier = 'full' | 'lite' | 'static';
export const TIER_ORDER: readonly Tier[] = ['full', 'lite', 'static'];

export interface GovernorOptions {
  /** Best tier allowed (the user's choice, WebGL2 availability, reduced motion). */
  ceiling: Tier;
  /** Drop a level when frame p95 exceeds this for `windowMs` (PLAN.md §3.4: 40 ms for 10 s). */
  thresholdMs?: number;
  windowMs?: number;
  /** Recover one level after this long under target (5 min). */
  recoverMs?: number;
  /** Drop a level when the SoC is hotter than this (75 C). */
  thermalC?: number;
  /** Target p95 used for the recovery check. */
  targetMs?: number;
}

function p95(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * 0.95) - 1)] as number;
}

/**
 * Auto-degrade controller (PLAN.md §3.4, UP-4). Pure: time is passed in, so it is unit-testable.
 * full -> lite -> static on slow frames or heat; steps back up after 5 minutes under target.
 */
export class QualityGovernor {
  tier: Tier;
  private ceiling: Tier;
  private frames: { t: number; ms: number }[] = [];
  private lastChange: number;
  private underTargetSince: number | null = null;
  private hot = false;
  private readonly thresholdMs: number;
  private readonly windowMs: number;
  private readonly recoverMs: number;
  private readonly thermalC: number;
  private readonly targetMs: number;

  constructor(opts: GovernorOptions, now = 0) {
    this.ceiling = opts.ceiling;
    this.tier = opts.ceiling;
    this.thresholdMs = opts.thresholdMs ?? 40;
    this.windowMs = opts.windowMs ?? 10_000;
    this.recoverMs = opts.recoverMs ?? 300_000;
    this.thermalC = opts.thermalC ?? 75;
    this.targetMs = opts.targetMs ?? 33;
    this.lastChange = now;
  }

  setCeiling(ceiling: Tier, now: number): void {
    this.ceiling = ceiling;
    if (TIER_ORDER.indexOf(this.tier) < TIER_ORDER.indexOf(ceiling)) this.change(ceiling, now);
  }

  /** Feed one frame time (ms between frames). Returns the tier to use. */
  recordFrame(frameMs: number, now: number): Tier {
    if (this.tier === 'static') return this.tier;
    this.frames.push({ t: now, ms: frameMs });
    while (this.frames.length > 0 && (this.frames[0] as { t: number }).t < now - this.windowMs) this.frames.shift();
    const span = now - this.lastChange;
    if (span >= this.windowMs && this.frames.length >= 10) {
      const p = p95(this.frames.map((f) => f.ms));
      if (p > this.thresholdMs) {
        this.degrade(now);
        return this.tier;
      }
      if (p <= this.targetMs) this.underTargetSince ??= now;
      else this.underTargetSince = null;
    }
    this.maybeRecover(now);
    return this.tier;
  }

  /** Feed the Pi's SoC temperature from host.status. */
  recordTemp(tempC: number, now: number): Tier {
    if (tempC > this.thermalC) {
      if (!this.hot || now - this.lastChange >= this.windowMs) {
        this.hot = true;
        this.degrade(now);
      }
    } else if (tempC <= this.thermalC - 5) {
      this.hot = false;
    }
    this.maybeRecover(now);
    return this.tier;
  }

  /** Static mode has no frames to measure, so recovery from it is time-based. */
  tick(now: number): Tier {
    this.maybeRecover(now);
    return this.tier;
  }

  private maybeRecover(now: number): void {
    if (this.hot || TIER_ORDER.indexOf(this.tier) <= TIER_ORDER.indexOf(this.ceiling)) return;
    const underFor = this.tier === 'static' ? now - this.lastChange : this.underTargetSince === null ? 0 : now - this.underTargetSince;
    if (underFor >= this.recoverMs && now - this.lastChange >= this.recoverMs) {
      const better = TIER_ORDER[TIER_ORDER.indexOf(this.tier) - 1];
      if (better) this.change(better, now);
    }
  }

  private degrade(now: number): void {
    const worse = TIER_ORDER[TIER_ORDER.indexOf(this.tier) + 1];
    if (worse) this.change(worse, now);
  }

  private change(tier: Tier, now: number): void {
    this.tier = tier;
    this.lastChange = now;
    this.frames = [];
    this.underTargetSince = null;
  }
}
