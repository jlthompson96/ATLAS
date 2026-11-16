// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { decimalSign, formatDecimal, formatPct, safeText } from '../src/lib/format/text';
import Shell from '../src/lib/Shell.svelte';
import { resolveProfile } from '../src/lib/state/settings.svelte';
import { sanitizeSlots } from '../src/lib/panels/registry';
import { ATTRIBUTION, fullSnapshot, TASKS } from './fixtures';
import { byText, frame, mountHud, q, settle, type Harness } from './helpers';

describe('money is a decimal string and never goes through floating point (§2.16)', () => {
  it('groups digits without parsing', () => {
    expect(formatDecimal('1234567.89')).toBe('1,234,567.89');
    expect(formatDecimal('-1234.5')).toBe('-1,234.50');
    expect(formatDecimal('0')).toBe('0.00');
    expect(formatDecimal('612.3800')).toBe('612.3800'); // keeps the server's digits
  });

  it('keeps precision a float would lose', () => {
    // 2^53 + 1 is not representable as a double; a string path preserves it.
    expect(formatDecimal('9007199254740993.01')).toBe('9,007,199,254,740,993.01');
    expect(Number('9007199254740993.01').toFixed(2)).not.toBe('9007199254740993.01');
  });

  it('refuses anything that is not a decimal string', () => {
    expect(formatDecimal(12.5)).toBe('—');
    expect(formatDecimal('1e9')).toBe('—');
    expect(formatDecimal('12,5')).toBe('—');
    expect(formatDecimal(undefined)).toBe('—');
  });

  it('reads the sign from the string', () => {
    expect([decimalSign('-0.01'), decimalSign('0.00'), decimalSign('12.3'), decimalSign('-0')]).toEqual([-1, 0, 1, 0]);
  });

  it('formats percentages (JSON numbers) with a sign and a real minus', () => {
    expect(formatPct(1.234)).toBe('+1.23%');
    expect(formatPct(-0.4)).toBe('−0.40%');
    expect(formatPct(0)).toBe('0.00%');
  });

  it('strips spoofing characters from untrusted text', () => {
    expect(safeText('a‮b​c\u0000d\te')).toBe('abcd\te');
    expect(safeText('x'.repeat(10), 5)).toBe('xxxx…');
  });
});

describe('profiles and layouts', () => {
  it('picks the profile from the device role and width', () => {
    expect(resolveProfile('auto', 'kiosk', 1920)).toBe('kiosk');
    expect(resolveProfile('auto', 'remote', 1920)).toBe('remote');
    expect(resolveProfile('auto', 'remote', 390)).toBe('compact');
    expect(resolveProfile('auto', 'kiosk', 1024)).toBe('kiosk');
    expect(resolveProfile('compact', 'kiosk', 1920)).toBe('compact');
  });

  it('drops panel ids this build does not know (a newer minor revision)', () => {
    expect(sanitizeSlots({ top: 'clock', left: ['system', 'hologram' as never], right: [], bottom: 'future' as never })).toEqual({ top: 'clock', left: ['system'], right: [] });
  });
});

describe('Shell with every panel', () => {
  let h: Harness | undefined;
  afterEach(() => h?.cleanup());

  async function shell(snap = fullSnapshot(), width = 1920) {
    h = mountHud(Shell, { snapshot: snap });
    h.settings.viewportWidth = width;
    await settle();
    return h;
  }
  const panel = (id: string) => q(document.body, `[data-panel=${id}]`) as HTMLElement;

  it('renders each default remote-layout panel', async () => {
    await shell();
    for (const id of ['system', 'weather', 'email', 'discord', 'calendar', 'pending', 'research', 'markets', 'portfolio', 'egress_audit', 'transcript']) {
      expect(panel(id), id).not.toBeNull();
    }
  });

  it('shows the required Open-Meteo attribution on the weather panel', async () => {
    await shell();
    expect(panel('weather').textContent).toContain(ATTRIBUTION);
  });

  it('renders money exactly as the string the server sent', async () => {
    await shell();
    const text = panel('portfolio').textContent ?? '';
    expect(text).toContain('1,234,567.89');
    expect(text).toContain('-1,234.50');
    expect(text).toContain('45,000.10');
    expect(text).toContain('fund NAV as of 2026-11-14');
  });

  it('shows a locked portfolio with the unlock form, and sends only the passphrase', async () => {
    const snap = fullSnapshot();
    snap.panels = snap.panels.map((p) => (p.panel_id === 'portfolio' ? { ...p, data: { locked: true } } : p));
    await shell(snap);
    expect(panel('portfolio').textContent).toContain('LOCKED');
    expect(panel('portfolio').textContent).not.toContain('1,234,567');
    const input = q<HTMLInputElement>(panel('portfolio'), 'input[type=password]') as HTMLInputElement;
    input.value = 'atlas-mock';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    q<HTMLFormElement>(panel('portfolio'), 'form')?.requestSubmit();
    await settle(5);
    expect(h?.transport.last('portfolio.unlock')?.payload).toEqual({ passphrase: 'atlas-mock' });
    expect(input.value).toBe(''); // not kept after sending
  });

  it('does not offer to unlock the portfolio on a kiosk', async () => {
    const snap = fullSnapshot({ device: { device_id: 'k', role: 'kiosk', can_approve: false, can_unlock_portfolio: false } });
    snap.panels = snap.panels.map((p) => (p.panel_id === 'portfolio' ? { ...p, data: { locked: true } } : p));
    await shell(snap, 1920);
    h?.settings.set({ profile: 'remote' }); // the kiosk layout has no portfolio panel by default
    await settle();
    expect(q(panel('portfolio'), 'input[type=password]')).toBeNull();
    expect(panel('portfolio').textContent).toContain("can't unlock");
  });

  it('labels delayed quotes and ETF proxies', async () => {
    await shell();
    const text = panel('markets').textContent ?? '';
    expect(text).toContain('DELAYED');
    expect(text).toContain('S&P 500 (ETF proxy)');
    expect(text).toContain('612.3800');
    expect(text).toContain('+0.42%');
    expect(text).toContain('−2.31%');
  });

  it('shows a clear failure when the audit chain is broken (PT-13)', async () => {
    const snap = fullSnapshot();
    snap.panels = snap.panels.map((p) =>
      p.panel_id === 'egress_audit' ? { ...p, data: { egress: { proxy: 'down', allowed_24h: 0, blocked_24h: 0 }, audit: { chain: 'broken', records_24h: 5 } } } : p
    );
    await shell(snap);
    const text = panel('egress_audit').textContent ?? '';
    expect(text).toContain('CHAIN ✗ BROKEN');
    expect(text).toContain('EGRESS PROXY DOWN');
    expect(panel('egress_audit').querySelector('[role=alert]')).not.toBeNull();
  });

  it('marks a failing source as stale', async () => {
    const snap = fullSnapshot();
    snap.panels = snap.panels.map((p) => (p.panel_id === 'markets' ? { ...p, stale_since: Date.now() - 600_000 } : p));
    await shell(snap);
    expect(panel('markets').textContent).toContain('STALE');
  });

  it('shows weekly Google re-authorization state', async () => {
    await shell();
    expect(panel('email').textContent).toContain('GOOGLE SIGN-IN EXPIRING');
  });

  it('lists research tasks and kills one on request', async () => {
    await shell();
    expect(panel('research').textContent).toContain('RISC-V vector extension status');
    expect(panel('research').textContent).toContain('LOW SOURCE COVERAGE');
    const kill = byText(panel('research'), 'button', /^Kill$/) as HTMLButtonElement;
    expect(kill.getAttribute('aria-label')).toContain('RISC-V vector extension status');
    kill.click();
    await settle();
    expect(h?.transport.last('task.kill')?.payload).toEqual({ task_id: TASKS[0]?.task_id });
    // A finished task has no kill button.
    expect(panel('research').querySelectorAll('button[aria-label^="Kill task"]')).toHaveLength(1);
  });

  it('shows alerts with a text label and shape, and lets them be dismissed', async () => {
    await shell();
    const alerts = q(document.body, '[aria-label=Alerts]') as HTMLElement;
    expect(alerts.textContent).toContain('WARNING');
    expect(alerts.textContent).toContain('Host degraded');
    byText(alerts, 'button', '✕')?.click();
    await settle();
    expect(q(document.body, '[aria-label=Alerts]')).toBeNull();
  });

  it('announces new alerts: polite for warnings, assertive for critical', async () => {
    await shell();
    h?.store.apply(frame('alert', { alert_id: 'x1', level: 'warn', code: 'PI_THERMAL', message: 'Pi at 78 C' }));
    h?.store.apply(frame('alert', { alert_id: 'x2', level: 'critical', code: 'AUDIT_CHAIN_BROKEN', message: 'Chain verification failed' }));
    await settle();
    expect(q(document.body, '[data-testid=live-polite]')?.textContent).toContain('Pi at 78 C');
    expect(q(document.body, '[data-testid=live-assertive]')?.textContent).toContain('AUDIT_CHAIN_BROKEN');
  });

  it('shows host problems as text: DEGRADED, BUSY, SIGNED OUT, VOICE UNAVAILABLE', async () => {
    const snap = fullSnapshot();
    snap.host = { ...snap.host, llm: { power: 'up', link: 'up', service: 'signed_out' }, voice: { status: 'unavailable', wake_word: 'off' } };
    await shell(snap);
    const text = document.body.textContent ?? '';
    expect(text).toContain('CORE OFFLINE · SIGNED OUT');
    expect(text).toContain('VOICE UNAVAILABLE · TYPE BELOW');
  });

  it('shows a disconnected banner when the socket drops', async () => {
    await shell();
    h?.store.setConnection('reconnecting');
    await settle();
    expect(document.body.textContent).toContain('RECONNECTING');
  });

  it('adapts to the three breakpoints (§4.2)', async () => {
    for (const [width, bp] of [[1920, 'wide'], [1000, 'medium'], [390, 'compact']] as const) {
      await shell(fullSnapshot(), width);
      expect(q(document.body, '.shell')?.getAttribute('data-bp')).toBe(bp);
      h?.cleanup();
    }
  });

  it('compact profile uses collapsible cards that toggle with the keyboard', async () => {
    await shell(fullSnapshot(), 390);
    const fold = byText(document.body, 'button[aria-expanded]', 'Weather') ?? byText(document.body, 'button[aria-expanded]', 'System');
    expect(fold).toBeDefined();
    const before = fold?.getAttribute('aria-expanded');
    fold?.click();
    await settle();
    expect(fold?.getAttribute('aria-expanded')).not.toBe(before);
  });
});

describe('layout editing round-trips through layout.save', () => {
  let h: Harness | undefined;
  afterEach(() => h?.cleanup());

  it('sends the edited slots for the active profile and updates the local layout on success', async () => {
    h = mountHud(Shell, { snapshot: fullSnapshot({ layouts: { remote: { top: 'clock', left: ['system', 'weather'], right: ['pending'], bottom: 'transcript' } } }) });
    h.settings.viewportWidth = 1920;
    await settle();
    byText(document.body, 'button', 'Settings')?.click();
    await settle(4);
    byText(document.body, 'button', 'Edit layout')?.click();
    await settle(4);
    // Move Weather to the right column, remove System, add Markets on the left.
    (q(document.body, 'button[aria-label="Move Weather to the right column"]') as HTMLButtonElement).click();
    await settle();
    (q(document.body, 'button[aria-label="Remove System"]') as HTMLButtonElement).click();
    await settle();
    const add = q<HTMLSelectElement>(document.body, '#add-left') as HTMLSelectElement;
    add.value = 'markets';
    add.dispatchEvent(new Event('change', { bubbles: true }));
    await settle();
    byText(document.body, 'button', 'Save layout')?.click();
    await settle(5);
    const sent = h.transport.last('layout.save')?.payload;
    expect(sent).toEqual({ profile: 'remote', slots: { top: 'clock', bottom: 'transcript', left: ['markets'], right: ['pending', 'weather'] } });
    expect(h.store.layouts.remote?.left).toEqual(['markets']);
    expect(q(document.body, '#layout-title')).toBeNull(); // back on the settings dialog
  });
});
