<script lang="ts">
  // Renders format/markdown.ts's tree. Every piece of text is a text node inside a fixed element: no raw HTML,
  // no links, no images. Only for ATLAS's own replies without untrusted provenance (see markdown.ts).
  import { parseMarkdown, type Inline, type ListBlock } from '../format/markdown';

  let { text }: { text: string } = $props();
  const blocks = $derived(parseMarkdown(text));
</script>

{#snippet inline(parts: Inline[])}{#each parts as part, i (i)}{#if part.kind === 'strong'}<strong>{part.text}</strong>{:else if part.kind === 'em'}<em>{part.text}</em>{:else if part.kind === 'code'}<code>{part.text}</code>{:else if part.kind === 'br'}<br />{:else}{part.text}{/if}{/each}{/snippet}

{#snippet list(l: ListBlock)}
  {#if l.kind === 'ol'}
    <ol start={l.start}>
      {#each l.items as item, i (i)}<li>{@render inline(item.inline)}{#if item.sub}{@render list(item.sub)}{/if}</li>{/each}
    </ol>
  {:else}
    <ul>
      {#each l.items as item, i (i)}<li>{@render inline(item.inline)}{#if item.sub}{@render list(item.sub)}{/if}</li>{/each}
    </ul>
  {/if}
{/snippet}

<div class="md">
  {#each blocks as block, i (i)}
    {#if block.kind === 'p'}
      <p>{@render inline(block.inline)}</p>
    {:else if block.kind === 'h'}
      <!-- Not h1-h6: a reply's headings must not join the page's outline. -->
      <p class="h">{@render inline(block.inline)}</p>
    {:else if block.kind === 'code'}
      <pre><code>{block.text}</code></pre>
    {:else}
      {@render list(block)}
    {/if}
  {/each}
</div>

<style>
  .md {
    display: grid;
    gap: 4px;
  }
  .md :global(p),
  .md :global(ul),
  .md :global(ol),
  .md :global(pre) {
    margin: 0;
  }
  .md :global(ul),
  .md :global(ol) {
    padding-left: 1.3em;
  }
  .md :global(li + li) {
    margin-top: 2px;
  }
  .h {
    font-weight: 600;
    color: var(--accent);
  }
  .md :global(strong) {
    font-weight: 600;
    color: var(--text);
  }
  .md :global(code) {
    font-family: var(--font-data);
    font-size: 0.88em;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 3px;
    padding: 0 3px;
  }
  pre {
    overflow-x: auto;
    padding: 6px 8px;
    background: rgba(255, 255, 255, 0.04);
    border-radius: 4px;
    white-space: pre;
  }
  pre code {
    background: none;
    padding: 0;
  }
</style>
