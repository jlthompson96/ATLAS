// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { parseInline, parseMarkdown } from '../src/lib/format/markdown';
import Shell from '../src/lib/Shell.svelte';
import type { Transcript } from '../src/lib/generated/events';
import { fullSnapshot } from './fixtures';
import { mountHud, q, settle, type Harness } from './helpers';

describe('the Markdown subset parses to plain strings', () => {
  it('formats bold, italic and code inline', () => {
    expect(parseInline('a **b** *c* `d` __e__ _f_')).toEqual([
      { kind: 'text', text: 'a ' },
      { kind: 'strong', text: 'b' },
      { kind: 'text', text: ' ' },
      { kind: 'em', text: 'c' },
      { kind: 'text', text: ' ' },
      { kind: 'code', text: 'd' },
      { kind: 'text', text: ' ' },
      { kind: 'strong', text: 'e' },
      { kind: 'text', text: ' ' },
      { kind: 'em', text: 'f' }
    ]);
  });

  it('leaves what it does not format exactly as written', () => {
    const literal = (s: string) => expect(parseInline(s)).toEqual([{ kind: 'text', text: s }]);
    literal('snake_case_name and file_name.py'); // underscores inside words
    literal('2 * 3 * 4'); // spaced asterisks
    literal('**unclosed bold');
    literal('[a link](https://example.com) and ![an image](https://example.com/x.png)'); // never links or images
    literal('<b>html</b> <img src=x onerror=alert(1)>'); // never markup
    expect(parseInline('`**not bold**`')).toEqual([{ kind: 'code', text: '**not bold**' }]);
  });

  it('splits paragraphs on blank lines and keeps single line breaks', () => {
    expect(parseMarkdown('one\ntwo\n\nthree')).toEqual([
      { kind: 'p', inline: [{ kind: 'text', text: 'one' }, { kind: 'br' }, { kind: 'text', text: 'two' }] },
      { kind: 'p', inline: [{ kind: 'text', text: 'three' }] }
    ]);
  });

  it('reads headings, code blocks and lists', () => {
    const blocks = parseMarkdown('## Title\n```\nx = **1**\n```\n- a\n- b\n\n3. c\n4. d');
    expect(blocks).toEqual([
      { kind: 'h', inline: [{ kind: 'text', text: 'Title' }] },
      { kind: 'code', text: 'x = **1**' },
      { kind: 'ul', start: 1, items: [{ inline: [{ kind: 'text', text: 'a' }] }, { inline: [{ kind: 'text', text: 'b' }] }] },
      { kind: 'ol', start: 3, items: [{ inline: [{ kind: 'text', text: 'c' }] }, { inline: [{ kind: 'text', text: 'd' }] }] }
    ]);
  });

  it('nests indented items under the item above (the shape models use for summaries)', () => {
    const [list] = parseMarkdown('1. **First**\n    *   detail a\n    *   detail b\n2. Second');
    expect(list).toMatchObject({
      kind: 'ol',
      items: [
        { inline: [{ kind: 'strong', text: 'First' }], sub: { kind: 'ul', items: [{ inline: [{ text: 'detail a' }] }, { inline: [{ text: 'detail b' }] }] } },
        { inline: [{ text: 'Second' }] }
      ]
    });
  });

  it('keeps a numbered list together across blank lines between items', () => {
    const blocks = parseMarkdown('1. a\n\n2. b\n\n3. c');
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({ kind: 'ol', items: [{}, {}, {}] });
  });

  it('stops nesting at three levels', () => {
    const [list] = parseMarkdown('- 1\n  - 2\n    - 3\n      - 4');
    expect(JSON.stringify(list).match(/"sub"/g)).toHaveLength(2);
    expect(JSON.stringify(list)).toContain('"4"');
  });
});

describe('the transcript formats only ATLAS\'s own replies', () => {
  let h: Harness | undefined;
  afterEach(() => h?.cleanup());

  const HOSTILE = '**bold** <img src=x onerror="window.__md=1"> [link](https://evil.example.com) `code`\n- item';

  async function show(entry: Partial<Transcript>) {
    const snap = fullSnapshot();
    snap.transcript = [{ utterance_id: 'a1', role: 'assistant', text: HOSTILE, source: 'text', ...entry }];
    h = mountHud(Shell, { snapshot: snap });
    h.settings.viewportWidth = 1920;
    await settle();
    return q(document.body, '[data-panel=transcript] [role=log]') as HTMLElement;
  }

  it('formats a reply with no untrusted provenance, as elements with text only', async () => {
    const log = await show({});
    expect(log.querySelector('strong')?.textContent).toBe('bold');
    expect(log.querySelector('code')?.textContent).toBe('code');
    expect(log.querySelector('li')?.textContent).toBe('item');
    expect(log.querySelector('img, script, a, [src], [href]')).toBeNull();
    expect(log.textContent).toContain('<img src=x onerror="window.__md=1">');
    expect(log.textContent).toContain('[link](https://evil.example.com)');
    expect((window as unknown as { __md?: number }).__md).toBeUndefined();
  });

  it('leaves a tainted reply plain, with the untrusted-source flag', async () => {
    const log = await show({ provenance: { tainted: true, sources: [] } });
    expect(log.querySelector('strong, em, code, li, ul, ol')).toBeNull();
    expect(log.textContent).toContain('**bold**');
    expect(log.textContent).toContain('untrusted source');
  });

  it('never formats what the user typed', async () => {
    const log = await show({ role: 'user' });
    expect(log.querySelector('strong, em, code, li')).toBeNull();
    expect(log.textContent).toContain('**bold**');
  });
});
