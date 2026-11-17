<script lang="ts">
  import { onMount } from 'svelte'
  import { errorMessage, get, put } from '../lib/api'
  import { toast } from '../lib/toast.svelte'
  import { fmtAgo, fmtTime } from '../lib/format'
  import { flightLeg, swimCounts, swimInterest, swimState, swimUsage, type FiledFlight, type SwimStatus } from '../lib/swim'

  interface SourceStatus {
    name: string
    kind: 'area' | 'receiver' | 'schedule'
    enabled: boolean
    healthy: boolean
    open_for_s: number
    last_error: string | null
    requests_today: number
    errors: number
    budget_unit: 'request' | 'credit' | 'message'
    budget_per_s: number | null
    budget_per_day: number | null
    used_per_s: number
    used_today: number
    interval_s: number | null
    last_latency_ms: number | null
    last_aircraft: number | null
    credits_remaining?: number | null
    swim?: SwimStatus
  }
  interface Summary {
    aircraft: number
    sources: Record<string, [number, number]>
  }
  interface Report {
    status: SourceStatus[]
    sources: string[]
    windows: { last_hour: Summary; last_24h: Summary }
    today: Summary & { day: number }
    days: (Summary & { day: number })[]
  }

  let r = $state<Report | null>(null)
  let windowKey = $state<'last_hour' | 'last_24h'>('last_hour')
  let filed = $state<FiledFlight[]>([])

  async function load() {
    r = await get<Report>('/api/sources')
    if (r.status.some((s) => s.kind === 'schedule')) {
      filed = (await get<{ flights: FiledFlight[] }>('/api/sources/swim/flights')).flights
    }
  }

  onMount(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  })

  async function toggle(s: SourceStatus) {
    const on = !s.enabled
    if (!on && r?.status.filter((x) => x.enabled && x.kind === 'area').length === 1 && s.kind === 'area') {
      if (!confirm(`${s.name} is the only area source switched on. Turn it off anyway?`)) return
    }
    try {
      await put(`/api/sources/${encodeURIComponent(s.name)}`, { enabled: on })
    } catch (e) {
      toast(errorMessage(e), 'error')
    }
    await load()
  }

  function rate(s: SourceStatus): string {
    if (s.budget_per_s == null) return s.interval_s ? `every ${s.interval_s} s, no limit` : 'no limit'
    if (s.budget_unit === 'credit') {
      const day = Math.round(s.budget_per_day ?? 0).toLocaleString()
      return `${Math.round(s.used_today).toLocaleString()} / ${day} credits today`
    }
    const pct = s.budget_per_s ? Math.round((100 * s.used_per_s) / s.budget_per_s) : 0
    return `${s.used_per_s.toFixed(2)} / ${s.budget_per_s.toFixed(2)} req/s (${pct}%)`
  }

  const day = (epoch: number) =>
    new Date(epoch * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
  const cell = (sum: Summary, name: string) => sum.sources[name] ?? [0, 0]
  let names = $derived(r ? r.sources : [])
  let win = $derived(r ? r.windows[windowKey] : null)
  let statusByName = $derived(new Map((r?.status ?? []).map((s) => [s.name, s])))
</script>

<div class="card">
  <div class="row between">
    <h2>Sources</h2>
    <div class="segmented" role="group" aria-label="Window">
      <button class="small" class:on={windowKey === 'last_hour'} onclick={() => (windowKey = 'last_hour')}>Last hour</button>
      <button class="small" class:on={windowKey === 'last_24h'} onclick={() => (windowKey = 'last_24h')}>Last 24 h</button>
    </div>
  </div>
  {#if r && win}
    <div class="table-wrap">
      <table class="data">
        <thead>
          <tr><th>Source</th><th>Status</th><th>Use vs budget</th><th>Aircraft seen</th><th>Only this source</th></tr>
        </thead>
        <tbody>
          {#each names as name (name)}
            {@const s = statusByName.get(name)}
            {@const [seen, unique] = cell(win, name)}
            <tr>
              <td>{name}{#if s?.kind === 'receiver'} <span class="muted small">(your receiver)</span>{:else if s?.kind === 'schedule'} <span class="muted small">(flight plans)</span>{/if}</td>
              <td>
                {#if !s}<span class="muted">not configured</span>
                {:else if s.swim}
                  {@const st = swimState(s.swim, s.enabled)}
                  <span class={st === 'connected' ? 'ok' : st === 'off' ? 'muted' : 'error'}>{st === 'off' ? '○' : '●'} {st}</span>
                  {#if s.open_for_s > 0} <span class="muted small">in {Math.round(s.open_for_s)} s</span>{/if}
                {:else if !s.enabled}<span class="muted">○ off</span>
                {:else if s.healthy}<span class="ok">● ok</span>
                {:else}<span class="error">● backing off {Math.round(s.open_for_s)} s</span>{/if}
                {#if s?.enabled && s.last_error}<div class="muted small">{s.last_error}</div>{/if}
                {#if s}
                  <div>
                    <button class="small" onclick={() => toggle(s)}>{s.enabled ? 'Turn off' : 'Turn on'}</button>
                  </div>
                {/if}
              </td>
              <td class="small">
                {#if s?.swim}
                  {swimUsage(s.swim)}
                  <div class="muted">
                    {swimCounts(s.swim)}{s.swim.last_message_at ? ` · last ${fmtAgo(s.swim.last_message_at)}` : ''}
                  </div>
                  <div class="muted">{swimInterest(s.swim)}</div>
                {:else if s}
                  {rate(s)}
                  <div class="muted">
                    {s.requests_today.toLocaleString()} requests today{s.errors ? `, ${s.errors} errors` : ''}{s.last_latency_ms != null ? ` · ${Math.round(s.last_latency_ms)} ms` : ''}
                    {#if s.credits_remaining != null} · {s.credits_remaining.toLocaleString()} credits left (OpenSky){/if}
                  </div>
                {:else}—{/if}
              </td>
              {#if s?.kind === 'schedule'}
                <td class="muted">—</td><td class="muted">—</td>
              {:else}
                <td>{seen.toLocaleString()}</td>
                <td>
                  <strong>{unique.toLocaleString()}</strong>
                  {#if seen}<span class="muted small">({Math.round((100 * unique) / seen)}%)</span>{/if}
                </td>
              {/if}
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <p class="muted small">
      {win.aircraft.toLocaleString()} aircraft with a position inside the polled areas
      ({windowKey === 'last_hour' ? 'last 60–75 min' : 'last 24 h'}). "Only this source" counts aircraft no other
      source reported in that time: what you'd lose without it. Lookups of followed aircraft and receiver
      reports outside the polled areas aren't counted. Budget: at most half of each source's published limit.
      A source switched off here stays configured and can be turned back on at any time.
    </p>
    {#if r.status.some((s) => s.kind === 'schedule')}
      <h3>Filed FAA flights for your airports</h3>
      {#if filed.length}
        <div class="table-wrap">
          <table class="data">
            <thead><tr><th>Flight</th><th>Route</th><th>Type</th><th>Departs</th><th>Arrives</th><th>Status</th></tr></thead>
            <tbody>
              {#each filed as f (f.key)}
                <tr>
                  <td>{f.acid}</td>
                  <td>{flightLeg(f)}</td>
                  <td>{f.aircraft_type ?? '—'}</td>
                  <td>{fmtTime(f.departure_time)}{#if f.etd_type} <span class="muted small">{f.etd_type.toLowerCase()}</span>{/if}</td>
                  <td>{fmtTime(f.arrival_time)}{#if f.eta_type} <span class="muted small">{f.eta_type.toLowerCase()}</span>{/if}</td>
                  <td class="small">{f.status ?? ''}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="muted small">None yet. US IFR flight plans usually appear hours before departure.</p>
      {/if}
      <p class="muted small">
        From FAA SWIM (TFMS): flights to or from your airport places that haven't arrived. Filters see them through
        the "Filed plan" fields once the aircraft is in range; most military flights aren't published.
      </p>
    {/if}
    {#if r.days.length}
      <h3>Unique aircraft per day (UTC)</h3>
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>Day</th><th>Aircraft</th>{#each names as n (n)}<th>{n}</th>{/each}</tr></thead>
          <tbody>
            {#each [r.today, ...r.days] as d, i (d.day)}
              <tr>
                <td>{i === 0 ? 'Today' : day(d.day)}</td>
                <td>{d.aircraft.toLocaleString()}</td>
                {#each names as n (n)}
                  {@const [seen, unique] = cell(d, n)}
                  <td>{unique.toLocaleString()} <span class="muted small">/ {seen.toLocaleString()}</span></td>
                {/each}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="muted small">Each cell: aircraft only that source saw / all it saw.</p>
    {/if}
  {/if}
</div>

<style>
  .segmented {
    display: inline-flex;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }
  .segmented button {
    border: 0;
    border-radius: 0;
    background: transparent;
  }
  .segmented button.on {
    background: var(--accent);
    color: var(--accent-contrast);
  }
  h3 {
    margin: 12px 0 4px;
    font-size: 1rem;
  }
</style>
