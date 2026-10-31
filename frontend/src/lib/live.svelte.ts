import { toast } from './toast.svelte'
import type { AircraftSummary } from './types'

interface AlertEvent {
  id: number
  filter_name: string
  mode: string
  hex: string
  callsign: string | null
  type_code: string | null
  type_desc: string | null
  notified: boolean
}

export const live = $state<{
  aircraft: AircraftSummary[]
  now: number
  connected: boolean
  recentAlerts: AlertEvent[]
}>({ aircraft: [], now: 0, connected: false, recentAlerts: [] })

let source: EventSource | null = null

export function connectLive(): void {
  if (source) return
  source = new EventSource('/api/stream')
  source.addEventListener('open', () => (live.connected = true))
  source.addEventListener('error', () => (live.connected = false))
  source.addEventListener('aircraft', (e) => {
    const data = JSON.parse((e as MessageEvent).data)
    live.aircraft = data.aircraft
    live.now = data.now
  })
  source.addEventListener('alert', (e) => {
    const a = JSON.parse((e as MessageEvent).data) as AlertEvent
    live.recentAlerts = [a, ...live.recentAlerts].slice(0, 20)
    const what = a.callsign || a.type_desc || a.type_code || a.hex
    toast(`${a.mode === 'shadow' ? '(shadow) ' : ''}${a.filter_name}: ${what}`)
  })
}

export function disconnectLive(): void {
  source?.close()
  source = null
  live.connected = false
}
