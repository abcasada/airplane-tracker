<script lang="ts">
  import { onMount } from 'svelte'
  import { get, post } from '../lib/api'
  import { fmtAgo } from '../lib/format'
  import { toast } from '../lib/toast.svelte'

  interface Status {
    version: string
    tracks: number
    places_active: number
    circles: { lat: number; lon: number; radius_nm: number; interval_s: number; tier: string }[]
    sources: {
      name: string
      healthy: boolean
      open_for_s: number
      last_ok_at: number | null
      last_error: string | null
      requests: number
      requests_today: number
      errors: number
      budget_per_s: number
      last_latency_ms: number | null
    }[]
    datasets: { name: string; url: string; rows?: number; imported_at?: number; checked_at?: number; last_error?: string }[]
    dataset_busy: string | null
    recordings: { days: number; bytes: number; retention_days?: number }
    coverage: { band: string; lowest_height_ft: number | null; reports: number; ground_reports: number }[]
    notifications_today: number
    daily_ceiling: number
    ceiling_hit_at: number | null
    last_eval_ms: number
    last_eval_at: number | null
    live_clients: number
    background: boolean
  }

  let s = $state<Status | null>(null)

  async function load() {
    s = await get<Status>('/api/status')
  }

  onMount(() => {
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  })

  async function refresh() {
    const r = await post<{ started: boolean; busy?: string }>('/api/datasets/refresh')
    toast(r.started ? 'Downloading datasets in the background…' : `Busy with ${r.busy}`)
  }

  const mb = (b: number) => `${(b / 1e6).toFixed(1)} MB`
</script>

<div class="page stack">
  <h1>System</h1>
  {#if s}
    <div class="card">
      <div class="grid-2 stats">
        <div><span class="muted small">Aircraft tracked</span><strong>{s.tracks}</strong></div>
        <div><span class="muted small">Active places</span><strong>{s.places_active}</strong></div>
        <div><span class="muted small">Notifications today (UTC)</span><strong>{s.notifications_today} / {s.daily_ceiling}</strong></div>
        <div><span class="muted small">Filter evaluation</span><strong>{s.last_eval_ms} ms</strong></div>
      </div>
      {#if s.ceiling_hit_at}
        <p class="notice danger small">The daily safety limit was reached {fmtAgo(s.ceiling_hit_at)}. Notifications pause until midnight UTC.</p>
      {/if}
      {#if !s.background}<p class="notice small">Background polling is off (RUN_BACKGROUND=false).</p>{/if}
    </div>

    <div class="card">
      <h2>Data sources</h2>
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>Source</th><th>Status</th><th>Requests today</th><th>Errors</th><th>Latency</th></tr></thead>
          <tbody>
            {#each s.sources as src (src.name)}
              <tr>
                <td>{src.name}</td>
                <td>
                  {#if src.healthy}<span class="ok">● ok</span>{:else}<span class="error">● backing off {Math.round(src.open_for_s)} s</span>{/if}
                  {#if src.last_error}<div class="muted small">{src.last_error}</div>{/if}
                </td>
                <td>{src.requests_today}</td>
                <td>{src.errors}</td>
                <td>{src.last_latency_ms != null ? `${Math.round(src.last_latency_ms)} ms` : '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="muted small">
        Polling {s.circles.length} area{s.circles.length === 1 ? '' : 's'}:
        {#each s.circles as c, i (i)}{i ? ', ' : ''}{c.radius_nm} nm every {c.interval_s} s{/each}.
        Budget: at most half of each source's published rate limit.
      </p>
    </div>

    <div class="card">
      <h2>Coverage near home</h2>
      <p class="muted small">The lowest height above home the public feeds have reported, by distance (last 14 days).</p>
      <table class="data">
        <thead><tr><th>Distance</th><th>Lowest seen</th><th>Reports</th></tr></thead>
        <tbody>
          {#each s.coverage as c (c.band)}
            <tr>
              <td>{c.band}</td>
              <td>{c.lowest_height_ft != null ? `${c.lowest_height_ft.toLocaleString()} ft` : '—'}</td>
              <td>{c.reports.toLocaleString()}{c.ground_reports ? ` (${c.ground_reports} on ground)` : ''}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="row between">
        <h2>Reference data</h2>
        <button class="small" onclick={refresh} disabled={!!s.dataset_busy}>{s.dataset_busy ? `Updating ${s.dataset_busy}…` : 'Update now'}</button>
      </div>
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>Dataset</th><th>Rows</th><th>Updated</th></tr></thead>
          <tbody>
            {#each s.datasets as d (d.name)}
              <tr>
                <td><a href={d.url} target="_blank" rel="noopener">{d.name}</a>{#if d.last_error}<div class="error small">{d.last_error}</div>{/if}</td>
                <td>{d.rows?.toLocaleString() ?? '—'}</td>
                <td>{d.imported_at ? fmtAgo(d.imported_at) : 'not yet'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="muted small">Recordings for replay: {s.recordings.days} day(s), {mb(s.recordings.bytes)}.</p>
    </div>
    <p class="muted small">Airplane Tracker {s.version}</p>
  {/if}
</div>

<style>
  .stats > div {
    display: flex;
    flex-direction: column;
  }
  .stats strong {
    font-size: 1.3rem;
  }
</style>
