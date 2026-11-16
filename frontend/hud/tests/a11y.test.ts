// @vitest-environment jsdom
import axe from 'axe-core';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import Shell from '../src/lib/Shell.svelte';
import { fullSnapshot, REDACTED_PANELS } from './fixtures';
import { byText, makeAction, mountHud, q, settle, type Harness } from './helpers';

// axe probes <canvas> for icon ligatures; jsdom has no canvas and would print a "not implemented" notice each time.
beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = (() => null) as never;
});

/**
 * A11Y-1: axe scan, zero serious or critical issues. jsdom has no layout engine, so colour contrast
 * can't be computed here: it is covered by the token-contrast test below and by running axe in real
 * Chrome (scripts/axe-browser.mjs).
 */
async function scan(root: HTMLElement = document.body) {
  const result = await axe.run(root, { rules: { 'color-contrast': { enabled: false } }, resultTypes: ['violations'] });
  return result.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => n.html.slice(0, 160)) }));
}

describe('A11Y-1: axe finds no serious or critical issues', () => {
  let h: Harness | undefined;
  afterEach(() => h?.cleanup());

  const widths: [string, number][] = [['wide', 1920], ['medium', 1000], ['compact', 390]];

  it.each(widths)('full data, %s layout', async (_name, width) => {
    h = mountHud(Shell, { snapshot: fullSnapshot() });
    h.settings.viewportWidth = width;
    await settle();
    expect(await scan()).toEqual([]);
  });

  it('privacy mode (redacted shapes, blurred panels)', async () => {
    h = mountHud(Shell, {
      snapshot: fullSnapshot({ privacy: { on: true, reason: 'user' }, panels: REDACTED_PANELS, transcript: [], tasks: [], actions: [{ action_id: 'a', expires_at: Date.now() + 5000, redacted: true }] })
    });
    await settle();
    expect(await scan()).toEqual([]);
  });

  it('every orb state', async () => {
    h = mountHud(Shell, { snapshot: fullSnapshot() });
    await settle();
    const states = [
      { state: 'listening' },
      { state: 'thinking' },
      { state: 'speaking' },
      { state: 'waking_host', wake: { stage: 'api', elapsed_ms: 9000, deadline_ms: 60000 } },
      { state: 'error', code: 'WAKE_FAILED', detail: 'core did not wake' },
      { state: 'llm_unreachable', code: 'CORE_UNREACHABLE' }
    ];
    for (const s of states) {
      h.store.orb = s as never;
      await settle();
      expect(await scan(), s.state).toEqual([]);
    }
  });

  it('with the approval dialog open', async () => {
    const a = await makeAction({ kind: 'email.send', to: ['dana@example.org'], body: 'hello' }, { provenance: { tainted: true, sources: [{ kind: 'email', ref: 'gmail:msg/1', label: 'IT Support' }] }, warnings: ['tainted_args'] });
    h = mountHud(Shell, { snapshot: fullSnapshot({ actions: [a as never] }) });
    await settle();
    byText(document.body, 'button', 'Review')?.click();
    await settle(6);
    expect(q(document.body, '[role=dialog]')).not.toBeNull();
    expect(await scan()).toEqual([]);
  });

  it('with the settings, layout editor and PIN dialogs open', async () => {
    h = mountHud(Shell, { snapshot: fullSnapshot() });
    await settle();
    byText(document.body, 'button', 'Settings')?.click();
    await settle(4);
    expect(await scan()).toEqual([]);
    byText(document.body, 'button', 'Edit layout')?.click();
    await settle(4);
    expect(q(document.body, '#layout-title')).not.toBeNull();
    expect(await scan()).toEqual([]);
  });

  it('PIN dialog', async () => {
    h = mountHud(Shell, { snapshot: fullSnapshot({ privacy: { on: true, reason: 'user' } }) });
    await settle();
    byText(document.body, 'button', 'Privacy on')?.click();
    await settle(4);
    expect(q(document.body, '#pin-input')).not.toBeNull();
    expect(await scan()).toEqual([]);
  });
});

describe('A11Y: structure, keyboard and motion', () => {
  let h: Harness | undefined;
  afterEach(() => h?.cleanup());

  it('has landmarks, a skip link, a single h1 and ARIA live regions', async () => {
    h = mountHud(Shell, { snapshot: fullSnapshot() });
    await settle();
    expect(q(document.body, 'main#main')).not.toBeNull();
    expect(q(document.body, 'a.skip[href="#main"]')).not.toBeNull();
    expect(document.querySelectorAll('h1')).toHaveLength(1);
    expect(q(document.body, '[role=status][aria-live=polite]')).not.toBeNull();
    expect(q(document.body, '[role=alert][aria-live=assertive]')).not.toBeNull();
  });

  it('A11Y-3: every control is a real, focusable button/input/select (no click-only divs)', async () => {
    h = mountHud(Shell, { snapshot: fullSnapshot() });
    await settle();
    const clickable = Array.from(document.body.querySelectorAll<HTMLElement>('[onclick], [role=button]'));
    expect(clickable).toEqual([]);
    for (const b of Array.from(document.body.querySelectorAll<HTMLButtonElement>('button:not([disabled])'))) {
      expect(b.tabIndex, b.outerHTML.slice(0, 80)).toBeGreaterThanOrEqual(0);
      const name = (b.getAttribute('aria-label') ?? b.textContent ?? '').trim();
      expect(name.length, b.outerHTML.slice(0, 80)).toBeGreaterThan(0);
    }
  });

  it('A11Y-3: the whole approval flow works from the keyboard (focus, Enter via click, Escape)', async () => {
    const a = await makeAction({ kind: 'discord.post', content: 'hi' });
    h = mountHud(Shell, { snapshot: fullSnapshot({ actions: [a as never] }) });
    await settle();
    const review = byText(document.body, 'button', 'Review') as HTMLButtonElement;
    review.focus();
    expect(document.activeElement).toBe(review);
    review.click(); // Enter/Space on a <button> dispatches click
    await settle(6);
    const dialog = q(document.body, '[role=dialog]') as HTMLElement;
    expect(dialog.contains(document.activeElement)).toBe(true);
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await settle(4);
    expect(q(document.body, '[role=dialog]')).toBeNull();
    expect(document.activeElement).toBe(review);
  });

  it('A11Y-2: reduced motion removes the animated canvas and applies the in-app override to the root', async () => {
    h = mountHud(Shell, { snapshot: fullSnapshot({ state: { state: 'thinking' } }) });
    h.settings.set({ quality: 'full', motion: 'reduce' });
    await settle();
    expect(q(document.body, 'canvas')).toBeNull();
    expect((q(document.body, '.orb') as HTMLElement).dataset.tier).toBe('static');
    expect(document.documentElement.dataset.motion).toBe('reduce');
    h.settings.set({ motion: 'system', quality: 'static' });
    await settle();
    expect(document.documentElement.dataset.motion).toBeUndefined();
  });

  it('A11Y-2: the stylesheet honours prefers-reduced-motion', async () => {
    const { readFileSync } = await import('node:fs');
    const { join } = await import('node:path');
    const css = readFileSync(join(__dirname, '../src/styles/app.css'), 'utf8');
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*animation-duration: 0\.001ms/);
    expect(css).toMatch(/:root\[data-motion='reduce'\]/);
  });
});

describe('A11Y: colour contrast of the design tokens (WCAG 2.2 AA, §4.1)', () => {
  const lum = (hex: string): number => {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * (c[0] as number) + 0.7152 * (c[1] as number) + 0.0722 * (c[2] as number);
  };
  const ratio = (a: string, b: string): number => {
    const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x) as [number, number];
    return (hi + 0.05) / (lo + 0.05);
  };
  const BG = '#05080d';
  // Panels are translucent over --bg; the lightest plausible panel surface is --panel-solid.
  const PANEL = '#0b1722';

  it.each([
    ['--text', '#d8f6ff'],
    ['--muted', '#7fa3b3'],
    ['--accent', '#3ff0ff'],
    ['--warn', '#ffb547'],
    ['--danger', '#ff4d5e'],
    ['--ok', '#4dffa6']
  ])('%s has >= 4.5:1 on the background and on panels', (_name, colour) => {
    expect(ratio(colour, BG)).toBeGreaterThanOrEqual(4.5);
    expect(ratio(colour, PANEL)).toBeGreaterThanOrEqual(4.5);
  });

  it('matches the tokens declared in app.css', async () => {
    const { readFileSync } = await import('node:fs');
    const { join } = await import('node:path');
    const css = readFileSync(join(__dirname, '../src/styles/app.css'), 'utf8');
    for (const hex of ['#05080d', '#d8f6ff', '#7fa3b3', '#3ff0ff', '#ffb547', '#ff4d5e', '#4dffa6', '#0b1722']) expect(css).toContain(hex);
  });
});
