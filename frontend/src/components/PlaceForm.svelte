<script lang="ts">
  import { errorMessage, get } from '../lib/api'
  import { catalog, loadCatalog } from '../lib/catalog.svelte'
  import { onMount } from 'svelte'
  import { toast } from '../lib/toast.svelte'
  import { defaultVisibility, type Airspace, type PlaceIn } from '../lib/types'
  import AirspaceSearch from './AirspaceSearch.svelte'
  import MapPicker from './MapPicker.svelte'

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
  let airportInfo = $state<string>('')
  let lookingUp = $state(false)

  const livePolygon = $derived.by(() => {
    try {
      return parsePolygon(polygonText)
    } catch {
      return null
    }
  })

  onMount(() => {
    loadCatalog().catch(() => {})
  })

  // Search near the area, or near home while the area is still empty.
  const searchCenter = $derived.by(() => {
    if (value.lat || value.lon) return { lat: value.lat, lon: value.lon }
    const home = catalog.places.find((p) => p.is_home)
    return home ? { lat: home.lat, lon: home.lon } : null
  })

  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

  function toLocalInput(epoch: number | null): string {
    if (epoch === null) return ''
    const d = new Date(epoch * 1000)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  }

  function fromLocalInput(v: string): number | null {
    return v ? new Date(v).getTime() / 1000 : null
  }

  function toggleSchedule(on: boolean) {
    value.schedule = on
      ? { days: [0, 1, 2, 3, 4, 5], start: '09:00', end: '17:00', tz: Intl.DateTimeFormat().resolvedOptions().timeZone }
      : null
  }

  function toggleDay(i: number) {
    if (!value.schedule) return
    const days = value.schedule.days
    value.schedule.days = days.includes(i) ? days.filter((d) => d !== i) : [...days, i].sort()
  }

  function pick(lat: number, lon: number) {
    if (value.role === 'area') {
      polygonText = (polygonText.trim() ? polygonText.trim() + '\n' : '') + `${lat}, ${lon}`
    } else {
      value.lat = lat
      value.lon = lon
      value.source = 'manual'
    }
  }

  function undoPoint() {
    const lines = polygonText.trim().split('\n')
    lines.pop()
    polygonText = lines.join('\n')
  }

  async function lookupAirport() {
    const code = (value.airport_icao ?? '').trim()
    if (!code) return
    lookingUp = true
    try {
      const a = await get<{
        ident: string
        icao: string | null
        name: string
        lat: number
        lon: number
        elevation_ft: number | null
        municipality: string | null
        runways: { le_ident: string; he_ident: string; length_ft: number | null }[]
      }>(`/api/airports/${encodeURIComponent(code)}`)
      value.airport_icao = a.icao ?? a.ident
      value.lat = +a.lat.toFixed(5)
      value.lon = +a.lon.toFixed(5)
      value.elevation_ft = Math.round(a.elevation_ft ?? 0)
      value.source = 'airport_lookup'
      if (!value.name) value.name = a.icao ?? a.ident
      const rw = a.runways.map((r) => `${r.le_ident}/${r.he_ident}${r.length_ft ? ` (${r.length_ft.toLocaleString()} ft)` : ''}`)
      airportInfo = `${a.name}${a.municipality ? `, ${a.municipality}` : ''} · ${rw.length ? `runways ${rw.join(', ')}` : 'no runway data'}`
    } catch (e) {
      airportInfo = ''
      toast(errorMessage(e), 'error')
    } finally {
      lookingUp = false
    }
  }

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

  let fitTo = $state<[number, number][] | null>(null)

  async function useAirspace(a: Airspace) {
    value.polygon = a.polygon
    polygonText = a.polygon.map(([lat, lon]) => `${lat}, ${lon}`).join('\n')
    fitTo = a.polygon
    value.lat = a.center[0]
    value.lon = a.center[1]
    value.radius_nm = a.radius_nm
    if (!value.name) value.name = a.name
    if (a.schedule) value.schedule = { ...a.schedule }
    // Limits "above ground" become MSL using the area's ground elevation, which needs a lookup.
    const agl = a.floor.ref === 'AGL' || a.ceiling.ref === 'AGL'
    if (agl) await lookupElevation()
    const msl = (l: Airspace['floor']) => (l.ft === null ? null : l.ref === 'AGL' ? l.ft + value.elevation_ft : l.ft)
    value.alt_floor_ft = msl(a.floor)
    value.alt_ceiling_ft = a.ceiling.ref === 'UNL' ? null : msl(a.ceiling)
    const notes = [
      agl ? `${a.floor.text} → ${value.alt_floor_ft} ft MSL using ground elevation ${value.elevation_ft} ft` : '',
      a.schedule ? `active hours from "${a.times_of_use}" (${a.schedule.tz})` : '',
    ].filter(Boolean)
    toast(`Imported ${a.name}${notes.length ? `: ${notes.join('; ')}` : ''}. Check before saving.`)
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
    <div class="row airport">
      <label class="field grow">
        Airport code <span class="hint">ICAO or IATA, e.g. KSLN or SLN</span>
        <input bind:value={value.airport_icao} maxlength="8" onkeydown={(e) => e.key === 'Enter' && (e.preventDefault(), lookupAirport())} />
      </label>
      <button type="button" onclick={lookupAirport} disabled={lookingUp}>{lookingUp ? 'Looking up…' : 'Look up'}</button>
    </div>
    {#if airportInfo}<p class="small ok">{airportInfo}</p>{/if}
  {/if}

  <div>
    <MapPicker lat={value.lat} lon={value.lon} radiusNm={value.radius_nm} polygon={value.role === 'area' ? livePolygon : null} onpick={pick} {fitTo} />
    <p class="muted small hint-line">
      {value.role === 'area' ? 'Click the map to add corners in order, or paste coordinates below.' : 'Click the map to set the location.'}
      {#if value.role === 'area' && polygonText.trim()}<button type="button" class="small ghost" onclick={undoPoint}>Undo last corner</button>{/if}
    </p>
  </div>

  {#if value.role === 'area'}
    <AirspaceSearch near={searchCenter} onuse={useAirspace} />
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

  <details open={value.active_from !== null || value.active_until !== null}>
    <summary>Active dates <span class="muted small">(optional, e.g. a trip)</span></summary>
    <div class="grid-2 inner">
      <label class="field">
        From
        <input type="datetime-local" value={toLocalInput(value.active_from)}
          onchange={(e) => (value.active_from = fromLocalInput((e.currentTarget as HTMLInputElement).value))} />
      </label>
      <label class="field">
        Until
        <input type="datetime-local" value={toLocalInput(value.active_until)}
          onchange={(e) => (value.active_until = fromLocalInput((e.currentTarget as HTMLInputElement).value))} />
      </label>
    </div>
  </details>

  <details open={value.schedule !== null}>
    <summary>Active hours <span class="muted small">(optional, e.g. a range's published hours)</span></summary>
    <div class="stack inner">
      <label class="check">
        <input type="checkbox" checked={value.schedule !== null} onchange={(e) => toggleSchedule((e.currentTarget as HTMLInputElement).checked)} />
        Use weekly active hours
      </label>
      {#if value.schedule}
        <div class="row">
          {#each DAYS as d, i (d)}
            <button type="button" class="small" class:on={value.schedule.days.includes(i)} onclick={() => toggleDay(i)}>{d}</button>
          {/each}
        </div>
        <div class="row">
          <input type="time" class="time" bind:value={value.schedule.start} /> to
          <input type="time" class="time" bind:value={value.schedule.end} />
          <span class="muted small">{value.schedule.tz}</span>
        </div>
        <p class="muted small">Filters can test "Within the place's active hours". Nothing changes automatically.</p>
      {/if}
    </div>
  </details>

  <details open={value.prediction !== null}>
    <summary>Prediction <span class="muted small">(optional, overrides Settings for this place)</span></summary>
    <div class="stack inner">
      <label class="check">
        <input type="checkbox" checked={value.prediction !== null}
          onchange={(e) => (value.prediction = (e.currentTarget as HTMLInputElement).checked ? { lead_max_s: null, min_confidence: null } : null)} />
        Use different prediction settings here
      </label>
      {#if value.prediction}
        <div class="grid-2">
          <label class="field">
            Earliest warning (min) <span class="hint">Empty uses the global setting</span>
            <input type="number" min="0.5" max="60" step="0.5"
              value={value.prediction.lead_max_s === null ? '' : value.prediction.lead_max_s / 60}
              onchange={(e) => {
                const v = (e.currentTarget as HTMLInputElement).value
                value.prediction!.lead_max_s = v === '' ? null : Math.round(+v * 60)
              }} />
          </label>
          <label class="field">
            Minimum confidence (%) <span class="hint">Empty uses the global setting</span>
            <input type="number" min="5" max="100" step="5"
              value={value.prediction.min_confidence === null ? '' : Math.round(value.prediction.min_confidence * 100)}
              onchange={(e) => {
                const v = (e.currentTarget as HTMLInputElement).value
                value.prediction!.min_confidence = v === '' ? null : +v / 100
              }} />
          </label>
        </div>
      {/if}
    </div>
  </details>

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
  details summary {
    cursor: pointer;
    font-weight: 600;
  }
  .inner {
    padding-top: 8px;
  }
  .time {
    width: 120px;
  }
  button.on {
    background: var(--accent);
    color: var(--accent-contrast);
    border-color: var(--accent);
  }
  .airport {
    align-items: flex-end;
  }
  .hint-line {
    margin: 4px 0 0;
  }
  legend {
    padding: 0 6px;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
</style>
