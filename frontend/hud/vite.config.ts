/// <reference types="vitest/config" />
import { defineConfig, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

/**
 * Content-Security-Policy for the built app (PLAN.md §2.14: `default-src 'self'`).
 * `connect-src 'self'` also covers the same-origin WebSocket (CSP3 matches ws/wss for 'self').
 * Applied only to the production build: the dev server injects inline styles and an HMR client
 * that a strict policy would block. The production server should also send it as a header.
 */
export const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self'",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'"
].join('; ');

function cspPlugin(): Plugin {
  return {
    name: 'atlas-csp',
    apply: 'build',
    transformIndexHtml: {
      order: 'post',
      handler() {
        return [
          {
            tag: 'meta',
            attrs: { 'http-equiv': 'Content-Security-Policy', content: CSP },
            injectTo: 'head-prepend'
          }
        ];
      }
    }
  };
}

const mock = { target: 'ws://127.0.0.1:8765', ws: true, changeOrigin: false };

export default defineConfig(({ mode }) => ({
  plugins: [svelte(), cspPlugin()],
  // Svelte's client build is selected by the "browser" condition; vitest needs it for mount().
  ...(mode === 'test' ? { resolve: { conditions: ['browser'] } } : {}),
  server: { host: '127.0.0.1', proxy: { '/ws/events': mock } },
  preview: { host: '127.0.0.1', proxy: { '/ws/events': mock } },
  build: {
    target: 'es2022',
    sourcemap: false,
    // Fonts and images stay files: `data:` URIs would need a looser CSP.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false }
  },
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
    setupFiles: ['tests/setup.ts'],
    testTimeout: 30_000
  }
}));
