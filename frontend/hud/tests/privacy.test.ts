// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import Shell from '../src/lib/Shell.svelte';
import { fullSnapshot, REDACTED_PANELS, TASKS } from './fixtures';
import { byText, frame, mountHud, q, settle, type Harness } from './helpers';

describe('privacy mode renders exactly what the server sends (the server is the protection)', () => {
  let h: Harness | undefined;
  afterEach(() => h?.cleanup());

  const redacted = () =>
    fullSnapshot({
      privacy: { on: true, reason: 'inactivity' },
      panels: REDACTED_PANELS,
      tasks: TASKS.map((t) => {
        const copy = { ...t, redacted: true as const };
        delete copy.title;
        return copy;
      }),
      actions: [{ action_id: 'act-1', expires_at: Date.now() + 60_000, redacted: true }],
      transcript: []
    });

  async function open(snap = redacted(), width = 1920) {
    h = mountHud(Shell, { snapshot: snap });
    h.settings.viewportWidth = width;
    await settle();
    return h;
  }
  const panel = (id: string) => q(document.body, `[data-panel=${id}]`) as HTMLElement;

  it('shows counts only for email and Discord', async () => {
    await open();
    expect(panel('email').textContent).toContain('4 unread');
    expect(panel('email').textContent).not.toContain('Saturday');
    expect(panel('discord').textContent).toContain('2 unread');
  });

  it("shows only the next event's time for the calendar", async () => {
    await open();
    expect(panel('calendar').textContent).toContain('Next event');
    expect(panel('calendar').textContent).not.toContain('Dentist');
  });

  it('shows day change in percent only, no amounts, for the portfolio', async () => {
    await open();
    const text = panel('portfolio').textContent ?? '';
    expect(text).toContain('−0.10%');
    expect(text).toContain('Amounts hidden');
    expect(text).not.toMatch(/\d,\d{3}/);
  });

  it('keeps public panels (weather, egress/audit) and markets visible', async () => {
    await open();
    expect(panel('weather').textContent).toContain('Light rain');
    expect(panel('egress_audit').textContent).toContain('CHAIN');
    expect(panel('markets').textContent).toContain('SPY');
  });

  it('hides research titles and shows a count of pending approvals', async () => {
    await open();
    expect(panel('research').textContent).toContain('Research task (title hidden)');
    expect(panel('research').textContent).not.toContain('RISC-V');
    expect(panel('pending').textContent).toContain('1 pending');
  });

  it('collapses the transcript but keeps text input available', async () => {
    await open();
    expect(panel('transcript').textContent).toContain('hidden in privacy mode');
    expect(q(document.body, 'input[type=text]')).not.toBeNull();
  });

  it('does not blur data the server already redacted, and tags the orb PRIVACY', async () => {
    await open();
    for (const id of ['email', 'discord', 'calendar', 'portfolio', 'research', 'pending', 'weather', 'egress_audit', 'markets']) {
      expect(panel(id).classList.contains('blur'), id).toBe(false);
    }
    expect(q(document.body, '.privacy-tag')?.textContent).toContain('PRIVACY');
  });

  it('adds a second-layer blur if sensitive data still arrives while privacy mode is on', async () => {
    // A server bug or race: privacy is on but full email, portfolio and research data came through (the transcript is always collapsed client-side).
    const leaky = fullSnapshot({ privacy: { on: true, reason: 'user' } });
    await open(leaky);
    for (const id of ['email', 'discord', 'calendar', 'portfolio', 'research']) {
      expect(panel(id).classList.contains('blur'), id).toBe(true);
    }
    // Public data and markets are meant to stay visible in privacy mode (§4.6).
    expect(panel('weather').classList.contains('blur')).toBe(false);
    expect(panel('egress_audit').classList.contains('blur')).toBe(false);
    expect(panel('markets').classList.contains('blur')).toBe(false);
  });

  it('never blurs anything when privacy mode is off', async () => {
    await open(fullSnapshot());
    expect(document.querySelectorAll('.blur')).toHaveLength(0);
  });

  it('applies the next snapshot (full data) when privacy mode is turned off', async () => {
    await open();
    h?.store.apply(frame('privacy.mode', { on: false, reason: 'user' }));
    h?.store.applySnapshot(fullSnapshot());
    await settle();
    expect(panel('email').textContent).toContain('Saturday plans?');
    expect(panel('email').classList.contains('blur')).toBe(false);
  });

  it('turning privacy on needs no PIN: sends privacy.set {on:true}', async () => {
    await open(fullSnapshot());
    byText(document.body, 'button', 'Privacy off')?.click();
    await settle();
    expect(h?.transport.last('privacy.set')?.payload).toEqual({ on: true });
  });

  it('leaving privacy mode on a remote device asks for the PIN first', async () => {
    await open();
    byText(document.body, 'button', 'Privacy on')?.click();
    await settle(4);
    expect(q(document.body, '#pin-input')).not.toBeNull();
    expect(h?.transport.sent.filter((s) => s.type === 'privacy.set')).toHaveLength(0);
  });

  it('leaving privacy mode on the kiosk is a tap: no PIN', async () => {
    const snap = redacted();
    snap.device = { device_id: 'k', role: 'kiosk', can_approve: false, can_unlock_portfolio: false };
    await open(snap);
    byText(document.body, 'button', 'Privacy on')?.click();
    await settle(4);
    expect(q(document.body, '#pin-input')).toBeNull();
    expect(h?.transport.last('privacy.set')?.payload).toEqual({ on: false });
  });

  it('sends the step-up token when leaving privacy mode after the PIN was verified', async () => {
    await open();
    (h as Harness).transport.reply = (type, _p, seq) =>
      type === 'stepup.verify'
        ? [frame('stepup.granted', { token: 'tok_0123456789abcdef0123', expires_at: Date.now() + 60_000 })]
        : [frame('request.result', { ref_seq: seq, ok: true })];
    byText(document.body, 'button', 'Privacy on')?.click();
    await settle(4);
    const pin = q<HTMLInputElement>(document.body, '#pin-input') as HTMLInputElement;
    pin.value = '0000';
    pin.dispatchEvent(new Event('input', { bubbles: true }));
    q<HTMLFormElement>(document.body, 'form')?.requestSubmit();
    await settle(8);
    expect(h?.transport.last('privacy.set')?.payload).toEqual({ on: false, step_up_token: 'tok_0123456789abcdef0123' });
  });
});
