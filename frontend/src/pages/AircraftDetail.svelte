<script lang="ts">
  import { onMount } from 'svelte'
  import QuickActions from '../components/QuickActions.svelte'
  import { errorMessage, get } from '../lib/api'
  import { catalog, loadCatalog } from '../lib/catalog.svelte'
  import { describePrediction, fmtAlt, fmtDuration } from '../lib/format'
  import AircraftPhoto from '../components/AircraftPhoto.svelte'
  import LiveMap from '../components/LiveMap.svelte'
  import type { AircraftSummary, Place } from '../lib/types'

  let { hex }: { hex: string } = $props()

  interface Detail {
    summary: AircraftSummary
    facts: Record<string, unknown>
    places: { place_id: number; name: string; role: string; facts: Record<string, unknown> }[]
    military_reasons: string[]
    plane_alert: {
      operator: string | null
      type: string | null
      category: string | null
      tags: string[]
      cmpg: string | null
      link: string | null
      images: string[]
    } | null
    first_seen: number
    history: [number, number, number, number | null][]
  }

  let d = $state<Detail | null>(null)
  let places = $state<Place[]>([])
  let error = $state('')

  async function load() {
    try {
      d = await get<Detail>(`/api/aircraft/${hex}`)
      error = ''
    } catch (e) {
      error = errorMessage(e)
    }
  }

  onMount(() => {
    loadCatalog()
    get<Place[]>('/api/places').then((p) => (places = p))
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  })

  function show(key: string, v: unknown): string {
    if (v === null || v === undefined) return '—'
    const f = catalog.byKey[key]
    if (typeof v === 'boolean') return v ? 'yes' : 'no'
    if (Array.isArray(v)) return v.length ? v.join(', ') : 'none'
    if (typeof v === 'number') {
      if (key === 'seconds_tracked' || key === 'data_age_s') return fmtDuration(v)
      const n = Number.isInteger(v) ? String(v) : v.toFixed(key.includes('score') ? 2 : 1)
      return f?.unit ? `${n} ${f.unit}` : n
    }
    return String(v)
  }
</script>

<div class="page stack">
  <a href="#/live" class="small">← Live</a>
  {#if error}<p class="error">{error}</p>{/if}
  {#if d}
    {@const a = d.summary}
    <div class="card stack">
      <div class="row between">
        <h1 class="mono">{a.callsign || a.registration || a.hex}</h1>
        <div class="row">
          {#if a.military}<span class="badge mil">MILITARY {a.military_score?.toFixed(2)}</span>{/if}
          {#if a.government}<span class="badge">GOVERNMENT</span>{/if}
          {#if a.police}<span class="badge">POLICE</span>{/if}
        </div>
      </div>
      <AircraftPhoto hex={a.hex} />
      <p>
        {a.type_desc || a.type_code || 'Unknown type'} · {a.registration ?? 'no registration'} ·
        <span class="mono">{a.hex}</span>
      </p>
      <p class="small">
        {fmtAlt(a.altitude_ft, a.on_ground)} · {a.gs_kt != null ? `${Math.round(a.gs_kt)} kt` : '—'} ·
        {a.distance_nm != null ? `${a.distance_nm.toFixed(1)} nm ${a.direction} of home` : ''}
        {#if a.elevation_deg != null && a.elevation_deg > 0}· {Math.round(a.elevation_deg)}° up{/if}
      </p>
      {#if a.predictions?.length}
        <ul class="small preds">
          {#each a.predictions as p (p.place_id)}<li>⏱ {describePrediction(p)}</li>{/each}
        </ul>
      {/if}
      {#if d.military_reasons.length}
        <div>
          <h3>Classification</h3>
          <ul class="small">{#each d.military_reasons as r (r)}<li>{r}</li>{/each}</ul>
        </div>
      {/if}
      {#if d.plane_alert}
        <p class="small">
          <strong>plane-alert-db:</strong> {d.plane_alert.category} · {d.plane_alert.operator}
          {#if d.plane_alert.tags.length}· {d.plane_alert.tags.join(', ')}{/if}
          {#if d.plane_alert.link}· <a href={d.plane_alert.link} target="_blank" rel="noopener noreferrer">more</a>{/if}
        </p>
      {/if}
    </div>

    {#if a.lat != null}
      <div class="mini-map">
        <LiveMap aircraft={[a]} {places} selected={a.hex} trail={d.history.map((p) => [p[1], p[2]] as [number, number])} />
      </div>
    {/if}

    <div class="card">
      <h2>Tune</h2>
      <QuickActions
        hex={a.hex}
        registration={a.registration}
        typeCode={a.type_code}
        operator={a.operator}
        callsign={a.callsign}
        military={a.military}
      />
    </div>

    <div class="card">
      <h2>Every filter field</h2>
      <p class="muted small">These are the exact values filters test right now.</p>
      <table class="data">
        <tbody>
          {#each Object.entries(d.facts) as [k, v] (k)}
            <tr><th>{catalog.byKey[k]?.label ?? k}</th><td>{show(k, v)}</td></tr>
          {/each}
        </tbody>
      </table>
      {#each d.places as p (p.place_id)}
        <h3 class="place">Relative to {p.name}</h3>
        <table class="data">
          <tbody>
            {#each Object.entries(p.facts) as [k, v] (k)}
              <tr><th>{catalog.byKey[k]?.label ?? k}</th><td>{show(k, v)}</td></tr>
            {/each}
          </tbody>
        </table>
      {/each}
    </div>
  {:else if !error}
    <p class="muted">Loading…</p>
  {/if}
</div>

<style>
  h1 {
    margin: 0;
  }
  table.data th {
    width: 45%;
    text-transform: none;
    letter-spacing: 0;
    font-size: 0.85rem;
  }
  .mini-map {
    height: 260px;
  }
  .place {
    margin-top: 16px;
  }
</style>
