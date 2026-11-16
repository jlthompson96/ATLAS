/** Client wall clock in epoch ms with sub-millisecond resolution (monotonic between samples). */
export function clientNow(): number {
  return performance.timeOrigin + performance.now();
}

interface Sample {
  offset: number;
  rtt: number;
}

/**
 * Estimates `server_clock - client_clock` from clock.ping/clock.pong round trips (PLAN.md §4.4).
 * The sample with the smallest round trip in the recent window is used: it has the least
 * queueing delay, so its half-RTT assumption is the most accurate.
 */
export class ClockSync {
  private samples: Sample[] = [];

  constructor(private readonly windowSize = 8) {}

  /** Record one completed round trip. `serverT` is the server's clock when it answered. */
  add(clientSent: number, clientReceived: number, serverT: number): void {
    const rtt = clientReceived - clientSent;
    if (!Number.isFinite(rtt) || rtt < 0) return;
    this.samples.push({ offset: serverT - (clientSent + rtt / 2), rtt });
    if (this.samples.length > this.windowSize) this.samples.shift();
  }

  get ready(): boolean {
    return this.samples.length > 0;
  }

  get best(): Sample | null {
    let best: Sample | null = null;
    for (const s of this.samples) if (!best || s.rtt < best.rtt) best = s;
    return best;
  }

  /** Milliseconds to add to the client clock to get server time (0 until the first sample). */
  get offsetMs(): number {
    return this.best?.offset ?? 0;
  }

  get rttMs(): number | null {
    return this.best?.rtt ?? null;
  }

  serverNow(clientTime: number = clientNow()): number {
    return clientTime + this.offsetMs;
  }

  /** Convert a server timestamp to the client clock. */
  toClient(serverTime: number): number {
    return serverTime - this.offsetMs;
  }

  reset(): void {
    this.samples = [];
  }
}
