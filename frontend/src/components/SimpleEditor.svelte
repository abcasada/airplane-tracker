<script lang="ts">
  import { untrack } from 'svelte'
  import { catalog, fieldAcceptsPlace, opLabel, placesFor, simpleOptions } from '../lib/catalog.svelte'
  import {
    emptyForm,
    followPlace,
    fromTree,
    rowIsEmpty,
    rowWarning,
    sectionOf,
    setRangeEnd,
    toTree,
    type SimpleRow,
  } from '../lib/simple'
  import type { FieldDef, Group } from '../lib/types'
  import ChipInput from './ChipInput.svelte'

  let {
    group,
    funnel = {},
  }: {
    group: Group
    funnel?: Record<string, number>
  } = $props()

  // Read the tree once; from here on the rows write it. The parent re-mounts this on a new tree.
  const opts = simpleOptions()
  const initial = untrack(() => fromTree($state.snapshot(group) as Group, opts))
  let form = $state(initial.ok ? initial.form : emptyForm(opts))
  let help = $state<string | null>(null)

  function sync() {
    const next = toTree($state.snapshot(form) as SimpleRow[], $state.snapshot(group) as Group)
    group.mode = next.mode
    group.children = next.children
  }

  function setValues(row: SimpleRow, values: string[]) {
    row.values = values
    sync()
  }

  function toggleChoice(row: SimpleRow, v: string) {
    setValues(row, row.values.includes(v) ? row.values.filter((x) => x !== v) : [...row.values, v])
  }

  function setFlag(row: SimpleRow, flag: boolean | null) {
    row.flag = flag
    sync()
  }

  function setEnd(row: SimpleRow, end: 'min' | 'max', raw: string) {
    setRangeEnd(row, end, raw.trim() === '' ? null : Number(raw))
    sync()
  }

  function setPlace(row: SimpleRow, raw: string) {
    row.place = raw === '' ? null : Number(raw)
    followPlace(form, row, fieldAcceptsPlace)
    sync()
  }

  const SECTION_TITLES = {
    where: 'At or headed to your places',
    what: 'Which aircraft',
  } as const

  function clearRow(row: SimpleRow) {
    row.values = []
    row.flag = null
    row.min = row.max = null
    row.minStrict = row.maxStrict = false
    sync()
  }

  const filled = $derived(form.filter((r) => !rowIsEmpty(r)).length)
</script>

<p class="muted small intro">
  Fill in only what you care about; empty rows are ignored. An aircraft must match every filled row.
  {#if filled === 0}<strong>With nothing filled in, the filter never matches.</strong>{/if}
</p>

{#snippet placePicker(row: SimpleRow, fd: FieldDef, word: string)}
  {@const places = placesFor(fd)}
  <span class="muted small">{word}</span>
  <select
    class="place"
    value={row.place ?? ''}
    onchange={(e) => setPlace(row, (e.currentTarget as HTMLSelectElement).value)}
    aria-label={`${fd.label}: place`}
  >
    {#if fd.any_viewpoint}<option value={0}>Any active viewpoint</option>{/if}
    {#if places.length === 0 && !fd.any_viewpoint}<option value="">No suitable place yet</option>{/if}
    {#each places as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
  </select>
{/snippet}

<div class="rows">
  {#each form as row, i (row.field)}
    {@const fd = catalog.byKey[row.field]}
    {@const section = sectionOf(row.field)}
    {#if section && (i === 0 || sectionOf(form[i - 1].field) !== section)}
      <h4 class="section">{SECTION_TITLES[section]}</h4>
    {/if}
    {#if fd}
      {@const warn = rowWarning(row)}
      <div class="srow" class:filled={!rowIsEmpty(row)}>
        <div class="label">
          <span class="name">{fd.label}</span>
          {#if !rowIsEmpty(row) && row.id in funnel}
            <span class="badge" title="Aircraft still matching after this row">{funnel[row.id]}</span>
          {/if}
          <button type="button" class="icon ghost" title="What is this?" onclick={() => (help = help === row.field ? null : row.field)}>ⓘ</button>
          {#if !rowIsEmpty(row)}
            <button type="button" class="icon ghost" title="Clear" aria-label={`Clear ${fd.label}`} onclick={() => clearRow(row)}>✕</button>
          {/if}
        </div>

        <div class="control">
          {#if row.kind === 'match' || row.kind === 'contains'}
            <ChipInput
              bind:values={() => row.values, (v) => setValues(row, v)}
              suggest={fd.suggest}
              upper={row.kind === 'match'}
              placeholder={row.kind === 'match' ? 'Any (wildcards * and ? work)' : 'Any (part of a name)'}
            />
          {:else if row.kind === 'choice'}
            <div class="choices">
              {#each fd.enum as e (e.value)}
                <button type="button" class="small" class:selected={row.values.includes(e.value)} onclick={() => toggleChoice(row, e.value)}>
                  {e.label}
                </button>
              {/each}
            </div>
          {:else if row.kind === 'flag'}
            <div class="inline">
              <div class="segmented" role="group" aria-label={fd.label}>
                <button type="button" class:on={row.flag === null} onclick={() => setFlag(row, null)}>Any</button>
                <button type="button" class:on={row.flag === true} onclick={() => setFlag(row, true)}>Yes</button>
                <button type="button" class:on={row.flag === false} onclick={() => setFlag(row, false)}>No</button>
              </div>
              {#if fd.place}{@render placePicker(row, fd, 'at')}{/if}
            </div>
          {:else}
            <div class="inline">
              <span class="op muted" title={opLabel(fd, row.minStrict ? 'gt' : 'ge')}>{row.minStrict ? '>' : '≥'}</span>
              <input
                type="number"
                step="any"
                inputmode="decimal"
                placeholder="min"
                value={row.min ?? ''}
                onchange={(e) => setEnd(row, 'min', (e.currentTarget as HTMLInputElement).value)}
                aria-label={`${fd.label} minimum`}
              />
              <span class="op muted" title={opLabel(fd, row.maxStrict ? 'lt' : 'le')}>{row.maxStrict ? '<' : '≤'}</span>
              <input
                type="number"
                step="any"
                inputmode="decimal"
                placeholder="max"
                value={row.max ?? ''}
                onchange={(e) => setEnd(row, 'max', (e.currentTarget as HTMLInputElement).value)}
                aria-label={`${fd.label} maximum`}
              />
              {#if fd.unit}<span class="muted">{fd.unit}</span>{/if}
              {#if fd.place}{@render placePicker(row, fd, row.field === 'distance_nm' ? 'from' : 'at')}{/if}
            </div>
          {/if}
          {#if warn}<p class="warn small">⚠ {warn}</p>{/if}
          {#if help === row.field}<p class="muted small help">{fd.help}</p>{/if}
        </div>
      </div>
    {/if}
  {/each}
</div>

<style>
  .intro {
    margin: 0 0 10px;
  }
  .rows {
    display: flex;
    flex-direction: column;
  }
  .section {
    margin: 14px 0 2px;
    font-size: 0.85em;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
  }
  .section:first-child {
    margin-top: 0;
  }
  .srow {
    display: grid;
    grid-template-columns: minmax(150px, 190px) 1fr;
    gap: 6px 12px;
    align-items: start;
    padding: 8px 0;
    border-top: 1px solid var(--border);
  }
  .srow:first-child,
  .section + .srow {
    border-top: 0;
  }
  .label {
    display: flex;
    align-items: center;
    gap: 2px;
    min-height: 38px;
  }
  .name {
    font-weight: 500;
    margin-right: 4px;
  }
  .srow.filled .name {
    font-weight: 700;
  }
  .label .icon {
    min-height: 28px;
    padding: 0 6px;
  }
  .control {
    min-width: 0;
  }
  .choices {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .choices button.selected {
    background: var(--accent);
    color: var(--accent-contrast);
    border-color: var(--accent);
  }
  .segmented {
    display: inline-flex;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }
  .segmented button {
    border: 0;
    border-radius: 0;
    min-height: 36px;
    min-width: 64px;
    background: transparent;
  }
  .segmented button.on {
    background: var(--accent);
    color: var(--accent-contrast);
  }
  .inline {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .inline input {
    width: 7.5em;
  }
  .op {
    font-weight: 600;
  }
  .place {
    width: auto;
    flex: 1 1 160px;
  }
  .warn {
    color: var(--warn);
    margin: 4px 0 0;
  }
  .help {
    margin: 4px 0 0;
  }
  @media (max-width: 600px) {
    .srow {
      grid-template-columns: 1fr;
      gap: 2px;
    }
    .label {
      min-height: 30px;
    }
  }
</style>
