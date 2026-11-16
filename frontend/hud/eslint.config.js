import js from '@eslint/js';
import globals from 'globals';
import svelte from 'eslint-plugin-svelte';
import tseslint from 'typescript-eslint';

// Untrusted text (x-atlas-untrusted) must stay plain text: no raw HTML sinks anywhere in src/.
const sinkMessage = 'Raw HTML sinks are forbidden: untrusted text is rendered as plain text only.';
const htmlSinks = [
  {
    selector: 'AssignmentExpression[left.property.name=/^(innerHTML|outerHTML)$/]',
    message: sinkMessage
  },
  {
    selector: 'CallExpression[callee.property.name=/^(insertAdjacentHTML|createContextualFragment|write|writeln)$/]',
    message: sinkMessage
  }
];

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'src/lib/generated/**', 'tests/__snapshots__/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...svelte.configs['flat/recommended'],
  {
    languageOptions: { globals: { ...globals.browser, ...globals.node } }
  },
  {
    files: ['**/*.svelte', '**/*.svelte.ts'],
    languageOptions: { parserOptions: { parser: tseslint.parser } }
  },
  {
    files: ['src/**/*.ts', 'src/**/*.svelte'],
    rules: {
      'no-restricted-syntax': ['error', ...htmlSinks],
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }]
    }
  },
  {
    files: ['**/*.svelte'],
    rules: { 'svelte/no-at-html-tags': 'error' }
  }
);
