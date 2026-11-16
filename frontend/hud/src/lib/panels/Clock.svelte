<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { formatClock, formatDay } from '../format/text';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store, ticker } = useHud();
  // Synced server time, so the HUD agrees with the Pi even if this device's clock is off.
  const now = $derived(store.toServer(ticker.now));
</script>

<Panel id="clock" title="Clock" {collapsible} collapsed={false}>
  <p class="time mono">{formatClock(now)}</p>
  <p class="sub">{formatDay(now)}</p>
</Panel>

<style>
  .time {
    margin: 0;
    font-size: 2rem;
    line-height: 1;
  }
  .sub {
    margin: 4px 0 0;
  }
</style>
