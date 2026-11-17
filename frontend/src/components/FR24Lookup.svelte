<script lang="ts">
  // On-demand Flightradar24 lookup (paid: each lookup spends credits under the monthly cap).
  // Shown only when FR24_API_KEY is set. FR24 data is shown here and never stored or exported.
  import { onMount } from 'svelte'
  import { errorMessage, get, post } from '../lib/api'
  import { compass, fmtAlt, fmtTime } from '../lib/format'

  // The server turns the hex into its registration (or, without one, its current callsign).
  let { hex, onfound }: { hex: string; onfound?: () => void } = $props()

  interface Month { used: number; cap: number; exhausted: boolean }
  interface Flight {
    fr24_id: string
    hex: string | null
    callsign: string | null
    registration: string | null
    type_code: string | null
    flight: string | null
    lat: number
    lon: number
    alt_ft: number
    gs_kt: number | null
    track_deg: number | null
    pos_time: number
    data_source: string
    estimated: boolean
    orig_icao: string | null
    orig_iata: string | null
    dest_icao: string | null
    dest_iata: string | null
    eta: number | null
  }
  interface Result {
    flights: Flight[]
    from_cache: boolean
    credits: number
    month: Month
    attribution: { label: string; url: string }
  }

  let configured = $state(false)
  let enabled = $state(false)
  let month = $state<Month | null>(null)
  let perFlight = $state(0)
  let result = $state<Result | null>(null)
  let busy = $state(false)
  let error = $state('')

  onMount(async () => {
    try {
      const s = await get<{ configured: boolean; status: { enabled: boolean; month: Month; credits_per_flight: number } | null }>('/api/fr24')
      configured = s.configured
      enabled = !!s.status?.enabled
      month = s.status?.month ?? null
      perFlight = s.status?.credits_per_flight ?? 0
    } catch {
      configured = false
    }
  })

  async function lookup() {
    busy = true
    error = ''
    try {
      result = await post<Result>('/api/fr24/lookup', { hex })
      month = result.month
      if (result.flights.some((f) => !f.estimated && f.hex)) onfound?.()
    } catch (e) {
      error = errorMessage(e)
    } finally {
      busy = false
    }
  }

  const airport = (icao: string | null, iata: string | null) => icao || iata || '?'
</script>

{#if configured}
  <div class="card stack fr24">
    <div class="row between">
      <h2>Flightradar24</h2>
      {#if month}<span class="muted small">{Math.round(month.used).toLocaleString()} / {month.cap.toLocaleString()} credits this month</span>{/if}
    </div>
    {#if !enabled}
      <p class="muted small">Switched off in the Sources report (System page).</p>
    {:else}
      <div class="row">
        <button class="small" disabled={busy} onclick={lookup}>{busy ? 'Looking up…' : 'Look up on Flightradar24'}</button>
        <span class="muted small">Uses about {perFlight} credits unless cached in the last minute.</span>
      </div>
    {/if}
    {#if error}<p class="error small">{error}</p>{/if}
    {#if result}
      {#each result.flights as f (f.fr24_id)}
        <div class="small">
          <strong class="mono">{f.callsign ?? f.flight ?? f.registration ?? f.fr24_id}</strong>
          {#if f.flight && f.flight !== f.callsign}<span class="muted">(flight {f.flight})</span>{/if}
          · {f.registration ?? 'no registration'} · {f.type_code ?? 'type ?'}
          {#if f.orig_icao || f.orig_iata || f.dest_icao || f.dest_iata}
            <div>Route: {airport(f.orig_icao, f.orig_iata)} → {airport(f.dest_icao, f.dest_iata)}{#if f.eta} · ETA {fmtTime(f.eta)}{/if}</div>
          {/if}
          <div>
            {fmtAlt(f.alt_ft, f.alt_ft <= 0)} · {f.gs_kt != null ? `${Math.round(f.gs_kt)} kt` : '—'}
            {f.track_deg != null ? `heading ${compass(f.track_deg)}` : ''} · {f.lat.toFixed(3)}, {f.lon.toFixed(3)}
          </div>
          <div class="muted">
            {f.estimated ? `Estimated by FR24 (${f.data_source}), not a measured position` : f.data_source} · position at {fmtTime(f.pos_time)}
          </div>
        </div>
      {:else}
        <p class="muted small">Flightradar24 doesn't see it right now (it may be on the ground, off, or hidden on FR24).</p>
      {/each}
      <p class="muted small">
        {result.from_cache ? 'From the cache (no credits spent)' : `${result.credits} credits spent`} ·
        Data: <a href={result.attribution.url} target="_blank" rel="noopener noreferrer">{result.attribution.label}</a>
      </p>
    {/if}
  </div>
{/if}

<style>
  .fr24 h2 {
    margin: 0;
  }
</style>
