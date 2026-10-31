export type PlaceRole = 'viewpoint' | 'airport' | 'area'

export interface Visibility {
  min_elevation_deg: number
  max_slant_nm_large: number
  max_slant_nm_medium: number
  max_slant_nm_light: number
}

export interface Schedule {
  days: number[]
  start: string
  end: string
  tz: string
}

export interface PredictionOverrides {
  lead_max_s: number | null
  min_confidence: number | null
}

export interface PredictionSettings {
  enabled: boolean
  horizon_s: number
  lead_max_s: number
  min_confidence: number
  samples: number
  step_s: number
}

export interface PlaceIn {
  name: string
  role: PlaceRole
  lat: number
  lon: number
  elevation_ft: number
  radius_nm: number
  polygon: [number, number][] | null
  alt_floor_ft: number | null
  alt_ceiling_ft: number | null
  airport_icao: string | null
  visibility: Visibility | null
  active_from: number | null
  active_until: number | null
  schedule: Schedule | null
  prediction: PredictionOverrides | null
  is_home: boolean
  enabled: boolean
  source: 'manual' | 'geolocation' | 'airport_lookup'
}

export interface Place extends PlaceIn {
  id: number
  created_at: number
  updated_at: number
}

export function defaultVisibility(): Visibility {
  return {
    min_elevation_deg: 15,
    max_slant_nm_large: 20,
    max_slant_nm_medium: 12,
    max_slant_nm_light: 5,
  }
}

export function blankPlace(role: PlaceRole = 'viewpoint'): PlaceIn {
  return {
    name: '',
    role,
    lat: 0,
    lon: 0,
    elevation_ft: 0,
    radius_nm: role === 'viewpoint' ? 10 : role === 'airport' ? 15 : 10,
    polygon: null,
    alt_floor_ft: null,
    alt_ceiling_ft: null,
    airport_icao: null,
    visibility: role === 'viewpoint' ? defaultVisibility() : null,
    active_from: null,
    active_until: null,
    schedule: null,
    prediction: null,
    is_home: false,
    enabled: true,
    source: 'manual',
  }
}

// ---- filters --------------------------------------------------------------------------------

export type FieldType = 'number' | 'string' | 'bool' | 'enum' | 'list'

export interface FieldDef {
  key: string
  label: string
  group: string
  type: FieldType
  unit: string | null
  help: string
  place: boolean
  place_roles: PlaceRole[]
  any_viewpoint: boolean
  ops: string[]
  enum: { value: string; label: string }[]
  suggest: string | null
}

export interface Condition {
  type: 'cond'
  id: string
  field: string
  op: string
  value: unknown
  place: number | null
  enabled: boolean
}

export interface Group {
  type: 'group'
  id: string
  mode: 'all' | 'any' | 'none'
  children: (Condition | Group)[]
  enabled: boolean
}

export interface QuietHours {
  start: string
  end: string
}

export interface NotifySettings {
  enabled: boolean
  on_match: boolean
  on_takeoff: boolean
  on_landing: boolean
  priority: 'low' | 'normal' | 'high' | 'critical'
  channel: string
  alarm_stream: boolean
  include_photo: boolean
  update_eta: boolean
  title_template: string | null
  message_template: string | null
  repeat_suppress_min: number | null
  max_per_day: number | null
  quiet_hours: QuietHours | null
}

export interface FilterDefinition {
  root: Group
  display_place_id: number | null
  notify: NotifySettings
}

export interface FilterIn {
  name: string
  description: string
  enabled: boolean
  mode: 'live' | 'shadow'
  definition: FilterDefinition
  note?: string
}

export interface Filter extends FilterIn {
  id: number
  sort_order: number
  version: number
  version_id: number
  created_at: number
  updated_at: number
  summary: string
  errors: string[]
  matching_now: number
  alerts_today: number
  notified_today: number
  last_alert_at: number | null
  stats_30d: { alerts: number; notified: number; up: number; down: number }
}

export interface AircraftSummary {
  hex: string
  callsign: string | null
  registration: string | null
  type_code: string | null
  type_desc: string | null
  operator: string | null
  lat: number | null
  lon: number | null
  altitude_ft: number | null
  on_ground: boolean
  gs_kt: number | null
  track_deg: number | null
  vertical_rate_fpm: number | null
  squawk: string | null
  category: string | null
  pos_source: string
  seen_s: number
  military: boolean
  military_score: number | null
  government: boolean
  police: boolean
  plane_alert_category: string | null
  distance_nm: number | null
  direction: string | null
  elevation_deg: number | null
  visible: boolean | null
  filters: number[]
  predictions: ActivePrediction[]
  path: [number, number][] | null
}

export interface ActivePrediction {
  place_id: number
  place: string
  kind: 'visible' | 'entry' | 'arrival'
  eta_s: number | null
  confidence: number
}

export interface FunnelStep {
  id: string | null
  text: string
  count: number
}

export interface PreviewResult {
  summary: string
  errors: string[]
  in_range: number
  match_count: number
  excluded: number
  matches: AircraftSummary[]
  funnel: FunnelStep[]
}

export interface ExplainNode {
  id: string
  type: 'cond' | 'group'
  text?: string
  value?: unknown
  result: boolean | null
  enabled: boolean
  mode?: string
  children?: ExplainNode[]
}

export interface Alert {
  id: number
  filter_id: number | null
  filter_name: string
  mode: 'live' | 'shadow'
  hex: string
  callsign: string | null
  registration: string | null
  type_code: string | null
  type_desc: string | null
  military: number
  place_id: number | null
  state: 'active' | 'ended'
  started_at: number
  ended_at: number | null
  closest_nm: number | null
  notified: number
  suppressed_reason: string | null
  title: string | null
  photo_url: string | null
  feedback: 'up' | 'down' | null
}

let idCounter = 0
export function newId(prefix = 'n'): string {
  idCounter += 1
  return `${prefix}${Date.now().toString(36)}${idCounter}`
}

export function defaultNotify(): NotifySettings {
  return {
    enabled: true,
    on_match: true,
    on_takeoff: false,
    on_landing: false,
    priority: 'high',
    channel: 'Aircraft',
    alarm_stream: false,
    include_photo: true,
    update_eta: true,
    title_template: null,
    message_template: null,
    repeat_suppress_min: null,
    max_per_day: null,
    quiet_hours: null,
  }
}

export function emptyGroup(mode: Group['mode'] = 'all'): Group {
  return { type: 'group', id: newId('g'), mode, children: [], enabled: true }
}

export function blankFilter(): FilterIn {
  return {
    name: '',
    description: '',
    enabled: true,
    mode: 'live',
    definition: { root: emptyGroup(), display_place_id: null, notify: defaultNotify() },
  }
}

// ---- backtest -------------------------------------------------------------------------------

export interface BacktestCounts {
  episodes: number
  notified: number
}

export interface BacktestAircraft {
  hex: string
  callsign: string | null
  registration: string | null
  type_code: string | null
  type_desc: string | null
  first_at: number
  episodes: number
}

export interface BacktestResult {
  days: { date: string; draft: BacktestCounts; saved: BacktestCounts | null }[]
  totals: { draft: BacktestCounts & { aircraft: number }; saved: (BacktestCounts & { aircraft: number }) | null }
  only_draft: BacktestAircraft[]
  only_saved: BacktestAircraft[]
  both: number
  batches: number
  predicted: boolean
}

export interface BacktestJob {
  id: string
  status: 'running' | 'done' | 'error' | 'cancelled'
  progress: number
  result: BacktestResult | null
  error: string | null
  elapsed_s: number
}

// ---- airspace -------------------------------------------------------------------------------

export interface AirspaceLimit {
  ft: number | null
  ref: 'MSL' | 'AGL' | 'SFC' | 'UNL'
  text: string
}

export interface Airspace {
  name: string
  type: string
  floor: AirspaceLimit
  ceiling: AirspaceLimit
  polygon: [number, number][]
  center: [number, number]
  radius_nm: number
  times_of_use: string | null
  schedule: { days: number[]; start: string; end: string; tz: string } | null
  location: string
  controlling_agency: string | null
  parts_dropped: number
}

// ---- watchlist ------------------------------------------------------------------------------

export interface WatchEntry {
  kind: 'hex' | 'reg'
  value: string
  hexes: string[]
  filter_ids: number[]
  filters: string[]
  last_seen: number | null
  resolved: boolean
  live: {
    hex: string
    callsign: string | null
    altitude_ft: number | null
    on_ground: boolean
    gs_kt: number | null
    near: string | null
    seen_s: number
  } | null
}
