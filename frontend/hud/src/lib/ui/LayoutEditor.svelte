<script lang="ts">
  import { untrack } from 'svelte';
  import { useHud } from '../ctx';
  import { LayoutPanelIdValues, type LayoutPanelId, type LayoutProfile, type LayoutSlots } from '../generated/events';
  import { FALLBACK_LAYOUTS, REGISTRY, sanitizeSlots } from '../panels/registry';
  import Modal from './Modal.svelte';

  let { profile, onclose }: { profile: LayoutProfile; onclose: () => void } = $props();
  const { store } = useHud();

  const start = untrack(() => sanitizeSlots(store.layouts[profile] ?? FALLBACK_LAYOUTS[profile]));
  let top = $state<LayoutPanelId | ''>(start.top ?? '');
  let bottom = $state<LayoutPanelId | ''>(start.bottom ?? '');
  let left = $state<LayoutPanelId[]>([...start.left]);
  let right = $state<LayoutPanelId[]>([...start.right]);
  let busy = $state(false);
  let message = $state('');

  const used = $derived(new Set<string>([...left, ...right, top, bottom].filter(Boolean)));
  const unused = $derived(LayoutPanelIdValues.filter((id) => !used.has(id)));
  const title = (id: LayoutPanelId): string => REGISTRY[id].title;

  type Side = 'left' | 'right';
  const list = (s: Side): LayoutPanelId[] => (s === 'left' ? left : right);
  const setList = (s: Side, v: LayoutPanelId[]): void => {
    if (s === 'left') left = v;
    else right = v;
  };

  function move(side: Side, i: number, delta: number): void {
    const next = list(side).slice();
    const j = i + delta;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j] as LayoutPanelId, next[i] as LayoutPanelId];
    setList(side, next);
  }
  function across(side: Side, i: number): void {
    const from = list(side).slice();
    const [id] = from.splice(i, 1);
    if (!id) return;
    setList(side, from);
    const other: Side = side === 'left' ? 'right' : 'left';
    setList(other, [...list(other), id]);
  }
  function remove(side: Side, i: number): void {
    setList(side, list(side).filter((_, k) => k !== i));
  }
  function add(e: Event, side: Side): void {
    const sel = e.currentTarget as HTMLSelectElement;
    const id = sel.value as LayoutPanelId;
    sel.value = '';
    if (id && !used.has(id)) setList(side, [...list(side), id]);
  }

  function current(): LayoutSlots {
    const slots: LayoutSlots = { left: [...left], right: [...right] };
    if (top) slots.top = top;
    if (bottom) slots.bottom = bottom;
    return slots;
  }

  async function save(): Promise<void> {
    busy = true;
    message = '';
    const r = await store.saveLayout(profile, current());
    busy = false;
    if (r.ok) {
      store.announce(`Layout saved for ${profile} profile`);
      onclose();
    } else {
      message = r.message;
    }
  }

  function reset(): void {
    const d = FALLBACK_LAYOUTS[profile];
    top = d.top ?? '';
    bottom = d.bottom ?? '';
    left = [...d.left];
    right = [...d.right];
  }
</script>

{#snippet column(side: Side, label: string)}
  <fieldset>
    <legend>{label}</legend>
    <ul class="list">
      {#each list(side) as id, i (id)}
        <li class="item">
          <span class="grow">{title(id)}</span>
          <button class="btn small" type="button" onclick={() => move(side, i, -1)} disabled={i === 0} aria-label={`Move ${title(id)} up`}>↑</button>
          <button class="btn small" type="button" onclick={() => move(side, i, 1)} disabled={i === list(side).length - 1} aria-label={`Move ${title(id)} down`}>↓</button>
          <button class="btn small" type="button" onclick={() => across(side, i)} aria-label={`Move ${title(id)} to the ${side === 'left' ? 'right' : 'left'} column`}>{side === 'left' ? '→' : '←'}</button>
          <button class="btn small danger" type="button" onclick={() => remove(side, i)} aria-label={`Remove ${title(id)}`}>✕</button>
        </li>
      {:else}
        <li class="empty">No panels</li>
      {/each}
    </ul>
    <label class="sr-only" for={`add-${side}`}>Add a panel to the {label} column</label>
    <select id={`add-${side}`} class="input" onchange={(e) => add(e, side)} disabled={unused.length === 0}>
      <option value="">+ Add panel…</option>
      {#each unused as id (id)}<option value={id}>{title(id)}</option>{/each}
    </select>
  </fieldset>
{/snippet}

<Modal id="layout" title={`Edit layout: ${profile}`} {onclose} wide>
  <p class="sub">Choose which panels appear in each slot. Changes are saved to the Pi and audited.</p>
  <div class="slots">
    <div>
      <label for="slot-top" class="sub">Top slot</label>
      <select id="slot-top" class="input" bind:value={top}>
        <option value="">(none)</option>
        {#each LayoutPanelIdValues as id (id)}<option value={id} disabled={used.has(id) && top !== id}>{title(id)}</option>{/each}
      </select>
    </div>
    <div>
      <label for="slot-bottom" class="sub">Bottom slot</label>
      <select id="slot-bottom" class="input" bind:value={bottom}>
        <option value="">(none)</option>
        {#each LayoutPanelIdValues as id (id)}<option value={id} disabled={used.has(id) && bottom !== id}>{title(id)}</option>{/each}
      </select>
    </div>
  </div>
  <div class="cols">
    {@render column('left', 'Left')}
    {@render column('right', 'Right')}
  </div>
  {#if message}<p class="err" role="alert">{message}</p>{/if}
  <div class="actions">
    <button class="btn" type="button" onclick={reset}>Reset to default</button>
    <button class="btn" type="button" onclick={onclose}>Cancel</button>
    <button class="btn primary" type="button" onclick={save} disabled={busy}>{busy ? 'Saving…' : 'Save layout'}</button>
  </div>
</Modal>

<style>
  .slots,
  .cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-top: 8px;
  }
  .slots label {
    display: block;
    margin-bottom: 2px;
  }
  .slots .input {
    width: 100%;
  }
  fieldset {
    border: 1px solid var(--line-dim);
    margin: 0;
    padding: 6px 8px 8px;
    min-width: 0;
  }
  legend {
    padding: 0 4px;
    color: var(--accent);
    font-size: 0.74rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }
  fieldset .input {
    width: 100%;
    margin-top: 6px;
  }
  .item {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .grow {
    flex: 1 1 auto;
    min-width: 0;
    font-size: 0.85rem;
  }
  .err {
    color: var(--danger);
    margin: 8px 0 0;
  }
  .actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 14px;
    flex-wrap: wrap;
  }
  @media (max-width: 520px) {
    .slots,
    .cols {
      grid-template-columns: 1fr;
    }
  }
</style>
