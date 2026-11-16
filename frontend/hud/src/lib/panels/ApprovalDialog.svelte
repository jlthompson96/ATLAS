<script lang="ts">
  import { useHud } from '../ctx';
  import { verifyAction, type FullAction, type Verification } from '../approvals/hash';
  import { formatCountdown, safeText } from '../format/text';
  import Modal from '../ui/Modal.svelte';
  import PinDialog from '../ui/PinDialog.svelte';

  let { action, onclose }: { action: FullAction; onclose: () => void } = $props();

  const { store, ticker } = useHud();

  const WARNINGS: Record<string, string> = {
    new_recipient: 'NEW RECIPIENT',
    not_on_allowlist: 'NOT ON ALLOWLIST',
    tainted_args: 'TAINTED ARGUMENTS',
    unattended_origin: 'UNATTENDED ORIGIN'
  };
  const KIND: Record<string, string> = {
    'email.send': 'Send email',
    'discord.post': 'Post to Discord',
    'research.schedule': 'Schedule research',
    'portfolio.import': 'Import portfolio'
  };

  let verification = $state<Verification | null>(null);
  let step = $state<'review' | 'pin'>('review');
  let pending = $state<'approve' | 'deny' | null>(null);
  let busy = $state(false);
  let error = $state('');

  // Recompute the hash of exactly what the executor will run, whenever the payload changes.
  $effect(() => {
    const snapshot = action;
    verification = null;
    let live = true;
    void verifyAction(snapshot).then((v) => {
      if (live) verification = v;
    });
    return () => {
      live = false;
    };
  });

  const verified = $derived(verification?.status === 'verified' ? verification : null);
  const remaining = $derived(action.expires_at - store.toServer(ticker.now));
  const expired = $derived(remaining <= 0);
  const outcome = $derived(store.resolved[action.action_id]);
  const canDecide = $derived(store.canApprove && verified !== null && !expired);

  const entries = $derived.by(() => {
    const p = verified?.payload;
    if (p && typeof p === 'object' && !Array.isArray(p)) return Object.entries(p as Record<string, unknown>);
    return [];
  });

  function show(v: unknown): string {
    if (typeof v === 'string') return safeText(v, 2000);
    return safeText(JSON.stringify(v, null, 2) ?? '', 2000);
  }

  async function send(decision: 'approve' | 'deny'): Promise<void> {
    if (!verified || busy) return;
    busy = true;
    error = '';
    // The hash sent is the one this HUD computed itself, never the server's claim.
    const r = await store.decide(action.action_id, decision, verified.hash);
    busy = false;
    pending = null;
    if (r.ok) {
      onclose();
    } else {
      error = r.message;
      if (r.code === 'STEP_UP_REQUIRED') step = 'pin';
    }
  }

  function begin(decision: 'approve' | 'deny'): void {
    error = '';
    pending = decision;
    if (store.stepUpValid) void send(decision);
    else step = 'pin';
  }

  function granted(): void {
    step = 'review';
    if (pending) void send(pending);
  }

  function cancelPin(): void {
    step = 'review';
    pending = null;
  }
</script>

{#if step === 'pin'}
  <PinDialog title={pending === 'deny' ? 'PIN to deny' : 'PIN to approve'} ongranted={granted} oncancel={cancelPin} />
{:else}
  <Modal id="approval" title="Review request" onclose={onclose} wide>
    <div class="head">
      <span class="chip" data-tone="muted">{KIND[action.kind] ?? safeText(action.kind, 40)}</span>
      <span class="chip" data-tone={expired ? 'danger' : 'muted'}>{expired ? '✗ EXPIRED' : `◔ EXPIRES IN ${formatCountdown(remaining)}`}</span>
    </div>
    <p class="summary untrusted">{safeText(action.summary, 200)}</p>

    {#if action.provenance.tainted}
      <div class="taint" role="note">
        <strong><span aria-hidden="true">▲</span> UNTRUSTED SOURCE</strong>
        <span>This request was influenced by third-party content. Check every field before approving.</span>
      </div>
    {/if}
    {#if action.provenance.sources.length}
      <ul class="sources" aria-label="Sources">
        {#each action.provenance.sources as s (s.ref)}
          <li class="untrusted"><span class="chip" data-tone="muted">{safeText(s.kind, 20)}</span> {safeText(s.label ?? s.ref, 200)}</li>
        {/each}
      </ul>
    {/if}
    {#if action.warnings?.length}
      <p class="warns">
        {#each action.warnings as w (w)}<span class="flag"><span aria-hidden="true">▲</span> {WARNINGS[w] ?? 'WARNING'}</span>{/each}
      </p>
    {/if}

    {#if verification === null}
      <p class="status" role="status">Verifying payload…</p>
    {:else if verification.status === 'mismatch'}
      <div class="mismatch" role="alert">
        <strong><span aria-hidden="true">✗</span> PAYLOAD HASH MISMATCH</strong>
        <span>The payload does not match its hash, so it can't be approved. This request is not safe to act on.</span>
        <span class="mono sub">claimed {verification.claimed.slice(0, 16)}… · computed {verification.computed.slice(0, 16)}…</span>
      </div>
    {:else if verification.status === 'unverifiable'}
      <div class="mismatch" role="alert">
        <strong><span aria-hidden="true">✗</span> CANNOT VERIFY</strong>
        <span>{verification.reason}</span>
      </div>
    {:else}
      <p class="status ok" role="status"><span aria-hidden="true">✓</span> Payload verified · SHA-256 <span class="mono">{verification.hash.slice(0, 12)}…</span></p>
      <dl class="payload">
        {#each entries as [k, v] (k)}
          <dt>{safeText(k, 60)}</dt>
          <dd class="untrusted"><pre>{show(v)}</pre></dd>
        {/each}
      </dl>
    {/if}

    {#if !store.canApprove}
      <p class="note">This device can't approve requests. Use a remote device and enter your PIN.</p>
    {/if}
    {#if outcome && outcome.outcome !== 'approved'}
      <p class="note" role="status">Resolved: {outcome.outcome}{outcome.code ? ` (${outcome.code})` : ''}</p>
    {/if}
    {#if error}<p class="err" role="alert">{error}</p>{/if}

    <div class="actions">
      <button class="btn" type="button" onclick={onclose}>Close</button>
      <button class="btn danger" type="button" disabled={!canDecide || busy} onclick={() => begin('deny')}>Deny</button>
      {#if canDecide}
        <button class="btn primary" type="button" disabled={busy} onclick={() => begin('approve')}>Approve</button>
      {/if}
    </div>
  </Modal>
{/if}

<style>
  .head {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .summary {
    margin: 8px 0;
    font-size: 1.05rem;
  }
  .taint,
  .mismatch {
    display: grid;
    gap: 2px;
    margin: 8px 0;
    padding: 8px 10px;
    border: 1px solid var(--warn);
    color: var(--warn);
    font-size: 0.85rem;
  }
  .mismatch {
    border-color: var(--danger);
    color: var(--danger);
  }
  .sources {
    list-style: none;
    margin: 6px 0;
    padding: 0;
    font-size: 0.82rem;
  }
  .warns {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin: 6px 0;
  }
  .status {
    margin: 8px 0 4px;
    font-size: 0.82rem;
    color: var(--muted);
  }
  .status.ok {
    color: var(--ok);
  }
  .payload {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 4px 12px;
    margin: 6px 0;
    padding: 8px 10px;
    border: 1px solid var(--line-dim);
    background: rgba(5, 8, 13, 0.6);
    font-size: 0.85rem;
  }
  .payload dt {
    color: var(--muted);
    font-family: var(--font-data);
  }
  .payload dd {
    margin: 0;
    min-width: 0;
  }
  pre {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-family: var(--font-data);
    font-size: 0.82rem;
  }
  .note {
    margin: 8px 0 0;
    color: var(--muted);
    font-size: 0.85rem;
  }
  .err {
    margin: 8px 0 0;
    color: var(--danger);
    font-size: 0.85rem;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 14px;
  }
</style>
