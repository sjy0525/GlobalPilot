import { apiJson } from './http'

export type AuthUserPayload = {
  id: string
  email: string
  name?: string
  role?: string
  team_id?: string | null
}

export type AccessTokenResponse = {
  access_token: string
  token_type: string
}

export type LoginResponse = AccessTokenResponse & {
  user: AuthUserPayload
}

export type CurrentUserResponse = {
  id: string
  email: string
  nickname: string | null
  created_at: string
  updated_at: string
}

export type RegisterResponse = {
  id: string
  email: string
}

export function loginWithPassword(email: string, password: string) {
  return apiJson<LoginResponse>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
}

export function registerWithPassword(email: string, password: string) {
  return apiJson<RegisterResponse>('/api/register', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
}

export function loginWithGoogleIdToken(idToken: string) {
  return apiJson<LoginResponse>('/api/auth/google-login', {
    method: 'POST',
    body: JSON.stringify({ id_token: idToken }),
  })
}

// Refresh credentials are an HttpOnly cookie; JavaScript never receives them.
export function refreshSession() {
  return apiJson<LoginResponse>('/api/auth/refresh', { method: 'POST' })
}

export function logoutSession() {
  return apiJson<void>('/api/auth/logout', { method: 'POST' })
}

export function fetchCurrentUser() {
  return apiJson<CurrentUserResponse>('/api/auth/me')
}
