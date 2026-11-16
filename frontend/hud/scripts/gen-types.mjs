#!/usr/bin/env node
// Generates src/lib/generated/events.ts from contracts/events.v1.schema.json.
//
//   node scripts/gen-types.mjs           write the file
//   node scripts/gen-types.mjs --check   exit 1 if the committed file is stale (used by `npm run lint` and tests)
//
// The contract is the source of truth; nothing in the generated file is hand-copied. The generator
// handles the subset of JSON Schema the contract uses (object, array, enum, const, oneOf, $ref and
// the type/payload dispatch in ServerMessage/ClientMessage). Conditional rules (if/then) are not
// expressed in the types; the runtime guards in src/lib/protocol cover them.

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
export const SCHEMA_PATH = resolve(here, '../../../contracts/events.v1.schema.json');
export const OUT_PATH = resolve(here, '../src/lib/generated/events.ts');

const SKIP = new Set(['Envelope', 'ServerMessage', 'ClientMessage']);

/** Render a JSON Schema node as a TypeScript type expression. */
function ts(node, indent = 0) {
  if (node === undefined) return 'unknown';
  if (node.$ref) return node.$ref.replace('#/$defs/', '');
  if ('const' in node) return JSON.stringify(node.const);
  if (node.enum) return node.enum.map((v) => JSON.stringify(v)).join(' | ');
  if (node.oneOf) return node.oneOf.map((n) => ts(n, indent)).join(' | ');
  switch (node.type) {
    case 'string':
      return 'string';
    case 'integer':
    case 'number':
      return 'number';
    case 'boolean':
      return 'boolean';
    case 'null':
      return 'null';
    case 'array': {
      const inner = ts(node.items, indent);
      return /[|{]/.test(inner) ? `(${inner})[]` : `${inner}[]`;
    }
    case 'object':
      return objectType(node, indent);
    default:
      return 'unknown';
  }
}

function objectType(node, indent) {
  const props = node.properties ?? {};
  const keys = Object.keys(props);
  if (keys.length === 0) return node.additionalProperties === false ? 'Record<string, never>' : 'Record<string, unknown>';
  const required = new Set(node.required ?? []);
  const pad = '  '.repeat(indent + 1);
  const lines = keys.map((k) => {
    const doc = props[k].description ? `${pad}/** ${props[k].description.replace(/\*\//g, '* /')} */\n` : '';
    return `${doc}${pad}${JSON.stringify(k).replace(/^"([A-Za-z_]\w*)"$/, '$1')}${required.has(k) ? '' : '?'}: ${ts(props[k], indent + 1)};`;
  });
  return `{\n${lines.join('\n')}\n${'  '.repeat(indent)}}`;
}

/** Names of properties marked x-atlas-sensitive anywhere in the schema. */
function sensitiveKeys(node, parentKey, out) {
  if (Array.isArray(node)) {
    for (const n of node) sensitiveKeys(n, null, out);
  } else if (node && typeof node === 'object') {
    if (node['x-atlas-sensitive'] && parentKey) out.add(parentKey);
    for (const [k, v] of Object.entries(node)) {
      if (k === 'properties' && v && typeof v === 'object') {
        for (const [prop, sub] of Object.entries(v)) sensitiveKeys(sub, prop, out);
      } else {
        sensitiveKeys(v, null, out);
      }
    }
  }
}

/** type -> payload $ref, read from the if/then dispatch in ServerMessage / ClientMessage. */
function dispatch(message) {
  const map = {};
  for (const part of message.allOf) {
    const type = part.if?.properties?.type?.const;
    const ref = part.then?.properties?.payload?.$ref;
    if (type && ref) map[type] = ref.replace('#/$defs/', '');
  }
  return map;
}

export function generate(schema) {
  const defs = schema.$defs;
  const out = [];
  out.push(
    '// GENERATED FILE. Do not edit.',
    '// Source: contracts/events.v1.schema.json (via `npm run gen:types`).',
    '// A test and `npm run lint` fail if this file is stale.',
    '',
    `export const SCHEMA_REVISION = ${JSON.stringify(schema['x-atlas-schema-revision'])} as const;`,
    ''
  );

  const serverTypes = defs.ServerMessage.allOf.find((p) => p.properties?.type?.enum).properties.type.enum;
  const clientTypes = defs.ClientMessage.allOf.find((p) => p.properties?.type?.enum).properties.type.enum;
  out.push(
    `export const SERVER_TYPES = ${JSON.stringify(serverTypes)} as const;`,
    'export type ServerType = (typeof SERVER_TYPES)[number];',
    `export const CLIENT_TYPES = ${JSON.stringify(clientTypes)} as const;`,
    'export type ClientType = (typeof CLIENT_TYPES)[number];',
    ''
  );

  const sensitive = new Set();
  sensitiveKeys(schema, null, sensitive);
  out.push(
    '/** Fields that must never be logged (x-atlas-sensitive). */',
    `export const SENSITIVE_KEYS: readonly string[] = ${JSON.stringify([...sensitive].sort())};`,
    '',
    `export const STATE_TRANSITIONS: Readonly<Record<string, readonly string[]>> = ${JSON.stringify(defs.State['x-atlas-state-transitions'], null, 2)};`,
    ''
  );

  for (const [name, node] of Object.entries(defs)) {
    if (SKIP.has(name)) continue;
    if (node.description) out.push(`/** ${node.description.replace(/\*\//g, '* /')} */`);
    if (node.enum) {
      out.push(
        `export const ${name}Values = ${JSON.stringify(node.enum)} as const;`,
        `export type ${name} = (typeof ${name}Values)[number];`
      );
    } else {
      const body = ts(node, 0);
      out.push(`export type ${name} = ${body};`);
    }
    out.push('');
  }

  const s = dispatch(defs.ServerMessage);
  const c = dispatch(defs.ClientMessage);
  out.push(
    '/** Payload type of each server -> client event. */',
    `export interface ServerPayloads {\n${Object.entries(s)
      .map(([k, v]) => `  ${JSON.stringify(k)}: ${v};`)
      .join('\n')}\n}`,
    '',
    '/** Payload type of each client -> server message. */',
    `export interface ClientPayloads {\n${Object.entries(c)
      .map(([k, v]) => `  ${JSON.stringify(k)}: ${v};`)
      .join('\n')}\n}`,
    ''
  );
  return out.join('\n');
}

export function build() {
  return generate(JSON.parse(readFileSync(SCHEMA_PATH, 'utf8')));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const text = build();
  if (process.argv.includes('--check')) {
    const current = existsSync(OUT_PATH) ? readFileSync(OUT_PATH, 'utf8') : '';
    if (current !== text) {
      console.error('src/lib/generated/events.ts is stale. Run `npm run gen:types` and commit the result.');
      process.exit(1);
    }
    console.log('generated types are up to date');
  } else {
    mkdirSync(dirname(OUT_PATH), { recursive: true });
    writeFileSync(OUT_PATH, text);
    console.log(`wrote ${OUT_PATH}`);
  }
}
