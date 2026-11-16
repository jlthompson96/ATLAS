#!/usr/bin/env node
// Real-browser checks that jsdom can't do: CSP violations (UI-SEC-4), network requests (UI-SEC-3),
// WebGL2 availability, console errors, and an axe-core scan WITH colour contrast (A11Y-1).
//
// Needs a local Chrome/Chromium (CHROME_BIN, or the macOS default path) and a running HUD:
//
//   npm run build && npx vite preview --port 4173 &                      # serves dist/, proxies /ws/events
//   (cd ../../backend && .venv/bin/python -m atlas.ui_server.mock --scenario conversation --loop &)
//   node scripts/browser-check.mjs http://127.0.0.1:4173/ --width 1920 --height 1080 --wait 6000 --axe --shot /tmp/hud
//
// Uses only the Chrome DevTools Protocol over Node's built-in WebSocket: no extra dependencies.
// Exits 1 on any CSP violation, off-origin request, console error, uncaught exception or serious/critical axe issue.

import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const args = process.argv.slice(2);
const url = args.find((a) => /^https?:/.test(a));
const opt = (name, d) => (args.includes(`--${name}`) ? args[args.indexOf(`--${name}`) + 1] : d);
if (!url) {
  console.error('usage: node scripts/browser-check.mjs <url> [--width N] [--height N] [--wait ms] [--axe] [--shot prefix] [--eval js]');
  process.exit(2);
}
const width = Number(opt('width', 1600));
const height = Number(opt('height', 900));
const wait = Number(opt('wait', 4000));
const shot = opt('shot');
const doAxe = args.includes('--axe');
const evalExpr = opt('eval');
const chromeBin = process.env.CHROME_BIN ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

const profile = mkdtempSync(join(tmpdir(), 'hud-check-'));
const port = 9222 + Math.floor(Math.random() * 600);
const chrome = spawn(
  chromeBin,
  ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--no-first-run', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', 'about:blank'],
  { stdio: 'ignore' }
);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const failures = [];
const notes = [];

try {
  let target;
  for (let i = 0; i < 60 && !target; i++) {
    try {
      target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((t) => t.type === 'page');
    } catch {
      /* not up yet */
    }
    if (!target) await sleep(250);
  }
  if (!target) throw new Error('could not reach Chrome');

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  let id = 0;
  const waiting = new Map();
  const origin = new URL(url).origin;
  const requests = new Set();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiting.has(m.id)) return waiting.get(m.id)(m.result ?? m.error), waiting.delete(m.id);
    const p = m.params ?? {};
    if (m.method === 'Network.requestWillBeSent') requests.add(p.request.url);
    if (m.method === 'Network.webSocketCreated') requests.add(p.url);
    if (m.method === 'Runtime.exceptionThrown') failures.push(`uncaught exception: ${p.exceptionDetails.exception?.description ?? p.exceptionDetails.text}`);
    if (m.method === 'Runtime.consoleAPICalled' && (p.type === 'error' || p.type === 'warning')) {
      failures.push(`console.${p.type}: ${p.args.map((a) => a.value ?? a.description).join(' ')}`);
    }
    if (m.method === 'Log.entryAdded') {
      const t = `${p.entry.text} ${p.entry.url ?? ''}`;
      if (/Content Security Policy|violat/i.test(t) || p.entry.source === 'security') failures.push(`CSP/security: ${t}`);
      else if (p.entry.level === 'error') failures.push(`log error [${p.entry.source}]: ${t}`);
      else if (!/GPU stall|ReadPixels/.test(t)) notes.push(`log ${p.entry.level} [${p.entry.source}]: ${t}`);
    }
    if (m.method === 'Network.loadingFailed' && !p.canceled) failures.push(`request failed: ${p.errorText} ${p.blockedReason ?? ''}`);
  };
  const send = (method, params = {}) => new Promise((res) => {
    const i = ++id;
    waiting.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params }));
  });

  for (const d of ['Page', 'Runtime', 'Log', 'Network']) await send(`${d}.enable`);
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url });
  await sleep(wait);

  const evalJs = async (expression) => {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? 'evaluate failed');
    return r.result?.value;
  };

  const probe = JSON.parse(
    await evalJs(`JSON.stringify({
      tier: document.querySelector('.orb')?.dataset.tier,
      state: document.querySelector('.orb')?.dataset.state,
      label: document.querySelector('[data-testid=orb-label]')?.textContent,
      bp: document.querySelector('.shell')?.dataset.bp,
      profile: document.querySelector('.shell')?.dataset.profile,
      connection: document.querySelector('.shell')?.dataset.connection,
      csp: document.querySelector('meta[http-equiv=Content-Security-Policy]')?.content ?? null,
      panels: [...document.querySelectorAll('[data-panel]')].map((e) => e.dataset.panel),
      overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth
    })`)
  );
  console.log('page:', JSON.stringify(probe));
  if (probe.connection !== 'open') failures.push(`websocket not open (connection=${probe.connection})`);
  if (probe.overflowX) failures.push('horizontal overflow at this viewport');

  const offOrigin = [...requests].filter((r) => !r.startsWith(origin) && !r.startsWith(origin.replace(/^http/, 'ws')) && !r.startsWith('about:') && !r.startsWith('data:'));
  console.log('requests:', [...requests].join(', '));
  if (offOrigin.length) failures.push(`off-origin requests: ${offOrigin.join(', ')}`);

  if (evalExpr) console.log('eval:', await evalJs(evalExpr));

  if (doAxe) {
    const require = createRequire(import.meta.url);
    const axeSrc = readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
    await evalJs(axeSrc);
    const res = JSON.parse(
      await evalJs(`axe.run(document, { resultTypes: ['violations', 'incomplete'] }).then((r) => JSON.stringify({
        violations: r.violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 4).map((n) => n.html.slice(0, 140) + ' :: ' + (n.any[0]?.message ?? '')) })),
        incomplete: r.incomplete.map((v) => ({ id: v.id, n: v.nodes.length }))
      }))`)
    );
    const bad = res.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
    console.log(`axe: ${res.violations.length} violations (${bad.length} serious/critical), incomplete: ${JSON.stringify(res.incomplete)}`);
    for (const v of res.violations) console.log(`  [${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.join('\n    ')}`);
    for (const v of bad) failures.push(`axe ${v.impact}: ${v.id}`);
  }

  if (shot) {
    const img = await send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(`${shot}-${width}x${height}.png`, Buffer.from(img.data, 'base64'));
    console.log(`screenshot: ${shot}-${width}x${height}.png`);
  }
  ws.close();
} catch (e) {
  failures.push(String(e));
} finally {
  chrome.kill();
  await sleep(300);
  try {
    rmSync(profile, { recursive: true, force: true, maxRetries: 5 });
  } catch {
    /* Chrome may still be flushing its profile; it's a temp dir */
  }
}

for (const n of notes) console.log('note:', n);
if (failures.length) {
  console.error(`\nFAILED (${failures.length}):\n  - ${failures.join('\n  - ')}`);
  process.exit(1);
}
console.log('OK: no CSP violations, no off-origin requests, no console errors');
process.exit(0);
