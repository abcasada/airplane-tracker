export interface Toast {
  id: number
  text: string
  kind: 'info' | 'error'
}

export const toasts = $state<Toast[]>([])
let nextId = 1

export function toast(text: string, kind: Toast['kind'] = 'info'): void {
  const id = nextId++
  toasts.push({ id, text, kind })
  setTimeout(
    () => {
      const i = toasts.findIndex((t) => t.id === id)
      if (i >= 0) toasts.splice(i, 1)
    },
    kind === 'error' ? 6000 : 3000,
  )
}
