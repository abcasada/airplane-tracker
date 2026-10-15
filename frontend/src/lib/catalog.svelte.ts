import { get } from './api'
import type { FieldDef, Place } from './types'

export const OP_LABELS: Record<string, string> = {
  lt: 'less than',
  le: 'at most',
  gt: 'more than',
  ge: 'at least',
  between: 'between',
  eq: 'equals',
  ne: 'not equal to',
  exists: 'is known',
  not_exists: 'is unknown',
  in: 'is any of',
  not_in: 'is none of',
  glob: 'matches pattern',
  not_glob: "doesn't match pattern",
  contains: 'contains',
  regex: 'matches regex',
  is_true: 'is yes',
  is_false: 'is no',
  contains_any: 'includes any of',
  contains_all: 'includes all of',
  contains_none: 'includes none of',
}

export const NO_VALUE_OPS = new Set(['exists', 'not_exists', 'is_true', 'is_false'])
export const LIST_VALUE_OPS = new Set([
  'in',
  'not_in',
  'glob',
  'not_glob',
  'contains',
  'contains_any',
  'contains_all',
  'contains_none',
])

export const catalog = $state<{
  fields: FieldDef[]
  byKey: Record<string, FieldDef>
  templateKeys: Record<string, string>
  places: Place[]
  loaded: boolean
}>({ fields: [], byKey: {}, templateKeys: {}, places: [], loaded: false })

export async function loadCatalog(force = false): Promise<void> {
  if (catalog.loaded && !force) return
  const [f, places] = await Promise.all([
    get<{ fields: FieldDef[]; template_keys: Record<string, string> }>('/api/fields'),
    get<Place[]>('/api/places'),
  ])
  catalog.fields = f.fields
  catalog.byKey = Object.fromEntries(f.fields.map((x) => [x.key, x]))
  catalog.templateKeys = f.template_keys
  catalog.places = places
  catalog.loaded = true
}

export function fieldGroups(): [string, FieldDef[]][] {
  const groups = new Map<string, FieldDef[]>()
  for (const f of catalog.fields) {
    if (!groups.has(f.group)) groups.set(f.group, [])
    groups.get(f.group)!.push(f)
  }
  return [...groups.entries()]
}

export function defaultOp(f: FieldDef): string {
  if (f.type === 'bool') return 'is_true'
  if (f.type === 'number') return 'lt'
  if (f.type === 'list') return 'contains_any'
  return 'in'
}

export function placesFor(f: FieldDef): Place[] {
  return catalog.places.filter((p) => !f.place_roles.length || f.place_roles.includes(p.role))
}

export function opLabel(f: FieldDef | undefined, op: string): string {
  if (f?.type === 'bool') return op === 'is_true' ? 'is yes' : 'is no'
  return OP_LABELS[op] ?? op
}
