<script lang="ts">
  import { onMount, untrack } from 'svelte'
  import { get, post } from '../lib/api'
  import { fmtAgo, fmtTime } from '../lib/format'
  import { live } from '../lib/live.svelte'
  import { route } from '../lib/router.svelte'
  import type { Alert, Filter } from '../lib/types'

  let alerts = $state<Alert[]>([])
  let filters = $state<Filter[]>([])
  let filterId = $state<string>(route.query.get('filter') ?? '')
  let mode = $state<string>('')
  let onlyNotified = $state(false)
  let onlyMilitary = $state(false)
  let loading = $state(false)
  let exhausted = $state(false)

  function query(before?: number): string {
    const q = new URLSearchParams({ limit: '100' })
    if (filterId) q.set('filter_id', filterId)
    if (mode) q.set('mode', mode)
    if (onlyNotified) q.set('notified', 'true')
    if (onlyMilitary) q.set('military', 'true')
    if (before) q.set('before', String(before))
    return `/api/alerts?${q}`
  }

  async function load() {
    loading = true
    const rows = await get<Alert[]>(query())
    alerts = rows
    exhausted = rows.length < 100
    loading = false
  }

  async function more() {
    const last = alerts.at(-1)
    if (!last) return
    const rows = await get<Alert[]>(query(last.started_at))
    alerts = [...alerts, ...rows]
    exhausted = rows.length < 100
  }

  onMount(async () => {
    filters = await get<Filter[]>('/api/filters')
    await load()
  })

  $effect(() => {
    // Reload when a new alert arrives over the live stream.
    if (live.recentAlerts.length) untrack(() => load())
  })

  async function feedback(a: Alert, value: 'up' | 'down') {
    const next = a.feedback === value ? null : value
    await post(`/api/alerts/${a.id}/feedback`, { value: next })
    a.feedback = next
  }
</script>

<div class="page stack">
  <h1>Alerts</h1>
  <div class="card row filters">
    <select bind:value={filterId} onchange={load} aria-label="Filter">
      <option value="">All filters</option>
      {#each filters as f (f.id)}<option value={String(f.id)}>{f.name}</option>{/each}
    </select>
    <select bind:value={mode} onchange={load} aria-label="Mode">
      <option value="">Live and shadow</option>
      <option value="live">Live only</option>
      <option value="shadow">Shadow only</option>
    </select>
    <label class="check small"><input type="checkbox" bind:checked={onlyNotified} onchange={load} /> Notified</label>
    <label class="check small"><input type="checkbox" bind:checked={onlyMilitary} onchange={load} /> Military</label>
  </div>

  <div class="card list">
    {#each alerts as a (a.id)}
      <div class="alert">
        {#if a.photo_url}<img src={a.photo_url} alt="" loading="lazy" referrerpolicy="no-referrer" />{/if}
        <a class="body" href={`#/alerts/${a.id}`}>
          <div class="row">
            <strong class="mono">{a.callsign || a.registration || a.hex}</strong>
            <span class="muted small">{a.type_desc || a.type_code || ''}</span>
            {#if a.military}<span class="badge mil">MIL</span>{/if}
            {#if a.state === 'active'}<span class="badge accent">in progress</span>{/if}
          </div>
          <div class="small">
            <span>{a.filter_name}</span>
            {#if a.mode === 'shadow'}<span class="badge">shadow</span>{/if}
            ·
            {#if a.notified}<span class="ok">notified</span>
            {:else if a.suppressed_reason}<span class="muted">not sent: {a.suppressed_reason}</span>
            {:else}<span class="muted">not sent</span>{/if}
          </div>
          <div class="muted small" title={fmtTime(a.started_at)}>
            {fmtAgo(a.started_at)}{a.closest_nm != null ? ` · closest ${a.closest_nm.toFixed(1)} nm` : ''}
          </div>
        </a>
        <div class="fb">
          <button class="icon ghost" class:sel={a.feedback === 'up'} onclick={() => feedback(a, 'up')} title="Useful">👍</button>
          <button class="icon ghost" class:sel={a.feedback === 'down'} onclick={() => feedback(a, 'down')} title="Not useful">👎</button>
        </div>
      </div>
    {:else}
      <p class="empty">{loading ? 'Loading…' : 'No alerts yet.'}</p>
    {/each}
    {#if alerts.length && !exhausted}
      <div class="more"><button onclick={more}>Load more</button></div>
    {/if}
  </div>
</div>

<style>
  .filters > select {
    width: auto;
  }
  .list {
    padding: 0;
  }
  .alert {
    display: flex;
    gap: 10px;
    align-items: center;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
  }
  .alert img {
    width: 72px;
    height: 48px;
    object-fit: cover;
    border-radius: 4px;
    flex-shrink: 0;
  }
  .body {
    flex: 1;
    min-width: 0;
    color: inherit;
    text-decoration: none;
  }
  .fb {
    display: flex;
  }
  .fb button {
    opacity: 0.4;
  }
  .fb button.sel {
    opacity: 1;
    background: var(--accent-soft);
  }
  .more {
    padding: 12px;
    text-align: center;
  }
</style>
