// @vitest-environment jsdom
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import Shell from '../src/lib/Shell.svelte';
import { frame, mountHud, settle, type Harness } from './helpers';
import { fullSnapshot } from './fixtures';

const SRC = join(__dirname, '../src');

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

describe('untrusted text is plain text only (x-atlas-untrusted)', () => {
  const files = walk(SRC).filter((f) => /\.(svelte|ts)$/.test(f) && !f.includes('/generated/'));

  it('has no {@html} anywhere in the source', () => {
    const hits = files.filter((f) => /\{@html\b/.test(readFileSync(f, 'utf8'))).map((f) => relative(SRC, f));
    expect(hits).toEqual([]);
  });

  it('has no raw HTML sinks (innerHTML, outerHTML, insertAdjacentHTML, document.write)', () => {
    const sink = /\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|createContextualFragment|document\.write/;
    const hits = files.filter((f) => sink.test(readFileSync(f, 'utf8'))).map((f) => relative(SRC, f));
    expect(hits).toEqual([]);
  });

  it('has no Markdown renderer and no URL-bearing attribute fed from a panel field', () => {
    const pkg = JSON.parse(readFileSync(join(SRC, '../package.json'), 'utf8')) as { dependencies?: object; devDependencies?: object };
    const names = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies });
    expect(names.filter((n) => /marked|markdown|remark|showdown|snarkdown|dompurify|sanitize/i.test(n))).toEqual([]);
    const svelteFiles = files.filter((f) => f.endsWith('.svelte'));
    // href/src/xlink:href/srcset may only carry static values or in-page anchors.
    const bad = svelteFiles.flatMap((f) =>
      Array.from(readFileSync(f, 'utf8').matchAll(/<[a-z][\w-]*[^>]*?\s(href|src|srcset|xlink:href|action|formaction)=\{[^}]*\}/g)).map((m) => `${relative(SRC, f)}: ${m[0]}`)
    );
    expect(bad).toEqual([]);
  });

  describe('rendered', () => {
    let h: Harness | undefined;
    afterEach(() => h?.cleanup());

    const EVIL = '<img src=x onerror="window.__pwned=1"> <script>window.__pwned=1</script> **bold** [link](https://evil.example.com) &lt;b&gt;';

    it('shows hostile text from email, Discord, calendar, transcript and alerts literally', async () => {
      const snap = fullSnapshot();
      snap.panels = snap.panels.map((p) => {
        if (p.panel_id === 'email') return { ...p, data: { unread: 1, auth: { status: 'ok' }, items: [{ id: 'm1', from_display: EVIL, from_domain: 'evil.example.com', subject: EVIL, received_at: Date.now(), unread: true }] } };
        if (p.panel_id === 'discord') return { ...p, data: { unread: 1, items: [{ id: 'd1', channel: EVIL, author_display: EVIL, excerpt: EVIL, at: Date.now() }] } };
        if (p.panel_id === 'calendar') return { ...p, data: { auth: { status: 'ok' }, events: [{ id: 'e1', title: EVIL, location: EVIL, start: Date.now() + 1000, end: Date.now() + 2000, all_day: false }] } };
        return p;
      });
      // A reply influenced by third-party content carries tainted provenance and stays plain (markdown.test.ts covers formatting).
      snap.transcript = [{ utterance_id: 'a1', role: 'assistant', text: EVIL, source: 'text', provenance: { tainted: true, sources: [] } }];
      h = mountHud(Shell, { snapshot: snap });
      h.settings.viewportWidth = 1920;
      await settle();
      h.store.apply(frame('alert', { alert_id: 'x', level: 'info', code: 'FOO', message: EVIL }));
      h.store.apply(frame('assistant.delta', { utterance_id: 'a2', text: EVIL }));
      await settle();

      const body = document.body;
      expect(body.querySelector('script')).toBeNull();
      expect(body.querySelector('img')).toBeNull();
      expect(body.querySelector('a[href*="evil"]')).toBeNull();
      expect(body.querySelector('b, i, em, u, a[href^=http]')).toBeNull();
      expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
      expect(body.textContent).toContain('<img src=x onerror="window.__pwned=1">');
      expect(body.textContent).toContain('**bold** [link](https://evil.example.com)');
      // The only anchor in the app is the in-page skip link.
      expect(Array.from(body.querySelectorAll('a')).map((a) => a.getAttribute('href'))).toEqual(['#main']);
      expect(body.querySelectorAll('[src], [srcset]')).toHaveLength(0);
    });
  });
});
