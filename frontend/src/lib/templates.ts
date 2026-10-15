import { blankFilter, newId, type Condition, type FilterIn, type Place } from './types'

function c(field: string, op: string, value: unknown = null, place: number | null = null): Condition {
  return { type: 'cond', id: newId('c'), field, op, value, place, enabled: true }
}

export interface Template {
  key: string
  title: string
  description: string
  needs?: 'home' | 'area'
  build: (home: Place | undefined, area: Place | undefined) => FilterIn
}

// Viewpoint conditions follow you: home, or a travel viewpoint while it's active.
const ANY_VIEWPOINT = 0

function make(name: string, description: string, conds: Condition[], place?: Place, display: number | null = ANY_VIEWPOINT): FilterIn {
  const f = blankFilter()
  f.name = name
  f.description = description
  f.definition.root.children = conds
  f.definition.display_place_id = display ?? place?.id ?? null
  return f
}

export const TEMPLATES: Template[] = [
  {
    key: 'military-visible',
    title: 'Military visible from where I am',
    description: 'Military aircraft high enough above your horizon to see, from home or an active travel viewpoint.',
    needs: 'home',
    build: (home) =>
      make('Military visible', '', [c('military', 'is_true'), c('visible_now', 'is_true', null, ANY_VIEWPOINT)], home),
  },
  {
    key: 'military-near',
    title: 'Military within 30 nm',
    description: 'Wider net: any military aircraft within 30 nm of home or an active travel viewpoint.',
    needs: 'home',
    build: (home) =>
      make('Military nearby', '', [c('military', 'is_true'), c('distance_nm', 'lt', 30, ANY_VIEWPOINT)], home),
  },
  {
    key: 'types-near',
    title: 'Specific types near me',
    description: 'Pick aircraft types (C17, K35R, B52…) and a distance.',
    needs: 'home',
    build: (home) =>
      make('Watched types', '', [c('type_code', 'in', []), c('distance_nm', 'lt', 20, ANY_VIEWPOINT)], home),
  },
  {
    key: 'interesting',
    title: 'Interesting aircraft (plane-alert-db)',
    description: 'Anything on the community list of ~17,000 notable aircraft, within 30 nm.',
    needs: 'home',
    build: (home) =>
      make('Interesting aircraft', '', [c('plane_alert_listed', 'is_true'), c('distance_nm', 'lt', 30, ANY_VIEWPOINT)], home),
  },
  {
    key: 'inside-area',
    title: 'Anything inside an area',
    description: 'Aircraft inside one of your area places, e.g. a range or restricted area.',
    needs: 'area',
    build: (home, area) => make(`Inside ${area!.name}`, '', [c('inside_area', 'is_true', null, area!.id)], area, area!.id),
  },
  {
    key: 'low-close',
    title: 'Low and close',
    description: 'Below 3,000 ft and within 5 nm of where you are. Good for helicopters.',
    needs: 'home',
    build: (home) =>
      make(
        'Low and close',
        '',
        [c('height_above_place_ft', 'lt', 3000, ANY_VIEWPOINT), c('distance_nm', 'lt', 5, ANY_VIEWPOINT), c('on_ground', 'is_false')],
        home,
      ),
  },
  {
    key: 'blank',
    title: 'Start from scratch',
    description: 'An empty filter. Add your own conditions.',
    build: (home) => make('', '', [], home),
  },
]
