<script lang="ts">
  import { errorMessage, get } from '../lib/api'
  import { toast } from '../lib/toast.svelte'
  import { defaultVisibility, type PlaceIn } from '../lib/types'

  let {
    value = $bindable(),
    onsave,
    oncancel,
    saving = false,
  }: {
    value: PlaceIn
    onsave: (p: PlaceIn) => void
    oncancel?: () => void
    saving?: boolean
  } = $props()

  let polygonText = $state(
    (value.polygon ?? []).map(([lat, lon]) => `${lat}, ${lon}`).join('\n'),
  )
  let locating = $state(false)
  let error = $state('')

  function useMyLocation() {
    if (!navigator.geolocation) {
      toast('This browser has no location access', 'error')
      return
    }
    locating = true
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        value.lat = +pos.coords.latitude.toFixed(5)
        value.lon = +pos.coords.longitude.toFixed(5)
        value.source = 'geolocation'
        locating = false
        await lookupElevation()
      },
      (err) => {
        locating = false
        toast(`Location unavailable: ${err.message}`, 'error')
      },
      { enableHighAccuracy: true, timeout: 15000 },
    )
  }

  async function lookupElevation() {
    try {
      const r = await get<{ elevation_ft: number | null }>(
        `/api/places/elevation?lat=${value.lat}&lon=${value.lon}`,
      )
      if (r.elevation_ft !== null) {
        value.elevation_ft = r.elevation_ft
        toast(`Ground elevation ${r.elevation_ft} ft`)
      } else {
        toast('No elevation data here; enter it manually', 'error')
      }
    } catch (e) {
      toast(errorMessage(e), 'error')
    }
  }

  function parsePolygon(text: string): [number, number][] | null {
    const pts: [number, number][] = []
    for (const line of text.split('\n')) {
      const t = line.trim()
      if (!t) continue
      const nums = t.split(/[\s,;]+/).map(Number)
      if (nums.length !== 2 || nums.some((n) => !Number.isFinite(n))) {
        throw new Error(`Can't read "${t}". Use "lat, lon" per line in decimal degrees.`)
      }
      pts.push([nums[0], nums[1]])
    }
    return pts.length ? pts : null
  }

  function submit(e: SubmitEvent) {
    e.preventDefault()
    error = ''
    try {
      if (value.role === 'area') {
        value.polygon = parsePolygon(polygonText)
        if (value.polygon) {
          value.lat = +(value.polygon.reduce((s, p) => s + p[0], 0) / value.polygon.length).toFixed(5)
          value.lon = +(value.polygon.reduce((s, p) => s + p[1], 0) / value.polygon.length).toFixed(5)
        }
      } else {
        value.polygon = null
      }
      if (value.role === 'viewpoint' && !value.visibility) value.visibility = defaultVisibility()
      if (value.role !== 'viewpoint') value.is_home = false
      onsave(value)
    } catch (err) {
      error = errorMessage(err)
    }
  }
</script>

<form class="stack" onsubmit={submit}>
  <div class="grid-2">
    <label class="field">
      Name
      <input bind:value={value.name} required maxlength="80" />
    </label>
    <label class="field">
      Kind
      <select bind:value={value.role}>
        <option value="viewpoint">Viewpoint (where you watch from)</option>
        <option value="airport">Airport</option>
        <option value="area">Area (polygon)</option>
      </select>
    </label>
  </div>

  {#if value.role === 'airport'}
    <label class="field">
      ICAO code <span class="hint">e.g. KSLN</span>
      <input bind:value={value.airport_icao} maxlength="8" />
    </label>
  {/if}

  {#if value.role === 'area'}
    <label class="field">
      Polygon corners <span class="hint">One "lat, lon" pair per line, decimal degrees.</span>
      <textarea rows="6" bind:value={polygonText} placeholder="38.7556, -97.7669"></textarea>
    </label>
    <div class="grid-2">
      <label class="field">
        Floor (ft MSL, optional)
        <input type="number" bind:value={value.alt_floor_ft} />
      </label>
      <label class="field">
        Ceiling (ft MSL, optional)
        <input type="number" bind:value={value.alt_ceiling_ft} />
      </label>
    </div>
  {:else}
    <div class="grid-2">
      <label class="field">
        Latitude
        <input type="number" step="any" bind:value={value.lat} required />
      </label>
      <label class="field">
        Longitude
        <input type="number" step="any" bind:value={value.lon} required />
      </label>
    </div>
    <div class="row">
      <button type="button" class="small" onclick={useMyLocation} disabled={locating}>
        {locating ? 'Locating…' : '◎ Use my location'}
      </button>
      <button type="button" class="small" onclick={lookupElevation}>Look up elevation</button>
    </div>
  {/if}

  <div class="grid-2">
    <label class="field">
      Ground elevation (ft)
      <input type="number" bind:value={value.elevation_ft} />
    </label>
    <label class="field">
      Watch radius (nm) <span class="hint">How far around this place to track aircraft</span>
      <input type="number" step="any" min="0.5" max="250" bind:value={value.radius_nm} required />
    </label>
  </div>

  {#if value.role === 'viewpoint' && value.visibility}
    <fieldset class="stack">
      <legend>Visibility from here</legend>
      <div class="grid-2">
        <label class="field">
          Min. elevation angle (°)
          <span class="hint">Skip aircraft lower than this above the horizon</span>
          <input type="number" step="any" min="0" max="90" bind:value={value.visibility.min_elevation_deg} />
        </label>
        <label class="field">
          Max. distance, large aircraft (nm)
          <input type="number" step="any" bind:value={value.visibility.max_slant_nm_large} />
        </label>
        <label class="field">
          Max. distance, medium (nm)
          <input type="number" step="any" bind:value={value.visibility.max_slant_nm_medium} />
        </label>
        <label class="field">
          Max. distance, light / helicopter (nm)
          <input type="number" step="any" bind:value={value.visibility.max_slant_nm_light} />
        </label>
      </div>
    </fieldset>
    <label class="check"><input type="checkbox" bind:checked={value.is_home} /> This is my home</label>
  {/if}

  <label class="check"><input type="checkbox" bind:checked={value.enabled} /> Enabled</label>

  {#if error}<p class="error">{error}</p>{/if}
  <div class="row">
    <button class="primary" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
    {#if oncancel}<button type="button" onclick={oncancel}>Cancel</button>{/if}
  </div>
</form>

<style>
  fieldset {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 12px;
  }
  legend {
    padding: 0 6px;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
</style>
