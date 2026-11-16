<script lang="ts">
  import { useHud } from '../ctx';

  const { store } = useHud();
</script>

<div class="notices">
  {#each store.notices as n (n.id)}
    <div class="notice" data-level={n.level} role={n.level === 'error' ? 'alert' : 'status'}>
      <span><span aria-hidden="true">{n.level === 'error' ? '✗' : n.level === 'warn' ? '▲' : '●'}</span> {n.text}</span>
      <button class="btn small" type="button" onclick={() => store.dismissNotice(n.id)} aria-label="Dismiss message">✕</button>
    </div>
  {/each}
</div>

<style>
  .notices {
    position: fixed;
    right: 12px;
    bottom: 12px;
    z-index: 40;
    display: grid;
    gap: 6px;
    width: min(380px, calc(100% - 24px));
  }
  .notice {
    display: flex;
    gap: 8px;
    justify-content: space-between;
    align-items: center;
    padding: 6px 10px;
    border: 1px solid var(--line);
    background: var(--panel-solid);
    font-size: 0.85rem;
  }
  .notice[data-level='error'] {
    border-color: var(--danger);
  }
  .notice[data-level='warn'] {
    border-color: var(--warn);
  }
</style>
