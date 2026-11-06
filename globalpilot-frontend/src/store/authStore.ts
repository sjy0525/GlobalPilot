import { create } from 'zustand'
import { ApiError, configureAuth } from '../api/http'
import {
  loginWithGoogleIdToken,
  loginWithPassword,
  logoutSession,
  refreshSession,
  registerWithPassword,
  type AuthUserPayload,
  type LoginResponse,
} from '../api/auth'

export type AuthUser = {
  id: string
  email: string
  name: string
  role?: string
  teamId?: string | null
}

type AuthState = {
  user: AuthUser | null
  token: string | null
  isAuthenticated: boolean
  ready: boolean
  loginWithEmail: (email: string, password: string) => Promise<void>
  registerWithEmail: (email: string, password: string) => Promise<void>
  loginWithGoogle: (idToken: string) => Promise<void>
  restoreSession: () => Promise<void>
  updateProfile: (patch: Partial<Pick<AuthUser, 'name'>>) => void
  logout: () => Promise<void>
}

// Remove credentials written by the old design. Never read or migrate them.
try {
  if (typeof window !== 'undefined') window.localStorage.removeItem('gp-auth')
} catch { /* Storage may be disabled; authentication no longer depends on it. */ }

function toAuthUser(payload: AuthUserPayload): AuthUser {
  const email = payload.email.trim().toLowerCase()
  const local = email.split('@')[0] || 'Pilot'
  return {
    id: payload.id,
    email,
    name: payload.name?.trim() || local.charAt(0).toUpperCase() + local.slice(1),
    role: payload.role,
    teamId: typeof payload.team_id === 'string' ? payload.team_id : null,
  }
}

function validateCredentials(email: string, password: string) {
  const trimmed = email.trim()
  if (!trimmed || !trimmed.includes('@')) throw new Error('Enter a valid email address.')
  if (password.length < 6) throw new Error('Password must be at least 6 characters.')
  return trimmed.toLowerCase()
}

function clearSession() {
  return { user: null, token: null, isAuthenticated: false, ready: true }
}

function applySession(data: LoginResponse) {
  return { user: toAuthUser(data.user), token: data.access_token, isAuthenticated: true, ready: true }
}

// Prevent a late restore/refresh response from undoing logout or a newer login.
let sessionVersion = 0
let loggingOut = false
let refreshInFlight: Promise<string | null> | null = null
let restoreInFlight: Promise<void> | null = null

async function refreshAccessToken(): Promise<string | null> {
  if (loggingOut) return null
  if (refreshInFlight) return refreshInFlight
  const version = sessionVersion
  refreshInFlight = (async () => {
    try {
      const data = await refreshSession()
      if (version !== sessionVersion || loggingOut) return null
      useAuthStore.setState(applySession(data))
      return data.access_token
    } catch (err) {
      if (version === sessionVersion && err instanceof ApiError && err.status === 401) {
        useAuthStore.setState(clearSession())
      }
      throw err
    } finally {
      refreshInFlight = null
    }
  })()
  return refreshInFlight
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  token: null,
  isAuthenticated: false,
  ready: false,

  loginWithEmail: async (email, password) => {
    const normalized = validateCredentials(email, password)
    const version = ++sessionVersion
    const data = await loginWithPassword(normalized, password)
    if (version === sessionVersion) set(applySession(data))
  },

  registerWithEmail: async (email, password) => {
    const normalized = validateCredentials(email, password)
    await registerWithPassword(normalized, password)
    await get().loginWithEmail(normalized, password)
  },

  loginWithGoogle: async (idToken) => {
    const version = ++sessionVersion
    const data = await loginWithGoogleIdToken(idToken)
    if (version === sessionVersion) set(applySession(data))
  },

  restoreSession: () => {
    // React StrictMode may mount the layout twice. Share the bootstrap request.
    if (restoreInFlight) return restoreInFlight
    if (get().ready) return Promise.resolve()
    const version = sessionVersion
    restoreInFlight = (async () => {
      try {
        await refreshAccessToken()
      } catch {
        // No Cookie, expired session or offline: do not trust cached identity.
      } finally {
        if (version === sessionVersion) set({ ready: true })
        restoreInFlight = null
      }
    })()
    return restoreInFlight
  },

  updateProfile: (patch) => {
    set((state) => state.user ? { user: { ...state.user, ...patch } } : {})
  },

  logout: async () => {
    if (loggingOut) return
    loggingOut = true
    ++sessionVersion
    try {
      // Only the server can delete an HttpOnly cookie. Surface failures for retry.
      await logoutSession()
      set(clearSession())
    } finally {
      loggingOut = false
    }
  },
}))

configureAuth({
  getAccessToken: () => useAuthStore.getState().token,
  refreshAccessToken,
  onUnauthorized: () => {
    if (!loggingOut) {
      ++sessionVersion
      useAuthStore.setState(clearSession())
    }
  },
})

/** Stable selectors for components. */
export function useAuth() {
  const user = useAuthStore((s) => s.user)
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const ready = useAuthStore((s) => s.ready)
  const loginWithEmail = useAuthStore((s) => s.loginWithEmail)
  const registerWithEmail = useAuthStore((s) => s.registerWithEmail)
  const loginWithGoogle = useAuthStore((s) => s.loginWithGoogle)
  const updateProfile = useAuthStore((s) => s.updateProfile)
  const logout = useAuthStore((s) => s.logout)
  return {
    user,
    isAuthenticated,
    ready,
    loginWithEmail,
    registerWithEmail,
    loginWithGoogle,
    updateProfile,
    logout,
  }
}
