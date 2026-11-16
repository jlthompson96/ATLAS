<script lang="ts">
  import { useHud } from '../ctx';
  import Modal from './Modal.svelte';

  let {
    title = 'Confirm with your PIN',
    hint = 'Your PIN is checked by the Pi and is valid for 60 seconds.',
    ongranted,
    oncancel
  }: { title?: string; hint?: string; ongranted: () => void; oncancel: () => void } = $props();

  const { store } = useHud();
  let pin = $state('');
  let busy = $state(false);
  let error = $state('');

  async function submit(e: SubmitEvent): Promise<void> {
    e.preventDefault();
    if (busy || !/^[0-9]{4,12}$/.test(pin)) return;
    busy = true;
    error = '';
    const r = await store.verifyPin(pin);
    busy = false;
    pin = ''; // the PIN is never kept after it is sent
    if (r.ok) ongranted();
    else error = r.message;
  }
</script>

<Modal id="pin" {title} onclose={oncancel}>
  <form onsubmit={submit}>
    <p class="sub">{hint}</p>
    <label class="sub" for="pin-input">PIN</label>
    <input
      id="pin-input"
      class="input mono"
      type="password"
      inputmode="numeric"
      autocomplete="off"
      maxlength="12"
      data-autofocus
      bind:value={pin}
      aria-describedby={error ? 'pin-error' : undefined}
      aria-invalid={error ? 'true' : undefined}
    />
    {#if error}<p id="pin-error" class="err" role="alert">{error}</p>{/if}
    <div class="actions">
      <button class="btn" type="button" onclick={oncancel}>Cancel</button>
      <button class="btn primary" type="submit" disabled={busy || !/^[0-9]{4,12}$/.test(pin)}>{busy ? 'Checking…' : 'Verify'}</button>
    </div>
  </form>
</Modal>

<style>
  form {
    display: grid;
    gap: 6px;
  }
  .sub {
    margin: 0;
  }
  .err {
    margin: 0;
    color: var(--danger);
    font-size: 0.85rem;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 8px;
  }
</style>
