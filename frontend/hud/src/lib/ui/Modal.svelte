<script lang="ts">
  import type { Snippet } from 'svelte';
  import { trapFocus } from './trap';

  let { title, id, onclose, children, wide = false }: { title: string; id: string; onclose: () => void; children: Snippet; wide?: boolean } = $props();
</script>

<div class="backdrop" role="presentation">
  <div class="dialog" class:wide role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} tabindex="-1" use:trapFocus={onclose}>
    <h2 id={`${id}-title`}>{title}</h2>
    {@render children()}
  </div>
</div>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    z-index: 50;
    display: grid;
    place-items: center;
    padding: 16px;
    background: rgba(2, 5, 9, 0.8);
  }
  .dialog {
    width: min(460px, 100%);
    max-height: 100%;
    overflow: auto;
    background: var(--panel-solid);
    border: 1px solid var(--accent);
    padding: 16px 18px 18px;
    box-shadow: 0 0 40px rgba(63, 240, 255, 0.15);
  }
  .dialog.wide {
    width: min(640px, 100%);
  }
  h2 {
    margin: 0 0 10px;
    font-size: 0.95rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--accent);
  }
</style>
