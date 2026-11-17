// Simple mode: a flat panel of common filter rows over the same expression tree.
// The tree stays the only source of truth; this module converts between it and the rows.
import type { Condition, Group } from './types'

/**
 * match: exact values ("is any of"), or patterns ("matches pattern") when a value has a wildcard.
 * contains: parts of a name. choice: enum values. flag: yes / no / either.
 * range: from/to, as at least (or above) / at most (or below) / between.
 */
export type SimpleKind = 'match' | 'contains' | 'choice' | 'flag' | 'range'

export interface SimpleField {
  field: string
  kind: SimpleKind
}

// Labels, units, help, enums, autocomplete and place support come from the field catalog.
export const SIMPLE_FIELDS: readonly SimpleField[] = [
  { field: 'registration', kind: 'match' },
  { field: 'callsign', kind: 'match' },
  { field: 'type_code', kind: 'match' },
  { field: 'airline_code', kind: 'match' },
  { field: 'operator', kind: 'contains' },
  { field: 'category', kind: 'choice' },
  { field: 'military', kind: 'flag' },
  { field: 'altitude_ft', kind: 'range' },
  { field: 'ground_speed_kt', kind: 'range' },
  { field: 'route_origin', kind: 'match' },
  { field: 'route_destination', kind: 'match' },
  { field: 'distance_nm', kind: 'range' },
]

const KIND: Record<string, SimpleKind> = Object.fromEntries(SIMPLE_FIELDS.map((s) => [s.field, s.kind]))

export interface SimpleRow {
  field: string
  kind: SimpleKind
  /** Id of the condition this row writes, kept so funnel counts and explanations line up. */
  id: string
  values: string[]
  flag: boolean | null
  min: number | null
  max: number | null
  /** One-sided ranges may be strict (above/below) so existing trees round-trip; `between` is inclusive. */
  minStrict: boolean
  maxStrict: boolean
  place: number | null
}

export type SimpleForm = SimpleRow[]

export interface SimpleOptions {
  /** Default place for a row whose field needs one (null for fields without places). */
  placeFor?: (field: string) => number | null
  fieldLabel?: (field: string) => string
  opLabel?: (field: string, op: string) => string
}

const WILDCARD = /[*?[]/

export function hasWildcard(v: string): boolean {
  return WILDCARD.test(v)
}

function blankRow(spec: SimpleField, opts: SimpleOptions): SimpleRow {
  return {
    field: spec.field,
    kind: spec.kind,
    // One row per field, so a fixed id is unique in the tree and keeps round trips exact.
    id: `s-${spec.field}`,
    values: [],
    flag: null,
    min: null,
    max: null,
    minStrict: false,
    maxStrict: false,
    place: opts.placeFor?.(spec.field) ?? null,
  }
}

export function emptyForm(opts: SimpleOptions = {}): SimpleForm {
  return SIMPLE_FIELDS.map((s) => blankRow(s, opts))
}

export function rowIsEmpty(row: SimpleRow): boolean {
  switch (row.kind) {
    case 'flag':
      return row.flag === null
    case 'range':
      return row.min === null && row.max === null
    default:
      return row.values.length === 0
  }
}

/** Sets one end of a range row. A two-sided range is `between` (inclusive), so it drops strictness. */
export function setRangeEnd(row: SimpleRow, end: 'min' | 'max', value: number | null): void {
  row[end] = value !== null && Number.isFinite(value) ? value : null
  if (row.min !== null && row.max !== null) {
    row.minStrict = false
    row.maxStrict = false
  }
}

/** A filled row that still compiles but probably doesn't mean what it says. */
export function rowWarning(row: SimpleRow): string | null {
  if (row.kind === 'range' && row.min !== null && row.max !== null && row.min > row.max) {
    return 'The first number is above the second, so this matches outside the range (wrap-around).'
  }
  return null
}

function rowToCondition(row: SimpleRow): Condition {
  const base = { type: 'cond' as const, id: row.id, field: row.field, place: row.place, enabled: true }
  switch (row.kind) {
    case 'match':
      return { ...base, op: row.values.some(hasWildcard) ? 'glob' : 'in', value: [...row.values] }
    case 'contains':
      return { ...base, op: 'contains', value: [...row.values] }
    case 'choice':
      return { ...base, op: 'in', value: [...row.values] }
    case 'flag':
      return { ...base, op: row.flag ? 'is_true' : 'is_false', value: null }
    case 'range':
      if (row.min !== null && row.max !== null) return { ...base, op: 'between', value: [row.min, row.max] }
      if (row.min !== null) return { ...base, op: row.minStrict ? 'gt' : 'ge', value: row.min }
      return { ...base, op: row.maxStrict ? 'lt' : 'le', value: row.max }
  }
}

/**
 * Rows → the root group's children ("all of"). Empty rows are omitted. Conditions that were
 * already in `root` keep their order, so switching views never reshuffles a filter.
 */
export function toTree(form: SimpleForm, root: Group): Group {
  const conds = form.filter((r) => !rowIsEmpty(r)).map(rowToCondition)
  const before = new Map(root.children.map((c, i) => [c.id, i]))
  const rank = (c: Condition, i: number) => before.get(c.id) ?? root.children.length + i
  const ordered = conds
    .map((c, i) => ({ c, r: rank(c, i) }))
    .sort((a, b) => a.r - b.r)
    .map((x) => x.c)
  return { ...root, mode: 'all', children: ordered }
}

export type SimpleResult = { ok: true; form: SimpleForm } | { ok: false; reasons: string[] }

function isStringList(v: unknown): v is string[] {
  return Array.isArray(v) && v.every((x) => typeof x === 'string')
}

function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/**
 * The tree → rows, if it has the simple shape: an enabled "all of" root holding only enabled
 * conditions on simple-mode fields, one per field, with the operators those rows write.
 * Otherwise the reasons it doesn't fit, so the full builder can say why.
 */
export function fromTree(root: Group, opts: SimpleOptions = {}): SimpleResult {
  const label = (f: string) => (opts.fieldLabel ? opts.fieldLabel(f) : f)
  const opName = (f: string, op: string) => (opts.opLabel ? opts.opLabel(f, op) : op)
  const reasons: string[] = []
  if (!root.enabled) reasons.push('The conditions are switched off.')
  if (root.mode === 'any') reasons.push('It matches when any one condition is true; simple mode needs all of them.')
  if (root.mode === 'none') reasons.push('It matches when none of the conditions are true.')

  const form = emptyForm(opts)
  const byField = new Map(form.map((r) => [r.field, r]))
  const seen = new Set<string>()

  for (const child of root.children) {
    if (child.type === 'group') {
      reasons.push(`It has a nested group (${child.mode} of …).`)
      continue
    }
    const c = child
    if (!c.field) {
      reasons.push('A condition has no field chosen yet.')
      continue
    }
    const row = byField.get(c.field)
    if (!row) {
      reasons.push(`'${label(c.field)}' isn't one of the simple-mode fields.`)
      continue
    }
    if (seen.has(c.field)) {
      reasons.push(`'${label(c.field)}' is used in more than one condition.`)
      continue
    }
    seen.add(c.field)
    if (!c.enabled) {
      reasons.push(`'${label(c.field)}' is switched off.`)
      continue
    }
    const why = fillRow(row, c)
    if (why) reasons.push(why.replace('{op}', `“${opName(c.field, c.op)}”`).replace('{field}', `'${label(c.field)}'`))
  }
  return reasons.length ? { ok: false, reasons } : { ok: true, form }
}

/** Copies one condition into its row; returns a reason template if it doesn't fit. */
function fillRow(row: SimpleRow, c: Condition): string | null {
  const unsupported = '{field} uses {op}.'
  row.id = c.id
  row.place = c.place
  const v = c.value
  switch (KIND[c.field]) {
    case 'match':
      if (c.op !== 'in' && c.op !== 'glob') return unsupported
      if (!isStringList(v)) return '{field} has an unexpected value.'
      if (c.op === 'in' && v.some(hasWildcard)) return '{field} has a value with * ? or [ matched literally.'
      if (c.op === 'glob' && v.length && !v.some(hasWildcard)) return '{field} uses a pattern without wildcards.'
      row.values = [...v]
      return null
    case 'contains':
    case 'choice':
      if (c.op !== (KIND[c.field] === 'contains' ? 'contains' : 'in')) return unsupported
      if (!isStringList(v)) return '{field} has an unexpected value.'
      row.values = [...v]
      return null
    case 'flag':
      if (c.op !== 'is_true' && c.op !== 'is_false') return unsupported
      row.flag = c.op === 'is_true'
      return null
    case 'range':
      if (c.op === 'between') {
        if (!Array.isArray(v) || v.length !== 2 || !isNum(v[0]) || !isNum(v[1])) return '{field} needs two numbers.'
        row.min = v[0]
        row.max = v[1]
        return null
      }
      if (!['ge', 'gt', 'le', 'lt'].includes(c.op)) return unsupported
      if (!isNum(v)) return '{field} needs a number.'
      if (c.op === 'ge' || c.op === 'gt') {
        row.min = v
        row.minStrict = c.op === 'gt'
      } else {
        row.max = v
        row.maxStrict = c.op === 'lt'
      }
      return null
  }
  return unsupported
}

// Last builder view chosen on this device. Browser storage can be missing or blocked.
const PREF_KEY = 'filter-builder-view'
export type BuilderView = 'simple' | 'advanced'

export function loadViewPref(): BuilderView | null {
  try {
    const v = localStorage.getItem(PREF_KEY)
    return v === 'simple' || v === 'advanced' ? v : null
  } catch {
    return null
  }
}

export function saveViewPref(v: BuilderView): void {
  try {
    localStorage.setItem(PREF_KEY, v)
  } catch {
    // Not remembered; the default applies next time.
  }
}
