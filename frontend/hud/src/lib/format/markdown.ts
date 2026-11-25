/**
 * A deliberately small Markdown subset for ATLAS's own replies: paragraphs with line breaks, **bold**, *italic*,
 * `code`, fenced code blocks, headings, and bullet or numbered lists (nested up to three levels).
 *
 * It returns a tree of plain strings, never HTML: Markdown.svelte renders each piece as a text node inside a fixed
 * element, so nothing in the text can become markup. There are no links and no images: `[text](url)` and `![x](y)`
 * stay literal text. Inline HTML stays literal text.
 *
 * Only for replies with no untrusted provenance. Text derived from third parties (`UntrustedText`, or a reply whose
 * provenance is tainted) is never formatted (contracts/README.md, "Untrusted text"): formatting would let a crafted
 * email dress its words up as ATLAS's own.
 */

export type Inline = { kind: 'text' | 'strong' | 'em' | 'code'; text: string } | { kind: 'br' };
export interface ListItem {
  inline: Inline[];
  sub?: ListBlock;
}
export interface ListBlock {
  kind: 'ul' | 'ol';
  start: number;
  items: ListItem[];
}
export type Block = { kind: 'p'; inline: Inline[] } | { kind: 'h'; inline: Inline[] } | { kind: 'code'; text: string } | ListBlock;

const MAX_DEPTH = 3;
const FENCE = /^\s*```/;
const BLANK = /^\s*$/;
const HEADING = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;
const BULLET = /^(\s*)[-*+]\s+(.*)$/;
const NUMBERED = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;

interface ListLine {
  indent: number;
  kind: 'ul' | 'ol';
  start: number;
  text: string;
}

function listLine(line: string): ListLine | null {
  const b = BULLET.exec(line);
  if (b) return { indent: width(b[1] ?? ''), kind: 'ul', start: 1, text: b[2] ?? '' };
  const n = NUMBERED.exec(line);
  if (n) return { indent: width(n[1] ?? ''), kind: 'ol', start: Number(n[2]), text: n[3] ?? '' };
  return null;
}

function width(spaces: string): number {
  return spaces.replace(/\t/g, '    ').length;
}

export function parseMarkdown(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let para: string[] = [];
  let items: ListLine[] = [];

  const flushPara = (): void => {
    if (para.length) blocks.push({ kind: 'p', inline: inlineLines(para) });
    para = [];
  };
  const flushList = (): void => {
    let i = 0;
    while (i < items.length) {
      const [list, next] = buildList(items, i, 1);
      blocks.push(list);
      i = next;
    }
    items = [];
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? '';
    if (FENCE.test(line)) {
      flushPara();
      flushList();
      const body: string[] = [];
      for (i++; i < lines.length && !FENCE.test(lines[i] ?? ''); i++) body.push(lines[i] ?? '');
      blocks.push({ kind: 'code', text: body.join('\n') });
      continue;
    }
    if (BLANK.test(line)) {
      flushPara();
      // A blank line between two items of the same list doesn't end it.
      const next = lines.slice(i + 1).find((l) => !BLANK.test(l));
      if (!(items.length && next !== undefined && listLine(next))) flushList();
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      flushPara();
      flushList();
      blocks.push({ kind: 'h', inline: parseInline(heading[1] ?? '') });
      continue;
    }
    const item = listLine(line);
    if (item) {
      flushPara();
      items.push(item);
      continue;
    }
    const last = items[items.length - 1];
    if (last && /^\s+\S/.test(line)) {
      last.text += ` ${line.trim()}`; // an indented line continues the item above
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
  return blocks;
}

/** One list from items[from], at its indent; deeper items become a sub-list of the item above them. */
function buildList(items: ListLine[], from: number, depth: number): [ListBlock, number] {
  const first = items[from] as ListLine;
  const list: ListBlock = { kind: first.kind, start: first.start, items: [] };
  let i = from;
  while (i < items.length) {
    const it = items[i] as ListLine;
    if (it.indent < first.indent) break; // back to a parent level
    const parent = list.items[list.items.length - 1];
    if (it.indent >= first.indent + 2 && parent) {
      if (depth < MAX_DEPTH && !parent.sub) {
        const [sub, next] = buildList(items, i, depth + 1);
        parent.sub = sub;
        i = next;
      } else {
        list.items.push({ inline: parseInline(it.text) }); // too deep, or a second sub-list: flatten
        i++;
      }
      continue;
    }
    if (it.kind !== list.kind) break; // a different kind of list starts here
    list.items.push({ inline: parseInline(it.text) });
    i++;
  }
  return [list, i];
}

function inlineLines(lines: string[]): Inline[] {
  const out: Inline[] = [];
  lines.forEach((line, n) => {
    if (n) out.push({ kind: 'br' });
    out.push(...parseInline(line.trim()));
  });
  return out;
}

// Code first (nothing inside it is formatted), then bold, then italic. Underscore emphasis only at word edges,
// so snake_case names stay as they are.
const INLINE =
  /`([^`\n]+)`|\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__|\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*|(?<![\p{L}\p{N}_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\p{L}\p{N}_])/gu;

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ kind: 'text', text: text.slice(last, at) });
    if (m[1] !== undefined) out.push({ kind: 'code', text: m[1] });
    else if (m[2] !== undefined || m[3] !== undefined) out.push({ kind: 'strong', text: m[2] ?? m[3] ?? '' });
    else out.push({ kind: 'em', text: m[4] ?? m[5] ?? '' });
    last = at + m[0].length;
  }
  if (last < text.length) out.push({ kind: 'text', text: text.slice(last) });
  return out;
}
