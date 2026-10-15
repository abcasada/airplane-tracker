<script lang="ts">
  import L from 'leaflet'
  import { onMount } from 'svelte'
  import { baseMap } from '../lib/map'

  let {
    lat,
    lon,
    radiusNm,
    polygon,
    onpick,
    fitTo = null,
  }: {
    lat: number
    lon: number
    radiusNm: number
    polygon: [number, number][] | null
    onpick: (lat: number, lon: number) => void
    fitTo?: [number, number][] | null // zoom to these points whenever a new list is passed
  } = $props()

  let el: HTMLDivElement
  let map: L.Map | null = null
  let layer: L.LayerGroup | null = null

  onMount(() => {
    const hasPoint = lat !== 0 || lon !== 0
    map = baseMap(el, hasPoint ? [lat, lon] : [39.5, -98.35], hasPoint ? 9 : 4)
    layer = L.layerGroup().addTo(map)
    map.on('click', (e: L.LeafletMouseEvent) => onpick(+e.latlng.lat.toFixed(5), +e.latlng.lng.toFixed(5)))
    if (polygon?.length) map.fitBounds(L.latLngBounds(polygon).pad(0.3))
    return () => map?.remove()
  })

  $effect(() => {
    if (map && fitTo?.length) map.fitBounds(L.latLngBounds(fitTo).pad(0.3))
  })

  $effect(() => {
    if (!map || !layer) return
    layer.clearLayers()
    if (polygon && polygon.length) {
      L.polygon(polygon, { color: '#c05621', weight: 2, fillOpacity: 0.1 }).addTo(layer)
      for (const p of polygon) L.circleMarker(p, { radius: 4, color: '#c05621' }).addTo(layer)
    } else if (lat !== 0 || lon !== 0) {
      L.circle([lat, lon], { radius: radiusNm * 1852, color: '#0b6bcb', weight: 1.5, fillOpacity: 0.05 }).addTo(layer)
      L.circleMarker([lat, lon], { radius: 6, color: '#0b6bcb', fillOpacity: 1 }).addTo(layer)
    }
  })
</script>

<div class="picker" bind:this={el}></div>

<style>
  .picker {
    height: 280px;
    border-radius: var(--radius-sm);
    overflow: hidden;
    border: 1px solid var(--border);
    z-index: 0;
  }
  @media (prefers-color-scheme: dark) {
    .picker :global(.tiles) {
      filter: invert(1) hue-rotate(180deg) brightness(0.85) contrast(0.9);
    }
  }
</style>
