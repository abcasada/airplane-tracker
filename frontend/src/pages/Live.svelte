<script lang="ts">
  import { onMount } from 'svelte'
  import AircraftRow from '../components/AircraftRow.svelte'
  import LiveMap from '../components/LiveMap.svelte'
  import { get } from '../lib/api'
  import { fmtAlt } from '../lib/format'
  import { live } from '../lib/live.svelte'
  import { navigate } from '../lib/router.svelte'
  import type { AircraftSummary, Place } from '../lib/types'

  let show = $state<'all' | 'matching' | 'military' | 'visible'>('all')
  let search = $state('')
  let places = $state<Place[]>([])
  let selected = $state<string | null>(null)

  onMount(async () => {
    places = await get<Place[]>('/api/places')
    if (!live.aircraft.length) {
      const r = await get<{ aircraft: AircraftSummary[]; now: number }>('/api/aircraft')
      live.aircraft = r.aircraft
      live.now = r.now
    }
  })

  const rows = $derived(
    live.aircraft
      .filter((a) => {
        if (show === 'matching' && !a.filters.length) return false
        if (show === 'military' && !a.military) return false
        if (show === 'visible' && !a.visible) return false
        if (search) {
          const q = search.toUpperCase()
          return [a.callsign, a.registration, a.type_code, a.type_desc, a.operator, a.hex].some(
            (v) => v && v.toUpperCase().includes(q),
          )
        }
        return true
      })
      .sort((a, b) => (a.distance_nm ?? 1e9) - (b.distance_nm ?? 1e9)),
  )
  const sel = $derived(live.aircraft.find((a) => a.hex === selected) ?? null)
</script>

<div class="live">
  <div class="map-pane">
    <LiveMap aircraft={rows} {places} {selected} onselect={(h) => (selected = h)} />
    {#if sel}
      <div class="sel card">
        <div class="row between">
          <strong class="mono">{sel.callsign || sel.registration || sel.hex}</strong>
          <button class="icon ghost" onclick={() => (selected = null)} aria-label="Close">✕</button>
        </div>
        <div class="small">{sel.type_desc || sel.type_code || 'Unknown type'}{sel.military ? ' · military' : ''}</div>
        <div class="small muted">
          {fmtAlt(sel.altitude_ft, sel.on_ground)} · {sel.gs_kt != null ? `${Math.round(sel.gs_kt)} kt` : '—'}
          {#if sel.distance_nm != null}· {sel.distance_nm.toFixed(1)} nm {sel.direction}{/if}
        </div>
        <button class="small" onclick={() => navigate(`/aircraft/${sel.hex}`)}>Details</button>
      </div>
    {/if}
  </div>
  <div class="list-pane">
    <div class="row between head">
      <span class="small" class:ok={live.connected} class:muted={!live.connected}>
        {live.connected ? '● live' : '○ connecting…'} · {rows.length} of {live.aircraft.length}
      </span>
      <div class="seg">
        {#each [['all', 'All'], ['matching', 'Matching'], ['military', 'Military'], ['visible', 'Visible']] as [k, label] (k)}
          <button class="small" class:on={show === k} onclick={() => (show = k as typeof show)}>{label}</button>
        {/each}
      </div>
    </div>
    <input class="search" bind:value={search} placeholder="Search callsign, type, operator…" />
    <div class="list card">
      {#each rows as a (a.hex)}
        <AircraftRow {a} onclick={() => (selected === a.hex ? navigate(`/aircraft/${a.hex}`) : (selected = a.hex))} />
      {:else}
        <p class="empty">
          {live.aircraft.length ? 'Nothing matches that view.' : 'No aircraft yet. Add a place and give it a few seconds.'}
        </p>
      {/each}
    </div>
    <p class="muted small attrib">
      Data: <a href="https://adsb.fi" target="_blank" rel="noopener">adsb.fi</a> open data, with
      <a href="https://adsb.lol" target="_blank" rel="noopener">adsb.lol</a> (ODbL) as backup.
    </p>
  </div>
</div>

<style>
  .live {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 10px;
  }
  .map-pane {
    position: relative;
    height: 48vh;
  }
  .sel {
    position: absolute;
    left: 10px;
    bottom: 10px;
    z-index: 500;
    min-width: 220px;
    max-width: calc(100% - 20px);
    padding: 10px;
  }
  .list-pane {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }
  .head {
    flex-wrap: wrap;
  }
  .seg {
    display: flex;
    gap: 4px;
  }
  .seg .on {
    background: var(--accent);
    color: var(--accent-contrast);
    border-color: var(--accent);
  }
  .list {
    padding: 0;
    overflow: auto;
  }
  .attrib {
    margin: 0;
  }
  @media (min-width: 900px) {
    .live {
      flex-direction: row;
      height: calc(100vh - var(--nav-h));
    }
    .map-pane {
      flex: 3;
      height: auto;
    }
    .list-pane {
      flex: 2;
      max-width: 460px;
    }
    .list {
      flex: 1;
    }
  }
</style>
