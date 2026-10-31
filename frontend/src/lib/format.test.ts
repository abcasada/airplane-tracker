import { describe, expect, it } from 'vitest'
import { compass, describePrediction, fmtAgo, fmtAlt, fmtDuration, fmtEta } from './format'

describe('compass', () => {
  it('maps degrees to 16-point names', () => {
    expect(compass(0)).toBe('N')
    expect(compass(359)).toBe('N')
    expect(compass(45)).toBe('NE')
    expect(compass(200)).toBe('SSW')
    expect(compass(-90)).toBe('W')
    expect(compass(null)).toBe('—')
  })
})

describe('fmtAlt', () => {
  it('uses flight levels above 18,000 ft', () => {
    expect(fmtAlt(35000)).toBe('FL350')
    expect(fmtAlt(3500)).toBe('3,500 ft')
    expect(fmtAlt(100, true)).toBe('ground')
  })
})

describe('durations', () => {
  it('formats ago and durations', () => {
    expect(fmtAgo(100, 130)).toBe('30s ago')
    expect(fmtAgo(100, 100 + 7200)).toBe('2 h ago')
    expect(fmtDuration(250)).toBe('4 min 10 s')
    expect(fmtDuration(120)).toBe('2 min')
  })
})

describe('predictions', () => {
  it('formats ETAs like the notifications do', () => {
    expect(fmtEta(null)).toBe('')
    expect(fmtEta(0)).toBe('now')
    expect(fmtEta(44)).toBe('in ~40 s')
    expect(fmtEta(250)).toBe('in ~4 min')
  })
  it('describes a prediction', () => {
    expect(describePrediction({ kind: 'arrival', place: 'KSLN', eta_s: 300, confidence: 0.81 })).toBe('Arrives at KSLN in ~5 min (81%)')
  })
})
