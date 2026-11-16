<script lang="ts">
  import type { AuthStatus } from '../generated/events';
  import { useHud } from '../ctx';
  import { formatCountdown } from '../format/text';
  import { known } from './data';

  let { auth }: { auth: AuthStatus } = $props();
  const { store, ticker } = useHud();
  const status = $derived(known(auth.status, ['ok', 'expiring', 'expired', 'missing'] as const, 'missing'));
  const left = $derived(auth.expires_at ? auth.expires_at - store.toServer(ticker.now) : null);
</script>

{#if status === 'expiring'}
  <span class="chip" data-tone="warn"><span aria-hidden="true">▲</span> GOOGLE SIGN-IN EXPIRING{left !== null && left > 0 ? ` · ${formatCountdown(left)}` : ''}</span>
{:else if status === 'expired'}
  <span class="chip" data-tone="danger"><span aria-hidden="true">✗</span> GOOGLE SIGN-IN EXPIRED</span>
{:else if status === 'missing'}
  <span class="chip" data-tone="danger"><span aria-hidden="true">✗</span> NOT SIGNED IN</span>
{/if}
