<script lang="ts">
  import {
    catalog,
    defaultOp,
    fieldGroups,
    LIST_VALUE_OPS,
    NO_VALUE_OPS,
    opLabel,
    placesFor,
  } from '../lib/catalog.svelte'
  import type { Condition, FieldDef } from '../lib/types'
  import ChipInput from './ChipInput.svelte'

  let {
    cond,
    onremove,
    onmove,
    first = false,
    last = false,
    funnelCount = null,
  }: {
    cond: Condition
    onremove: () => void
    onmove: (dir: -1 | 1) => void
    first?: boolean
    last?: boolean
    funnelCount?: number | null
  } = $props()

  let showHelp = $state(false)
  const fd = $derived<FieldDef | undefined>(catalog.byKey[cond.field])
  const places = $derived(fd?.place ? placesFor(fd) : [])

  function defaultValue(f: FieldDef, op: string): unknown {
    if (NO_VALUE_OPS.has(op)) return null
    if (op === 'between') return [0, 0]
    if (LIST_VALUE_OPS.has(op)) return []
    if (f.type === 'number') return 0
    return ''
  }

  function setField(key: string) {
    const f = catalog.byKey[key]
    cond.field = key
    if (!f) return
    cond.op = defaultOp(f)
    cond.value = defaultValue(f, cond.op)
    if (f.place) {
      const options = placesFor(f)
      const home = options.find((p) => p.is_home)
      cond.place = (home ?? options[0])?.id ?? null
    } else {
      cond.place = null
    }
  }

  function setOp(op: string) {
    const prev = cond.op
    cond.op = op
    if (!fd) return
    const wasList = LIST_VALUE_OPS.has(prev)
    const isList = LIST_VALUE_OPS.has(op)
    if (NO_VALUE_OPS.has(op) || op === 'between' || wasList !== isList || NO_VALUE_OPS.has(prev) || prev === 'between') {
      cond.value = defaultValue(fd, op)
    }
  }

  function listValue(): string[] {
    return Array.isArray(cond.value) ? (cond.value as unknown[]).map(String) : []
  }

  function toggleEnum(v: string) {
    const cur = listValue()
    cond.value = cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v]
  }

  function between(i: 0 | 1, raw: string) {
    const arr = Array.isArray(cond.value) ? [...(cond.value as number[])] : [0, 0]
    arr[i] = raw === '' ? 0 : Number(raw)
    cond.value = arr
  }

  const chipUpper = $derived(
    !!fd && ['type_code', 'registration', 'callsign', 'airline_code', 'hex', 'squawk'].includes(fd.key),
  )
</script>

<div class="cond" class:off={!cond.enabled}>
  <div class="line">
    <input
      type="checkbox"
      bind:checked={cond.enabled}
      title={cond.enabled ? 'Condition on' : 'Condition off (ignored)'}
      aria-label="Condition enabled"
    />
    <select
      class="field-select"
      value={cond.field}
      onchange={(e) => setField((e.currentTarget as HTMLSelectElement).value)}
      aria-label="Field"
    >
      {#if !cond.field}<option value="">Choose what to test…</option>{/if}
      {#each fieldGroups() as [group, fields] (group)}
        <optgroup label={group}>
          {#each fields as f (f.key)}<option value={f.key}>{f.label}</option>{/each}
        </optgroup>
      {/each}
    </select>

    {#if fd?.place}
      <select bind:value={cond.place} aria-label="Place" class="place-select">
        {#if places.length === 0}<option value={null}>No suitable place yet</option>{/if}
        {#each places as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
      </select>
    {/if}

    {#if fd}
      <select value={cond.op} onchange={(e) => setOp((e.currentTarget as HTMLSelectElement).value)} aria-label="Test">
        {#each fd.ops as op (op)}<option value={op}>{opLabel(fd, op)}</option>{/each}
      </select>
    {/if}

    <div class="tools">
      {#if funnelCount !== null}<span class="badge" title="Aircraft still matching after this condition">{funnelCount}</span>{/if}
      {#if fd}<button type="button" class="icon ghost" title="What is this?" onclick={() => (showHelp = !showHelp)}>ⓘ</button>{/if}
      <button type="button" class="icon ghost" title="Move up" disabled={first} onclick={() => onmove(-1)}>↑</button>
      <button type="button" class="icon ghost" title="Move down" disabled={last} onclick={() => onmove(1)}>↓</button>
      <button type="button" class="icon ghost danger" title="Remove" onclick={onremove}>✕</button>
    </div>
  </div>

  {#if fd && !NO_VALUE_OPS.has(cond.op)}
    <div class="value">
      {#if cond.op === 'between'}
        <div class="row">
          <input type="number" step="any" value={(cond.value as number[])?.[0] ?? 0} oninput={(e) => between(0, (e.currentTarget as HTMLInputElement).value)} aria-label="From" />
          <span class="muted">and</span>
          <input type="number" step="any" value={(cond.value as number[])?.[1] ?? 0} oninput={(e) => between(1, (e.currentTarget as HTMLInputElement).value)} aria-label="To" />
          {#if fd.unit}<span class="muted">{fd.unit}</span>{/if}
        </div>
      {:else if fd.type === 'number'}
        <div class="row">
          <input type="number" step="any" bind:value={cond.value} aria-label="Value" />
          {#if fd.unit}<span class="muted">{fd.unit}</span>{/if}
        </div>
      {:else if fd.enum.length && LIST_VALUE_OPS.has(cond.op)}
        <div class="enum">
          {#each fd.enum as e (e.value)}
            <button
              type="button"
              class="small"
              class:selected={listValue().includes(e.value)}
              onclick={() => toggleEnum(e.value)}
            >{e.label}</button>
          {/each}
        </div>
      {:else if LIST_VALUE_OPS.has(cond.op)}
        <ChipInput
          bind:values={() => listValue(), (v) => (cond.value = v)}
          suggest={fd.suggest}
          upper={chipUpper}
          placeholder={cond.op.includes('glob') ? 'Patterns like B7* or *-0001' : 'Type, then Enter'}
        />
      {:else}
        <input bind:value={cond.value} placeholder={cond.op === 'regex' ? 'e.g. ^(RCH|REACH)\\d+' : ''} aria-label="Value" />
      {/if}
    </div>
  {/if}

  {#if showHelp && fd}
    <p class="help muted small">{fd.help}</p>
  {/if}
</div>

<style>
  .cond {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--surface);
    padding: 8px;
  }
  .cond.off {
    opacity: 0.55;
  }
  .line {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }
  .line > select {
    width: auto;
    flex: 1 1 140px;
  }
  .field-select {
    flex: 2 1 200px !important;
    font-weight: 600;
  }
  .tools {
    display: flex;
    gap: 2px;
    align-items: center;
    margin-left: auto;
  }
  .value {
    margin-top: 6px;
    padding-left: 26px;
  }
  .value input[type='number'] {
    max-width: 140px;
  }
  .enum {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .enum button.selected {
    background: var(--accent);
    color: var(--accent-contrast);
    border-color: var(--accent);
  }
  .help {
    margin: 6px 0 0 26px;
  }
  @media (max-width: 600px) {
    .value {
      padding-left: 0;
    }
    .help {
      margin-left: 0;
    }
  }
</style>
