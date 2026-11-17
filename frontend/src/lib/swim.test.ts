import { describe, expect, it } from 'vitest'
import { flightLeg, swimCounts, swimInterest, swimState, type SwimStatus } from './swim'

const base: SwimStatus = {
  state: 'connected',
  library_installed: true,
  connected_since: 1,
  messages_per_min: 120.5,
  received: 1000,
  kept: 12,
  irrelevant: 988,
  dropped: 0,
  queued: 0,
  parse_errors: 0,
  flight_updates: 30,
  flights: 7,
  last_message_at: 2,
  airports: 1,
  aircraft: 0,
}

describe('swim status', () => {
  it('names the state, and a missing library first', () => {
    expect(swimState(base, true)).toBe('connected')
    expect(swimState({ ...base, state: 'backing off' }, true)).toBe('reconnecting')
    expect(swimState(base, false)).toBe('off')
    expect(swimState({ ...base, library_installed: false }, true)).toMatch(/not installed/)
  })

  it('summarizes counts and what is kept', () => {
    expect(swimCounts(base)).toContain('12 about your airports/aircraft')
    expect(swimCounts({ ...base, parse_errors: 2 })).toContain('2 unreadable')
    expect(swimInterest(base)).toBe('Keeping flights for 1 airport.')
    expect(swimInterest({ ...base, airports: 0 })).toMatch(/Add an airport place/)
    expect(swimInterest({ ...base, airports: 2, aircraft: 3 })).toBe('Keeping flights for 2 airports and 3 followed aircraft.')
  })

  it('formats a leg', () => {
    expect(
      flightLeg({
        key: 'k', acid: 'TST1', origin: 'KDEN', destination: null, aircraft_type: null, status: null,
        departure_time: null, arrival_time: null, etd_type: null, eta_type: null,
      }),
    ).toBe('KDEN → ?')
  })
})
