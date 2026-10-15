<script lang="ts">
  import { emptyGroup, newId, type Condition, type Group } from '../lib/types'
  import ConditionRow from './ConditionRow.svelte'
  import GroupEditor from './GroupEditor.svelte'

  let {
    group,
    depth = 0,
    onremove,
    funnel = {},
  }: {
    group: Group
    depth?: number
    onremove?: () => void
    funnel?: Record<string, number>
  } = $props()

  const MODE_TEXT = {
    all: 'All of these',
    any: 'Any of these',
    none: 'None of these',
  }

  function addCondition() {
    const c: Condition = {
      type: 'cond',
      id: newId('c'),
      field: '',
      op: 'in',
      value: [],
      place: null,
      enabled: true,
    }
    group.children.push(c)
  }

  function addGroup() {
    const g = emptyGroup(group.mode === 'any' ? 'all' : 'any')
    group.children.push(g)
  }

  function remove(i: number) {
    group.children.splice(i, 1)
  }

  function move(i: number, dir: -1 | 1) {
    const j = i + dir
    if (j < 0 || j >= group.children.length) return
    const [item] = group.children.splice(i, 1)
    group.children.splice(j, 0, item)
  }
</script>

<div class="group" class:nested={depth > 0} class:off={!group.enabled}>
  <div class="head">
    {#if depth > 0}
      <input type="checkbox" bind:checked={group.enabled} aria-label="Group enabled" />
    {/if}
    <select bind:value={group.mode} class="mode" aria-label="Group logic">
      {#each Object.entries(MODE_TEXT) as [k, label] (k)}<option value={k}>{label}</option>{/each}
    </select>
    <span class="muted small">
      {group.mode === 'all' ? 'must all be true' : group.mode === 'any' ? 'at least one must be true' : 'must all be false'}
    </span>
    {#if onremove}
      <button type="button" class="icon ghost danger head-remove" title="Remove group" onclick={onremove}>✕</button>
    {/if}
  </div>

  <div class="children">
    {#each group.children as child, i (child.id)}
      {#if child.type === 'cond'}
        <ConditionRow
          cond={child}
          onremove={() => remove(i)}
          onmove={(d) => move(i, d)}
          first={i === 0}
          last={i === group.children.length - 1}
          funnelCount={depth === 0 && group.mode === 'all' && child.id in funnel ? funnel[child.id] : null}
        />
      {:else}
        <div class="nested-wrap">
          <GroupEditor group={child} depth={depth + 1} onremove={() => remove(i)} {funnel} />
          <div class="nested-move">
            <button type="button" class="icon ghost" disabled={i === 0} onclick={() => move(i, -1)} title="Move group up">↑</button>
            <button type="button" class="icon ghost" disabled={i === group.children.length - 1} onclick={() => move(i, 1)} title="Move group down">↓</button>
          </div>
        </div>
      {/if}
    {:else}
      <p class="muted small empty-hint">No conditions yet. A filter with no conditions never matches.</p>
    {/each}
  </div>

  <div class="row add">
    <button type="button" class="small" onclick={addCondition}>+ Condition</button>
    {#if depth < 3}<button type="button" class="small" onclick={addGroup}>+ Group</button>{/if}
  </div>
</div>

<style>
  .group {
    border-left: 3px solid var(--accent);
    padding: 8px 0 8px 10px;
  }
  .group.nested {
    border-left-color: var(--mil);
    background: var(--surface-2);
    border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
    padding-right: 8px;
  }
  .group.off {
    opacity: 0.6;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }
  .mode {
    width: auto;
    font-weight: 600;
  }
  .head-remove {
    margin-left: auto;
  }
  .children {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .nested-wrap {
    position: relative;
  }
  .nested-move {
    position: absolute;
    top: 8px;
    right: 44px;
    display: flex;
  }
  .add {
    margin-top: 8px;
  }
  .empty-hint {
    margin: 0;
  }
</style>
