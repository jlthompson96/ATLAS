/** A 1 Hz reactive clock for countdowns, the wall clock and wake-stage seconds. Never drives animation. */
export class Ticker {
  /** Client epoch ms, refreshed once a second. */
  now = $state(Date.now());
  private timer: ReturnType<typeof setInterval> | null = null;

  start(): void {
    if (this.timer) return;
    this.now = Date.now();
    this.timer = setInterval(() => {
      this.now = Date.now();
    }, 1000);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
}
