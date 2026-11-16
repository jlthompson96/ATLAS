<script lang="ts">
  import { useHud } from '../ctx';

  const { store } = useHud();
</script>

<!-- Screen-reader announcements for state changes and alerts (A11Y: ARIA live region). -->
<div class="sr-only" role="status" aria-live="polite" aria-atomic="true" data-testid="live-polite">{store.announcement}</div>
<div class="sr-only" role="alert" aria-live="assertive" aria-atomic="true" data-testid="live-assertive">{store.urgent}</div>
