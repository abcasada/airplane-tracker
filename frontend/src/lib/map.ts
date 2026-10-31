import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import type { AircraftSummary, Place } from './types'

export const TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
export const TILE_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
const NM = 1852

export function baseMap(el: HTMLElement, center: [number, number] = [38.8, -97.6], zoom = 8): L.Map {
  const map = L.map(el, { zoomControl: true, attributionControl: true }).setView(center, zoom)
  L.tileLayer(TILE_URL, { maxZoom: 17, attribution: TILE_ATTRIBUTION, className: 'tiles' }).addTo(map)
  return map
}

export function aircraftColor(a: AircraftSummary): string {
  if (a.squawk && ['7500', '7600', '7700'].includes(a.squawk)) return '#e5484d'
  if (a.military) return '#8a9a2b'
  if (a.filters.length) return '#0b6bcb'
  return '#6b7689'
}

export function planeIcon(a: AircraftSummary, selected = false): L.DivIcon {
  const color = aircraftColor(a)
  const size = selected ? 30 : 24
  const rot = a.track_deg ?? 0
  const label = a.callsign || a.registration || a.hex
  return L.divIcon({
    className: 'plane-icon',
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    html: `<div class="plane${selected ? ' sel' : ''}" style="transform: rotate(${rot}deg)">
      <svg viewBox="0 0 24 24" width="${size}" height="${size}"><path fill="${color}" stroke="#fff" stroke-width="1"
      d="M12 2c.8 0 1.4.8 1.4 1.9v5.6l7.6 4.4v2l-7.6-2.2v4.2l2 1.6v1.5L12 20l-3.4 1v-1.5l2-1.6v-4.2L3 15.9v-2l7.6-4.4V3.9C10.6 2.8 11.2 2 12 2z"/></svg>
    </div><span class="plane-label">${escapeHtml(label)}</span>`,
  })
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!)
}

export function placeLayer(p: Place): L.Layer {
  const color = p.is_home ? '#0b6bcb' : p.role === 'area' ? '#c05621' : p.role === 'airport' ? '#6b46c1' : '#2f855a'
  const group = L.layerGroup()
  if (p.role === 'area' && p.polygon) {
    L.polygon(p.polygon, { color, weight: 2, fillOpacity: 0.08 }).bindTooltip(p.name).addTo(group)
  } else {
    L.circle([p.lat, p.lon], {
      radius: p.radius_nm * NM,
      color,
      weight: 1.5,
      dashArray: '6 6',
      fillOpacity: 0.03,
    }).addTo(group)
    L.circleMarker([p.lat, p.lon], { radius: 5, color, fillColor: color, fillOpacity: 0.9 })
      .bindTooltip(p.name)
      .addTo(group)
  }
  return group
}

export function placesBounds(places: Place[]): L.LatLngBounds | null {
  if (!places.length) return null
  const pts: [number, number][] = []
  for (const p of places) {
    if (p.polygon) pts.push(...p.polygon)
    else {
      const dLat = (p.radius_nm * NM) / 111_320
      const dLon = dLat / Math.cos((p.lat * Math.PI) / 180)
      pts.push([p.lat - dLat, p.lon - dLon], [p.lat + dLat, p.lon + dLon])
    }
  }
  return L.latLngBounds(pts)
}
