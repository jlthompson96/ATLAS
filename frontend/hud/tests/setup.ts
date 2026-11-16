import { webcrypto } from 'node:crypto';

// jsdom's window.crypto has no `subtle`; the HUD's approval check needs real WebCrypto, so use Node's.
if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true });
}
