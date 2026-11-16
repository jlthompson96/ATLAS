<script lang="ts">
  import Panel from '../ui/Panel.svelte';
  import { useHud } from '../ctx';
  import { safeText } from '../format/text';
  import { known } from './data';

  let { collapsible = false }: { collapsible?: boolean } = $props();
  const { store } = useHud();

  const STATUS = ['queued', 'running', 'blocked', 'done', 'failed', 'killed'] as const;
  const GLYPH = { queued: '◌', running: '◔', blocked: '■', done: '✓', failed: '✗', killed: '⊘' } as const;
  const TONE = { queued: 'muted', running: 'ok', blocked: 'warn', done: 'ok', failed: 'danger', killed: 'muted' } as const;
  const FLAGS: Record<string, string> = {
    low_source_coverage: 'LOW SOURCE COVERAGE',
    paused_window_end: 'PAUSED AT WINDOW END',
    citation_failures: 'CITATION FAILURES'
  };

  const tasks = $derived(store.tasks);
  const active = (s: string): boolean => s === 'queued' || s === 'running' || s === 'blocked';
  const anyActive = $derived(tasks.some((t) => active(t.status)));
  const pct = (used: number, limit: number): number => (limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0);
</script>

<Panel id="research" title="Research" sensitivity="personal" exposed={tasks.some((t) => !t.redacted)} status={tasks.length ? `${tasks.filter((t) => active(t.status)).length} active` : ''} {collapsible} collapsed={collapsible}>
  {#if tasks.length === 0}
    <p class="empty">No research tasks.</p>
  {:else}
    <ul class="list">
      {#each tasks as t (t.task_id)}
        {@const st = known(t.status, STATUS, 'queued')}
        {@const title = t.redacted ? 'Research task (title hidden)' : safeText(t.title, 120)}
        <li class="task">
          <div class="row">
            <span class="grow untrusted">{title}</span>
            <span class="chip" data-tone={TONE[st]}><span aria-hidden="true">{GLYPH[st]}</span> {st}</span>
          </div>
          <div class="sub row">
            <span>step {t.step}</span>
            <span class="mono">{pct(t.used.time_s, t.budget.time_s)}% of time budget</span>
          </div>
          <progress max="100" value={pct(t.used.time_s, t.budget.time_s)} aria-label={`Time budget used, ${title}`}></progress>
          <div class="meta">
            {#if t.unattended}<span class="flag"><span aria-hidden="true">◐</span> unattended</span>{/if}
            {#each t.flags ?? [] as f (f)}<span class="flag"><span aria-hidden="true">▲</span> {FLAGS[f] ?? 'FLAGGED'}</span>{/each}
            {#if active(st)}
              <button class="btn small danger" type="button" onclick={() => store.killTask(t.task_id)} aria-label={`Kill task: ${title}`}>Kill</button>
            {/if}
          </div>
        </li>
      {/each}
    </ul>
    {#if anyActive}
      <p class="all"><button class="btn small danger" type="button" onclick={() => store.killAll()}>Kill switch: stop all</button></p>
    {/if}
  {/if}
</Panel>

<style>
  .task {
    padding: 4px 0;
    border-bottom: 1px solid rgba(31, 209, 255, 0.12);
  }
  progress {
    width: 100%;
    height: 6px;
    accent-color: var(--accent);
  }
  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 6px;
    align-items: center;
    margin-top: 3px;
  }
  .meta .btn {
    margin-left: auto;
  }
  .all {
    margin: 8px 0 0;
  }
</style>
