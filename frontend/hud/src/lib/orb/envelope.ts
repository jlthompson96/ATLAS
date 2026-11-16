/**
 * Audio level envelope, scheduled by playback time (PLAN.md §4.4).
 *
 * The server sends `audio.level` batches at least 100 ms ahead of `t_play` (the server-clock time at which
 * `levels[0]` becomes audible). Each animation frame samples the buffer at `serverNow`, where
 * `serverNow = client clock + clock offset`, so the visual follows the audio rather than the network.
 */
export class EnvelopeBuffer {
  private times: number[] = [];
  private values: number[] = [];

  /** How long a value is held past the newest frame before it fades to 0 (live mic levels arrive just late). */
  constructor(
    private readonly holdMs = 48,
    private readonly fadeMs = 120,
    private readonly keepMs = 800
  ) {}

  get size(): number {
    return this.times.length;
  }

  push(tPlay: number, frameMs: number, levels: readonly number[]): void {
    let sorted = true;
    levels.forEach((raw, i) => {
      const t = tPlay + i * frameMs;
      const v = Number.isFinite(raw) ? Math.min(1, Math.max(0, raw)) : 0;
      const last = this.times[this.times.length - 1];
      if (last !== undefined && t <= last) sorted = false;
      this.times.push(t);
      this.values.push(v);
    });
    if (!sorted) this.resort();
  }

  /** Newest frame time, or null when empty. */
  get endTime(): number | null {
    return this.times[this.times.length - 1] ?? null;
  }

  /** Envelope value at server time `t`: linear interpolation between frames. */
  sample(t: number): number {
    const n = this.times.length;
    if (n === 0) return 0;
    const first = this.times[0] as number;
    const last = this.times[n - 1] as number;
    if (t < first) return 0;
    if (t >= last) {
      const over = t - last;
      const lastV = this.values[n - 1] as number;
      if (over <= this.holdMs) return lastV;
      return Math.max(0, lastV * (1 - (over - this.holdMs) / this.fadeMs));
    }
    // binary search for the last frame <= t
    let lo = 0;
    let hi = n - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if ((this.times[mid] as number) <= t) lo = mid;
      else hi = mid - 1;
    }
    const t0 = this.times[lo] as number;
    const t1 = this.times[lo + 1] as number;
    const v0 = this.values[lo] as number;
    const v1 = this.values[lo + 1] as number;
    return t1 === t0 ? v0 : v0 + ((v1 - v0) * (t - t0)) / (t1 - t0);
  }

  /** Fill `out` with samples at t, t - stepMs, t - 2*stepMs ... (newest first): the radial waveform history. */
  history(t: number, stepMs: number, out: number[]): void {
    for (let i = 0; i < out.length; i++) out[i] = this.sample(t - i * stepMs);
  }

  /** Drop frames older than `keepMs` before `t`. */
  prune(t: number): void {
    const cutoff = t - this.keepMs;
    let drop = 0;
    while (drop < this.times.length && (this.times[drop] as number) < cutoff) drop++;
    if (drop > 0) {
      this.times.splice(0, drop);
      this.values.splice(0, drop);
    }
  }

  clear(): void {
    this.times = [];
    this.values = [];
  }

  private resort(): void {
    const order = this.times.map((_, i) => i).sort((a, b) => (this.times[a] as number) - (this.times[b] as number));
    this.times = order.map((i) => this.times[i] as number);
    this.values = order.map((i) => this.values[i] as number);
  }
}
