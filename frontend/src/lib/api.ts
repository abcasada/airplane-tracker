export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

let csrfToken = ''
let onUnauthorized: (() => void) | null = null

export function setCsrfToken(token: string): void {
  csrfToken = token
}

export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn
}

function detailToMessage(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) => {
        const where = (d.loc ?? []).filter((x) => x !== 'body').join('.')
        return where ? `${where}: ${d.msg}` : (d.msg ?? '')
      })
      .join('; ')
  }
  return 'Request failed'
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET' && csrfToken) headers['X-CSRF-Token'] = csrfToken
  const resp = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
  })
  if (resp.status === 401 && onUnauthorized && !path.startsWith('/api/auth/login')) {
    onUnauthorized()
  }
  const text = await resp.text()
  const data = text ? JSON.parse(text) : null
  if (!resp.ok) {
    throw new ApiError(resp.status, detailToMessage(data?.detail) || resp.statusText)
  }
  return data as T
}

export const get = <T>(path: string) => api<T>('GET', path)
export const post = <T>(path: string, body?: unknown) => api<T>('POST', path, body ?? {})
export const put = <T>(path: string, body: unknown) => api<T>('PUT', path, body)
export const del = <T>(path: string) => api<T>('DELETE', path)

export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message
  return String(e)
}
