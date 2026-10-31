<script lang="ts">
  import type { ExplainNode } from '../lib/types'
  import ExplainTree from './ExplainTree.svelte'

  let { node, root = true }: { node: ExplainNode; root?: boolean } = $props()

  const MODE = { all: 'All of', any: 'Any of', none: 'None of' } as Record<string, string>

  function mark(r: boolean | null): string {
    return r === true ? '✓' : r === false ? '✗' : '–'
  }

  function show(v: unknown): string {
    if (v === null || v === undefined) return 'unknown'
    if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(2)
    if (typeof v === 'boolean') return v ? 'yes' : 'no'
    if (Array.isArray(v)) return v.length ? v.join(', ') : 'none'
    return String(v)
  }
</script>

{#if node.type === 'group'}
  <div class="group" class:root>
    <div class="line">
      <span class="mark" class:pass={node.result === true} class:fail={node.result === false}>{mark(node.result)}</span>
      <strong>{MODE[node.mode ?? 'all']}</strong>
      {#if !node.enabled}<span class="badge">off</span>{/if}
    </div>
    <div class="kids">
      {#each node.children ?? [] as child, i (child.id || i)}
        <ExplainTree node={child} root={false} />
      {/each}
    </div>
  </div>
{:else}
  <div class="line cond" class:disabled={!node.enabled}>
    <span class="mark" class:pass={node.result === true} class:fail={node.result === false}>{mark(node.result)}</span>
    <span class="grow">{node.text}</span>
    <span class="value mono small" title="Actual value">{show(node.value)}</span>
  </div>
{/if}

<style>
  .group.root {
    font-size: 0.92rem;
  }
  .kids {
    margin-left: 14px;
    padding-left: 8px;
    border-left: 1px dashed var(--border);
  }
  .line {
    display: flex;
    gap: 8px;
    align-items: baseline;
    padding: 3px 0;
  }
  .cond.disabled {
    opacity: 0.55;
  }
  .mark {
    width: 1.1em;
    text-align: center;
    font-weight: 700;
    color: var(--text-muted);
  }
  .mark.pass {
    color: var(--ok);
  }
  .mark.fail {
    color: var(--danger);
  }
  .value {
    color: var(--text-muted);
    background: var(--surface-2);
    border-radius: 4px;
    padding: 0 6px;
    max-width: 45%;
    overflow-wrap: anywhere;
  }
</style>
