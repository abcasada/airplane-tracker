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
    is_home: false,
    enabled: true,
    source: 'manual',
  }
}
