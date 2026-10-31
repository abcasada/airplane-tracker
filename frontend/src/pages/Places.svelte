<script lang="ts">
  import { onMount } from 'svelte'
  import PlaceForm from '../components/PlaceForm.svelte'
  import { del, errorMessage, get, post, put } from '../lib/api'
  import { toast } from '../lib/toast.svelte'
  import { blankPlace, type Place, type PlaceIn } from '../lib/types'

  let places = $state<Place[]>([])
  let editing = $state<{ id: number | null; value: PlaceIn } | null>(null)
  let saving = $state(false)

  const roleLabel = { viewpoint: 'Viewpoint', airport: 'Airport', area: 'Area' }

  async function load() {
    places = await get<Place[]>('/api/places')
  }
  onMount(load)

  function startNew() {
    const p = blankPlace()
    if (!places.some((x) => x.is_home)) {
      p.name = 'Home'
      p.is_home = true
    }
    editing = { id: null, value: p }
  }

  function startEdit(p: Place) {
    const { id, created_at: _c, updated_at: _u, ...rest } = $state.snapshot(p)
    editing = { id, value: rest }
  }

  async function save(value: PlaceIn) {
    if (!editing) return
    saving = true
    try {
      if (editing.id === null) await post('/api/places', value)
      else await put(`/api/places/${editing.id}`, value)
      toast('Saved')
      editing = null
      await load()
    } catch (e) {
      toast(errorMessage(e), 'error')
    } finally {
      saving = false
    }
  }

  let hereDays = $state('1')
  let locating = $state(false)

  function status(p: Place): string | null {
    const now = Date.now() / 1000
    if (p.active_until !== null && now > p.active_until) return 'expired'
    if (p.active_from !== null && now < p.active_from) return 'upcoming'
    if (p.active_until !== null) return `until ${new Date(p.active_until * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`
    return null
  }

  function imHere() {
    if (!navigator.geolocation) {
      toast('This browser has no location access', 'error')
      return
    }
    locating = true
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        try {
          const lat = +pos.coords.latitude.toFixed(5)
          const lon = +pos.coords.longitude.toFixed(5)
          const elev = await get<{ elevation_ft: number | null }>(`/api/places/elevation?lat=${lat}&lon=${lon}`).catch(() => ({ elevation_ft: null }))
          const p = blankPlace('viewpoint')
          const now = Date.now() / 1000
          p.name = `Here · ${new Date().toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`
          p.lat = lat
          p.lon = lon
          p.elevation_ft = elev.elevation_ft ?? 0
          p.source = 'geolocation'
          p.active_until = now + Number(hereDays) * 86400
          await post('/api/places', p)
          toast(`Added a viewpoint here for ${hereDays} day(s)`)
          await load()
        } catch (e) {
          toast(errorMessage(e), 'error')
        } finally {
          locating = false
        }
      },
      (err) => {
        locating = false
        toast(`Location unavailable: ${err.message}`, 'error')
      },
      { enableHighAccuracy: true, timeout: 15000 },
    )
  }

  async function remove(p: Place) {
    if (!confirm(`Delete "${p.name}"? Filters that use it will stop matching it.`)) return
    try {
      await del(`/api/places/${p.id}`)
      await load()
    } catch (e) {
      toast(errorMessage(e), 'error')
    }
  }
</script>

<div class="page stack">
  <div class="row between">
    <h1>Places</h1>
    {#if !editing}<button class="primary" onclick={startNew}>+ Add place</button>{/if}
  </div>
  <p class="muted small">
    Viewpoints are where you watch from. Airports and areas are places aircraft might head to. What
    happens near them is decided by your filters.
  </p>

  {#if !editing}
    <div class="card row here">
      <div class="grow">
        <strong>Traveling?</strong>
        <span class="muted small">Add a temporary viewpoint at your phone's location.</span>
      </div>
      <select bind:value={hereDays} class="inline" aria-label="For how long">
        <option value="1">for 1 day</option>
        <option value="3">for 3 days</option>
        <option value="7">for a week</option>
      </select>
      <button onclick={imHere} disabled={locating}>{locating ? 'Locating…' : "◎ I'm here now"}</button>
    </div>
  {/if}

  {#if editing}
    <div class="card">
      <h2>{editing.id === null ? 'New place' : 'Edit place'}</h2>
      <PlaceForm bind:value={editing.value} onsave={save} oncancel={() => (editing = null)} {saving} />
    </div>
  {/if}

  {#if places.length === 0 && !editing}
    <div class="card empty">No places yet. Add your home to get started.</div>
  {/if}

  {#each places as p (p.id)}
    <div class="card row between">
      <div class="grow">
        <div class="row">
          <strong>{p.name}</strong>
          <span class="badge">{roleLabel[p.role]}</span>
          {#if p.is_home}<span class="badge accent">Home</span>{/if}
          {#if !p.enabled}<span class="badge">Disabled</span>{/if}
          {#if status(p)}<span class="badge" class:danger={status(p) === 'expired'}>{status(p)}</span>{/if}
          {#if p.schedule}<span class="badge">{p.schedule.start}–{p.schedule.end}</span>{/if}
          {#if p.airport_icao}<span class="badge">{p.airport_icao}</span>{/if}
        </div>
        <div class="muted small">
          {p.lat.toFixed(4)}, {p.lon.toFixed(4)} · {p.radius_nm} nm radius · {p.elevation_ft} ft
          {#if p.polygon}· {p.polygon.length}-point polygon{/if}
        </div>
      </div>
      <div class="row">
        <button class="small" onclick={() => startEdit(p)}>Edit</button>
        <button class="small danger" onclick={() => remove(p)}>Delete</button>
      </div>
    </div>
  {/each}
</div>

<style>
  .here {
    border-style: dashed;
  }
  .inline {
    width: auto;
  }
</style>
