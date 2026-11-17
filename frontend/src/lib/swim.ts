// FAA SWIM (schedule source) status lines for the Sources report.

export interface SwimStatus {
  state: string
  library_installed: boolean
  connected_since: number | null
  messages_per_min: number
  received: number
  kept: number
  irrelevant: number
  dropped: number
  queued: number
  parse_errors: number
  flight_updates: number
  flights: number
  last_message_at: number | null
  airports: number
  aircraft: number
}

export interface FiledFlight {
  key: string
  acid: string
  origin: string | null
  destination: string | null
  aircraft_type: string | null
  status: string | null
  departure_time: number | null
  arrival_time: number | null
  etd_type: string | null
  eta_type: string | null
}

export function swimState(s: SwimStatus, enabled: boolean): string {
  if (!s.library_installed) return 'Solace library not installed (rebuild the image with the swim extra)'
  if (!enabled) return 'off'
  if (s.state === 'connected') return 'connected'
  if (s.state === 'backing off') return 'reconnecting'
  return s.state
}

export function swimUsage(s: SwimStatus): string {
  return `${s.messages_per_min.toLocaleString()} messages/min`
}

export function swimCounts(s: SwimStatus): string {
  const parts = [
    `${s.received.toLocaleString()} received`,
    `${s.kept.toLocaleString()} about your airports/aircraft`,
    `${s.dropped.toLocaleString()} dropped`,
  ]
  if (s.parse_errors) parts.push(`${s.parse_errors.toLocaleString()} unreadable`)
  parts.push(`${s.flights.toLocaleString()} flights stored`)
  return parts.join(' · ')
}

export function swimInterest(s: SwimStatus): string {
  if (!s.airports && !s.aircraft) return 'Add an airport place (or follow a registration) to keep flights.'
  const what = []
  if (s.airports) what.push(`${s.airports} airport${s.airports === 1 ? '' : 's'}`)
  if (s.aircraft) what.push(`${s.aircraft} followed aircraft`)
  return `Keeping flights for ${what.join(' and ')}.`
}

export function flightLeg(f: FiledFlight): string {
  return `${f.origin ?? '?'} → ${f.destination ?? '?'}`
}
