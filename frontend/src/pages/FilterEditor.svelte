<script lang="ts">
  import { onMount } from 'svelte'
  import AircraftRow from '../components/AircraftRow.svelte'
  import Backtest from '../components/Backtest.svelte'
  import ExplainTree from '../components/ExplainTree.svelte'
  import Funnel from '../components/Funnel.svelte'
  import GroupEditor from '../components/GroupEditor.svelte'
  import Modal from '../components/Modal.svelte'
  import NotifyEditor from '../components/NotifyEditor.svelte'
  import { del, errorMessage, get, post, put } from '../lib/api'
  import { catalog, loadCatalog } from '../lib/catalog.svelte'
  import { fmtTime } from '../lib/format'
  import { navigate } from '../lib/router.svelte'
  import { toast } from '../lib/toast.svelte'
  import {
    blankFilter,
    type AircraftSummary,
    type Condition,
    type ExplainNode,
    type Filter,
    type FilterDefinition,
    type FilterIn,
    type Group,
    type PreviewResult,
  } from '../lib/types'

  let { id }: { id: string } = $props()

  let draft = $state<FilterIn | null>(null)
  let original = $state('')
  let saved = $state<Filter | null>(null)
  let loadError = $state('')
  let saving = $state(false)
  let note = $state('')

  let preview = $state<PreviewResult | null>(null)
  let previewBusy = $state(false)
  let inRange = $state<AircraftSummary[]>([])
  let whyHex = $state('')
  let why = $state<{ matches: boolean; explanation: ExplainNode; aircraft: AircraftSummary; errors: string[] } | null>(null)
  let tab = $state<'build' | 'preview'>('build')
  let history = $state<{ id: number; version: number; name: string; note: string; created_at: number; summary: string }[] | null>(null)

  const isNew = $derived(id === 'new')
  const dirty = $derived(draft !== null && JSON.stringify(draft) !== original)

  function clean(g: Group): Group {
    return {
      ...g,
      children: g.children
        .filter((c) => c.type === 'group' || (c as Condition).field)
        .map((c) => (c.type === 'group' ? clean(c) : c)),
    }
  }

  function cleanDefinition(d: FilterDefinition): FilterDefinition {
    return { ...d, root: clean(d.root) }
  }

  onMount(() => {
    init()
    const t = setInterval(() => draft && runPreview(), 10000)
    return () => clearInterval(t)
  })

  async function init() {
    try {
      await loadCatalog(true)
      if (isNew) {
        const stored = sessionStorage.getItem('filter-template')
        sessionStorage.removeItem('filter-template')
        draft = stored ? (JSON.parse(stored) as FilterIn) : blankFilter()
        if (!draft.definition.display_place_id) {
          draft.definition.display_place_id = catalog.places.find((p) => p.is_home)?.id ?? null
        }
        original = ''
      } else {
        saved = await get<Filter>(`/api/filters/${id}`)
        draft = {
          name: saved.name,
          description: saved.description,
          enabled: saved.enabled,
          mode: saved.mode,
          definition: saved.definition,
        }
        original = JSON.stringify(draft)
      }
    } catch (e) {
      loadError = errorMessage(e)
    }
  }

  let previewTimer: ReturnType<typeof setTimeout> | undefined
  $effect(() => {
    if (!draft) return
    JSON.stringify(draft.definition) // track deep changes
    clearTimeout(previewTimer)
    previewTimer = setTimeout(runPreview, 400)
  })

  async function runPreview() {
    if (!draft) return
    previewBusy = true
    try {
      const definition = cleanDefinition($state.snapshot(draft.definition) as FilterDefinition)
      const [p, ac] = await Promise.all([
        post<PreviewResult>('/api/filters/preview', { definition, filter_id: saved?.id ?? null }),
        get<{ aircraft: AircraftSummary[] }>('/api/aircraft'),
      ])
      preview = p
      inRange = ac.aircraft.sort((a, b) => (a.distance_nm ?? 1e9) - (b.distance_nm ?? 1e9))
      if (whyHex) await explain(whyHex, false)
    } catch (e) {
      preview = null
      toast(errorMessage(e), 'error')
    } finally {
      previewBusy = false
    }
  }

  async function explain(hex: string, switchTab = true) {
    if (!draft || !hex) {
      why = null
      return
    }
    whyHex = hex
    try {
      const definition = cleanDefinition($state.snapshot(draft.definition) as FilterDefinition)
      why = await post('/api/filters/explain', { definition, hex })
      if (switchTab) tab = 'preview'
    } catch (e) {
      why = null
      whyHex = ''
      // On background refresh the aircraft may simply have left range; only report user picks.
      if (switchTab) toast(errorMessage(e), 'error')
    }
  }

  const funnelById = $derived(
    Object.fromEntries((preview?.funnel ?? []).filter((s) => s.id).map((s) => [s.id as string, s.count])),
  )

  async function save() {
    if (!draft) return
    if (!draft.name.trim()) {
      toast('Give the filter a name first', 'error')
      return
    }
    saving = true
    try {
      const body = { ...$state.snapshot(draft), definition: cleanDefinition($state.snapshot(draft.definition) as FilterDefinition), note }
      const f = isNew ? await post<Filter>('/api/filters', body) : await put<Filter>(`/api/filters/${id}`, body)
      saved = f
      draft = { name: f.name, description: f.description, enabled: f.enabled, mode: f.mode, definition: f.definition }
      original = JSON.stringify(draft)
      note = ''
      toast(`Saved (version ${f.version})`)
      if (isNew) navigate(`/filters/${f.id}`)
    } catch (e) {
      toast(errorMessage(e), 'error')
    } finally {
      saving = false
    }
  }

  function discard() {
    if (!original) {
      navigate('/filters')
      return
    }
    draft = JSON.parse(original)
  }

  async function remove() {
    if (!saved || !confirm(`Delete filter "${saved.name}"?`)) return
    await del(`/api/filters/${saved.id}`)
    navigate('/filters')
  }

  async function openHistory() {
    if (!saved) return
    history = await get(`/api/filters/${saved.id}/versions`)
  }

  async function revert(versionId: number) {
    if (!saved) return
    const f = await post<Filter>(`/api/filters/${saved.id}/revert`, { version_id: versionId })
    saved = f
    draft = { name: f.name, description: f.description, enabled: f.enabled, mode: f.mode, definition: f.definition }
    original = JSON.stringify(draft)
    history = null
    toast(`Reverted (now version ${f.version})`)
  }
</script>

{#if loadError}
  <div class="page"><p class="error">{loadError}</p><a href="#/filters">← Filters</a></div>
{:else if draft}
  <div class="page editor">
    <div class="topbar card">
      <a href="#/filters" class="back small">← Filters</a>
      <input class="name" bind:value={draft.name} placeholder="Filter name" maxlength="80" aria-label="Filter name" />
      <div class="row controls">
        <div class="segmented" role="group" aria-label="Mode">
          <button class:on={draft.mode === 'live'} onclick={() => (draft!.mode = 'live')}>Live</button>
          <button class:on={draft.mode === 'shadow'} onclick={() => (draft!.mode = 'shadow')} title="Log matches without notifying">Shadow</button>
        </div>
        <label class="check small"><input type="checkbox" bind:checked={draft.enabled} /> Enabled</label>
        {#if saved}<span class="muted small">v{saved.version}</span>{/if}
        {#if dirty}<span class="badge accent">Unsaved</span>{/if}
        <button onclick={discard} disabled={!dirty && !isNew}>{isNew ? 'Cancel' : 'Discard'}</button>
        <button class="primary" onclick={save} disabled={saving || (!dirty && !isNew)}>{saving ? 'Saving…' : 'Save'}</button>
      </div>
      {#if dirty && !isNew}
        <input class="note small" bind:value={note} placeholder="What changed? (optional, saved in history)" maxlength="200" />
      {/if}
    </div>

    <div class="summary card">
      <span class="muted small">This filter matches:</span>
      <p class="sentence">{preview?.summary ?? '…'}</p>
      {#if preview?.errors.length}
        <ul class="errors">{#each preview.errors as e (e)}<li class="error small">⚠ {e}</li>{/each}</ul>
      {/if}
    </div>

    <div class="tabs">
      <button class:on={tab === 'build'} onclick={() => (tab = 'build')}>Build</button>
      <button class:on={tab === 'preview'} onclick={() => (tab = 'preview')}>
        Preview {#if preview}<span class="badge accent">{preview.match_count}</span>{/if}
      </button>
    </div>

    <div class="cols">
      <div class="col build" class:hide={tab !== 'build'}>
        <section class="card">
          <h2>Conditions</h2>
          <GroupEditor group={draft.definition.root} funnel={funnelById} />
        </section>
        <section class="card">
          <h2>Notifications</h2>
          <NotifyEditor bind:definition={draft.definition} />
        </section>
        <section class="card">
          <h2>What if?</h2>
          <Backtest definition={draft.definition} filterId={saved?.id ?? null} mode={draft.mode} dirty={dirty || isNew} />
        </section>
        <section class="card stack">
          <label class="field">
            Notes for yourself
            <textarea rows="2" bind:value={draft.description} maxlength="500"></textarea>
          </label>
          {#if saved}
            <div class="row">
              <button class="small" onclick={openHistory}>Version history</button>
              <a class="btn small" href={`#/alerts?filter=${saved.id}`}>This filter's alerts</a>
              <button class="small danger" onclick={remove}>Delete filter</button>
            </div>
          {/if}
        </section>
      </div>

      <div class="col preview" class:hide={tab !== 'preview'}>
        <section class="card sticky">
          <div class="row between">
            <h2>Live preview</h2>
            <button class="small ghost" onclick={runPreview} disabled={previewBusy}>{previewBusy ? '…' : '↻'}</button>
          </div>
          {#if preview}
            <p class="big">
              <strong>{preview.match_count}</strong> of {preview.in_range} aircraft in range match right now
              {#if preview.excluded}<span class="muted small">({preview.excluded} muted)</span>{/if}
            </p>
            {#if preview.funnel.length > 1}
              <Funnel steps={preview.funnel} mode={draft.definition.root.mode} />
            {/if}
            <h3 class="sub">Matching now</h3>
            <div class="list">
              {#each preview.matches as a (a.hex)}
                <AircraftRow {a} onclick={() => explain(a.hex)} />
              {:else}
                <p class="muted small">Nothing matches at the moment.</p>
              {/each}
            </div>
          {:else}
            <p class="muted">Loading…</p>
          {/if}

          <h3 class="sub">Why (not)?</h3>
          <select bind:value={whyHex} onchange={() => explain(whyHex, true)} aria-label="Pick an aircraft to explain">
            <option value="">Pick any aircraft in range…</option>
            {#each inRange as a (a.hex)}
              <option value={a.hex}>
                {a.callsign || a.registration || a.hex} · {a.type_code ?? '?'}{a.distance_nm != null ? ` · ${a.distance_nm.toFixed(0)} nm` : ''}{a.military ? ' · MIL' : ''}
              </option>
            {/each}
          </select>
          {#if why}
            <div class="why">
              <p>
                <strong>{why.aircraft.callsign || why.aircraft.hex}</strong>
                {#if why.matches}<span class="badge ok">matches</span>{:else}<span class="badge danger">doesn't match</span>{/if}
                <a class="small" href={`#/aircraft/${why.aircraft.hex}`}>details</a>
              </p>
              <ExplainTree node={why.explanation} />
            </div>
          {/if}
        </section>
      </div>
    </div>
  </div>
{:else}
  <div class="page muted">Loading…</div>
{/if}

{#if history}
  <Modal title="Version history" onclose={() => (history = null)}>
    <div class="stack">
      {#each history as v (v.id)}
        <div class="version">
          <div class="row between">
            <strong>v{v.version} · {v.name}</strong>
            {#if saved && v.id !== saved.version_id}
              <button class="small" onclick={() => revert(v.id)}>Restore</button>
            {:else}
              <span class="badge accent">current</span>
            {/if}
          </div>
          <div class="muted small">{fmtTime(v.created_at)}{v.note ? ` · ${v.note}` : ''}</div>
          <div class="small">{v.summary}</div>
        </div>
      {/each}
    </div>
  </Modal>
{/if}

<style>
  .editor {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .topbar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    position: sticky;
    top: calc(var(--nav-h) + 4px);
    z-index: 40;
  }
  .back {
    text-decoration: none;
  }
  .name {
    flex: 1 1 220px;
    font-size: 1.1rem;
    font-weight: 700;
  }
  .controls {
    margin-left: auto;
  }
  .note {
    flex-basis: 100%;
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
    min-height: 34px;
    background: transparent;
  }
  .segmented button.on {
    background: var(--accent);
    color: var(--accent-contrast);
  }
  .sentence {
    margin: 4px 0 0;
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .errors {
    margin: 6px 0 0;
    padding-left: 18px;
  }
  .tabs {
    display: flex;
    gap: 4px;
  }
  .tabs button {
    flex: 1;
  }
  .tabs button.on {
    background: var(--accent-soft);
    color: var(--accent);
    border-color: var(--accent);
  }
  .cols {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .col {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  .col.hide {
    display: none;
  }
  .card + .card {
    margin-top: 0;
  }
  .big {
    font-size: 1.05rem;
  }
  .sub {
    margin-top: 16px;
    font-size: 0.85rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
  }
  .list {
    max-height: 340px;
    overflow: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }
  .why {
    margin-top: 10px;
  }
  .version {
    border-bottom: 1px solid var(--border);
    padding-bottom: 8px;
  }
  @media (min-width: 1000px) {
    .tabs {
      display: none;
    }
    .cols {
      flex-direction: row;
      align-items: flex-start;
    }
    .col.build {
      flex: 3;
    }
    .col.preview {
      flex: 2;
    }
    .col.hide {
      display: flex;
    }
    .sticky {
      position: sticky;
      top: calc(var(--nav-h) + 90px);
      max-height: calc(100vh - var(--nav-h) - 110px);
      overflow: auto;
    }
  }
</style>
