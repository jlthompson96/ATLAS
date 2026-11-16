import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

const ROOT = join(__dirname, '..');
let out = '';

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

beforeAll(() => {
  out = mkdtempSync(join(tmpdir(), 'atlas-hud-build-'));
  execFileSync('npx', ['vite', 'build', '--outDir', out, '--emptyOutDir', '--logLevel', 'error'], { cwd: ROOT, stdio: 'pipe' });
}, 120_000);

afterAll(() => {
  if (out) rmSync(out, { recursive: true, force: true });
});

describe('production build (UI-SEC-3, UI-SEC-4)', () => {
  it('writes index.html with a strict CSP meta tag', () => {
    const html = readFileSync(join(out, 'index.html'), 'utf8');
    const m = /<meta http-equiv="Content-Security-Policy" content="([^"]+)"/.exec(html);
    expect(m, 'CSP meta tag').not.toBeNull();
    const csp = (m?.[1] ?? '').replaceAll('&#39;', "'");
    const directives = Object.fromEntries(csp.split(';').map((d) => d.trim().split(/\s+/)).map(([name, ...v]) => [name, v]));
    expect(directives['default-src']).toEqual(["'self'"]);
    expect(directives['connect-src']).toEqual(["'self'"]); // covers the same-origin WebSocket
    expect(directives['script-src']).toEqual(["'self'"]);
    expect(directives['object-src']).toEqual(["'none'"]);
    expect(csp).not.toMatch(/unsafe-inline|unsafe-eval|\*|https?:|data:|blob:/);
  });

  it('has no inline scripts or styles, which the CSP would block', () => {
    const html = readFileSync(join(out, 'index.html'), 'utf8');
    expect(html).not.toMatch(/<script(?![^>]*\ssrc=)[^>]*>/);
    expect(html).not.toMatch(/<style[\s>]/);
    expect(html).not.toMatch(/\sstyle=/);
    expect(html).not.toMatch(/\son[a-z]+=/);
  });

  it('references only local assets, all of which exist (no CDN)', () => {
    const html = readFileSync(join(out, 'index.html'), 'utf8');
    const refs = Array.from(html.matchAll(/(?:src|href)="([^"]+)"/g)).map((m) => m[1] as string);
    expect(refs.length).toBeGreaterThan(0);
    for (const ref of refs) {
      expect(ref, ref).toMatch(/^\//);
      expect(ref, ref).not.toMatch(/^\/\//);
      expect(existsSync(join(out, ref)), ref).toBe(true);
    }
  });

  it('self-hosts its fonts and loads them from files, not data: URIs', () => {
    const files = walk(out).map((f) => relative(out, f));
    expect(files.filter((f) => f.endsWith('.woff2')).length).toBeGreaterThanOrEqual(2);
    const css = walk(out).filter((f) => f.endsWith('.css')).map((f) => readFileSync(f, 'utf8')).join('\n');
    expect(css).toContain('@font-face');
    expect(css).not.toMatch(/url\(\s*["']?(data:|https?:|\/\/)/);
    expect(css).not.toMatch(/@import/);
  });

  it('contains no external URLs other than inert namespace and error-message strings', () => {
    const allowed = [
      /^http:\/\/www\.w3\.org\/(1999\/xhtml|2000\/svg)$/, // XML namespaces, identifiers not requests
      /^https:\/\/svelte\.dev\/e\/[a-z_]+$/ // text inside Svelte's runtime error messages, never fetched
    ];
    const offenders: string[] = [];
    for (const f of walk(out)) {
      if (/\.(woff2?|png|ico|jpg|webp)$/.test(f)) continue;
      const text = readFileSync(f, 'utf8');
      for (const m of text.matchAll(/(?:https?:)?\/\/[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s"'`)\\<>]*/g)) {
        const url = m[0];
        if (!/^https?:/.test(url) && !url.startsWith('//')) continue;
        if (!allowed.some((re) => re.test(url))) offenders.push(`${relative(out, f)}: ${url}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('has no eval, Function constructor, or remote import() in the bundle', () => {
    const js = walk(out).filter((f) => f.endsWith('.js')).map((f) => readFileSync(f, 'utf8')).join('\n');
    expect(js).not.toMatch(/\beval\s*\(/);
    expect(js).not.toMatch(/new Function\s*\(/);
    expect(js).not.toMatch(/import\(\s*["']https?:/);
    expect(js).not.toMatch(/navigator\.sendBeacon|XMLHttpRequest/);
    expect(js).not.toMatch(/\bfetch\s*\(\s*["']https?:/);
  });

  it('opens the WebSocket on the page origin only', () => {
    const js = walk(out).filter((f) => f.endsWith('.js')).map((f) => readFileSync(f, 'utf8')).join('\n');
    expect(js).toContain('/ws/events');
    expect(js).not.toMatch(/wss?:\/\/[a-z0-9]/i);
  });
});
