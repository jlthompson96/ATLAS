import type { ActionPending } from '../generated/events';

/** SHA-256 of the string's UTF-8 bytes, lower-case hex, computed with WebCrypto. */
export async function sha256Hex(text: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new Error('WebCrypto (crypto.subtle) is unavailable; approvals cannot be verified');
  const digest = await subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

export type FullAction = Extract<ActionPending, { redacted: false }>;

export type Verification =
  | { status: 'verified'; hash: string; payload: unknown }
  | { status: 'mismatch'; computed: string; claimed: string }
  | { status: 'unverifiable'; reason: string };

/**
 * Recompute the hash of `payload_canonical` and compare it with the claimed `payload_sha256`.
 * The HUD offers Approve only for `verified`, and sends the hash it computed itself.
 * The payload shown to the user is parsed from the very string that was hashed.
 */
export async function verifyAction(action: FullAction): Promise<Verification> {
  let computed: string;
  try {
    computed = await sha256Hex(action.payload_canonical);
  } catch (e) {
    return { status: 'unverifiable', reason: e instanceof Error ? e.message : 'hash failed' };
  }
  if (computed !== action.payload_sha256) return { status: 'mismatch', computed, claimed: action.payload_sha256 };
  try {
    return { status: 'verified', hash: computed, payload: JSON.parse(action.payload_canonical) };
  } catch {
    return { status: 'unverifiable', reason: 'payload is not valid JSON' };
  }
}
