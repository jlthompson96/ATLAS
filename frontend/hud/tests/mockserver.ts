import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { join } from 'node:path';

const REPO = join(__dirname, '../../..');
const BACKEND = join(REPO, 'backend');
const PYTHON = join(BACKEND, '.venv/bin/python');

export const mockAvailable = existsSync(PYTHON);

export interface RunningMock {
  url: string;
  port: number;
  stop: () => Promise<void>;
}

async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address();
      srv.close(() => (addr && typeof addr === 'object' ? resolve(addr.port) : reject(new Error('no port'))));
    });
    srv.on('error', reject);
  });
}

/** Start the real mock event server (backend/atlas/ui_server/mock) on a free loopback port. */
export async function startMock(args: string[]): Promise<RunningMock> {
  if (!mockAvailable) throw new Error(`backend venv missing at ${PYTHON}: create it per CLAUDE.md (python3 -m venv .venv; pip install --require-hashes -r requirements-dev.lock)`);
  const port = await freePort();
  const child: ChildProcess = spawn(PYTHON, ['-m', 'atlas.ui_server.mock', '--port', String(port), ...args], { cwd: BACKEND, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = '';
  child.stdout?.on('data', (d: Buffer) => (log += d.toString()));
  child.stderr?.on('data', (d: Buffer) => (log += d.toString()));
  const url = `ws://127.0.0.1:${port}/ws/events`;
  const deadline = Date.now() + 15_000;
  for (;;) {
    if (child.exitCode !== null) throw new Error(`mock exited early:\n${log}`);
    if (log.includes('listening') || log.includes('mock event server')) break;
    if (Date.now() > deadline) throw new Error(`mock did not start:\n${log}`);
    await new Promise((r) => setTimeout(r, 50));
  }
  return {
    url,
    port,
    stop: () =>
      new Promise((resolve) => {
        if (child.exitCode !== null || child.signalCode !== null) return resolve();
        child.once('exit', () => resolve());
        child.kill('SIGTERM');
        setTimeout(() => child.kill('SIGKILL'), 3000).unref();
      })
  };
}

export const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

export async function until(cond: () => boolean, what: string, timeoutMs = 15_000): Promise<void> {
  const end = Date.now() + timeoutMs;
  while (!cond()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await sleep(25);
  }
}
