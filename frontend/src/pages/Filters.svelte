<script lang="ts">
  import { onMount } from 'svelte'
  import Modal from '../components/Modal.svelte'
  import SetupChecklist from '../components/SetupChecklist.svelte'
  import { del, errorMessage, get, patch, post } from '../lib/api'
  import { catalog, loadCatalog } from '../lib/catalog.svelte'
  import { fmtAgo } from '../lib/format'
  import { navigate } from '../lib/router.svelte'
  import { TEMPLATES, type Template } from '../lib/templates'
  import { toast } from '../lib/toast.svelte'
  import type { Filter } from '../lib/types'

  let filters = $state<Filter[]>([])
  let loaded = $state(false)
  let choosing = $state(false)
  let importInput: HTMLInputElement | undefined = $state()

  async function load() {
    filters = await get<Filter[]>('/api/filters')
    loaded = true
  }

  onMount(() => {
    load()
    loadCatalog()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  })

  const home = $derived(catalog.places.find((p) => p.is_home))
  const area = $derived(catalog.places.find((p) => p.role === 'area'))

  function available(t: Template): boolean {
    if (t.needs === 'home') return !!home
    if (t.needs === 'area') return !!area
    return true
  }

  function useTemplate(t: Template) {
    sessionStorage.setItem('filter-template', JSON.stringify(t.build(home, area)))
    choosing = false
    navigate('/filters/new')
  }

  async function toggle(f: Filter) {
    try {
      await patch(`/api/filters/${f.id}`, { enabled: !f.enabled })
      await load()
    } catch (e) {
      toast(errorMessage(e), 'error')
    }
  }

  async function setMode(f: Filter, mode: 'live' | 'shadow') {
    await patch(`/api/filters/${f.id}`, { mode })
    toast(mode === 'shadow' ? 'Shadow mode: logs matches without notifying' : 'Live: will notify')
    await load()
  }

  async function duplicate(f: Filter) {
    const copy = await post<Filter>(`/api/filters/${f.id}/duplicate`)
    toast('Copied (in shadow mode)')
    navigate(`/filters/${copy.id}`)
  }

  async function remove(f: Filter) {
    if (!confirm(`Delete filter "${f.name}"? Its alert history is kept.`)) return
    await del(`/api/filters/${f.id}`)
    await load()
  }

  async function move(i: number, dir: -1 | 1) {
    const ids = filters.map((f) => f.id)
    const j = i + dir
    if (j < 0 || j >= ids.length) return
    ;[ids[i], ids[j]] = [ids[j], ids[i]]
    await post('/api/filters/reorder', { ids })
    await load()
  }

  async function exportAll() {
    const data = await get<unknown>('/api/filters/export')
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `airplane-tracker-filters-${new Date().toISOString().slice(0, 10)}.json`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  async function importFile(e: Event) {
    const file = (e.currentTarget as HTMLInputElement).files?.[0]
    if (!file) return
    try {
      const data = JSON.parse(await file.text())
      const r = await post<{ imported: number }>('/api/filters/import', { filters: data.filters ?? data })
      toast(`Imported ${r.imported} filter(s) in shadow mode`)
      await load()
    } catch (err) {
      toast(`Import failed: ${errorMessage(err)}`, 'error')
    }
  }
</script>

<div class="page stack">
  <SetupChecklist />
  <div class="row between">
    <h1>Filters</h1>
    <div class="row">
      <button class="small" onclick={exportAll}>Export</button>
      <button class="small" onclick={() => importInput?.click()}>Import</button>
      <input type="file" accept="application/json" hidden bind:this={importInput} onchange={importFile} />
      <button class="primary" onclick={() => (choosing = true)}>+ New filter</button>
    </div>
  </div>
  <p class="muted small">
    Filters decide what reaches your phone. Each one notifies once per aircraft while it keeps matching.
    <strong>Shadow</strong> filters log matches without notifying, so you can try ideas safely.
  </p>

  {#if loaded && filters.length === 0}
    <div class="card empty">No filters yet. Start from a template.</div>
  {/if}

  {#each filters as f, i (f.id)}
    <div class="card filter" class:disabled={!f.enabled}>
      <div class="row between">
        <a class="name" href={`#/filters/${f.id}`}>{f.name}</a>
        <div class="row">
          <span class="badge" class:accent={f.mode === 'live'}>{f.mode === 'live' ? 'Live' : 'Shadow'}</span>
          <label class="switch" title={f.enabled ? 'Enabled' : 'Disabled'}>
            <input type="checkbox" checked={f.enabled} onchange={() => toggle(f)} />
            <span>{f.enabled ? 'On' : 'Off'}</span>
          </label>
        </div>
      </div>
      <p class="summary small">{f.summary}</p>
      {#if f.errors.length}<p class="error small">⚠ {f.errors.join('; ')}</p>{/if}
      <div class="row between">
        <div class="row stats small muted">
          <span><strong>{f.matching_now}</strong> matching now</span>
          <span><strong>{f.alerts_today}</strong> today ({f.notified_today} notified)</span>
          {#if f.last_alert_at}<span>last {fmtAgo(f.last_alert_at)}</span>{/if}
          {#if f.stats_30d.alerts}
            <span title="Last 30 days. Rate alerts with 👍/👎 on the Alerts page or from the notification.">
              {f.stats_30d.alerts} in 30 days{#if f.stats_30d.up || f.stats_30d.down}
                · 👍 {f.stats_30d.up} 👎 {f.stats_30d.down}{/if}
            </span>
          {/if}
        </div>
        <div class="row actions">
          <button class="icon ghost" title="Move up" disabled={i === 0} onclick={() => move(i, -1)}>↑</button>
          <button class="icon ghost" title="Move down" disabled={i === filters.length - 1} onclick={() => move(i, 1)}>↓</button>
          <a class="btn small" href={`#/alerts?filter=${f.id}`}>Alerts</a>
          <button class="small" onclick={() => setMode(f, f.mode === 'live' ? 'shadow' : 'live')}>
            {f.mode === 'live' ? 'Shadow' : 'Go live'}
          </button>
          <button class="small" onclick={() => duplicate(f)}>Copy</button>
          <button class="small danger" onclick={() => remove(f)}>Delete</button>
        </div>
      </div>
    </div>
  {/each}
</div>

{#if choosing}
  <Modal title="New filter" onclose={() => (choosing = false)}>
    <div class="templates">
      {#each TEMPLATES as t (t.key)}
        <button class="template" disabled={!available(t)} onclick={() => useTemplate(t)}>
          <strong>{t.title}</strong>
          <span class="muted small">{t.description}</span>
          {#if !available(t)}
            <span class="small error">Needs {t.needs === 'home' ? 'a home place' : 'an area place'} first</span>
          {/if}
        </button>
      {/each}
    </div>
  </Modal>
{/if}

<style>
  .filter.disabled {
    opacity: 0.65;
  }
  .name {
    font-weight: 700;
    font-size: 1.05rem;
    color: var(--text);
    text-decoration: none;
  }
  .summary {
    margin: 6px 0;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }
  .stats {
    gap: 14px;
  }
  .actions {
    gap: 4px;
  }
  .switch {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 0.85rem;
  }
  .templates {
    display: grid;
    gap: 8px;
  }
  .template {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    white-space: normal;
    text-align: left;
    padding: 10px 12px;
  }
</style>
