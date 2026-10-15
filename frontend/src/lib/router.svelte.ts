/** Minimal hash router: "#/filters/3" → { path: "/filters/3", parts: ["filters", "3"] }. */

function parse(): { path: string; parts: string[]; query: URLSearchParams } {
  const raw = location.hash.replace(/^#/, '') || '/'
  const [path, qs] = raw.split('?')
  return {
    path,
    parts: path.split('/').filter(Boolean),
    query: new URLSearchParams(qs ?? ''),
  }
}

export const route = $state(parse())

window.addEventListener('hashchange', () => {
  Object.assign(route, parse())
})

export function navigate(path: string): void {
  location.hash = path
}
