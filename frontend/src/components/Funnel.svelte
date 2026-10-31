<script lang="ts">
  import type { FunnelStep } from '../lib/types'

  let { steps, mode = 'all' }: { steps: FunnelStep[]; mode?: string } = $props()
  const max = $derived(Math.max(1, ...steps.map((s) => s.count)))
</script>

<div class="funnel">
  {#if mode !== 'all'}
    <p class="muted small">Each top-level condition on its own:</p>
  {/if}
  {#each steps as s, i (i)}
    <div class="step" class:first={i === 0}>
      <div class="label small">{s.text}</div>
      <div class="bar-wrap">
        <div class="bar" class:zero={s.count === 0} style:width={`${Math.max(2, (s.count / max) * 100)}%`}></div>
        <span class="count">{s.count}</span>
      </div>
    </div>
  {/each}
</div>

<style>
  .funnel {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .label {
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }
  .bar-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .bar {
    height: 10px;
    border-radius: 5px;
    background: var(--accent);
    transition: width 0.3s;
  }
  .step.first .bar {
    background: var(--text-muted);
  }
  .bar.zero {
    background: var(--danger);
  }
  .count {
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    min-width: 2ch;
  }
</style>
