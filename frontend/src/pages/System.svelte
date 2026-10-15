<script lang="ts">
  // Mirrors NOTICE. Keep both in sync when adding a source.
  const CREDITS = [
    { name: 'adsb.fi', url: 'https://adsb.fi', note: 'live positions (open data API, personal non-commercial use)' },
    { name: 'adsb.lol', url: 'https://adsb.lol', note: 'live positions, backup (ODbL 1.0)' },
    { name: 'tar1090-db', url: 'https://github.com/wiedehopf/tar1090-db', note: 'aircraft database and military address ranges (wiedehopf, from Mictronics)' },
    { name: 'plane-alert-db', url: 'https://github.com/sdr-enthusiasts/plane-alert-db', note: 'notable aircraft and photo links (ODbL 1.0, contents DbCL 1.0)' },
    { name: 'VRS standing data', url: 'https://github.com/vradarserver/standing-data', note: 'airlines and routes (CC0)' },
    { name: 'adsb.im', url: 'https://adsb.im', note: 'flight routes' },
    { name: 'Planespotters.net', url: 'https://www.planespotters.net', note: 'photos, credited on each photo' },
    { name: 'OurAirports', url: 'https://ourairports.com/data/', note: 'airports and runways (public domain)' },
    { name: 'FAA ADDS', url: 'https://adds-faa.opendata.arcgis.com/', note: 'special use airspace (public domain)' },
    { name: 'USGS EPQS', url: 'https://epqs.nationalmap.gov', note: 'ground elevation (public domain)' },
    { name: 'OpenStreetMap', url: 'https://www.openstreetmap.org/copyright', note: 'map tiles © OpenStreetMap contributors' },
  ]
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
    sightings: { rows?: number; days?: number; since?: string | null }
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
      <p class="muted small">
        Sightings history (for "rare visitor" fields): {s.sightings.days ?? 0} day(s){s.sightings.since ? ` since ${s.sightings.since}` : ''}.
        History fields count only days before today, so they become meaningful after a few weeks.
      </p>
    </div>
    <div class="card">
      <h2>Data sources and credits</h2>
      <ul class="small credits">
        {#each CREDITS as c (c.name)}
          <li><a href={c.url} target="_blank" rel="noopener">{c.name}</a>: {c.note}</li>
        {/each}
      </ul>
    </div>
    <p class="muted small">Airplane Tracker {s.version} · MIT License</p>
  {/if}
</div>

<style>
  .credits {
    margin: 0;
    padding-left: 18px;
  }
  .stats > div {
    display: flex;
    flex-direction: column;
  }
  .stats strong {
    font-size: 1.3rem;
  }
</style>
