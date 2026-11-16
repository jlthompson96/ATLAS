export const ACTIVITY_MIN_INTERVAL_MS = 30_000;

const EVENTS = ['pointerdown', 'pointermove', 'keydown', 'touchstart', 'wheel'] as const;

export interface ActivityTarget {
  addEventListener(type: string, fn: (e: Event) => void, opts?: AddEventListenerOptions): void;
  removeEventListener(type: string, fn: (e: Event) => void): void;
}

/**
 * Sends `activity.ping` (empty payload, no reply) on real user input so the server's privacy
 * inactivity timer sees someone is here. At most one per 30 s, never while the tab is hidden, only for
 * trusted (user-generated) events, and it carries no coordinates or content.
 */
export function startActivityPings(opts: {
  target: ActivityTarget;
  send: () => unknown;
  isHidden: () => boolean;
  now?: () => number;
}): () => void {
  const now = opts.now ?? (() => performance.now());
  let last = -Infinity;
  const onInput = (e: Event): void => {
    if (!e.isTrusted || opts.isHidden()) return;
    const t = now();
    if (t - last < ACTIVITY_MIN_INTERVAL_MS) return;
    last = t;
    opts.send();
  };
  for (const ev of EVENTS) opts.target.addEventListener(ev, onInput, { passive: true });
  return () => {
    for (const ev of EVENTS) opts.target.removeEventListener(ev, onInput);
  };
}
