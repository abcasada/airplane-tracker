<script lang="ts">
  import L from 'leaflet'
  import { onMount } from 'svelte'
  import { baseMap, placeLayer, placesBounds, planeIcon } from '../lib/map'
  import type { AircraftSummary, Place } from '../lib/types'

  let {
    aircraft,
    places,
    selected = null,
    onselect,
    trail = null,
  }: {
    aircraft: AircraftSummary[]
    places: Place[]
    selected?: string | null
    onselect?: (hex: string) => void
    trail?: [number, number][] | null
  } = $props()

  let el: HTMLDivElement
  let map: L.Map | null = null
  const markers = new Map<string, { m: L.Marker; key: string }>()
  let placeGroup: L.LayerGroup | null = null
  let trailLine: L.Polyline | null = null
  let fitted = false

  onMount(() => {
    map = baseMap(el)
    placeGroup = L.layerGroup().addTo(map)
    const ro = new ResizeObserver(() => map?.invalidateSize())
    ro.observe(el)
    return () => {
      ro.disconnect()
      map?.remove()
      map = null
      markers.clear()
    }
  })

  $effect(() => {
    if (!map || !placeGroup) return
    placeGroup.clearLayers()
    for (const p of places) placeGroup.addLayer(placeLayer(p))
    if (!fitted) {
      const b = placesBounds(places.filter((p) => p.enabled))
      if (b) {
        map.fitBounds(b.pad(0.15))
        fitted = true
      }
    }
  })

  $effect(() => {
    if (!map) return
    const seen = new Set<string>()
    for (const a of aircraft) {
      if (a.lat == null || a.lon == null) continue
      seen.add(a.hex)
      const isSel = a.hex === selected
      // Only rebuild the icon when something visible changed.
      const key = `${Math.round(a.track_deg ?? 0)}|${a.military}|${a.filters.length}|${a.squawk}|${isSel}|${a.callsign}`
      const existing = markers.get(a.hex)
      if (existing) {
        existing.m.setLatLng([a.lat, a.lon])
        if (existing.key !== key) {
          existing.m.setIcon(planeIcon(a, isSel))
          existing.key = key
        }
      } else {
        const m = L.marker([a.lat, a.lon], { icon: planeIcon(a, isSel), keyboard: false })
        m.on('click', () => onselect?.(a.hex))
        m.addTo(map)
        markers.set(a.hex, { m, key })
      }
    }
    for (const [hex, { m }] of markers) {
      if (!seen.has(hex)) {
        m.remove()
        markers.delete(hex)
      }
    }
  })

  $effect(() => {
    if (!map) return
    trailLine?.remove()
    trailLine = trail && trail.length > 1 ? L.polyline(trail, { color: '#0b6bcb', weight: 2, opacity: 0.7 }).addTo(map) : null
  })
</script>

<div class="map" bind:this={el}></div>

<style>
  .map {
    width: 100%;
    height: 100%;
    min-height: 300px;
    border-radius: var(--radius);
    overflow: hidden;
    z-index: 0;
  }
  .map :global(.plane-icon) {
    background: none;
    border: 0;
  }
  .map :global(.plane) {
    transition: transform 0.4s;
    filter: drop-shadow(0 1px 1px rgb(0 0 0 / 40%));
  }
  .map :global(.plane-label) {
    position: absolute;
    left: 100%;
    top: 50%;
    transform: translateY(-50%);
    margin-left: 2px;
    font: 600 10px/1 var(--mono);
    color: #0f172a;
    background: rgb(255 255 255 / 80%);
    padding: 1px 3px;
    border-radius: 3px;
    white-space: nowrap;
    pointer-events: none;
  }
  @media (prefers-color-scheme: dark) {
    .map :global(.tiles) {
      filter: invert(1) hue-rotate(180deg) brightness(0.85) contrast(0.9);
    }
  }
</style>
