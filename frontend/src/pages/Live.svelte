<script lang="ts">
  import { onMount } from 'svelte'
  import AircraftRow from '../components/AircraftRow.svelte'
  import { get } from '../lib/api'
  import { live } from '../lib/live.svelte'
  import { navigate } from '../lib/router.svelte'
  import type { AircraftSummary } from '../lib/types'

  let show = $state<'all' | 'matching' | 'military' | 'visible'>('all')
  let search = $state('')

  onMount(async () => {
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
          return [a.callsign, a.registration, a.type_code, a.type_desc, a.operator, a.hex]
            .some((v) => v && v.toUpperCase().includes(q))
        }
        return true
      })
      .sort((a, b) => (a.distance_nm ?? 1e9) - (b.distance_nm ?? 1e9)),
  )
</script>

<div class="page stack">
  <div class="row between">
    <h1>Live</h1>
    <span class="small" class:ok={live.connected} class:muted={!live.connected}>
      {live.connected ? '● live' : '○ connecting…'} · {live.aircraft.length} aircraft
    </span>
  </div>
  <div class="row">
    <div class="seg">
      {#each [['all', 'All'], ['matching', 'Matching'], ['military', 'Military'], ['visible', 'Visible']] as [k, label] (k)}
        <button class="small" class:on={show === k} onclick={() => (show = k as typeof show)}>{label}</button>
      {/each}
    </div>
    <input class="search" bind:value={search} placeholder="Search callsign, type, operator…" />
  </div>
  <div class="card list">
    {#each rows as a (a.hex)}
      <AircraftRow {a} onclick={() => navigate(`/aircraft/${a.hex}`)} />
    {:else}
      <p class="empty">
        {live.aircraft.length ? 'Nothing matches that view.' : 'No aircraft yet. Add a place and give the tracker a few seconds.'}
      </p>
    {/each}
  </div>
  <p class="muted small">
    Data: <a href="https://adsb.fi" target="_blank" rel="noopener">adsb.fi</a> open data, with
    <a href="https://adsb.lol" target="_blank" rel="noopener">adsb.lol</a> (ODbL) as backup.
  </p>
</div>

<style>
  .list {
    padding: 0;
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
  .search {
    flex: 1 1 200px;
    width: auto;
  }
</style>
