import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  emptyForm,
  fromTree,
  loadViewPref,
  rowIsEmpty,
  rowWarning,
  saveViewPref,
  setRangeEnd,
  SIMPLE_FIELDS,
  toTree,
  type SimpleForm,
  type SimpleOptions,
  type SimpleRow,
} from './simple'
import type { Condition, Group } from './types'

const opts: SimpleOptions = {
  placeFor: (f) => (f === 'distance_nm' ? 0 : null),
  fieldLabel: (f) => ({ altitude_ft: 'Altitude (MSL)', visible_now: 'Visible now' })[f] ?? f,
  opLabel: (_f, op) => ({ ne: 'not equal to', exists: 'is known' })[op] ?? op,
}

function root(children: Group['children'], mode: Group['mode'] = 'all'): Group {
  return { type: 'group', id: 'root', mode, enabled: true, children }
}

function c(field: string, op: string, value: unknown = null, extra: Partial<Condition> = {}): Condition {
  return { type: 'cond', id: `c-${field}`, field, op, value, place: null, enabled: true, ...extra }
}

function row(form: SimpleForm, field: string): SimpleRow {
  return form.find((r) => r.field === field)!
}

function ok(g: Group): SimpleForm {
  const r = fromTree(g, opts)
  if (!r.ok) throw new Error(r.reasons.join(' '))
  return r.form
}

function reasons(g: Group): string[] {
  const r = fromTree(g, opts)
  if (r.ok) throw new Error('expected the tree to be rejected')
  return r.reasons
}

function filledForm(): SimpleForm {
  const form = emptyForm(opts)
  row(form, 'registration').values = ['N628TS', '98-0001']
  row(form, 'callsign').values = ['RCH*']
  row(form, 'type_code').values = ['C17', 'K35R']
  row(form, 'airline_code').values = ['UAL']
  row(form, 'operator').values = ['Air Force']
  row(form, 'category').values = ['A5', 'A7']
  row(form, 'military').flag = true
  setRangeEnd(row(form, 'altitude_ft'), 'min', 1000)
  setRangeEnd(row(form, 'altitude_ft'), 'max', 15000)
  setRangeEnd(row(form, 'ground_speed_kt'), 'max', 250)
  row(form, 'route_origin').values = ['KDEN']
  row(form, 'route_destination').values = ['K*']
  setRangeEnd(row(form, 'distance_nm'), 'max', 30)
  return form
}

describe('toTree', () => {
  it('writes one condition per filled row with the expected operators', () => {
    const tree = toTree(filledForm(), root([]))
    expect(tree.mode).toBe('all')
    const byField = Object.fromEntries(tree.children.map((x) => [(x as Condition).field, x as Condition]))
    expect(byField.registration).toMatchObject({ op: 'in', value: ['N628TS', '98-0001'], place: null, enabled: true })
    expect(byField.callsign).toMatchObject({ op: 'glob', value: ['RCH*'] })
    expect(byField.operator).toMatchObject({ op: 'contains', value: ['Air Force'] })
    expect(byField.category).toMatchObject({ op: 'in', value: ['A5', 'A7'] })
    expect(byField.military).toMatchObject({ op: 'is_true', value: null })
    expect(byField.altitude_ft).toMatchObject({ op: 'between', value: [1000, 15000] })
    expect(byField.ground_speed_kt).toMatchObject({ op: 'le', value: 250 })
    expect(byField.route_destination).toMatchObject({ op: 'glob', value: ['K*'] })
    expect(byField.distance_nm).toMatchObject({ op: 'le', value: 30, place: 0 })
    expect(tree.children).toHaveLength(SIMPLE_FIELDS.length)
  })

  it('omits empty rows', () => {
    const form = emptyForm(opts)
    expect(toTree(form, root([])).children).toEqual([])
    row(form, 'military').flag = false
    const tree = toTree(form, root([]))
    expect(tree.children).toEqual([
      { type: 'cond', id: 's-military', field: 'military', op: 'is_false', value: null, place: null, enabled: true },
    ])
  })

  it('keeps the root group and the existing order of conditions', () => {
    const g = root([c('military', 'is_true'), c('type_code', 'in', ['C17'])])
    const form = ok(g)
    row(form, 'callsign').values = ['RCH*']
    const tree = toTree(form, g)
    expect(tree.id).toBe('root')
    expect(tree.children.map((x) => (x as Condition).field)).toEqual(['military', 'type_code', 'callsign'])
  })
})

describe('round trips', () => {
  it('simple → tree → simple gives the same rows', () => {
    const form = filledForm()
    const back = ok(toTree(form, root([])))
    expect(back).toEqual(form)
  })

  it('simple → tree → simple keeps strict one-sided ranges', () => {
    const form = emptyForm(opts)
    const alt = row(form, 'altitude_ft')
    alt.max = 3000
    alt.maxStrict = true
    const speed = row(form, 'ground_speed_kt')
    speed.min = 100
    speed.minStrict = true
    const tree = toTree(form, root([]))
    expect(tree.children).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ field: 'altitude_ft', op: 'lt', value: 3000 }),
        expect.objectContaining({ field: 'ground_speed_kt', op: 'gt', value: 100 }),
      ]),
    )
    expect(ok(tree)).toEqual(form)
  })

  it('tree → simple → tree gives the same tree', () => {
    const g = root([
      c('distance_nm', 'lt', 30, { place: 0 }),
      c('type_code', 'glob', ['B7*', 'C17']),
      c('military', 'is_false'),
      c('altitude_ft', 'between', [500, 9000]),
      c('ground_speed_kt', 'ge', 120),
      c('operator', 'contains', ['NASA']),
      c('route_origin', 'in', ['KDEN']),
    ])
    expect(toTree(ok(g), g)).toEqual(g)
  })

  it('an empty tree is an empty form', () => {
    const form = ok(root([]))
    expect(form.every(rowIsEmpty)).toBe(true)
    expect(form.map((r) => r.field)).toEqual(SIMPLE_FIELDS.map((s) => s.field))
  })

  it('drops conditions that have no values yet (e.g. from a template)', () => {
    const g = root([c('registration', 'in', []), c('military', 'is_true')])
    const form = ok(g)
    expect(rowIsEmpty(row(form, 'registration'))).toBe(true)
    expect(row(form, 'registration').id).toBe('c-registration')
    expect(toTree(form, g).children.map((x) => (x as Condition).field)).toEqual(['military'])
  })

  it('setting both ends of a range makes it an inclusive between', () => {
    const form = ok(root([c('altitude_ft', 'lt', 3000)]))
    const alt = row(form, 'altitude_ft')
    expect(alt.maxStrict).toBe(true)
    setRangeEnd(alt, 'min', 500)
    expect(alt).toMatchObject({ min: 500, max: 3000, minStrict: false, maxStrict: false })
    expect(ok(toTree(form, root([])))).toEqual(form)
    setRangeEnd(alt, 'min', null)
    expect(toTree(form, root([])).children[0]).toMatchObject({ op: 'le', value: 3000 })
  })
})

describe('trees that need the full builder', () => {
  it('rejects "any" and "none" roots', () => {
    expect(reasons(root([c('military', 'is_true')], 'any'))[0]).toMatch(/any one condition/)
    expect(reasons(root([c('military', 'is_true')], 'none'))[0]).toMatch(/none of the conditions/)
  })

  it('rejects nested groups', () => {
    const nested: Group = { type: 'group', id: 'g', mode: 'any', enabled: true, children: [c('police', 'is_true')] }
    expect(reasons(root([c('military', 'is_true'), nested]))).toEqual(['It has a nested group (any of …).'])
  })

  it('rejects fields that simple mode does not show', () => {
    expect(reasons(root([c('visible_now', 'is_true', null, { place: 0 })]))).toEqual([
      "'Visible now' isn't one of the simple-mode fields.",
    ])
  })

  it('rejects unsupported operators', () => {
    expect(reasons(root([c('altitude_ft', 'ne', 0)]))).toEqual(["'Altitude (MSL)' uses “not equal to”."])
    expect(reasons(root([c('registration', 'exists')]))).toEqual(["'registration' uses “is known”."])
    expect(reasons(root([c('operator', 'in', ['NASA'])]))).toHaveLength(1)
    expect(reasons(root([c('military', 'exists')]))).toHaveLength(1)
  })

  it('rejects a field used twice', () => {
    const g = root([c('altitude_ft', 'ge', 1000), { ...c('altitude_ft', 'le', 5000), id: 'other' }])
    expect(reasons(g)).toEqual(["'Altitude (MSL)' is used in more than one condition."])
  })

  it('rejects switched-off conditions and unfinished ones', () => {
    expect(reasons(root([c('military', 'is_true', null, { enabled: false })]))).toEqual(["'military' is switched off."])
    expect(reasons(root([c('', 'in', [])]))).toEqual(['A condition has no field chosen yet.'])
  })

  it('rejects wildcard mismatches that would not round-trip', () => {
    expect(reasons(root([c('callsign', 'in', ['RCH*'])]))[0]).toMatch(/matched literally/)
    expect(reasons(root([c('callsign', 'glob', ['RCH452'])]))[0]).toMatch(/without wildcards/)
  })

  it('rejects malformed values', () => {
    expect(reasons(root([c('altitude_ft', 'between', [1000])]))[0]).toMatch(/two numbers/)
    expect(reasons(root([c('altitude_ft', 'ge', '1000')]))[0]).toMatch(/a number/)
    expect(reasons(root([c('type_code', 'in', 'C17')]))[0]).toMatch(/unexpected value/)
  })

  it('lists every reason', () => {
    const g = root([c('military', 'is_true', null, { enabled: false }), c('visible_now', 'is_true')], 'any')
    expect(reasons(g)).toHaveLength(3)
  })
})

describe('rowWarning', () => {
  it('flags an inverted range, which the engine treats as wrap-around', () => {
    const form = emptyForm(opts)
    const alt = row(form, 'altitude_ft')
    setRangeEnd(alt, 'min', 10000)
    expect(rowWarning(alt)).toBeNull()
    setRangeEnd(alt, 'max', 2000)
    expect(rowWarning(alt)).toMatch(/wrap-around/)
    expect(ok(toTree(form, root([])))).toEqual(form)
  })
})

describe('view preference', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('survives missing or blocked storage', () => {
    vi.stubGlobal('localStorage', undefined)
    expect(loadViewPref()).toBeNull()
    expect(() => saveViewPref('advanced')).not.toThrow()
  })

  it('remembers the last choice', () => {
    const store = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    })
    expect(loadViewPref()).toBeNull()
    saveViewPref('advanced')
    expect(loadViewPref()).toBe('advanced')
    store.set('filter-builder-view', 'nonsense')
    expect(loadViewPref()).toBeNull()
  })
})
