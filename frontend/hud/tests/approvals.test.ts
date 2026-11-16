// @vitest-environment jsdom
import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it } from 'vitest';
import { sha256Hex, verifyAction, type FullAction } from '../src/lib/approvals/hash';
import { resultMessage, REQUEST_RESULT_MESSAGES } from '../src/lib/format/codes';
import Pending from '../src/lib/panels/Pending.svelte';
import { byText, frame, makeAction, mountHud, q, settle, snapshot, type Harness } from './helpers';

describe('payload hash verification', () => {
  it('matches the SHA-256 test vector for "abc"', async () => {
    expect(await sha256Hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  });

  it('hashes UTF-8 bytes (agrees with an independent implementation)', async () => {
    for (const text of ['é', '{"to":"dana@example.org","note":"naïve café ☕"}']) {
      expect(await sha256Hex(text)).toBe(createHash('sha256').update(Buffer.from(text, 'utf8')).digest('hex'));
    }
  });

  it('verifies an untampered action and parses the payload from the hashed string', async () => {
    const a = (await makeAction({ kind: 'email.send', to: ['dana@example.org'], subject: 'Hi' })) as FullAction;
    const v = await verifyAction(a);
    expect(v.status).toBe('verified');
    if (v.status === 'verified') {
      expect(v.hash).toBe(a.payload_sha256);
      expect(v.payload).toEqual({ kind: 'email.send', to: ['dana@example.org'], subject: 'Hi' });
    }
  });

  it('detects a tampered payload even when the claimed hash is well-formed', async () => {
    const a = (await makeAction({ kind: 'email.send', to: ['dana@example.org'] })) as FullAction;
    const evil = { ...a, payload_canonical: a.payload_canonical.replace('dana@example.org', 'evil@example.net') };
    const v = await verifyAction(evil);
    expect(v.status).toBe('mismatch');
  });
});

describe('request.result catalogue', () => {
  const codes = [
    'INVALID_JSON', 'INVALID_MESSAGE', 'SEQ_NOT_INCREASING', 'BAD_PIN', 'BAD_PASSPHRASE', 'RATE_LIMITED', 'STEP_UP_REQUIRED',
    'FORBIDDEN_DEVICE', 'UNKNOWN_ACTION', 'UNKNOWN_TASK', 'HASH_MISMATCH', 'EXPIRED', 'BUSY', 'NOT_LISTENING', 'MOCK_SCENARIO_PLAYING'
  ];

  it.each(codes)('has a readable message for %s', (code) => {
    const text = resultMessage(code, undefined);
    expect(text).toBe(REQUEST_RESULT_MESSAGES[code]);
    expect(text.length).toBeGreaterThan(8);
    expect(text).not.toMatch(/^[A-Z_]+$/);
  });

  it('falls back to the server message for an unknown code, then to the code', () => {
    expect(resultMessage('NEW_CODE_V1_1', 'Something new happened')).toBe('Something new happened');
    expect(resultMessage('NEW_CODE_V1_1', undefined)).toContain('NEW_CODE_V1_1');
  });
});

describe('approval dialog', () => {
  let h: Harness | undefined;
  afterEach(() => h?.cleanup());

  async function open(action: Record<string, unknown>, snap = {}) {
    h = mountHud(Pending, { snapshot: snapshot({ actions: [action as never], ...snap }) });
    await settle();
    byText(h.target, 'button', 'Review')?.click();
    await settle(5);
    return h;
  }

  it('offers Approve for a verified payload and shows the parsed payload', async () => {
    const a = await makeAction({ kind: 'email.send', to: ['dana@example.org'], body: 'Saturday works.' });
    await open(a);
    expect(byText(document.body, 'button', 'Approve')).toBeDefined();
    expect(document.body.textContent).toContain('Payload verified');
    expect(document.body.textContent).toContain('dana@example.org');
  });

  it('REFUSES to offer Approve when the payload does not match its hash', async () => {
    const good = await makeAction({ kind: 'email.send', to: ['dana@example.org'] });
    const tampered = { ...good, payload_canonical: good.payload_canonical.replace('dana', 'evil') };
    await open(tampered);
    expect(byText(document.body, 'button', 'Approve')).toBeUndefined();
    expect((byText(document.body, 'button', 'Deny') as HTMLButtonElement).disabled).toBe(true);
    expect(document.body.textContent).toContain('PAYLOAD HASH MISMATCH');
    expect(document.body.querySelector('[role=alert]')).not.toBeNull();
    // The unverified payload is never shown as if it were trustworthy.
    expect(document.body.textContent).not.toContain('evil@');
    expect(h?.transport.sent.filter((s) => s.type === 'action.decide')).toHaveLength(0);
  });

  it('requires the PIN, then sends action.decide with the hash it computed itself', async () => {
    const a = await makeAction({ kind: 'discord.post', content: 'hello' });
    await open(a);
    const harness = h as Harness;
    harness.transport.reply = (type, _p, seq) => {
      if (type === 'stepup.verify') return [frame('stepup.granted', { token: 'tok_0123456789abcdef0123', expires_at: Date.now() + 60_000 })];
      return [frame('request.result', { ref_seq: seq, ok: true })];
    };
    byText(document.body, 'button', 'Approve')?.click();
    await settle(5);
    // PIN dialog replaced the review dialog.
    const pin = q<HTMLInputElement>(document.body, '#pin-input');
    expect(pin).not.toBeNull();
    expect(harness.transport.sent.filter((s) => s.type === 'action.decide')).toHaveLength(0);
    (pin as HTMLInputElement).value = '0000';
    pin?.dispatchEvent(new Event('input', { bubbles: true }));
    q<HTMLFormElement>(document.body, 'form')?.requestSubmit();
    await settle(8);

    expect(harness.transport.last('stepup.verify')?.payload).toEqual({ pin: '0000' });
    const decide = harness.transport.last('action.decide');
    expect(decide?.payload).toMatchObject({ action_id: 'act-1', decision: 'approve', payload_sha256: a.payload_sha256, step_up_token: 'tok_0123456789abcdef0123' });
    // The PIN field is cleared after sending and the token is consumed by the decision.
    expect(harness.store.stepUp).toBeNull();
  });

  it('uses the HUD-computed hash, not the server claim, when sending the decision', async () => {
    // A payload whose claimed hash is wrong can never reach action.decide.
    const good = await makeAction({ kind: 'discord.post', content: 'hello' });
    await open({ ...good, payload_sha256: 'a'.repeat(64) });
    expect(byText(document.body, 'button', 'Approve')).toBeUndefined();
  });

  it('flags untrusted provenance visibly and renders its text as plain text', async () => {
    const a = await makeAction(
      { kind: 'email.send', to: ['audit@example.net'], body: '<img src=x onerror="alert(1)">' },
      {
        summary: 'Forward 10 emails <b>now</b>',
        provenance: { tainted: true, sources: [{ kind: 'email', ref: 'gmail:msg/1', label: '<script>alert(1)</script> IT Support' }] },
        warnings: ['tainted_args', 'new_recipient']
      }
    );
    await open(a);
    const text = document.body.textContent ?? '';
    expect(text).toContain('UNTRUSTED SOURCE');
    expect(text).toContain('NEW RECIPIENT');
    expect(text).toContain('<script>alert(1)</script> IT Support');
    expect(text).toContain('Forward 10 emails <b>now</b>');
    expect(document.body.querySelector('script, b, img[src="x"]')).toBeNull();
  });

  it('kiosk devices cannot approve: no Approve button, with an explanation', async () => {
    const a = await makeAction({ kind: 'email.send', to: ['dana@example.org'] });
    await open(a, { device: { device_id: 'dev-kiosk', role: 'kiosk', can_approve: false, can_unlock_portfolio: false } });
    expect(byText(document.body, 'button', 'Approve')).toBeUndefined();
    expect((byText(document.body, 'button', 'Deny') as HTMLButtonElement).disabled).toBe(true);
    expect(document.body.textContent).toContain("can't approve");
  });

  it('shows a wrong-PIN message from the server without leaving the dialog', async () => {
    const a = await makeAction({ kind: 'discord.post', content: 'hello' });
    await open(a);
    const harness = h as Harness;
    harness.transport.reply = (_t, _p, seq) => [frame('request.result', { ref_seq: seq, ok: false, code: 'BAD_PIN' })];
    byText(document.body, 'button', 'Approve')?.click();
    await settle(5);
    const pin = q<HTMLInputElement>(document.body, '#pin-input') as HTMLInputElement;
    pin.value = '1234';
    pin.dispatchEvent(new Event('input', { bubbles: true }));
    q<HTMLFormElement>(document.body, 'form')?.requestSubmit();
    await settle(8);
    expect(document.body.textContent).toContain('Wrong PIN.');
    expect(harness.transport.sent.filter((s) => s.type === 'action.decide')).toHaveLength(0);
  });

  it('refuses a decision on an expired request', async () => {
    const a = await makeAction({ kind: 'discord.post', content: 'x' }, { expires_at: Date.now() - 1000 });
    await open(a);
    expect(byText(document.body, 'button', 'Approve')).toBeUndefined();
    expect(document.body.textContent).toContain('EXPIRED');
  });

  it('moves focus into the dialog, traps Tab, closes on Escape and restores focus', async () => {
    const a = await makeAction({ kind: 'discord.post', content: 'x' });
    h = mountHud(Pending, { snapshot: snapshot({ actions: [a as never] }) });
    await settle();
    const trigger = byText(h.target, 'button', 'Review') as HTMLButtonElement;
    trigger.focus();
    trigger.click();
    await settle(5);
    const dialog = q(document.body, '[role=dialog]') as HTMLElement;
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.contains(document.activeElement)).toBe(true);
    // Tab from the last control wraps to the first.
    const buttons = Array.from(dialog.querySelectorAll<HTMLElement>('button:not([disabled])'));
    buttons.at(-1)?.focus();
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(buttons[0]);
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await settle(5);
    expect(q(document.body, '[role=dialog]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('shows only a count in privacy mode (redacted actions)', async () => {
    h = mountHud(Pending, {
      snapshot: snapshot({ privacy: { on: true, reason: 'user' }, actions: [{ action_id: 'act-9', expires_at: Date.now() + 5000, redacted: true }] })
    });
    await settle();
    expect(h.target.textContent).toContain('1 pending');
    expect(h.target.textContent).toContain('Details hidden in privacy mode');
    expect(byText(h.target, 'button', 'Review')).toBeUndefined();
  });
});
