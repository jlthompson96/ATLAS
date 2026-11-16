import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { CLIENT_TYPES, SCHEMA_REVISION, SENSITIVE_KEYS, SERVER_TYPES, STATE_TRANSITIONS, StateValues } from '../src/lib/generated/events';

const ROOT = join(__dirname, '..');
const schema = JSON.parse(readFileSync(join(ROOT, '../../contracts/events.v1.schema.json'), 'utf8')) as {
  'x-atlas-schema-revision': string;
  $defs: Record<string, { enum?: string[]; allOf?: { properties?: { type?: { enum?: string[] } } }[]; 'x-atlas-state-transitions'?: Record<string, string[]> }>;
};

describe('generated event types', () => {
  it('are not stale: regenerating from contracts/events.v1.schema.json changes nothing', () => {
    // Exits non-zero (and execFileSync throws) if src/lib/generated/events.ts differs from a fresh generation.
    const out = execFileSync('node', ['scripts/gen-types.mjs', '--check'], { cwd: ROOT, encoding: 'utf8' });
    expect(out).toContain('up to date');
  });

  it('carry the contract revision and message lists straight from the schema', () => {
    expect(SCHEMA_REVISION).toBe(schema['x-atlas-schema-revision']);
    const serverTypes = schema.$defs.ServerMessage?.allOf?.find((p) => p.properties?.type?.enum)?.properties?.type?.enum;
    const clientTypes = schema.$defs.ClientMessage?.allOf?.find((p) => p.properties?.type?.enum)?.properties?.type?.enum;
    expect([...SERVER_TYPES]).toEqual(serverTypes);
    expect([...CLIENT_TYPES]).toEqual(clientTypes);
    expect([...StateValues]).toEqual(schema.$defs.State?.enum);
    expect(STATE_TRANSITIONS).toEqual(schema.$defs.State?.['x-atlas-state-transitions']);
  });

  it('list the sensitive fields from the schema (never logged)', () => {
    expect([...SENSITIVE_KEYS].sort()).toEqual(['passphrase', 'pin', 'step_up_token', 'token']);
  });

  it('is checked by `npm run lint`', () => {
    const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')) as { scripts: Record<string, string> };
    expect(pkg.scripts.lint).toContain('gen-types.mjs --check');
    expect(pkg.scripts['gen:types']).toContain('gen-types.mjs');
  });
});
