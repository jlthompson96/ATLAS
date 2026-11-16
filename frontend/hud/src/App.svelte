<script lang="ts">
  import { onMount, setContext } from 'svelte';
  import { HUD_CONTEXT } from './lib/ctx';
  import Shell from './lib/Shell.svelte';
  import { HudClient, defaultSocketUrl, type Logger } from './lib/protocol/client';
  import { HudStore } from './lib/state/hud.svelte';
  import { Settings } from './lib/state/settings.svelte';
  import { Ticker } from './lib/state/ticker.svelte';

  const store = new HudStore();
  const settings = new Settings();
  const ticker = new Ticker();
  setContext(HUD_CONTEXT, { store, settings, ticker });

  // Frames can carry private content: log only types and reasons, never payloads (CLAUDE.md).
  const logger: Logger = {
    info: () => {},
    warn: (msg) => console.warn(`[atlas] ${msg}`)
  };

  const client = new HudClient({
    url: () => defaultSocketUrl(),
    logger,
    onMessage: (m) => store.apply(m),
    onStatus: (s) => store.setConnection(s),
    onConnection: (open) => store.onConnection(open)
  });
  store.attach(client);

  onMount(() => {
    ticker.start();
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onMq = (): void => {
      settings.systemReducedMotion = mq.matches;
    };
    onMq();
    mq.addEventListener('change', onMq);
    const onResize = (): void => {
      settings.viewportWidth = window.innerWidth;
    };
    onResize();
    window.addEventListener('resize', onResize);
    client.connect();
    return () => {
      client.close();
      ticker.stop();
      mq.removeEventListener('change', onMq);
      window.removeEventListener('resize', onResize);
    };
  });
</script>

<Shell />
