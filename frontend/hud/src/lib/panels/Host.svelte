<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { safeText } from '../format/text';
  import { linkBadge, powerBadge, serviceBadge } from './hostview';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store } = useHud();
  const host = $derived(store.host);
  const power = $derived(powerBadge(host));
  const link = $derived(linkBadge(host));
  const service = $derived(serviceBadge(host));
</script>

<Panel id="host" title="Core host" {collapsible} collapsed={collapsible}>
  {#if host}
    <ul class="list">
      <li><span class="chip" data-tone={power.tone}><span aria-hidden="true">{power.glyph}</span> {power.text}</span></li>
      <li><span class="chip" data-tone={link.tone}><span aria-hidden="true">{link.glyph}</span> {link.text}</span></li>
      {#if service}
        <li><span class="chip" data-tone={service.tone}><span aria-hidden="true">{service.glyph}</span> {service.text}</span></li>
      {/if}
      {#if host.llm.model_loaded}<li class="sub truncate">Model: {safeText(host.llm.model_loaded, 100)}</li>{/if}
    </ul>
  {:else}
    <p class="empty">Waiting for host status.</p>
  {/if}
</Panel>
