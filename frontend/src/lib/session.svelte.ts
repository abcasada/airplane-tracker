import { get, setCsrfToken } from './api'

export interface Me {
  username: string
  csrf_token: string
  recovery_codes_left: number
  auth_mode: string
}

export const session = $state<{
  status: 'loading' | 'setup' | 'login' | 'ready'
  me: Me | null
}>({ status: 'loading', me: null })

export async function refreshSession(): Promise<void> {
  const setup = await get<{ needs_setup: boolean }>('/api/setup/status')
  if (setup.needs_setup) {
    session.status = 'setup'
    session.me = null
    return
  }
  try {
    const me = await get<Me>('/api/auth/me')
    setCsrfToken(me.csrf_token)
    session.me = me
    session.status = 'ready'
  } catch {
    session.me = null
    session.status = 'login'
  }
}

export function loggedOut(): void {
  setCsrfToken('')
  session.me = null
  session.status = 'login'
}
