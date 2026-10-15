import { describe, expect, it } from 'vitest'
import { compass, fmtAgo, fmtAlt, fmtDuration } from './format'

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
