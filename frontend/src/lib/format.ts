const COMPASS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']

export function compass(deg: number | null | undefined): string {
  if (deg === null || deg === undefined || !Number.isFinite(deg)) return '—'
  return COMPASS[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16]
}

export function fmtAlt(ft: number | null | undefined, onGround = false): string {
  if (onGround) return 'ground'
  if (ft === null || ft === undefined) return '—'
  if (ft >= 18000) return `FL${Math.round(ft / 100)}`
  return `${Math.round(ft).toLocaleString('en-US')} ft`
}

export function fmtNum(v: number | null | undefined, digits = 0, unit = ''): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return `${v.toFixed(digits)}${unit ? ' ' + unit : ''}`
}

export function fmtTime(epoch: number | null | undefined): string {
  if (!epoch) return '—'
  return new Date(epoch * 1000).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function fmtAgo(epoch: number | null | undefined, now = Date.now() / 1000): string {
  if (!epoch) return '—'
  const s = Math.max(0, Math.round(now - epoch))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const s = Math.round(seconds)
  if (s < 60) return `${s} s`
  const m = Math.floor(s / 60)
  const r = s % 60
  return r ? `${m} min ${r} s` : `${m} min`
}

export function fmtEta(eta_s: number | null | undefined): string {
  if (eta_s == null) return ''
  if (eta_s <= 0) return 'now'
  if (eta_s < 90) return `in ~${Math.round(eta_s / 10) * 10} s`
  return `in ~${Math.round(eta_s / 60)} min`
}

const PREDICTION_VERB = { visible: 'Visible from', entry: 'Enters', arrival: 'Arrives at' } as const

export function describePrediction(p: { kind: keyof typeof PREDICTION_VERB; place: string; eta_s: number | null; confidence: number }): string {
  return `${PREDICTION_VERB[p.kind]} ${p.place} ${fmtEta(p.eta_s)} (${Math.round(p.confidence * 100)}%)`
}
