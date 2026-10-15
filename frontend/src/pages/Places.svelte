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
